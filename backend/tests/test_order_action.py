"""ACT stage: submitting / recording the order action.

AC-1  Order actions are recorded in SQLite against the seeded mock supplier.
AC-2  If approval was required, nothing is recorded until it is granted.
AC-3  The outcome is stored and shows up in order listings and validation.
"""
from datetime import date, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app import agent, db, tools
from app.main import app

NEED_BY = (date.today() + timedelta(days=10)).isoformat()


@pytest.fixture(autouse=True)
def fresh_db():
    db.reset_and_seed()
    agent.RUNS.clear()
    yield


def action_count() -> int:
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) AS n FROM order_actions").fetchone()["n"]
    conn.close()
    return n


async def small_order(run_id: str = "run-a") -> dict:
    # 1,000 x S-002 is ~$5k, under the $10k threshold.
    return await tools.create_purchase_order("SKU-4471", "S-002", 1000,
                                             NEED_BY, run_id=run_id)


async def large_order(run_id: str = "run-b") -> dict:
    # 2,500 x S-002 is ~$12k, over the $10k threshold.
    return await tools.create_purchase_order("SKU-4471", "S-002", 2500,
                                             NEED_BY, run_id=run_id)


# --- AC-1: mock recording ----------------------------------------------------

async def test_auto_approved_order_is_recorded_as_submitted():
    po = await small_order()
    action = db.get_order_action(po["po_id"])
    assert po["status"] == "placed"
    assert po["action_id"] == action["id"]
    assert action["run_id"] == "run-a"
    assert action["action"] == "submit"
    assert action["channel"] == "mock_supplier"
    assert action["outcome"] == "submitted"
    assert action["executed_at"]


async def test_a_po_can_only_be_submitted_once():
    po = await small_order()
    conn = db.connect()
    with pytest.raises(Exception):
        db.record_order_submission(conn, po["po_id"], "run-a")
    conn.close()
    assert action_count() == 1


# --- AC-2: only after approval -----------------------------------------------

async def test_order_held_for_approval_is_not_recorded():
    po = await large_order()
    assert po["approval_required"] is True
    assert po["action_id"] is None
    assert db.get_order_action(po["po_id"]) is None


async def test_approval_records_the_submission():
    po = await large_order()
    assert db.resolve_pending_approval("run-b", po["po_id"], approved=True)
    action = db.get_order_action(po["po_id"])
    assert action["outcome"] == "submitted"
    assert action["run_id"] == "run-b"


async def test_rejection_never_records_a_submission():
    po = await large_order()
    assert db.resolve_pending_approval("run-b", po["po_id"], approved=False)
    assert db.get_order_action(po["po_id"]) is None


async def test_unconfigured_threshold_order_is_not_recorded(monkeypatch):
    monkeypatch.setenv("APPROVAL_THRESHOLD_USD", "")
    po = await small_order()
    assert po["status"] == "threshold_unknown"
    assert db.get_order_action(po["po_id"]) is None


async def test_no_confirmation_for_an_unsubmitted_order():
    po = await large_order()
    db.resolve_pending_approval("run-b", po["po_id"], approved=False)
    r = await tools.get_order_confirmation(po["po_id"])
    assert "never submitted" in r["error"]
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) AS n FROM confirmations").fetchone()["n"]
    conn.close()
    assert n == 0


# --- AC-3: outcome persisted and visible -------------------------------------

async def test_order_listing_shows_the_action_outcome():
    placed = await small_order()
    held = await large_order()
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as client:
        orders = (await client.get("/api/orders")).json()["orders"]
    by_id = {o["id"]: o for o in orders}

    assert by_id[placed["po_id"]]["action_outcome"] == "submitted"
    assert by_id[placed["po_id"]]["action_channel"] == "mock_supplier"
    assert by_id[placed["po_id"]]["submitted_at"]
    assert by_id[held["po_id"]]["status"] == "pending_approval"
    assert by_id[held["po_id"]]["action_outcome"] is None


async def test_validation_reports_the_submission():
    po = await small_order()
    await tools.get_order_confirmation(po["po_id"])
    result = await tools.validate_order(po["po_id"])
    assert result["submission"]["id"] == po["action_id"]
    assert result["submission"]["outcome"] == "submitted"


async def test_submission_survives_a_cold_start_via_the_token():
    po = await large_order()
    db.resolve_pending_approval("run-b", po["po_id"], approved=True)
    state = db.export_run_state("run-b")

    db.reset_and_seed()
    assert db.get_order_action(po["po_id"]) is None

    db.import_run_state(state)
    assert db.get_order_action(po["po_id"])["outcome"] == "submitted"
