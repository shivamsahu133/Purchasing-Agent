"""ACT stage: the approval gate.

AC-1  A run that needs approval ends with a pending-approval state.
AC-2  Nothing is placed or confirmed until the buyer approves.
AC-3  The pending state carries enough to identify the PO and run to approve.
AC-4  The pending state is persisted to SQLite and the run resumes from it.

The model is replaced with a scripted one so the gate is tested without Groq.
"""
import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app import agent, db
from app.main import app

NEED_BY = (date.today() + timedelta(days=10)).isoformat()


def tool_call(name: str, args: dict, call_id: str = "call-1"):
    fn = SimpleNamespace(name=name, arguments=json.dumps(args))
    return SimpleNamespace(content="", tool_calls=[
        SimpleNamespace(id=call_id, function=fn)], reasoning=None)


def reply(text: str):
    return SimpleNamespace(content=text, tool_calls=None, reasoning=None)


@pytest.fixture(autouse=True)
def fresh_db():
    db.reset_and_seed()
    agent.RUNS.clear()
    yield


@pytest.fixture
def scripted_model(monkeypatch):
    """Model that raises a PO over the threshold, then wraps up after resume."""
    script = [
        # 2,500 x S-002 is ~$12k, above the $10k default threshold.
        tool_call("create_purchase_order", {
            "sku": "SKU-4471", "supplier_id": "S-002", "qty": 2500,
            "need_by": NEED_BY}),
        reply("Done."),
    ]

    async def fake_call_model(run):
        return script.pop(0)

    monkeypatch.setattr(agent, "_call_model", fake_call_model)
    return script


async def collect(events):
    return [e async for e in events]


async def park_a_run(run_id: str = "gate-1") -> dict:
    events = await collect(agent.start(run_id, "buy 2500 SKU-4471"))
    return events[-1]


def po_status(po_id: str) -> str:
    conn = db.connect()
    row = conn.execute("SELECT status FROM purchase_orders WHERE id = ?",
                       (po_id,)).fetchone()
    conn.close()
    return row["status"]


# --- AC-1: pending approval state ------------------------------------------

async def test_run_ends_in_awaiting_approval(scripted_model):
    last = await park_a_run()
    assert last["type"] == "awaiting_approval"
    assert last["status"] == "awaiting_approval"
    assert last["run_id"] == "gate-1"
    assert agent.RUNS["gate-1"].status == "awaiting_approval"


async def test_api_run_stream_reports_pending_approval(scripted_model):
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as client:
        resp = await client.post("/api/run", json={"request": "buy bolts"})
    events = [json.loads(line) for line in resp.text.splitlines() if line]
    assert events[-1]["type"] == "awaiting_approval"
    assert events[-1]["approval"]["po_id"].startswith("PO-")


# --- AC-2: no action before approval ---------------------------------------

async def test_po_is_drafted_not_placed(scripted_model):
    last = await park_a_run()
    po_id = last["approval"]["po_id"]
    assert po_status(po_id) == "pending_approval"
    conn = db.connect()
    confirmations = conn.execute("SELECT COUNT(*) AS n FROM confirmations"
                                 ).fetchone()["n"]
    conn.close()
    assert confirmations == 0


async def test_approval_places_the_order(scripted_model):
    last = await park_a_run()
    await collect(agent.resume("gate-1", approved=True))
    assert po_status(last["approval"]["po_id"]) == "placed"


async def test_rejection_never_places_the_order(scripted_model):
    last = await park_a_run()
    await collect(agent.resume("gate-1", approved=False, note="too pricey"))
    assert po_status(last["approval"]["po_id"]) == "rejected"


async def test_a_decision_cannot_be_applied_twice(scripted_model):
    last = await park_a_run()
    await collect(agent.resume("gate-1", approved=False))
    agent.RUNS.clear()
    events = await collect(agent.resume("gate-1", approved=True))
    assert events[0]["type"] == "error"
    assert po_status(last["approval"]["po_id"]) == "rejected"


# --- AC-3: approval context -------------------------------------------------

async def test_pending_state_identifies_the_po_and_run(scripted_model):
    approval = (await park_a_run())["approval"]
    assert approval["run_id"] == "gate-1"
    assert approval["po_id"].startswith("PO-")
    assert approval["gate_id"].startswith("GATE-")
    assert approval["supplier_id"] == "S-002"
    assert approval["qty"] == 2500
    assert approval["total"] >= approval["approval_threshold"]
    assert approval["approve_endpoint"] == "/api/approve"


# --- AC-4: persisted to SQLite ----------------------------------------------

async def test_pending_approval_is_written_to_sqlite(scripted_model):
    last = await park_a_run()
    saved = db.get_pending_approval("gate-1")
    assert saved["status"] == "pending"
    assert saved["po_id"] == last["approval"]["po_id"]
    assert saved["messages"][-1]["role"] == "tool"


async def test_run_resumes_from_sqlite_after_a_restart(scripted_model):
    last = await park_a_run()
    agent.RUNS.clear()  # process restart: memory gone, database kept

    events = await collect(agent.resume("gate-1", approved=True))
    assert events[0] == {"type": "status", "status": "running",
                         "decision": "approved"}
    assert events[-1]["type"] == "done"
    assert po_status(last["approval"]["po_id"]) == "placed"
    assert db.get_pending_approval("gate-1")["status"] == "approved"


async def test_get_run_reports_pending_then_decided(scripted_model):
    await park_a_run()
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as client:
        pending = (await client.get("/api/runs/gate-1")).json()
        await collect(agent.resume("gate-1", approved=False, note="no"))
        decided = (await client.get("/api/runs/gate-1")).json()
    assert pending["status"] == "awaiting_approval"
    assert pending["approval"]["run_id"] == "gate-1"
    assert decided["status"] == "rejected"
    assert decided["decision_note"] == "no"
