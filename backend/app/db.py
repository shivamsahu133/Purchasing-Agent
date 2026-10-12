"""SQLite store for inventory, contracts, suppliers and purchase orders.

Seeded deterministically so the demo tells the same story on every run.
Dates are stored relative to "today" so the scenario never goes stale.
"""
import sqlite3
import uuid
import os
from datetime import date, datetime, timedelta
from pathlib import Path

def _default_db_path() -> Path:
    """Serverless filesystems are read-only apart from /tmp.

    On Vercel the database is scratch space for one invocation: reference data
    is re-seeded deterministically on a cold start, and anything a run actually
    created travels with the request (see `export_run_state`). Locally it is a
    normal file next to the app.
    """
    if os.getenv("VERCEL"):
        return Path("/tmp/purchasing.db")
    return Path(__file__).parent.parent / "purchasing.db"


DB_PATH = Path(os.getenv("DB_PATH") or _default_db_path())

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    on_hand INTEGER NOT NULL,
    reorder_point INTEGER NOT NULL,
    unit TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suppliers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    reliability REAL NOT NULL,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS contracts (
    sku TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    contracted_price REAL NOT NULL,
    lead_time_days INTEGER NOT NULL,
    valid_until TEXT NOT NULL,
    PRIMARY KEY (sku, supplier_id)
);
CREATE TABLE IF NOT EXISTS catalog (
    supplier_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    unit_price REAL NOT NULL,
    lead_time_days INTEGER NOT NULL,
    min_order_qty INTEGER NOT NULL,
    available_qty INTEGER NOT NULL,
    PRIMARY KEY (supplier_id, sku)
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    sku TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    qty INTEGER NOT NULL,
    unit_price REAL NOT NULL,
    total REAL NOT NULL,
    need_by TEXT NOT NULL,
    promised_date TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- What the supplier sends back. Deliberately allowed to disagree with the PO:
-- this is what the validation stage exists to catch.
CREATE TABLE IF NOT EXISTS confirmations (
    po_id TEXT PRIMARY KEY,
    confirmed_unit_price REAL NOT NULL,
    confirmed_qty INTEGER NOT NULL,
    promised_date TEXT NOT NULL,
    received_at TEXT NOT NULL
);
-- DECIDE stage: one row per rank_suppliers call recording the selected option.
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    qty INTEGER NOT NULL,
    need_by TEXT NOT NULL,
    recommended_supplier_id TEXT,
    recommended_unit_price REAL,
    recommended_total REAL,
    recommended_lead_time_days INTEGER,
    recommended_estimated_delivery TEXT,
    feasible_count INTEGER NOT NULL,
    confidence TEXT NOT NULL,
    confidence_note TEXT,
    rationale TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
"""

# --- seed ----------------------------------------------------------------
TODAY = date.today()


def d(offset_days: int) -> str:
    return (TODAY + timedelta(days=offset_days)).isoformat()


INVENTORY = [
    ("SKU-4471", "M8x40 Stainless Hex Bolt (A4-80)", 340, 2000, "each"),
    ("SKU-2210", "Nitrile O-Ring 22mm ID", 8400, 3000, "each"),
    ("SKU-7788", "Shielded Ball Bearing 6204-2RS", 190, 400, "each"),
    ("SKU-1502", "Copper Lug 16mm2", 5200, 1500, "each"),
]

SUPPLIERS = [
    ("S-001", "Meridian Industrial", 0.96, "Primary contracted supplier. Plant maintenance shutdown in progress."),
    ("S-002", "Apex Fasteners", 0.91, "Secondary approved vendor."),
    ("S-003", "Global Supply Co", 0.88, "Lowest cost, long ocean freight lead times."),
    ("S-004", "RapidParts Express", 0.82, "Air-freight expeditor. Premium pricing."),
]

CONTRACTS = [
    ("SKU-4471", "S-001", 4.20, 10, d(240)),
    ("SKU-7788", "S-001", 11.50, 14, d(240)),
]

# supplier_id, sku, unit_price, lead_time_days, min_order_qty, available_qty
CATALOG = [
    # Meridian is contracted and cheapest-on-paper, but is short on stock and
    # its lead time has blown out because of the shutdown. This is the disruption.
    ("S-001", "SKU-4471", 4.20, 18, 100, 600),
    ("S-002", "SKU-4471", 4.95, 7, 250, 6000),
    ("S-003", "SKU-4471", 3.80, 21, 1000, 9000),
    ("S-004", "SKU-4471", 6.40, 3, 100, 3000),
    ("S-002", "SKU-7788", 12.10, 9, 50, 800),
    ("S-004", "SKU-7788", 15.75, 4, 25, 400),
]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def reset_and_seed() -> None:
    """Drop transactional state and re-seed reference data.

    Called on every server start so the demo is repeatable.
    """
    conn = connect()
    conn.executescript(SCHEMA)
    for table in ("inventory", "suppliers", "contracts", "catalog",
                  "purchase_orders", "confirmations", "decisions"):
        conn.execute(f"DELETE FROM {table}")
    conn.executemany("INSERT INTO inventory VALUES (?,?,?,?,?)", INVENTORY)
    conn.executemany("INSERT INTO suppliers VALUES (?,?,?,?)", SUPPLIERS)
    conn.executemany("INSERT INTO contracts VALUES (?,?,?,?,?)", CONTRACTS)
    conn.executemany("INSERT INTO catalog VALUES (?,?,?,?,?,?)", CATALOG)
    conn.commit()
    conn.close()


def ensure_seeded() -> None:
    """Seed reference data only if it is missing.

    A warm serverless instance keeps /tmp between invocations, so a blind reset
    here would wipe an order mid-run, between the draft and the approval.
    """
    conn = connect()
    conn.executescript(SCHEMA)
    empty = conn.execute("SELECT COUNT(*) AS n FROM suppliers").fetchone()["n"] == 0
    conn.close()
    if empty:
        reset_and_seed()


# --- run state travel ----------------------------------------------------
# On a stateless platform the rows a run created cannot be assumed to survive
# until the buyer approves. They ride along with the request instead.

def export_run_state(run_id: str) -> dict:
    conn = connect()
    orders = [dict(r) for r in conn.execute(
        "SELECT * FROM purchase_orders WHERE run_id = ?", (run_id,)).fetchall()]
    ids = [o["id"] for o in orders]
    confirmations = []
    if ids:
        marks = ",".join("?" * len(ids))
        confirmations = [dict(r) for r in conn.execute(
            "SELECT * FROM confirmations WHERE po_id IN (" + marks + ")",
            ids).fetchall()]
    decisions = [dict(r) for r in conn.execute(
        "SELECT * FROM decisions WHERE run_id = ?", (run_id,)).fetchall()]
    conn.close()
    return {"orders": orders, "confirmations": confirmations, "decisions": decisions}


def import_run_state(state: dict | None) -> None:
    """Re-insert rows a previous invocation created, if they are not here."""
    if not state:
        return
    conn = connect()
    conn.executescript(SCHEMA)
    for o in state.get("orders") or []:
        conn.execute(
            "INSERT OR REPLACE INTO purchase_orders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (o["id"], o["run_id"], o["sku"], o["supplier_id"], o["qty"],
             o["unit_price"], o["total"], o["need_by"], o["promised_date"],
             o["status"], o["created_at"]))
    for c in state.get("confirmations") or []:
        conn.execute("INSERT OR REPLACE INTO confirmations VALUES (?,?,?,?,?)",
                     (c["po_id"], c["confirmed_unit_price"], c["confirmed_qty"],
                      c["promised_date"], c["received_at"]))
    for d in state.get("decisions") or []:
        conn.execute(
            "INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d["id"], d["run_id"], d["sku"], d["qty"], d["need_by"],
             d["recommended_supplier_id"], d["recommended_unit_price"],
             d["recommended_total"], d["recommended_lead_time_days"],
             d["recommended_estimated_delivery"], d["feasible_count"],
             d["confidence"], d["confidence_note"], d["rationale"], d["decided_at"]))
    conn.commit()
    conn.close()


def save_decision(run_id: str, sku: str, qty: int, need_by: str,
                  ranked: list, recommendation: dict, rationale: str) -> str:
    """Persist the DECIDE stage recommendation and return its id."""
    decision_id = "DEC-" + uuid.uuid4().hex[:8].upper()
    rec = recommendation.get("recommended_option") or {}
    feasible_count = sum(1 for r in ranked if r.get("feasible"))
    conn = connect()
    conn.execute(
        "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (decision_id, run_id, sku, qty, need_by,
         recommendation.get("recommended_supplier_id"),
         rec.get("unit_price"), rec.get("total_cost"),
         rec.get("lead_time_days"), rec.get("estimated_delivery"),
         feasible_count,
         recommendation["confidence"],
         recommendation.get("confidence_note"),
         rationale,
         datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()
    return decision_id
