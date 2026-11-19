"""POST /api/approve.

AC-1  Approving a pending PO places it and moves the run on.
AC-2  Rejecting a pending PO cancels it, moves the run on, and records the
      denial for escalation.
Also: unknown or already-decided runs are refused before anything changes,
and a run that stalls after the decision is recorded for escalation.

The model is replaced with a scripted one so this runs without Groq.
"""
import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app import agent, db
from app.main import app

NEED_BY = (date.today() + timedelta(days=10)).isoformat()


def tool_call(name: str, args: dict):
    fn = SimpleNamespace(name=name, arguments=json.dumps(args))
    return SimpleNamespace(content="", tool_calls=[
        SimpleNamespace(id="call-1", function=fn)], reasoning=None)


def reply(text: str):
    return SimpleNamespace(content=text, tool_calls=None, reasoning=None)


# 2,500 x S-002 is ~$12k, above the $10k default threshold.
RAISE_PO = tool_call("create_purchase_order", {
    "sku": "SKU-4471", "supplier_id": "S-002", "qty": 2500, "need_by": NEED_BY})


@pytest.fixture(autouse=True)
def fresh_db():
    db.reset_and_seed()
    agent.RUNS.clear()
    yield


def script_model(monkeypatch, *steps):
    """Each step is a model message, or an exception to raise."""
    steps = list(steps)

    async def fake_call_model(run):
        step = steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    monkeypatch.setattr(agent, "_call_model", fake_call_model)


def ndjson(resp) -> list[dict]:
    return [json.loads(line) for line in resp.text.splitlines() if line]


async def post(path: str, body: dict):
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as client:
        return await client.post(path, json=body)


async def get(path: str):
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as client:
        return await client.get(path)


async def park_a_run() -> dict:
    events = ndjson(await post("/api/run", {"request": "buy bolts"}))
    assert events[-1]["type"] == "awaiting_approval"
    return events[-1]["approval"]


def po_status(po_id: str) -> str:
    conn = db.connect()
    row = conn.execute("SELECT status FROM purchase_orders WHERE id = ?",
                       (po_id,)).fetchone()
    conn.close()
    return row["status"]


# --- AC-1: approve -----------------------------------------------------------

async def test_approve_places_the_po_and_resumes_the_run(monkeypatch):
    script_model(monkeypatch, RAISE_PO, reply("Placed and validated."))
    approval = await park_a_run()

    resp = await post("/api/approve", {"run_id": approval["run_id"],
                                       "approved": True})
    events = ndjson(resp)

    assert resp.status_code == 200
    assert events[0]["decision"] == "approved"
    assert events[-1] == {"type": "done", "reason": "complete"}
    assert po_status(approval["po_id"]) == "placed"
    assert agent.RUNS[approval["run_id"]].status == "done"


async def test_approve_updates_persisted_run_state(monkeypatch):
    script_model(monkeypatch, RAISE_PO, reply("Done."))
    approval = await park_a_run()
    await post("/api/approve", {"run_id": approval["run_id"], "approved": True,
                                "note": "ok to proceed"})

    state = (await get("/api/runs/" + approval["run_id"])).json()
    assert state["status"] == "approved"
    assert state["decision_note"] == "ok to proceed"
    assert state["decided_at"]
    assert state["escalations"] == []


# --- AC-2: reject -----------------------------------------------------------

async def test_reject_cancels_the_po_and_resumes_the_run(monkeypatch):
    script_model(monkeypatch, RAISE_PO, reply("Rejected; here are options."))
    approval = await park_a_run()

    resp = await post("/api/approve", {"run_id": approval["run_id"],
                                       "approved": False, "note": "too pricey"})
    events = ndjson(resp)

    assert resp.status_code == 200
    assert events[0]["decision"] == "rejected"
    assert po_status(approval["po_id"]) == "rejected"


async def test_reject_updates_run_state_and_records_the_denial(monkeypatch):
    script_model(monkeypatch, RAISE_PO, reply("Rejected."))
    approval = await park_a_run()
    await post("/api/approve", {"run_id": approval["run_id"],
                                "approved": False, "note": "too pricey"})

    state = (await get("/api/runs/" + approval["run_id"])).json()
    assert state["status"] == "rejected"
    assert [(e["trigger"], e["po_id"], e["detail"])
            for e in state["escalations"]] == [
        ("approval_rejected", approval["po_id"], "too pricey")]


# --- refused before anything changes ----------------------------------------

async def test_unknown_run_is_404():
    resp = await post("/api/approve", {"run_id": "nope", "approved": True})
    assert resp.status_code == 404


async def test_deciding_twice_is_409_and_changes_nothing(monkeypatch):
    script_model(monkeypatch, RAISE_PO, reply("Rejected."))
    approval = await park_a_run()
    await post("/api/approve", {"run_id": approval["run_id"], "approved": False})

    resp = await post("/api/approve", {"run_id": approval["run_id"],
                                       "approved": True})
    assert resp.status_code == 409
    assert "already rejected" in resp.json()["detail"]
    assert po_status(approval["po_id"]) == "rejected"


async def test_approval_works_after_a_restart(monkeypatch):
    script_model(monkeypatch, RAISE_PO, reply("Done."))
    approval = await park_a_run()
    agent.RUNS.clear()  # process restart: memory gone, SQLite kept

    resp = await post("/api/approve", {"run_id": approval["run_id"],
                                       "approved": True})
    assert resp.status_code == 200
    assert po_status(approval["po_id"]) == "placed"


# --- stall after the decision ------------------------------------------------

async def test_run_that_stalls_after_approval_is_escalated(monkeypatch):
    script_model(monkeypatch, RAISE_PO, RuntimeError("model unavailable"))
    approval = await park_a_run()

    events = ndjson(await post("/api/approve", {"run_id": approval["run_id"],
                                                "approved": True}))
    assert events[-1]["type"] == "error"
    assert events[-1]["escalated"] is True

    state = (await get("/api/runs/" + approval["run_id"])).json()
    assert state["status"] == "approved"  # the decision itself still stands
    assert [(e["trigger"], e["detail"]) for e in state["escalations"]] == [
        ("run_stalled", "model unavailable")]
