"""Mock external supplier APIs.

Each supplier is treated as a separate remote system with its own latency and
quirks, rather than a single table read, so the investigate stage looks like
real integration work. Swap these for HTTP calls and nothing downstream changes.
"""
import asyncio
import random
from datetime import date, timedelta

from . import db

# Per-supplier simulated round-trip latency in seconds.
LATENCY = {"S-001": 0.45, "S-002": 0.30, "S-003": 0.80, "S-004": 0.20}


async def fetch_quote(supplier_id: str, sku: str, qty: int) -> dict | None:
    """Ask one supplier what they'd charge for `qty` of `sku`.

    Returns None when the supplier does not carry the part at all.
    """
    await asyncio.sleep(LATENCY.get(supplier_id, 0.3))

    conn = db.connect()
    row = conn.execute(
        "SELECT c.*, s.name, s.reliability, s.notes FROM catalog c "
        "JOIN suppliers s ON s.id = c.supplier_id "
        "WHERE c.supplier_id = ? AND c.sku = ?",
        (supplier_id, sku),
    ).fetchone()
    contract = conn.execute(
        "SELECT contracted_price, lead_time_days FROM contracts "
        "WHERE sku = ? AND supplier_id = ?",
        (sku, supplier_id),
    ).fetchone()
    conn.close()

    if row is None:
        return None

    unit_price = row["unit_price"]
    # Volume break: suppliers discount 3% above 2000 units.
    if qty >= 2000:
        unit_price = round(unit_price * 0.97, 4)

    fillable = min(qty, row["available_qty"])
    eta = date.today() + timedelta(days=row["lead_time_days"])

    return {
        "supplier_id": supplier_id,
        "supplier_name": row["name"],
        "sku": sku,
        "unit_price": unit_price,
        "requested_qty": qty,
        "available_qty": row["available_qty"],
        "fillable_qty": fillable,
        "min_order_qty": row["min_order_qty"],
        "lead_time_days": row["lead_time_days"],
        "estimated_delivery": eta.isoformat(),
        "reliability": row["reliability"],
        "under_contract": contract is not None,
        "contracted_price": contract["contracted_price"] if contract else None,
        "supplier_note": row["notes"],
    }


async def fetch_all_quotes(sku: str, qty: int) -> list[dict]:
    """Fan out to every supplier in parallel and collect whoever responds."""
    conn = db.connect()
    ids = [r["id"] for r in conn.execute("SELECT id FROM suppliers").fetchall()]
    conn.close()

    results = await asyncio.gather(*(fetch_quote(i, sku, qty) for i in ids))
    return [r for r in results if r is not None]
