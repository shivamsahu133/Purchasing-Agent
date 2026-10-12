"""Tools exposed to the agent, plus their JSON schemas.

Design rule: the model chooses *which* tool to call and explains the result.
It never supplies a number that matters. Prices, totals and rankings are
recomputed server-side from the catalog, so a hallucinated figure cannot reach
a purchase order.
"""
import asyncio
import os
import uuid
from datetime import date, datetime, timedelta

from . import db, scoring, suppliers

# Default approval threshold. Can be overridden by APPROVAL_THRESHOLD_USD env
# var. Setting the env var to an empty string signals the policy is explicitly
# unconfigured — the run must escalate rather than guess.
APPROVAL_THRESHOLD = 10_000.0


def get_approval_threshold() -> float | None:
    """Return the configured approval threshold, or None if unconfigured.

    Reads APPROVAL_THRESHOLD_USD from the environment:
      - absent  → fall back to the APPROVAL_THRESHOLD constant (10 000)
      - empty / non-numeric → policy intentionally unset; caller must escalate
      - valid float → use that value
    """
    raw = os.getenv("APPROVAL_THRESHOLD_USD")
    if raw is None:
        return APPROVAL_THRESHOLD
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None

# Deviation between the PO we send and the confirmation we get back, per
# supplier. This is the seam the validation stage exists to find.
# (unit_price_multiplier, extra_days, qty_delta)
CONFIRMATION_DEVIATIONS = {
    "S-001": (1.000, 0, 0),
    "S-002": (1.040, 5, 0),    # quietly reprices +4% and slips 5 days
    "S-003": (1.000, 1, 0),
    "S-004": (1.000, 0, 0),
}


# --- individual tools ----------------------------------------------------

async def check_inventory(sku: str) -> dict:
    conn = db.connect()
    row = conn.execute("SELECT * FROM inventory WHERE sku = ?", (sku,)).fetchone()
    open_pos = conn.execute(
        "SELECT id, qty, status, promised_date FROM purchase_orders "
        "WHERE sku = ? AND status NOT IN ('cancelled','rejected')", (sku,)
    ).fetchall()
    conn.close()

    if row is None:
        return {"error": "SKU " + sku + " not found in inventory."}

    on_hand, reorder_point = row["on_hand"], row["reorder_point"]
    return {
        "sku": sku,
        "name": row["name"],
        "on_hand": on_hand,
        "reorder_point": reorder_point,
        "unit": row["unit"],
        "below_reorder_point": on_hand < reorder_point,
        "shortfall_vs_reorder_point": max(0, reorder_point - on_hand),
        "open_purchase_orders": [dict(p) for p in open_pos],
    }


async def get_contract_terms(sku: str) -> dict:
    conn = db.connect()
    rows = conn.execute(
        "SELECT c.*, s.name, s.reliability, s.notes FROM contracts c "
        "JOIN suppliers s ON s.id = c.supplier_id WHERE c.sku = ?", (sku,)
    ).fetchall()
    conn.close()

    if not rows:
        return {"sku": sku, "under_contract": False,
                "note": "No active contract. Open-market sourcing required."}
    return {
        "sku": sku,
        "under_contract": True,
        "contracts": [{
            "supplier_id": r["supplier_id"],
            "supplier_name": r["name"],
            "contracted_price": r["contracted_price"],
            "contracted_lead_time_days": r["lead_time_days"],
            "valid_until": r["valid_until"],
            "supplier_note": r["notes"],
        } for r in rows],
    }


async def get_supplier_quotes(sku: str, qty: int) -> dict:
    quotes = await suppliers.fetch_all_quotes(sku, qty)
    if not quotes:
        return {"error": "No approved supplier carries " + sku + "."}
    return {"sku": sku, "requested_qty": qty, "quote_count": len(quotes),
            "quotes": quotes}


async def rank_suppliers(sku: str, qty: int, need_by: str,
                         budget: float = None, run_id: str = "") -> dict:
    """Score every live quote, select a recommendation, and persist the decision."""
    quotes = await suppliers.fetch_all_quotes(sku, qty)
    ranked = scoring.score_quotes(quotes, qty, need_by, budget)
    recommendation = scoring.select_recommendation(ranked)
    rationale = scoring.explain_choice(ranked)

    decision_id = None
    if run_id:
        decision_id = db.save_decision(
            run_id, sku, qty, need_by, ranked, recommendation, rationale)

    return {
        "sku": sku,
        "qty": qty,
        "need_by": need_by,
        "weights_used": scoring.WEIGHTS,
        "ranked_options": ranked,
        "feasible_count": sum(1 for r in ranked if r["feasible"]),
        "recommendation": recommendation["recommended_supplier_id"],
        "confidence": recommendation["confidence"],
        "confidence_note": recommendation.get("confidence_note"),
        "recommendation_detail": recommendation.get("recommended_option"),
        "rationale": rationale,
        "decision_id": decision_id,
    }


async def create_purchase_order(sku: str, supplier_id: str, qty: int,
                                need_by: str, run_id: str = "") -> dict:
    """Raise a PO. Blocks on human approval at or above the threshold.

    The unit price is taken from the catalog, never from the caller.
    The approval gate decision is always persisted to the approval_gates table.
    """
    conn = db.connect()
    row = conn.execute(
        "SELECT c.unit_price, c.lead_time_days, c.available_qty, "
        "c.min_order_qty, s.name FROM catalog c "
        "JOIN suppliers s ON s.id = c.supplier_id "
        "WHERE c.supplier_id = ? AND c.sku = ?", (supplier_id, sku)).fetchone()
    if row is None:
        conn.close()
        return {"error": "Supplier " + supplier_id + " does not list " + sku + "."}

    available, moq, name = row["available_qty"], row["min_order_qty"], row["name"]
    if available < qty:
        conn.close()
        return {"error": "Order rejected by the supplier system.",
                "reason": "{} has {:,} units available, {:,} requested.".format(
                    name, available, qty)}
    if qty < moq:
        conn.close()
        return {"error": "Order rejected by the supplier system.",
                "reason": "Minimum order quantity is {:,} units.".format(moq)}

    unit_price = round(row["unit_price"] * (0.97 if qty >= 2000 else 1.0), 4)
    total = round(unit_price * qty, 2)
    promised = (date.today() + timedelta(days=row["lead_time_days"])).isoformat()
    po_id = "PO-" + uuid.uuid4().hex[:6].upper()

    threshold = get_approval_threshold()

    if threshold is None:
        # AC-3: policy unconfigured — draft without placing and flag for escalation.
        status = "threshold_unknown"
        conn.execute(
            "INSERT INTO purchase_orders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (po_id, run_id, sku, supplier_id, qty, unit_price, total, need_by,
             promised, status, datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        conn.close()
        gate_id = db.save_approval_gate(run_id, po_id, total, None, "uncertain")
        return {
            "po_id": po_id, "sku": sku, "supplier_id": supplier_id,
            "supplier_name": name, "qty": qty, "unit_price": unit_price,
            "total": total, "need_by": need_by, "promised_date": promised,
            "status": status, "gate_id": gate_id,
            "approval_required": True,
            "message": (
                "Approval threshold is not configured. The order has been "
                "drafted but NOT placed. Call escalate_to_buyer — do not "
                "proceed without a policy decision."),
        }

    needs_approval = total >= threshold
    outcome = "requires_approval" if needs_approval else "auto_approved"
    status = "pending_approval" if needs_approval else "placed"

    conn.execute(
        "INSERT INTO purchase_orders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (po_id, run_id, sku, supplier_id, qty, unit_price, total, need_by,
         promised, status, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()

    gate_id = db.save_approval_gate(run_id, po_id, total, threshold, outcome)

    result = {
        "po_id": po_id, "sku": sku, "supplier_id": supplier_id,
        "supplier_name": name, "qty": qty, "unit_price": unit_price,
        "total": total, "need_by": need_by, "promised_date": promised,
        "status": status, "gate_id": gate_id,
    }
    if needs_approval:
        result["approval_required"] = True
        result["message"] = (
            "${:,.2f} is at or above the ${:,.0f} approval threshold. "
            "The order is DRAFTED but NOT placed. Stop here and wait for the "
            "buyer to approve or reject.".format(total, threshold))
    else:
        result["message"] = "Order placed with the supplier."
    return result


async def get_order_confirmation(po_id: str) -> dict:
    """Retrieve the supplier acknowledgement for a placed order."""
    conn = db.connect()
    po = conn.execute("SELECT * FROM purchase_orders WHERE id = ?",
                      (po_id,)).fetchone()
    if po is None:
        conn.close()
        return {"error": "No purchase order " + po_id + "."}
    if po["status"] == "threshold_unknown":
        conn.close()
        return {"error": po_id + " cannot be confirmed — approval threshold is "
                "unconfigured. Call escalate_to_buyer."}
    if po["status"] == "pending_approval":
        conn.close()
        return {"error": po_id + " has not been placed yet - it is awaiting "
                "buyer approval. No confirmation exists."}

    existing = conn.execute("SELECT * FROM confirmations WHERE po_id = ?",
                            (po_id,)).fetchone()
    if existing is None:
        mult, extra_days, qty_delta = CONFIRMATION_DEVIATIONS.get(
            po["supplier_id"], (1.0, 0, 0))
        conn.execute("INSERT INTO confirmations VALUES (?,?,?,?,?)", (
            po_id,
            round(po["unit_price"] * mult, 4),
            po["qty"] + qty_delta,
            (date.fromisoformat(po["promised_date"])
             + timedelta(days=extra_days)).isoformat(),
            datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        existing = conn.execute("SELECT * FROM confirmations WHERE po_id = ?",
                                (po_id,)).fetchone()
    conn.close()
    return {
        "po_id": po_id,
        "confirmed_unit_price": existing["confirmed_unit_price"],
        "confirmed_qty": existing["confirmed_qty"],
        "promised_date": existing["promised_date"],
        "received_at": existing["received_at"],
    }


async def validate_order(po_id: str) -> dict:
    """Diff what the supplier confirmed against what we actually ordered.

    This is the stage that separates an agent from a demo: the action is not
    finished until the outcome has been checked against the intent.
    """
    conn = db.connect()
    po = conn.execute("SELECT * FROM purchase_orders WHERE id = ?",
                      (po_id,)).fetchone()
    conf = conn.execute("SELECT * FROM confirmations WHERE po_id = ?",
                        (po_id,)).fetchone()
    conn.close()

    if po is None:
        return {"error": "No purchase order " + po_id + "."}
    if conf is None:
        return {"error": "No confirmation received for " + po_id +
                " yet. Call get_order_confirmation first."}

    discrepancies = []

    price_delta = conf["confirmed_unit_price"] - po["unit_price"]
    if abs(price_delta) > 0.001:
        pct = price_delta / po["unit_price"] * 100
        discrepancies.append({
            "field": "unit_price",
            "severity": "high" if abs(pct) > 2 else "low",
            "ordered": po["unit_price"],
            "confirmed": conf["confirmed_unit_price"],
            "detail": ("Unit price moved {:+.1f}% after the order was placed "
                       "(${:+.4f}/unit, ${:+,.2f} on this order).").format(
                           pct, price_delta, price_delta * po["qty"]),
        })

    if conf["confirmed_qty"] != po["qty"]:
        discrepancies.append({
            "field": "quantity", "severity": "high",
            "ordered": po["qty"], "confirmed": conf["confirmed_qty"],
            "detail": "Supplier confirmed {:,} units against an order for "
                      "{:,}.".format(conf["confirmed_qty"], po["qty"]),
        })

    slip = (date.fromisoformat(conf["promised_date"])
            - date.fromisoformat(po["need_by"])).days
    if slip > 0:
        discrepancies.append({
            "field": "delivery_date", "severity": "high",
            "ordered": po["need_by"], "confirmed": conf["promised_date"],
            "detail": "Confirmed delivery is {} day(s) AFTER the need-by date "
                      "of {}.".format(slip, po["need_by"]),
        })

    passed = not discrepancies
    return {
        "po_id": po_id,
        "validation_passed": passed,
        "discrepancy_count": len(discrepancies),
        "discrepancies": discrepancies,
        "ordered": {"unit_price": po["unit_price"], "qty": po["qty"],
                    "need_by": po["need_by"], "total": po["total"]},
        "confirmed": {"unit_price": conf["confirmed_unit_price"],
                      "qty": conf["confirmed_qty"],
                      "promised_date": conf["promised_date"],
                      "total": round(conf["confirmed_unit_price"]
                                     * conf["confirmed_qty"], 2)},
        "next_step": ("Order is confirmed as ordered. Summarise and finish."
                      if passed else
                      "Validation FAILED. Do not finish silently - explain each "
                      "discrepancy to the buyer, then call escalate_to_buyer "
                      "with concrete remediation options."),
    }


async def escalate_to_buyer(summary: str, options: list) -> dict:
    """Hand the decision back to the human with named options."""
    return {"escalated": True, "summary": summary, "options": options,
            "message": "Surfaced to the buyer for a decision."}


async def investigate_replenishment_need(sku: str, qty: int, need_by: str) -> dict:
    """Full INVESTIGATE stage: inventory assessment, contract check, and
    supplier quote collection in one deterministic call.

    Runs all three lookups in parallel against seeded/mock data and captures
    missing or low-confidence findings in structured lists so the DECIDE stage
    and any escalation paths have complete context.
    """
    missing_data: list[dict] = []
    low_confidence: list[dict] = []

    inv_result, contract_result, raw_quotes = await asyncio.gather(
        check_inventory(sku),
        get_contract_terms(sku),
        suppliers.fetch_all_quotes(sku, qty),
    )

    # Inventory assessment
    if "error" in inv_result:
        missing_data.append({
            "field": "inventory",
            "severity": "critical",
            "detail": inv_result["error"],
        })
        inventory = None
    else:
        inventory = inv_result

    # Contract check
    if not contract_result.get("under_contract"):
        missing_data.append({
            "field": "contracts",
            "severity": "medium",
            "detail": "No active contract for {}. Open-market sourcing required.".format(sku),
        })
    contracts = contract_result

    # Supplier quotes - surface gaps and reliability concerns
    if not raw_quotes:
        missing_data.append({
            "field": "supplier_quotes",
            "severity": "critical",
            "detail": "No approved supplier carries {} at quantity {:,}.".format(sku, qty),
        })
    else:
        if len(raw_quotes) == 1:
            low_confidence.append({
                "field": "supplier_quotes",
                "detail": "Only one supplier quote received — competitive comparison not possible.",
            })
        for q in raw_quotes:
            if q["fillable_qty"] < qty:
                low_confidence.append({
                    "field": "supplier_availability",
                    "supplier_id": q["supplier_id"],
                    "detail": "{} can only fill {:,} of {:,} requested units.".format(
                        q["supplier_name"], q["fillable_qty"], qty),
                })
            note = q.get("supplier_note") or ""
            if any(kw in note.lower() for kw in ("shutdown", "disruption", "maintenance")):
                low_confidence.append({
                    "field": "supplier_reliability",
                    "supplier_id": q["supplier_id"],
                    "detail": "Disruption note on {}: {}".format(q["supplier_name"], note),
                })

    return {
        "sku": sku,
        "qty_requested": qty,
        "need_by": need_by,
        "replenishment_needed": inventory["below_reorder_point"] if inventory else None,
        "inventory": inventory,
        "contracts": contracts,
        "quotes": {"count": len(raw_quotes), "quotes": raw_quotes},
        "missing_data": missing_data,
        "low_confidence": low_confidence,
        "data_complete": len(missing_data) == 0,
    }


# --- schemas + dispatch --------------------------------------------------

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "investigate_replenishment_need",
        "description": (
            "INVESTIGATE stage entry point. Checks inventory position, active contracts, "
            "and collects supplier quotes in one parallel call. Returns structured "
            "missing_data and low_confidence lists for anything that needs escalation. "
            "Call this first at the start of every run."
        ),
        "parameters": {"type": "object", "properties": {
            "sku": {"type": "string", "description": "e.g. SKU-4471"},
            "qty": {"type": "integer", "description": "Quantity to source"},
            "need_by": {"type": "string", "description": "ISO date YYYY-MM-DD"}},
            "required": ["sku", "qty", "need_by"]}}},
    {"type": "function", "function": {
        "name": "check_inventory",
        "description": "Current stock, reorder point and open POs for a SKU. Start here.",
        "parameters": {"type": "object", "properties": {
            "sku": {"type": "string", "description": "e.g. SKU-4471"}},
            "required": ["sku"]}}},
    {"type": "function", "function": {
        "name": "get_contract_terms",
        "description": "Active supply contracts for a SKU, with contracted price and lead time.",
        "parameters": {"type": "object", "properties": {
            "sku": {"type": "string"}}, "required": ["sku"]}}},
    {"type": "function", "function": {
        "name": "get_supplier_quotes",
        "description": "Live quotes from every approved supplier for a SKU and quantity.",
        "parameters": {"type": "object", "properties": {
            "sku": {"type": "string"}, "qty": {"type": "integer"}},
            "required": ["sku", "qty"]}}},
    {"type": "function", "function": {
        "name": "rank_suppliers",
        "description": ("Score and rank all suppliers on price, speed, reliability "
                        "and contract status. Returns a ranked list with a "
                        "per-criterion breakdown and the reasons any option is "
                        "infeasible. Use this to make the sourcing decision."),
        "parameters": {"type": "object", "properties": {
            "sku": {"type": "string"}, "qty": {"type": "integer"},
            "need_by": {"type": "string", "description": "ISO date YYYY-MM-DD"},
            "budget": {"type": "number", "description": "Optional cap on total spend"}},
            "required": ["sku", "qty", "need_by"]}}},
    {"type": "function", "function": {
        "name": "create_purchase_order",
        "description": ("Raise a purchase order. The unit price comes from the "
                        "supplier catalog - you do not supply it. Orders at or "
                        "above the approval threshold are drafted, not placed, "
                        "and require buyer approval."),
        "parameters": {"type": "object", "properties": {
            "sku": {"type": "string"}, "supplier_id": {"type": "string"},
            "qty": {"type": "integer"},
            "need_by": {"type": "string", "description": "ISO date YYYY-MM-DD"}},
            "required": ["sku", "supplier_id", "qty", "need_by"]}}},
    {"type": "function", "function": {
        "name": "get_order_confirmation",
        "description": "Fetch the supplier acknowledgement for a placed PO.",
        "parameters": {"type": "object", "properties": {
            "po_id": {"type": "string"}}, "required": ["po_id"]}}},
    {"type": "function", "function": {
        "name": "validate_order",
        "description": ("Compare the supplier confirmation against what was "
                        "ordered. Always call this after an order is placed."),
        "parameters": {"type": "object", "properties": {
            "po_id": {"type": "string"}}, "required": ["po_id"]}}},
    {"type": "function", "function": {
        "name": "escalate_to_buyer",
        "description": ("Raise an issue to the human buyer with concrete options, "
                        "when validation fails or no option is viable."),
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"},
                        "description": "Named remediation options"}},
            "required": ["summary", "options"]}}},
]

DISPATCH = {
    "investigate_replenishment_need": investigate_replenishment_need,
    "check_inventory": check_inventory,
    "get_contract_terms": get_contract_terms,
    "get_supplier_quotes": get_supplier_quotes,
    "rank_suppliers": rank_suppliers,
    "create_purchase_order": create_purchase_order,
    "get_order_confirmation": get_order_confirmation,
    "validate_order": validate_order,
    "escalate_to_buyer": escalate_to_buyer,
}


async def call_tool(name: str, args: dict, run_id: str = "") -> dict:
    fn = DISPATCH.get(name)
    if fn is None:
        return {"error": "Unknown tool " + str(name) + "."}
    if name in ("create_purchase_order", "rank_suppliers"):
        args = dict(args, run_id=run_id)
    try:
        return await fn(**args)
    except TypeError as exc:
        return {"error": "Bad arguments for " + name + ": " + str(exc)}
    except Exception as exc:  # surfaced to the model so it can recover
        return {"error": name + " failed: " + str(exc)}
