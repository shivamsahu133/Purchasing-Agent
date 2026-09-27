"""Shrink tool results before they go back into the model's context.

The UI and the model want different things from the same tool call. The UI
renders the full payload - every score component, every supplier field - because
a buyer needs to audit the decision. The model only needs enough to choose the
next step and explain itself.

Sending the full payload to both is what pushes a long run over a tokens-per-
minute limit, and it buys nothing: the model does not compute the ranking, it
reads the result. So the event stream carries the full object to the browser and
this module carries a compact one into the message history.
"""


def _quote(q: dict) -> dict:
    return {
        "supplier_id": q["supplier_id"],
        "supplier": q["supplier_name"],
        "unit_price": q["unit_price"],
        "available": q["available_qty"],
        "moq": q["min_order_qty"],
        "lead_days": q["lead_time_days"],
        "eta": q["estimated_delivery"],
        "under_contract": q["under_contract"],
    }


def _option(o: dict) -> dict:
    out = {
        "rank": o["rank"],
        "supplier_id": o["supplier_id"],
        "supplier": o["supplier_name"],
        "unit_price": o["unit_price"],
        "total": o["total_cost"],
        "eta": o["estimated_delivery"],
        "slack_days": o["days_of_slack"],
        "score": o["score"],
        "feasible": o["feasible"],
    }
    if o["blocking_reasons"]:
        out["blocked_because"] = o["blocking_reasons"]
    return out


def _check_inventory(r: dict) -> dict:
    return {
        "sku": r["sku"], "name": r["name"], "on_hand": r["on_hand"],
        "reorder_point": r["reorder_point"],
        "below_reorder_point": r["below_reorder_point"],
        "shortfall": r["shortfall_vs_reorder_point"],
        "open_po_count": len(r["open_purchase_orders"]),
    }


def _get_contract_terms(r: dict) -> dict:
    if not r.get("under_contract"):
        return r
    return {"sku": r["sku"], "under_contract": True,
            "contracts": [{k: c[k] for k in
                           ("supplier_id", "supplier_name", "contracted_price",
                            "contracted_lead_time_days", "supplier_note")}
                          for c in r["contracts"]]}


def _get_supplier_quotes(r: dict) -> dict:
    return {"sku": r["sku"], "requested_qty": r["requested_qty"],
            "quotes": [_quote(q) for q in r["quotes"]]}


def _rank_suppliers(r: dict) -> dict:
    return {
        "sku": r["sku"], "qty": r["qty"], "need_by": r["need_by"],
        "options": [_option(o) for o in r["ranked_options"]],
        "feasible_count": r["feasible_count"],
        "recommendation": r["recommendation"],
        "rationale": r["rationale"],
    }


def _validate_order(r: dict) -> dict:
    return {
        "po_id": r["po_id"],
        "validation_passed": r["validation_passed"],
        "discrepancies": [{"field": d["field"], "severity": d["severity"],
                           "detail": d["detail"]} for d in r["discrepancies"]],
        "ordered": r["ordered"],
        "confirmed": r["confirmed"],
        "next_step": r["next_step"],
    }


COMPACTORS = {
    "check_inventory": _check_inventory,
    "get_contract_terms": _get_contract_terms,
    "get_supplier_quotes": _get_supplier_quotes,
    "rank_suppliers": _rank_suppliers,
    "validate_order": _validate_order,
}


def for_model(name: str, result):
    """Return the version of `result` that belongs in the message history."""
    if not isinstance(result, dict) or "error" in result:
        return result
    fn = COMPACTORS.get(name)
    if fn is None:
        return result
    try:
        return fn(result)
    except (KeyError, TypeError):
        # Never let compaction break a run - fall back to the full payload.
        return result
