"""HTTP surface.

Agent runs stream back as newline-delimited JSON so the UI can render each
step as it happens rather than waiting for a final answer. A run that parks for
approval simply ends its stream; the approve/reject call opens a new one and
the agent picks up where it left off.
"""
import json
import os
import uuid
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

load_dotenv(Path(__file__).parent.parent / ".env")

from . import agent, db, tools  # noqa: E402  (must follow load_dotenv)

app = FastAPI(title="Purchasing Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173",
                   "http://localhost:4173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    db.ensure_seeded()


class RunRequest(BaseModel):
    request: str


class ApprovalRequest(BaseModel):
    run_id: str
    approved: bool
    note: str = ""
    # Carries a suspended run across a cold start on a stateless platform.
    resume_token: dict | None = None


async def ndjson(events):
    async for event in events:
        yield json.dumps(event, default=str) + "\n"


STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@app.post("/api/run")
async def run(body: RunRequest):
    run_id = uuid.uuid4().hex[:8]
    return StreamingResponse(ndjson(agent.start(run_id, body.request)),
                             media_type="application/x-ndjson",
                             headers=STREAM_HEADERS)


@app.post("/api/approve")
async def approve(body: ApprovalRequest):
    return StreamingResponse(
        ndjson(agent.resume(body.run_id, body.approved, body.note,
                            body.resume_token)),
        media_type="application/x-ndjson", headers=STREAM_HEADERS)


@app.get("/api/scenario")
def scenario():
    """Seed context for the UI, and the demo request pre-filled for the buyer."""
    db.ensure_seeded()
    need_by = (date.today() + timedelta(days=10)).isoformat()
    conn = db.connect()
    inventory = [dict(r) for r in conn.execute(
        "SELECT * FROM inventory").fetchall()]
    suppliers = [dict(r) for r in conn.execute(
        "SELECT * FROM suppliers").fetchall()]
    conn.close()
    return {
        "default_request": (
            "We are short on SKU-4471. Raise a replenishment order for 2,500 "
            "units, needed by " + need_by + ". Budget is flexible but keep it "
            "sensible."),
        "need_by": need_by,
        "approval_threshold": tools.APPROVAL_THRESHOLD,
        "model": agent.MODEL,
        "inventory": inventory,
        "suppliers": suppliers,
    }


@app.get("/api/orders")
def orders():
    db.ensure_seeded()
    conn = db.connect()
    rows = conn.execute(
        "SELECT p.*, s.name AS supplier_name FROM purchase_orders p "
        "JOIN suppliers s ON s.id = p.supplier_id "
        "ORDER BY p.created_at DESC").fetchall()
    conn.close()
    return {"orders": [dict(r) for r in rows]}


@app.post("/api/reset")
def reset():
    db.reset_and_seed()
    agent.RUNS.clear()
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True, "model": agent.MODEL,
            "key_loaded": bool(os.getenv("GROQ_API_KEY"))}
