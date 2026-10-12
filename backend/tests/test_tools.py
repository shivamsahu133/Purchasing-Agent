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


async def test_unknown_tool_is_reported_not_raised():
    assert "error" in await tools.call_tool("nope", {})


async def test_bad_arguments_are_reported_back_to_the_model():
    r = await tools.call_tool("check_inventory", {"wrong_arg": 1})
    assert "error" in r


# --- DECIDE stage: recommendation selection and persistence (AC-1 / AC-2) -

async def test_rank_suppliers_returns_recommendation_and_confidence():
    r = await tools.rank_suppliers("SKU-4471", 1000, NEED_BY)
    assert "recommendation" in r
    assert "confidence" in r
    assert r["confidence"] in ("high", "low")


async def test_rank_suppliers_persists_decision_when_run_id_given():
    r = await tools.rank_suppliers("SKU-4471", 1000, NEED_BY, run_id="RUN-DEC-1")
    assert r["decision_id"] is not None
    assert r["decision_id"].startswith("DEC-")
    conn = db.connect()
    row = conn.execute("SELECT * FROM decisions WHERE id = ?",
                       (r["decision_id"],)).fetchone()
    conn.close()
    assert row is not None
    assert row["run_id"] == "RUN-DEC-1"
    assert row["sku"] == "SKU-4471"
    assert row["confidence"] in ("high", "low")
    assert row["rationale"] != ""


async def test_rank_suppliers_no_decision_written_without_run_id():
    r = await tools.rank_suppliers("SKU-4471", 1000, NEED_BY)
    assert r["decision_id"] is None
    conn = db.connect()
    count = conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"]
    conn.close()
    assert count == 0


async def test_decision_included_in_run_state_export():
    r = await tools.rank_suppliers("SKU-4471", 1000, NEED_BY, run_id="RUN-DEC-2")
    state = db.export_run_state("RUN-DEC-2")
    assert "decisions" in state
    assert len(state["decisions"]) == 1
    assert state["decisions"][0]["id"] == r["decision_id"]


async def test_decision_survives_export_import_cycle():
    r = await tools.rank_suppliers("SKU-4471", 1000, NEED_BY, run_id="RUN-DEC-3")
    state = db.export_run_state("RUN-DEC-3")

    db.reset_and_seed()
    conn = db.connect()
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM decisions").fetchone()["n"] == 0
    conn.close()

    db.import_run_state(state)
    conn = db.connect()
    row = conn.execute("SELECT * FROM decisions WHERE id = ?",
                       (r["decision_id"],)).fetchone()
    conn.close()
    assert row is not None
    assert row["sku"] == "SKU-4471"
    assert row["run_id"] == "RUN-DEC-3"


async def test_recommendation_detail_present_when_feasible_option_exists():
    r = await tools.rank_suppliers("SKU-4471", 1000, NEED_BY)
    assert r["recommendation_detail"] is not None
    detail = r["recommendation_detail"]
    assert detail["supplier_id"] == r["recommendation"]
    assert detail["unit_price"] > 0
