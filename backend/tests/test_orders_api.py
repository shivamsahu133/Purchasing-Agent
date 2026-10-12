"""ACT stage: PO creation, SQLite persistence, and GET /api/orders visibility.

AC-1  create_purchase_order returns sku, qty, unit_price, total, lead_time_days.
AC-2  The PO row is present in SQLite immediately after creation.
AC-3  The PO appears in GET /api/orders with all required fields.
"""
from datetime import date, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app import db, tools
from app.main import app

NEED_BY = (date.today() + timedelta(days=10)).isoformat()


@pytest.fixture(autouse=True)
def fresh_db():
    db.reset_and_seed()
    yield


# ── AC-1: tool response carries all required fields ──────────────────────────

async def test_create_po_response_has_item_identifiers():
    r = await tools.create_purchase_order("SKU-4471", "S-002", 1000, NEED_BY)
    assert r["po_id"].startswith("PO-")
    assert r["sku"] == "SKU-4471"
    assert r["supplier_id"] == "S-002"
    assert r["supplier_name"] != ""


async def test_create_po_response_has_quantity_and_pricing():
    r = await tools.create_purchase_order("SKU-4471", "S-002", 1000, NEED_BY)
    assert r["qty"] == 1000
    assert r["unit_price"] > 0
    assert r["total"] == pytest.approx(r["unit_price"] * 1000)


async def test_create_po_response_has_lead_time():
    r = await tools.create_purchase_order("SKU-4471", "S-002", 1000, NEED_BY)
    assert "lead_time_days" in r
    assert r["lead_time_days"] > 0
    assert r["promised_date"] != ""


# ── AC-2: PO is persisted to SQLite ──────────────────────────────────────────

async def test_created_po_is_persisted_in_sqlite():
    r = await tools.create_purchase_order("SKU-4471", "S-002", 1000, NEED_BY)
    conn = db.connect()
    row = conn.execute("SELECT * FROM purchase_orders WHERE id = ?",
                       (r["po_id"],)).fetchone()
    conn.close()
    assert row is not None
    assert row["sku"] == "SKU-4471"
    assert row["supplier_id"] == "S-002"
    assert row["qty"] == 1000
    assert row["unit_price"] == pytest.approx(r["unit_price"])
    assert row["total"] == pytest.approx(r["total"])
    assert row["need_by"] == NEED_BY
    assert row["promised_date"] == r["promised_date"]


async def test_large_po_is_persisted_with_pending_approval_status():
    r = await tools.create_purchase_order("SKU-4471", "S-002", 2500, NEED_BY)
    conn = db.connect()
    row = conn.execute("SELECT status FROM purchase_orders WHERE id = ?",
                       (r["po_id"],)).fetchone()
    conn.close()
    assert row["status"] == "pending_approval"


async def test_small_po_is_persisted_with_placed_status():
    r = await tools.create_purchase_order("SKU-4471", "S-004", 1000, NEED_BY)
    conn = db.connect()
    row = conn.execute("SELECT status FROM purchase_orders WHERE id = ?",
                       (r["po_id"],)).fetchone()
    conn.close()
    assert row["status"] == "placed"


# ── AC-3: PO appears in GET /api/orders ──────────────────────────────────────

async def test_get_orders_returns_empty_list_before_any_po():
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as client:
        resp = await client.get("/api/orders")
    assert resp.status_code == 200
    assert resp.json()["orders"] == []


async def test_created_po_appears_in_get_orders():
    r = await tools.create_purchase_order("SKU-4471", "S-002", 1000, NEED_BY)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as client:
        resp = await client.get("/api/orders")
    assert resp.status_code == 200
    orders = resp.json()["orders"]
    assert len(orders) == 1
    order = orders[0]
    assert order["id"] == r["po_id"]
    assert order["sku"] == "SKU-4471"
    assert order["supplier_id"] == "S-002"
    assert order["qty"] == 1000
    assert order["unit_price"] == pytest.approx(r["unit_price"])
    assert order["total"] == pytest.approx(r["total"])


async def test_orders_endpoint_includes_lead_time_days():
    await tools.create_purchase_order("SKU-4471", "S-002", 1000, NEED_BY)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as client:
        resp = await client.get("/api/orders")
    order = resp.json()["orders"][0]
    assert "lead_time_days" in order
    assert order["lead_time_days"] > 0


async def test_orders_endpoint_includes_supplier_name():
    await tools.create_purchase_order("SKU-4471", "S-002", 1000, NEED_BY)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as client:
        resp = await client.get("/api/orders")
    order = resp.json()["orders"][0]
    assert order["supplier_name"] == "Apex Fasteners"


async def test_multiple_pos_all_appear_in_orders_newest_first():
    r1 = await tools.create_purchase_order("SKU-4471", "S-004", 500, NEED_BY)
    r2 = await tools.create_purchase_order("SKU-4471", "S-002", 1000, NEED_BY)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as client:
        resp = await client.get("/api/orders")
    orders = resp.json()["orders"]
    assert len(orders) == 2
    assert orders[0]["id"] == r2["po_id"]   # newest first
    assert orders[1]["id"] == r1["po_id"]
