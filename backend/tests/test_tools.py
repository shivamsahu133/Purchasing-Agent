"""Guardrails around the act and validate stages."""
from datetime import date, timedelta

import pytest

from app import db, tools

NEED_BY = (date.today() + timedelta(days=10)).isoformat()


@pytest.fixture(autouse=True)
def fresh_db():
    db.reset_and_seed()
    yield


async def test_inventory_flags_the_shortfall():
    r = await tools.check_inventory("SKU-4471")
    assert r["below_reorder_point"] is True
    assert r["shortfall_vs_reorder_point"] == 2000 - 340


async def test_unknown_sku_errors_rather_than_guessing():
    assert "error" in await tools.check_inventory("SKU-DOES-NOT-EXIST")


async def test_contracted_supplier_is_surfaced():
    r = await tools.get_contract_terms("SKU-4471")
    assert r["under_contract"] is True
    assert r["contracts"][0]["supplier_id"] == "S-001"


async def test_large_order_is_drafted_not_placed():
    r = await tools.create_purchase_order("SKU-4471", "S-002", 2500, NEED_BY)
    assert r["approval_required"] is True
    assert r["status"] == "pending_approval"
    assert r["total"] >= tools.APPROVAL_THRESHOLD


async def test_small_order_goes_straight_through():
    r = await tools.create_purchase_order("SKU-4471", "S-002", 1000, NEED_BY)
    assert r["status"] == "placed"
    assert "approval_required" not in r


async def test_price_comes_from_the_catalog_not_the_caller():
    """The model cannot put a number it invented onto a purchase order:
    create_purchase_order does not accept a price at all."""
    r = await tools.create_purchase_order("SKU-4471", "S-004", 1000, NEED_BY)
    conn = db.connect()
    catalog_price = conn.execute(
        "SELECT unit_price FROM catalog WHERE supplier_id='S-004' "
        "AND sku='SKU-4471'").fetchone()["unit_price"]
    conn.close()
    assert r["unit_price"] == pytest.approx(catalog_price)


async def test_volume_break_applies_above_the_threshold_quantity():
    small = await tools.create_purchase_order("SKU-4471", "S-002", 1000, NEED_BY)
    large = await tools.create_purchase_order("SKU-4471", "S-002", 2500, NEED_BY)
    assert large["unit_price"] < small["unit_price"]


async def test_supplier_rejects_an_order_beyond_available_stock():
    r = await tools.create_purchase_order("SKU-4471", "S-001", 2500, NEED_BY)
    assert "error" in r
    assert "600" in r["reason"]


async def test_no_confirmation_exists_before_approval():
    po = await tools.create_purchase_order("SKU-4471", "S-002", 2500, NEED_BY)
    r = await tools.get_order_confirmation(po["po_id"])
    assert "error" in r
    assert "awaiting buyer approval" in r["error"]


async def test_validation_catches_a_reprice_and_a_late_delivery():
    """The seeded scenario: Apex confirms at +4% and two days late."""
    po = await tools.create_purchase_order("SKU-4471", "S-002", 2500, NEED_BY)
    conn = db.connect()
    conn.execute("UPDATE purchase_orders SET status='placed' WHERE id=?",
                 (po["po_id"],))
    conn.commit()
    conn.close()

    await tools.get_order_confirmation(po["po_id"])
    r = await tools.validate_order(po["po_id"])

    assert r["validation_passed"] is False
    fields = {d["field"] for d in r["discrepancies"]}
    assert fields == {"unit_price", "delivery_date"}
    assert all(d["severity"] == "high" for d in r["discrepancies"])
    assert "escalate_to_buyer" in r["next_step"]


async def test_validation_passes_when_the_supplier_honours_the_order():
    po = await tools.create_purchase_order("SKU-4471", "S-004", 1000, NEED_BY)
    await tools.get_order_confirmation(po["po_id"])
    r = await tools.validate_order(po["po_id"])
    assert r["validation_passed"] is True
    assert r["discrepancy_count"] == 0


async def test_validation_refuses_to_run_without_a_confirmation():
    po = await tools.create_purchase_order("SKU-4471", "S-004", 1000, NEED_BY)
    r = await tools.validate_order(po["po_id"])
    assert "error" in r


async def test_investigate_returns_inventory_and_quotes():
    r = await tools.investigate_replenishment_need("SKU-4471", 2500, NEED_BY)
    assert r["sku"] == "SKU-4471"
    assert r["qty_requested"] == 2500
    assert r["replenishment_needed"] is True
    assert r["inventory"]["below_reorder_point"] is True
    assert r["quotes"]["count"] > 0


async def test_investigate_surfaces_no_contract_as_missing_data():
    # SKU-2210 has no contract in the seed data
    r = await tools.investigate_replenishment_need("SKU-2210", 500, NEED_BY)
    fields = {m["field"] for m in r["missing_data"]}
    assert "contracts" in fields


async def test_investigate_surfaces_contracted_sku_has_no_missing_contract():
    # SKU-4471 has a contract with S-001
    r = await tools.investigate_replenishment_need("SKU-4471", 2500, NEED_BY)
    missing_fields = {m["field"] for m in r["missing_data"]}
    assert "contracts" not in missing_fields


async def test_investigate_surfaces_partial_fill_as_low_confidence():
    # S-001 only has 600 units of SKU-4471; ordering 2500 should flag it
    r = await tools.investigate_replenishment_need("SKU-4471", 2500, NEED_BY)
    low_fields = {lc["field"] for lc in r["low_confidence"]}
    assert "supplier_availability" in low_fields


async def test_investigate_surfaces_supplier_disruption_as_low_confidence():
    # S-001 has a "shutdown" note in the seed data
    r = await tools.investigate_replenishment_need("SKU-4471", 2500, NEED_BY)
    disrupted = [lc for lc in r["low_confidence"]
                 if lc["field"] == "supplier_reliability"]
    assert any(lc["supplier_id"] == "S-001" for lc in disrupted)


async def test_investigate_unknown_sku_surfaces_as_critical_missing():
    r = await tools.investigate_replenishment_need("SKU-DOES-NOT-EXIST", 100, NEED_BY)
    critical = [m for m in r["missing_data"] if m["severity"] == "critical"]
    assert any(m["field"] == "inventory" for m in critical)
    assert r["replenishment_needed"] is None
    assert r["data_complete"] is False


async def test_investigate_is_repeatable_on_same_seeded_data():
    r1 = await tools.investigate_replenishment_need("SKU-4471", 2500, NEED_BY)
    r2 = await tools.investigate_replenishment_need("SKU-4471", 2500, NEED_BY)
    assert r1["quotes"]["count"] == r2["quotes"]["count"]
    assert r1["missing_data"] == r2["missing_data"]
    assert r1["low_confidence"] == r2["low_confidence"]
    assert r1["replenishment_needed"] == r2["replenishment_needed"]


async def test_unknown_tool_is_reported_not_raised():
    assert "error" in await tools.call_tool("nope", {})


async def test_bad_arguments_are_reported_back_to_the_model():
    r = await tools.call_tool("check_inventory", {"wrong_arg": 1})
    assert "error" in r


# --- approval gate persistence (AC-1 / AC-2) -----------------------------

async def test_placed_order_creates_auto_approved_gate():
    r = await tools.create_purchase_order("SKU-4471", "S-002", 1000, NEED_BY,
                                          run_id="RUN-GATE-1")
    assert r["status"] == "placed"
    assert "gate_id" in r
    conn = db.connect()
    gate = conn.execute("SELECT * FROM approval_gates WHERE id = ?",
                        (r["gate_id"],)).fetchone()
    conn.close()
    assert gate is not None
    assert gate["outcome"] == "auto_approved"
    assert gate["po_id"] == r["po_id"]
    assert gate["threshold_used"] == pytest.approx(tools.APPROVAL_THRESHOLD)


async def test_large_order_creates_requires_approval_gate():
    r = await tools.create_purchase_order("SKU-4471", "S-002", 2500, NEED_BY,
                                          run_id="RUN-GATE-2")
    assert r["status"] == "pending_approval"
    conn = db.connect()
    gate = conn.execute("SELECT * FROM approval_gates WHERE id = ?",
                        (r["gate_id"],)).fetchone()
    conn.close()
    assert gate["outcome"] == "requires_approval"
    assert gate["order_total"] == pytest.approx(r["total"])


async def test_gate_included_in_run_state_export():
    r = await tools.create_purchase_order("SKU-4471", "S-004", 1000, NEED_BY,
                                          run_id="RUN-GATE-3")
    state = db.export_run_state("RUN-GATE-3")
    assert "gates" in state
    assert len(state["gates"]) == 1
    assert state["gates"][0]["id"] == r["gate_id"]


async def test_gate_survives_export_import_cycle():
    r = await tools.create_purchase_order("SKU-4471", "S-004", 1000, NEED_BY,
                                          run_id="RUN-GATE-4")
    state = db.export_run_state("RUN-GATE-4")

    db.reset_and_seed()
    conn = db.connect()
    count_before = conn.execute(
        "SELECT COUNT(*) AS n FROM approval_gates").fetchone()["n"]
    conn.close()
    assert count_before == 0

    db.import_run_state(state)
    conn = db.connect()
    gate = conn.execute("SELECT * FROM approval_gates WHERE id = ?",
                        (r["gate_id"],)).fetchone()
    conn.close()
    assert gate is not None
    assert gate["outcome"] == "auto_approved"


# --- unconfigured threshold path (AC-3) ----------------------------------

async def test_uncertain_outcome_when_threshold_env_var_is_empty(monkeypatch):
    monkeypatch.setenv("APPROVAL_THRESHOLD_USD", "")
    r = await tools.create_purchase_order("SKU-4471", "S-004", 1000, NEED_BY,
                                          run_id="RUN-GATE-5")
    assert r["status"] == "threshold_unknown"
    assert r["approval_required"] is True
    assert "escalate_to_buyer" in r["message"]


async def test_uncertain_gate_row_when_threshold_unconfigured(monkeypatch):
    monkeypatch.setenv("APPROVAL_THRESHOLD_USD", "")
    r = await tools.create_purchase_order("SKU-4471", "S-004", 1000, NEED_BY,
                                          run_id="RUN-GATE-6")
    conn = db.connect()
    gate = conn.execute("SELECT * FROM approval_gates WHERE id = ?",
                        (r["gate_id"],)).fetchone()
    conn.close()
    assert gate["outcome"] == "uncertain"
    assert gate["threshold_used"] is None


async def test_confirmation_blocked_for_threshold_unknown_order(monkeypatch):
    monkeypatch.setenv("APPROVAL_THRESHOLD_USD", "")
    r = await tools.create_purchase_order("SKU-4471", "S-004", 1000, NEED_BY)
    conf = await tools.get_order_confirmation(r["po_id"])
    assert "error" in conf
    assert "unconfigured" in conf["error"]


async def test_custom_threshold_via_env_var(monkeypatch):
    monkeypatch.setenv("APPROVAL_THRESHOLD_USD", "5000")
    small = await tools.create_purchase_order("SKU-4471", "S-004", 500, NEED_BY)
    large = await tools.create_purchase_order("SKU-4471", "S-004", 1000, NEED_BY)
    assert small["status"] == "placed"
    assert large["status"] == "pending_approval"
