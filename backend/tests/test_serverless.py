"""Surviving a cold start.

On a stateless host the buyer may approve minutes after the order was drafted,
and that request can land on an instance that has never seen the run. The
approval gate is the headline feature, so it has to work anyway: the state
needed to resume travels to the client and back.
"""
from datetime import date, timedelta

import pytest

from app import agent, db, tools

NEED_BY = (date.today() + timedelta(days=10)).isoformat()


@pytest.fixture(autouse=True)
def fresh_db():
    db.reset_and_seed()
    agent.RUNS.clear()
    yield


def cold_start():
    """Wipe everything an instance holds: memory and scratch database."""
    db.reset_and_seed()
    agent.RUNS.clear()


async def test_run_state_round_trips_through_the_client():
    po = await tools.create_purchase_order("SKU-4471", "S-002", 2500,
                                           NEED_BY, run_id="run-1")
    state = db.export_run_state("run-1")
    assert [o["id"] for o in state["orders"]] == [po["po_id"]]

    cold_start()
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) AS n FROM purchase_orders"
                        ).fetchone()["n"] == 0
    conn.close()

    db.import_run_state(state)
    conn = db.connect()
    restored = conn.execute("SELECT * FROM purchase_orders WHERE id = ?",
                            (po["po_id"],)).fetchone()
    conn.close()
    assert restored["total"] == po["total"]
    assert restored["status"] == "pending_approval"


async def test_suspended_run_rebuilds_from_the_token_alone():
    po = await tools.create_purchase_order("SKU-4471", "S-002", 2500,
                                           NEED_BY, run_id="run-2")
    token = {"run_id": "run-2",
             "messages": [{"role": "user", "content": "buy things"}],
             "pending_po": po,
             **db.export_run_state("run-2")}

    cold_start()
    assert agent.RUNS.get("run-2") is None

    run = agent._rehydrate("run-2", token)
    assert run is not None
    assert run.status == "awaiting_approval"
    assert run.pending_po["po_id"] == po["po_id"]
    assert run.messages == token["messages"]


async def test_validation_still_works_after_a_cold_start():
    """The end the buyer actually cares about: approve on a fresh instance and
    the discrepancies are still found."""
    po = await tools.create_purchase_order("SKU-4471", "S-002", 2500,
                                           NEED_BY, run_id="run-3")
    token = {"run_id": "run-3", "messages": [{"role": "user", "content": "x"}],
             "pending_po": po, **db.export_run_state("run-3")}

    cold_start()
    agent._rehydrate("run-3", token)

    conn = db.connect()
    conn.execute("UPDATE purchase_orders SET status='placed' WHERE id=?",
                 (po["po_id"],))
    conn.commit()
    conn.close()

    await tools.get_order_confirmation(po["po_id"])
    result = await tools.validate_order(po["po_id"])
    assert result["validation_passed"] is False
    assert {d["field"] for d in result["discrepancies"]} == {
        "unit_price", "delivery_date"}


async def test_memory_is_preferred_when_it_is_available():
    """Locally, and on a warm instance, nothing changes."""
    run = agent.Run("run-4", "original request")
    run.status = "awaiting_approval"
    run.pending_po = {"po_id": "PO-WARM"}
    agent.RUNS["run-4"] = run

    rebuilt = agent._rehydrate("run-4", {"run_id": "run-4", "messages": [],
                                         "pending_po": {"po_id": "PO-STALE"}})
    assert rebuilt is run
    assert rebuilt.pending_po["po_id"] == "PO-WARM"


async def test_resume_without_state_fails_honestly():
    cold_start()
    events = [e async for e in agent.resume("gone", True, "", None)]
    assert events[0]["type"] == "error"
    assert "could not be resumed" in events[0]["message"]


async def test_ensure_seeded_does_not_wipe_a_live_order():
    """A warm instance re-running startup must not destroy a pending order."""
    po = await tools.create_purchase_order("SKU-4471", "S-002", 2500,
                                           NEED_BY, run_id="run-5")
    db.ensure_seeded()
    conn = db.connect()
    still_there = conn.execute("SELECT * FROM purchase_orders WHERE id=?",
                               (po["po_id"],)).fetchone()
    conn.close()
    assert still_there is not None
