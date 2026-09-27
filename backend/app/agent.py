"""The agent loop.

A plain tool-calling loop against Groq, deliberately kept readable rather than
wrapped in a framework - every step the agent takes is something you can point
at in this file.

The loop can suspend. When a purchase order crosses the approval threshold the
run parks in `awaiting_approval` with its message history intact, and resumes
from exactly that point once the buyer decides.
"""
import asyncio
import json
import os
import re
from datetime import date

from openai import AsyncOpenAI, RateLimitError

from . import compact, db, tools

MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
MAX_STEPS = 14

# Free Groq tiers cap tokens-per-minute, and a multi-step run bumps into it.
# The limit is a pause, not a failure, so the loop waits it out rather than
# dropping a run that is halfway through a purchase.
MAX_RATE_LIMIT_RETRIES = 6
MAX_BACKOFF_SECONDS = 45.0

_client: AsyncOpenAI | None = None


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        key = os.getenv("GROQ_API_KEY")
        if not key:
            raise RuntimeError(
                "GROQ_API_KEY is not set. Copy .env.example to .env and add it.")
        _client = AsyncOpenAI(api_key=key,
                              base_url="https://api.groq.com/openai/v1")
    return _client


SYSTEM_PROMPT = """You are a purchasing agent working for a buyer in an \
industrial supply team. You handle replenishment end to end.

Today is {today}.

Work in four stages, and do not skip any of them:

1. INVESTIGATE. Establish the facts before you form a view. Check stock, check
   whether the SKU is under contract, then get quotes from every supplier. Never
   assume a price or a lead time - read it from a tool.
   If the buyer states a quantity, source exactly that quantity. Do not net it
   off against stock on hand or silently round it. If you think a different
   quantity is the right call, order what was asked and flag your
   recommendation separately for the buyer to act on.
2. DECIDE. Call rank_suppliers to score the options. The ranking is computed by
   code, not by you. Your job is to read it, sanity-check it against the
   buyer's constraints, and state plainly which supplier you are choosing, what
   it costs, when it arrives, and why the runner-up lost. If the contracted
   supplier cannot be used, say so explicitly - that is important to the buyer.
3. ACT. Raise the purchase order. If the tool tells you approval is required,
   STOP and wait. Do not attempt to place it another way and do not pretend it
   was placed.
4. VALIDATE. After an order is placed, fetch the confirmation and call
   validate_order. An order is not done because it was sent - it is done when
   what came back matches what you asked for. If validation fails, explain every
   discrepancy in plain language and call escalate_to_buyer with concrete,
   specific options (for example: accept the slip, split the order with a faster
   supplier to cover the gap, or push back on the price increase).

Style: write for a busy buyer. Short paragraphs, concrete numbers, no filler and
no restating of the tool output as JSON. When you finish, give a short summary
of what you did and what, if anything, the buyer needs to decide."""


class Run:
    """In-memory state for one agent run."""

    def __init__(self, run_id: str, request: str):
        self.id = run_id
        self.request = request
        self.status = "running"
        self.pending_po: dict | None = None
        self.messages: list[dict] = [
            {"role": "system",
             "content": SYSTEM_PROMPT.format(today=date.today().isoformat())},
            {"role": "user", "content": request},
        ]


RUNS: dict[str, Run] = {}


def _assistant_message(msg) -> dict:
    """Convert the SDK message object into a plain dict for the history."""
    out: dict = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        out["tool_calls"] = [{
            "id": tc.id, "type": "function",
            "function": {"name": tc.function.name,
                         "arguments": tc.function.arguments},
        } for tc in msg.tool_calls]
    return out


async def _call_model(run: Run):
    """One model call."""
    resp = await client().chat.completions.create(
        model=MODEL,
        messages=run.messages,
        tools=tools.TOOL_SCHEMAS,
        tool_choice="auto",
        temperature=0.2,
    )
    return resp.choices[0].message


def _retry_after(exc: RateLimitError, fallback: float) -> float:
    """How long the provider wants us to wait.

    Prefer the Retry-After header; otherwise read the hint out of the message
    ("try again in 1.5s"); otherwise fall back to exponential backoff.
    """
    header = getattr(getattr(exc, "response", None), "headers", None)
    if header:
        raw = header.get("retry-after")
        if raw:
            try:
                return min(float(raw), MAX_BACKOFF_SECONDS)
            except ValueError:
                pass

    match = re.search(r"try again in ([\d.]+)\s*(ms|s)", str(exc))
    if match:
        value = float(match.group(1))
        seconds = value / 1000 if match.group(2) == "ms" else value
        # A sub-second hint means the window is about to roll over; waiting a
        # beat longer is cheaper than burning a retry that fails again.
        return min(max(seconds, 2.0), MAX_BACKOFF_SECONDS)

    return min(fallback, MAX_BACKOFF_SECONDS)


async def drive(run: Run):
    """Run the loop until it finishes or parks for approval. Yields events."""
    steps = 0
    while steps < MAX_STEPS:
        steps += 1
        msg = None
        backoff = 4.0
        for attempt in range(MAX_RATE_LIMIT_RETRIES):
            try:
                msg = await _call_model(run)
                break
            except RateLimitError as exc:
                if attempt == MAX_RATE_LIMIT_RETRIES - 1:
                    run.status = "error"
                    yield {"type": "error",
                           "message": "Rate limited by the provider and out of "
                                      "retries. Wait a minute and run again."}
                    return
                wait = _retry_after(exc, backoff)
                backoff *= 2
                yield {"type": "notice",
                       "text": "Provider rate limit hit. Waiting {:.0f}s and "
                               "retrying (attempt {} of {}).".format(
                                   wait, attempt + 2, MAX_RATE_LIMIT_RETRIES)}
                await asyncio.sleep(wait)
            except Exception as exc:
                run.status = "error"
                yield {"type": "error", "message": str(exc)}
                return

        if msg is None:
            run.status = "error"
            yield {"type": "error", "message": "No response from the model."}
            return

        reasoning = getattr(msg, "reasoning", None)
        if reasoning:
            yield {"type": "thought", "text": reasoning}
        if msg.content:
            yield {"type": "message", "text": msg.content}

        run.messages.append(_assistant_message(msg))

        if not msg.tool_calls:
            run.status = "done"
            yield {"type": "done", "reason": "complete"}
            return

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            yield {"type": "tool_call", "id": tc.id, "name": name, "args": args}
            result = await tools.call_tool(name, args, run_id=run.id)
            yield {"type": "tool_result", "id": tc.id, "name": name,
                   "result": result}

            # Full payload to the browser (above), compact one into context.
            run.messages.append({
                "role": "tool", "tool_call_id": tc.id, "name": name,
                "content": json.dumps(compact.for_model(name, result),
                                      default=str),
            })

            if isinstance(result, dict) and result.get("approval_required"):
                run.status = "awaiting_approval"
                run.pending_po = result
                # The run suspends here, possibly for minutes. On a stateless
                # platform the next request may reach a different instance, so
                # everything needed to resume goes back to the client and
                # returns with the approval.
                yield {"type": "awaiting_approval", "po": result,
                       "resume_token": {
                           "run_id": run.id,
                           "messages": run.messages,
                           "pending_po": result,
                           **db.export_run_state(run.id),
                       }}
                return

    run.status = "done"
    yield {"type": "done", "reason": "step limit reached"}


async def start(run_id: str, request: str):
    run = Run(run_id, request)
    RUNS[run_id] = run
    yield {"type": "status", "status": "running", "run_id": run_id}
    async for event in drive(run):
        yield event


def _rehydrate(run_id: str, token: dict | None) -> Run | None:
    """Rebuild a suspended run.

    Prefers the in-process cache, which is what a local server or a warm
    instance will have. Falls back to the token the client sent back, which is
    what makes the approval gate survive a cold start.
    """
    run = RUNS.get(run_id)
    if run is not None and run.status == "awaiting_approval":
        return run
    if not token or not token.get("messages"):
        return None

    db.import_run_state(token)
    rebuilt = Run(token.get("run_id") or run_id, "")
    rebuilt.messages = token["messages"]
    rebuilt.pending_po = token.get("pending_po")
    rebuilt.status = "awaiting_approval"
    RUNS[rebuilt.id] = rebuilt
    return rebuilt


async def resume(run_id: str, approved: bool, note: str = "",
                 resume_token: dict | None = None):
    """Resume a parked run once the buyer has decided."""
    run = _rehydrate(run_id, resume_token)
    if run is None:
        yield {"type": "error",
               "message": "That run could not be resumed - its state is gone. "
                          "Start a new run."}
        return

    po_id = (run.pending_po or {}).get("po_id", "")
    conn = db.connect()
    conn.execute("UPDATE purchase_orders SET status = ? WHERE id = ?",
                 ("placed" if approved else "rejected", po_id))
    conn.commit()
    conn.close()

    if approved:
        instruction = (
            "The buyer APPROVED " + po_id + ". The order has now been placed "
            "with the supplier. Continue with the validation stage: fetch the "
            "order confirmation and validate it against what was ordered.")
    else:
        instruction = (
            "The buyer REJECTED " + po_id + ". It has been cancelled and no "
            "order exists. Do not place it again. Explain the remaining options "
            "and escalate to the buyer for a decision.")
    if note:
        instruction += " Buyer note: " + note

    run.messages.append({"role": "user", "content": instruction})
    run.status = "running"
    run.pending_po = None

    yield {"type": "status", "status": "running",
           "decision": "approved" if approved else "rejected"}
    async for event in drive(run):
        yield event
