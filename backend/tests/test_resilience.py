"""Context compaction and rate-limit backoff.

Free provider tiers cap tokens per minute, and a multi-step agent run resends
its whole history on every step. These two mechanisms are what keep a long run
inside the budget, and keep it alive when it goes over anyway.
"""
import json
from datetime import date, timedelta

import pytest

from app import agent, compact, db, tools

NEED_BY = (date.today() + timedelta(days=10)).isoformat()


@pytest.fixture(autouse=True)
def fresh_db():
    db.reset_and_seed()
    yield


def size(obj) -> int:
    return len(json.dumps(obj, default=str))


async def test_ranking_is_much_smaller_for_the_model():
    full = await tools.rank_suppliers("SKU-4471", 2500, NEED_BY)
    small = compact.for_model("rank_suppliers", full)
    assert size(small) < size(full) * 0.5


async def test_compaction_keeps_what_the_model_needs_to_decide():
    full = await tools.rank_suppliers("SKU-4471", 2500, NEED_BY)
    small = compact.for_model("rank_suppliers", full)

    assert small["recommendation"] == full["recommendation"]
    assert small["rationale"] == full["rationale"]
    assert len(small["options"]) == len(full["ranked_options"])
    # The reason an option was rejected must survive - it is what the agent
    # tells the buyer about the contracted supplier.
    blocked = [o for o in small["options"] if not o["feasible"]]
    assert blocked and all("blocked_because" in o for o in blocked)


async def test_compaction_preserves_every_validation_discrepancy():
    po = await tools.create_purchase_order("SKU-4471", "S-002", 2500, NEED_BY)
    conn = db.connect()
    conn.execute("UPDATE purchase_orders SET status='placed' WHERE id=?",
                 (po["po_id"],))
    conn.commit()
    conn.close()
    await tools.get_order_confirmation(po["po_id"])

    full = await tools.validate_order(po["po_id"])
    small = compact.for_model("validate_order", full)
    assert len(small["discrepancies"]) == len(full["discrepancies"])
    assert small["validation_passed"] is False


async def test_errors_are_never_compacted_away():
    err = {"error": "something broke", "reason": "detail the model needs"}
    assert compact.for_model("rank_suppliers", err) == err


async def test_unknown_tool_passes_through_untouched():
    payload = {"anything": [1, 2, 3]}
    assert compact.for_model("some_future_tool", payload) == payload


async def test_malformed_payload_falls_back_instead_of_crashing():
    """Compaction must never be the reason a run dies."""
    assert compact.for_model("rank_suppliers", {"unexpected": True}) == {
        "unexpected": True}


# --- backoff -------------------------------------------------------------

class FakeResponse:
    def __init__(self, headers):
        self.headers = headers


class FakeRateLimit(Exception):
    def __init__(self, message, headers=None):
        super().__init__(message)
        self.response = FakeResponse(headers or {})


def test_retry_after_header_wins():
    exc = FakeRateLimit("rate limited", {"retry-after": "7"})
    assert agent._retry_after(exc, fallback=99) == 7.0


def test_millisecond_hint_is_floored_to_something_useful():
    """Groq says "try again in 75ms", but the minute window has not actually
    rolled over yet - retrying that fast just burns an attempt."""
    exc = FakeRateLimit("Please try again in 75ms")
    assert agent._retry_after(exc, fallback=99) == 2.0


def test_second_hint_is_respected():
    exc = FakeRateLimit("Please try again in 12.5s")
    assert agent._retry_after(exc, fallback=99) == 12.5


def test_falls_back_to_exponential_backoff_when_unparseable():
    exc = FakeRateLimit("slow down")
    assert agent._retry_after(exc, fallback=8.0) == 8.0


def test_wait_is_capped():
    exc = FakeRateLimit("Please try again in 600s")
    assert agent._retry_after(exc, fallback=1) == agent.MAX_BACKOFF_SECONDS
