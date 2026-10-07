"""Deterministic supplier scoring.

Deliberately NOT done by the language model. Prices, dates and the final
ranking come from code that can be unit-tested and audited; the model decides
which tools to call and explains the outcome in prose. An LLM that invents a
unit price is a liability, so it never gets the chance.
"""
from datetime import date

# All tunable purchasing policy parameters live here. Pass a partial dict to
# score_quotes to override any subset; the rest fall back to these defaults.
# Weights must sum to 1.0 — caller's responsibility if they override.
DEFAULT_POLICY: dict = {
    "weights": {
        "price": 0.35,       # lowest total cost relative to field
        "speed": 0.30,       # delivery within the need-by window
        "reliability": 0.20, # historical on-time rate
        "contract": 0.15,    # preference for contracted suppliers
    },
    "infeasible_penalty": 0.30,  # multiplier applied to infeasible option scores
}

# Backward-compatible aliases — existing code that references WEIGHTS or
# INFEASIBLE_PENALTY directly continues to work unchanged.
WEIGHTS = DEFAULT_POLICY["weights"]
INFEASIBLE_PENALTY = DEFAULT_POLICY["infeasible_penalty"]


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def score_quotes(quotes: list[dict], qty: int, need_by: str,
                 budget: float | None = None,
                 policy: dict | None = None) -> list[dict]:
    """Rank quotes best-first, with a per-criterion breakdown for each.

    Pass a partial `policy` dict to override any DEFAULT_POLICY fields for
    this ranking call. Only the keys you supply are overridden; the rest stay
    at their defaults. This is the AC-3 policy hook.
    """
    if not quotes:
        return []

    # Merge caller policy on top of defaults — any key left out keeps its default.
    effective = {**DEFAULT_POLICY, **(policy or {})}
    weights = {**DEFAULT_POLICY["weights"], **effective.get("weights", {})}
    infeasible_penalty = effective.get("infeasible_penalty", INFEASIBLE_PENALTY)

    days_available = (date.fromisoformat(need_by) - date.today()).days
    totals = [q["unit_price"] * qty for q in quotes]
    cheapest = min(totals)

    scored = []
    for q in quotes:
        total = q["unit_price"] * qty
        blocking = []

        if q["available_qty"] < qty:
            blocking.append(
                f"can only supply {q['available_qty']:,} of {qty:,} units")
        if qty < q["min_order_qty"]:
            blocking.append(
                f"order below minimum of {q['min_order_qty']:,} units")
        slack = days_available - q["lead_time_days"]
        if slack < 0:
            blocking.append(
                f"arrives {abs(slack)} days after the need-by date")
        if budget is not None and total > budget:
            blocking.append(f"total ${total:,.2f} exceeds budget ${budget:,.2f}")

        price_score = cheapest / total if total else 0.0
        # Meeting the date is what carries the value; buffer beyond it helps
        # only a little. A linear-in-slack score would hand every order to the
        # air-freight expeditor, which is not how a buyer actually thinks.
        if slack < 0 or days_available <= 0:
            speed_score = 0.0
        else:
            speed_score = 0.80 + 0.20 * _clamp(slack / days_available)
        parts = {
            "price": round(price_score, 4),
            "speed": round(speed_score, 4),
            "reliability": round(q["reliability"], 4),
            "contract": 1.0 if q["under_contract"] else 0.0,
        }
        raw = sum(weights[k] * v for k, v in parts.items())
        feasible = not blocking
        final = raw if feasible else raw * infeasible_penalty

        scored.append({
            **q,
            "total_cost": round(total, 2),
            "days_of_slack": slack,
            "feasible": feasible,
            "blocking_reasons": blocking,
            "score_breakdown": parts,
            "weights": weights,
            "score": round(final, 4),
        })

    scored.sort(key=lambda s: s["score"], reverse=True)
    for i, s in enumerate(scored, 1):
        s["rank"] = i
    return scored


def explain_choice(ranked: list[dict]) -> str:
    """One-line human-readable rationale comparing the winner to the runner-up."""
    if not ranked:
        return "No quotes were returned."
    win = ranked[0]
    if not win["feasible"]:
        return ("No supplier can meet the requirement as specified. "
                f"Closest is {win['supplier_name']}: "
                f"{'; '.join(win['blocking_reasons'])}.")
    line = (f"{win['supplier_name']} at ${win['unit_price']:.2f}/unit "
            f"(${win['total_cost']:,.2f} total), arriving {win['estimated_delivery']} "
            f"with {win['days_of_slack']} days to spare.")
    if len(ranked) > 1:
        run = ranked[1]
        why = ("; ".join(run["blocking_reasons"]) if run["blocking_reasons"]
               else f"scored {run['score']:.3f} against {win['score']:.3f}")
        line += f" Runner-up {run['supplier_name']} was not chosen: {why}."
    return line
