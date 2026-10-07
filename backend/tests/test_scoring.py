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


# --- AC-3: policy-aware hook ---------------------------------------------

def test_default_policy_has_all_required_weight_keys():
    assert set(scoring.DEFAULT_POLICY["weights"]) == {"price", "speed", "reliability", "contract"}
    assert "infeasible_penalty" in scoring.DEFAULT_POLICY


def test_no_policy_produces_same_result_as_default_policy():
    q = [quote("A", 4.0, 5, avail=9999), quote("B", 5.0, 3, avail=9999)]
    without = scoring.score_quotes(q, 100, NEED_BY)
    with_default = scoring.score_quotes(q, 100, NEED_BY, policy=scoring.DEFAULT_POLICY)
    assert [r["supplier_id"] for r in without] == [r["supplier_id"] for r in with_default]
    assert [r["score"] for r in without] == [r["score"] for r in with_default]


def test_custom_policy_weights_change_the_ranking():
    """A speed-first policy promotes the faster-but-pricier supplier over the
    cheaper-but-slower one. Proves the policy hook actually affects outcomes."""
    cheap_slow = quote("CHEAP_SLOW", price=4.0, lead=7, avail=9999)
    expensive_fast = quote("EXPENSIVE_FAST", price=6.0, lead=2, avail=9999)

    # Default weights: cheaper supplier wins on price
    default_ranked = scoring.score_quotes([cheap_slow, expensive_fast], 100, NEED_BY)
    assert default_ranked[0]["supplier_id"] == "CHEAP_SLOW"

    # Speed-first policy: faster supplier wins
    speed_policy = {"weights": {"price": 0.10, "speed": 0.70,
                                "reliability": 0.15, "contract": 0.05}}
    speed_ranked = scoring.score_quotes([cheap_slow, expensive_fast], 100, NEED_BY,
                                        policy=speed_policy)
    assert speed_ranked[0]["supplier_id"] == "EXPENSIVE_FAST"


def test_custom_infeasible_penalty_is_applied():
    """A stricter penalty should push infeasible options further down."""
    feasible = quote("OK", price=5.0, lead=5, avail=9999)
    infeasible = quote("LATE", price=1.0, lead=30, avail=9999)  # arrives too late

    strict_policy = {"infeasible_penalty": 0.01}
    ranked = scoring.score_quotes([feasible, infeasible], 100, NEED_BY,
                                   policy=strict_policy)
    assert ranked[0]["supplier_id"] == "OK"
    assert ranked[1]["score"] < ranked[0]["score"] * 0.10


def test_effective_weights_recorded_in_each_ranked_entry():
    custom_weights = {"price": 0.50, "speed": 0.20, "reliability": 0.20, "contract": 0.10}
    ranked = scoring.score_quotes([quote("A", 4.0, 5, avail=9999)], 100, NEED_BY,
                                   policy={"weights": custom_weights})
    assert ranked[0]["weights"] == custom_weights
