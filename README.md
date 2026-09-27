# AI Purchasing Agent

A full-stack agent that handles industrial replenishment end to end: it
investigates the facts, makes a sourcing decision it can defend, raises a
purchase order behind a human approval gate, and then **validates that what the
supplier confirmed matches what was actually ordered**.

The scenario implemented is **replenishment under supplier disruption** — the
case where the obvious answer (the contracted supplier) turns out not to work,
and the agent has to reason about the alternatives.

---

## The scenario

> "We are short on SKU-4471. Raise a replenishment order for 2,500 units,
> needed in 10 days."

What the agent finds, and what it does about it:

| Stage | What happens |
| --- | --- |
| **Investigate** | Stock is 340 against a reorder point of 2,000. SKU-4471 is under contract with Meridian Industrial at $4.20/unit — but Meridian is in a maintenance shutdown, holds only 600 units, and its lead time has blown out to 18 days. Quotes are pulled from all four approved suppliers in parallel. |
| **Decide** | Four options, no dominant one. Global Supply is cheapest at $3.80 but arrives 11 days late. RapidParts can deliver in 3 days but costs $6.40. Apex Fasteners at $4.95 hits the date with 3 days to spare. The agent picks Apex and states why the others lost. |
| **Act** | The PO totals **$12,003.75**, above the $10,000 approval threshold, so it is *drafted, not placed*. The run suspends and waits for a human. |
| **Validate** | Once approved, the agent fetches the supplier confirmation and diffs it against the order. Apex has quietly **repriced +4%** and **slipped delivery 2 days past the need-by date**. Neither was visible from the order itself. |
| **Escalate** | The agent explains both discrepancies in plain language and hands the buyer concrete options: accept, renegotiate, split the order with a faster supplier, or cancel and re-source. |

That last transition is the point of the project. Sending an order is not the
same as getting what you ordered, and an agent that stops at "PO placed" has
not finished the job.

---

## Running it

Requires Python 3.11+ and Node 18+.

```bash
# 1. Backend
cd backend
pip install -r requirements.txt
cp .env.example .env          # then add your GROQ_API_KEY
python -m uvicorn app.main:app --reload --port 8000

# 2. Frontend (separate terminal)
cd frontend
npm install
npm run dev                   # http://localhost:5173
```

Open http://localhost:5173, press **Run agent**, and approve the order when the
gate appears.

**No frontend?** The whole scenario runs in the terminal:

```bash
cd backend
python demo_cli.py            # auto-approves
python demo_cli.py --reject   # exercises the rejection path
```

**Tests** (deterministic, no API calls, ~3s):

```bash
cd backend && python -m pytest -q     # 34 tests
```

> **On Groq's free tier** the 8,000 tokens/minute cap can be reached partway
> through a run. The agent handles this itself — it backs off, tells you it is
> waiting, and resumes. Tool payloads sent to the model are compacted to stay
> inside the budget in the first place. Nothing to configure.

---

## How it is built

```
backend/
  app/
    agent.py       tool-calling loop; suspends and resumes around approval
    tools.py       the 8 tools the agent can call, and their schemas
    scoring.py     supplier ranking — deterministic, not model-generated
    suppliers.py   mock supplier APIs (independent, latency-simulated)
    db.py          SQLite schema + deterministic seed
    main.py        FastAPI; streams agent events as NDJSON
  demo_cli.py      headless runner
  tests/           23 tests over the decision and validation logic
frontend/
  src/App.jsx              streaming trace, stage rail, approval gate
  src/components/Results.jsx   purpose-built views per tool result
```

**Model:** `openai/gpt-oss-120b` via Groq, using the OpenAI-compatible API.

### Design decisions worth explaining

**The model orchestrates; it does not calculate.** Supplier ranking lives in
`scoring.py` as a weighted function over price, speed, reliability and contract
status. The agent decides *which tools to call* and explains the outcome in
prose, but every number that reaches a purchase order comes from code. An LLM
that invents a unit price is a liability, so it never gets the opportunity —
`create_purchase_order` does not even accept a price parameter, it reads the
catalog. There is a test asserting exactly this.

**Feasibility is a hard constraint, not a weighting.** An option that cannot
deliver on time, or cannot supply the quantity, is marked infeasible and pushed
below every viable option — but it stays in the ranking *with its blocking
reasons attached*, because a buyer needs to see why the cheap option was
rejected, not just that it was.

**Meeting the date is what scores, not beating it.** An early version scored
speed linearly in slack, which handed every order to the air-freight expeditor
regardless of urgency. Arriving 7 days early is not meaningfully better than
arriving 3 days early when the deadline is the deadline. The current model is
`0.80 + 0.20 × normalised slack` once the date is met, and 0 when it is not.
`test_arriving_very_early_does_not_beat_arriving_on_time_on_price` guards it.

**The agent can suspend.** When a PO crosses the approval threshold the run
parks in `awaiting_approval` with its full message history intact; the stream
ends there. Approving or rejecting opens a new stream, appends the buyer's
decision to the history, and the loop resumes at exactly the point it stopped.
The gate is enforced server-side in the tool, not by prompting — the model
cannot talk its way past a threshold it has no way to reach.

**Validation is adversarial by construction.** Each mock supplier has a
deviation profile: what they confirm differs from what you ordered, in ways
that are individually small and easy to miss. Apex reprices 4% and slips 5 days
against a 3-day buffer. The validation stage exists to catch precisely this.

**The UI and the model get different payloads.** A multi-step run resends its
whole history on every call, so verbose tool results compound fast — enough to
blow through Groq's free-tier 8,000 tokens/minute mid-purchase. But the UI and
the model want different things from the same call: the browser needs every
score component to make the decision auditable, while the model only needs
enough to pick the next step. So the event stream carries the full object to the
browser and [`compact.py`](backend/app/compact.py) carries a reduced one into the
message history — **55% smaller** across the four investigate-stage tools, 63%
on the ranking. Tests assert that what the model actually needs survives
compaction, including the reasons an option was rejected.

**Rate limits are a pause, not a failure.** A 429 mid-run would otherwise abandon
a half-finished purchase. The loop reads `Retry-After`, falls back to parsing the
provider's hint, then to exponential backoff, and retries up to six times while
telling the buyer what it is waiting for.

**Everything streams.** Agent events reach the UI as NDJSON as they happen —
reasoning, tool calls, tool results, the gate, the verdict — so the reviewer
watches the agent think rather than waiting for a paragraph. Each tool result
gets a purpose-built view; the ranking table is expandable to show the
per-criterion score breakdown behind every decision.

---

## Deploying

The app runs on Vercel as a static React build plus one Python function
(`api/index.py`, which re-exports the same FastAPI app). Config is in
[`vercel.json`](vercel.json).

```bash
npm i -g vercel
vercel login
vercel --prod
```

Then set `GROQ_API_KEY` in **Project → Settings → Environment Variables** and
redeploy. Check `/api/health` — it reports whether the key was picked up.

### Making a stateful agent work on a stateless platform

Serverless was the awkward target for this app, and the two problems are worth
stating because the fixes are the interesting part.

**The approval gate needs state that outlives the request.** The run suspends
between drafting the order and the buyer approving it — possibly minutes — and
the approval may land on an instance that has never seen the run. Rather than
add a database purely to hold a conversation, the suspend point returns a
`resume_token` carrying the message history and the rows the run created; the
client sends it back with the approval and the agent rebuilds and continues. The
in-process cache is still consulted first, so a local server and a warm instance
behave exactly as before. `test_serverless.py` proves the gate survives a wipe of
both memory and the database.

**The filesystem is read-only apart from `/tmp`.** Reference data — inventory,
suppliers, contracts, catalog — is deterministic, so it is simply re-seeded on a
cold start. Only what a run *created* needs to survive, and that rides in the
token. `ensure_seeded()` seeds only when the tables are empty, so a warm
instance re-running startup cannot wipe a pending order.

**On the 60-second function cap:** the approval gate splits the work into two
requests of roughly 30 and 20 seconds, so neither leg approaches the limit. The
human-in-the-loop design happens to be what makes it fit.

The honest trade-off: a real deployment would put run state in Redis or
Postgres rather than trusting the client to return it, and would sign the token
so it cannot be tampered with. For a single-session demo this is the smaller,
clearer mechanism, and it needs no database to provision.

## What is mocked

Suppliers, the ERP and the inventory system are mock modules over seeded SQLite,
which the brief explicitly allows. They are deliberately shaped like remote
systems — separate per-supplier calls, simulated latency, independent failure
and rejection paths — so swapping in real HTTP clients would not change anything
downstream of `suppliers.py`.

Seed data is regenerated on every server start and dates are stored relative to
today, so the demo tells the same story whenever it is run.

## Known limitations

- A suspended run is held in process memory, and mirrored into a client-returned
  `resume_token` so it survives a serverless cold start. The token is not signed,
  so a tampered one could in principle rewrite history — acceptable for a demo,
  not for production, where this belongs in Redis or Postgres.
- One scenario is implemented end to end, by choice — depth over breadth.
- The split-order remediation is *proposed* to the buyer but not executable;
  acting on it would need a multi-PO flow.
- No authentication, and no spend limits per user or per supplier.
- Agent output quality varies slightly run to run, as with any LLM. The
  decision itself does not — that path is deterministic and tested.
