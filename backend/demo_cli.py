"""Headless run of the full scenario, for reviewers who do not want to start
the frontend. Auto-approves the purchase order so the whole arc runs unattended.

    python demo_cli.py            # auto-approve
    python demo_cli.py --reject   # exercise the rejection path
"""
import asyncio
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import uuid
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from app import agent, db  # noqa: E402

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
CYAN, YELLOW, GREEN, RED = "\033[36m", "\033[33m", "\033[32m", "\033[31m"


def render(event: dict) -> None:
    kind = event.get("type")
    if kind == "thought":
        text = " ".join(event["text"].split())
        print(DIM + "  thinking: " + text[:220] + RESET)
    elif kind == "message":
        print("\n" + event["text"] + "\n")
    elif kind == "tool_call":
        print(CYAN + "  -> " + event["name"] + "(" +
              ", ".join(str(k) + "=" + str(v)
                        for k, v in event["args"].items()) + ")" + RESET)
    elif kind == "tool_result":
        result = event["result"]
        if isinstance(result, dict) and result.get("error"):
            print(RED + "     error: " + str(result["error"]) + RESET)
        else:
            keys = ", ".join(list(result)[:6]) if isinstance(result, dict) else ""
            print(DIM + "     ok (" + keys + ")" + RESET)
    elif kind == "awaiting_approval":
        po = event["po"]
        print(YELLOW + BOLD + "\n  [APPROVAL GATE] " + po["po_id"] + "  " +
              po["supplier_name"] + "  " + str(po["qty"]) + " units  $" +
              "{:,.2f}".format(po["total"]) + RESET)
    elif kind == "notice":
        print(YELLOW + "  " + event["text"] + RESET)
    elif kind == "error":
        print(RED + "  ERROR: " + event["message"] + RESET)


async def main() -> None:
    approve = "--reject" not in sys.argv
    db.reset_and_seed()

    need_by = (date.today() + timedelta(days=10)).isoformat()
    request = ("We are short on SKU-4471. Raise a replenishment order for 2,500 "
               "units, needed by " + need_by + ".")
    run_id = uuid.uuid4().hex[:8]

    print(BOLD + "\nBUYER: " + RESET + request + "\n" + "-" * 70)

    parked = False
    async for event in agent.start(run_id, request):
        render(event)
        if event.get("type") == "awaiting_approval":
            parked = True

    if parked:
        print("-" * 70)
        print(BOLD + ("BUYER: approved." if approve else "BUYER: rejected.")
              + RESET + "\n")
        async for event in agent.resume(run_id, approve):
            render(event)

    print("-" * 70)
    conn = db.connect()
    for row in conn.execute("SELECT id, supplier_id, qty, total, status, "
                            "promised_date FROM purchase_orders").fetchall():
        print(GREEN + "  PO " + row["id"] + "  " + row["supplier_id"] + "  " +
              str(row["qty"]) + " units  $" + "{:,.2f}".format(row["total"]) +
              "  " + row["status"] + "  eta " + str(row["promised_date"]) + RESET)
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
