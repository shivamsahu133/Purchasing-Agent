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


# --- AC-1: quote fields present -----------------------------------------

async def test_investigate_returns_inventory_and_quotes():
    r = await tools.investigate_replenishment_need("SKU-4471", 2500, NEED_BY)
    assert r["sku"] == "SKU-4471"
    assert r["qty_requested"] == 2500
    assert r["replenishment_needed"] is True
    assert r["inventory"]["below_reorder_point"] is True
    assert r["quotes"]["count"] > 0


async def test_investigate_quote_contains_required_fields():
    r = await tools.investigate_replenishment_need("SKU-4471", 2500, NEED_BY)
    for q in r["quotes"]["quotes"]:
        assert "unit_price" in q
        assert "lead_time_days" in q
        assert "min_order_qty" in q
        assert "available_qty" in q


# --- AC-2: unavailable quotes recorded ----------------------------------


async def test_investigate_unavailable_supplier_recorded_in_missing_data():
    # SKU-7788 is only in catalog for S-002 and S-004; S-001 and S-003 carry nothing
    r = await tools.investigate_replenishment_need("SKU-7788", 100, NEED_BY)
    unavailable = r["unavailable_supplier_ids"]
    assert "S-001" in unavailable or "S-003" in unavailable
    missing_supplier_ids = {m.get("supplier_id") for m in r["missing_data"]
                            if m["field"] == "supplier_quotes"}
    assert missing_supplier_ids & {"S-001", "S-003"}


# --- AC-3: conflicts recorded -------------------------------------------

async def test_investigate_detects_lead_time_conflict_for_contracted_supplier():
    # S-001/SKU-4471: contract specifies 10 days, catalog shows 18 (shutdown)
    r = await tools.investigate_replenishment_need("SKU-4471", 2500, NEED_BY)
    conflicts = r["conflicts"]
    lt_conflicts = [c for c in conflicts
                    if c["supplier_id"] == "S-001" and c["field"] == "lead_time_days"]
    assert lt_conflicts, "Expected a lead-time conflict for S-001 on SKU-4471"
    assert lt_conflicts[0]["contracted"] == 10
    assert lt_conflicts[0]["quoted"] == 18


async def test_investigate_no_conflict_when_terms_match():
    # S-002 is not under contract for SKU-4471, so no conflict expected
    r = await tools.investigate_replenishment_need("SKU-4471", 2500, NEED_BY)
    s002_conflicts = [c for c in r["conflicts"] if c["supplier_id"] == "S-002"]
    assert not s002_conflicts


# --- AC-4: quotes persisted to SQLite ------------------------------------

async def test_investigate_persists_received_quotes_to_db():
    await tools.investigate_replenishment_need("SKU-4471", 2500, NEED_BY, run_id="test-run")
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM quote_results WHERE run_id = 'test-run' AND sku = 'SKU-4471'"
    ).fetchall()
    conn.close()
    received = [r for r in rows if r["status"] == "received"]
    assert len(received) > 0
    for row in received:
        assert row["unit_price"] is not None
        assert row["lead_time_days"] is not None
        assert row["min_order_qty"] is not None
        assert row["available_qty"] is not None


async def test_investigate_persists_unavailable_suppliers_to_db():
    await tools.investigate_replenishment_need("SKU-7788", 100, NEED_BY, run_id="test-gaps")
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM quote_results WHERE run_id = 'test-gaps' AND sku = 'SKU-7788'"
    ).fetchall()
    conn.close()
    unavailable = [r for r in rows if r["status"] == "unavailable"]
    assert len(unavailable) > 0
    assert any(r["supplier_id"] in ("S-001", "S-003") for r in unavailable)


async def test_investigate_persists_conflict_detail_to_db():
    await tools.investigate_replenishment_need("SKU-4471", 2500, NEED_BY, run_id="test-conflict")
    conn = db.connect()
    row = conn.execute(
        "SELECT conflict_detail FROM quote_results "
        "WHERE run_id = 'test-conflict' AND supplier_id = 'S-001'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["conflict_detail"] is not None  # lead-time conflict was stored


# --- existing investigate tests -----------------------------------------

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
