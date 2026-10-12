"""The decision logic is deterministic, so it is testable. That is the whole
reason ranking lives in code rather than in the model."""
from datetime import date, timedelta

from app import scoring


def quote(sid, price, lead, avail, rel=0.9, moq=1, contract=False):
    return {
        "supplier_id": sid, "supplier_name": sid, "sku": "SKU-1",
        "unit_price": price, "requested_qty": 100, "available_qty": avail,
        "fillable_qty": min(100, avail), "min_order_qty": moq,
        "lead_time_days": lead,
        "estimated_delivery": (date.today() + timedelta(days=lead)).isoformat(),
        "reliability": rel, "under_contract": contract,
        "contracted_price": price if contract else None, "supplier_note": "",
    }


NEED_BY = (date.today() + timedelta(days=10)).isoformat()


def test_short_stock_is_infeasible_and_says_why():
    ranked = scoring.score_quotes([quote("A", 5.0, 3, avail=50)], 100, NEED_BY)
    assert ranked[0]["feasible"] is False
    assert "can only supply" in ranked[0]["blocking_reasons"][0]


def test_late_delivery_is_infeasible():
    ranked = scoring.score_quotes([quote("A", 5.0, 20, avail=500)], 100, NEED_BY)
    assert ranked[0]["feasible"] is False
    assert "after the need-by date" in ranked[0]["blocking_reasons"][0]


def test_below_minimum_order_quantity_is_infeasible():
    ranked = scoring.score_quotes(
        [quote("A", 5.0, 3, avail=500, moq=250)], 100, NEED_BY)
    assert ranked[0]["feasible"] is False
    assert "below minimum" in ranked[0]["blocking_reasons"][0]


def test_budget_cap_blocks_an_otherwise_viable_option():
    ranked = scoring.score_quotes(
        [quote("A", 5.0, 3, avail=500)], 100, NEED_BY, budget=100.0)
    assert ranked[0]["feasible"] is False
    assert "exceeds budget" in ranked[0]["blocking_reasons"][0]


def test_feasible_always_outranks_infeasible():
    """Even a much cheaper option loses if it cannot actually be delivered."""
    ranked = scoring.score_quotes([
        quote("CHEAP_BUT_LATE", 1.0, 30, avail=9999),
        quote("VIABLE", 9.0, 5, avail=9999),
    ], 100, NEED_BY)
    assert ranked[0]["supplier_id"] == "VIABLE"
    assert ranked[1]["feasible"] is False


def test_arriving_very_early_does_not_beat_arriving_on_time_on_price():
    """Regression: a linear-in-slack speed score handed every order to the
    air-freight expeditor. Meeting the date is what matters; extra buffer is
    worth only a little."""
    ranked = scoring.score_quotes([
        quote("ON_TIME_CHEAP", 4.95, 7, avail=9999),
        quote("EARLY_EXPENSIVE", 6.40, 3, avail=9999, rel=0.82),
    ], 2500, NEED_BY)
    assert ranked[0]["supplier_id"] == "ON_TIME_CHEAP"


def test_contract_status_breaks_a_tie():
    base = dict(price=5.0, lead=5, avail=9999, rel=0.9)
    ranked = scoring.score_quotes([
        quote("OPEN_MARKET", **base),
        quote("CONTRACTED", **base, contract=True),
    ], 100, NEED_BY)
    assert ranked[0]["supplier_id"] == "CONTRACTED"


def test_explanation_names_the_runner_up():
    ranked = scoring.score_quotes([
        quote("A", 4.0, 5, avail=9999),
        quote("B", 9.0, 5, avail=9999),
    ], 100, NEED_BY)
    text = scoring.explain_choice(ranked)
    assert "A" in text and "B" in text


def test_explanation_is_honest_when_nothing_is_viable():
    ranked = scoring.score_quotes([quote("A", 4.0, 40, avail=10)], 100, NEED_BY)
    assert "No supplier can meet" in scoring.explain_choice(ranked)


# --- select_recommendation -----------------------------------------------

def test_select_recommendation_picks_the_top_feasible_option():
    ranked = scoring.score_quotes([
        quote("CHEAP", 4.0, 5, avail=9999),
        quote("PRICEY", 9.0, 5, avail=9999, rel=0.5),
    ], 100, NEED_BY)
    result = scoring.select_recommendation(ranked)
    assert result["recommended_supplier_id"] == "CHEAP"
    assert result["recommended_option"]["supplier_id"] == "CHEAP"


def test_select_recommendation_high_confidence_when_clear_winner():
    ranked = scoring.score_quotes([
        quote("A", 4.0, 5, avail=9999),
        quote("B", 9.0, 9, avail=9999, rel=0.5),
    ], 100, NEED_BY)
    result = scoring.select_recommendation(ranked)
    assert result["confidence"] == "high"
    assert result["confidence_note"] is None


def test_select_recommendation_low_confidence_when_only_one_feasible():
    ranked = scoring.score_quotes([
        quote("GOOD", 5.0, 5, avail=9999),
        quote("LATE", 5.0, 40, avail=9999),
    ], 100, NEED_BY)
    result = scoring.select_recommendation(ranked)
    assert result["confidence"] == "low"
    assert "GOOD" in result["confidence_note"]


def test_select_recommendation_low_confidence_on_near_tie():
    ranked = scoring.score_quotes([
        quote("A", 5.0, 5, avail=9999, rel=0.9),
        quote("B", 5.0, 5, avail=9999, rel=0.9),
    ], 100, NEED_BY)
    result = scoring.select_recommendation(ranked)
    assert result["confidence"] == "low"
    assert result["recommended_supplier_id"] is not None


def test_select_recommendation_returns_none_when_no_feasible_options():
    ranked = scoring.score_quotes([quote("A", 5.0, 40, avail=10)], 100, NEED_BY)
    result = scoring.select_recommendation(ranked)
    assert result["recommended_supplier_id"] is None
    assert result["recommended_option"] is None


def test_select_recommendation_returns_none_for_empty_input():
    result = scoring.select_recommendation([])
    assert result["recommended_supplier_id"] is None


def test_select_recommendation_option_snapshot_has_required_fields():
    ranked = scoring.score_quotes([
        quote("A", 4.0, 5, avail=9999),
        quote("B", 9.0, 9, avail=9999, rel=0.5),
    ], 100, NEED_BY)
    opt = scoring.select_recommendation(ranked)["recommended_option"]
    for field in ("supplier_id", "supplier_name", "unit_price", "total_cost",
                  "lead_time_days", "estimated_delivery", "score", "score_breakdown"):
        assert field in opt, f"missing field: {field}"
