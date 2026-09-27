import { useEffect, useRef, useState } from "react";
import {
  InventoryResult,
  QuotesResult,
  RankingResult,
  ValidationResult,
  EscalationResult,
  OrderResult,
  RawResult,
} from "./components/Results.jsx";

/** Read an NDJSON stream and hand each parsed object to `onEvent`. */
async function streamNdjson(url, body, onEvent) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) throw new Error("Request failed: " + res.status);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (line.trim()) onEvent(JSON.parse(line));
    }
  }
  if (buffer.trim()) onEvent(JSON.parse(buffer));
}

/* --- event rendering --------------------------------------------------- */

function Thought({ text }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="ev thought">
      <button className="thought-toggle" onClick={() => setOpen(!open)}>
        <span className="chev">{open ? "▾" : "▸"}</span> reasoning
      </button>
      {open && <p className="thought-body">{text}</p>}
    </div>
  );
}

function ToolCall({ name, args }) {
  const pairs = Object.entries(args || {});
  return (
    <div className="ev toolcall">
      <span className="tool-name">{name}</span>
      {pairs.length > 0 && (
        <span className="tool-args">
          {pairs.map(([k, v]) => (
            <span key={k} className="arg">
              {k}=<b>{typeof v === "object" ? JSON.stringify(v) : String(v)}</b>
            </span>
          ))}
        </span>
      )}
    </div>
  );
}

const RESULT_VIEWS = {
  check_inventory: InventoryResult,
  get_supplier_quotes: QuotesResult,
  rank_suppliers: RankingResult,
  validate_order: ValidationResult,
  escalate_to_buyer: EscalationResult,
  create_purchase_order: OrderResult,
};

function ToolResult({ name, result }) {
  if (result && result.error) {
    return (
      <div className="ev result error-box">
        <b>{name} failed.</b> {result.error}
        {result.reason && <div className="muted">{result.reason}</div>}
      </div>
    );
  }
  const View = RESULT_VIEWS[name] || RawResult;
  return (
    <div className="ev result">
      <View result={result} />
    </div>
  );
}

/** Minimal markdown: bold, bullets and paragraphs. Enough for agent prose. */
function Prose({ text }) {
  const blocks = text.split(/\n{2,}/);
  return (
    <div className="ev message">
      {blocks.map((block, i) => {
        const lines = block.split("\n");
        const isList = lines.every((l) => /^\s*[-*\d]+[.)]?\s+/.test(l));
        if (isList) {
          return (
            <ul key={i}>
              {lines.map((l, j) => (
                <li key={j}>{inline(l.replace(/^\s*[-*\d]+[.)]?\s+/, ""))}</li>
              ))}
            </ul>
          );
        }
        return <p key={i}>{inline(block)}</p>;
      })}
    </div>
  );
}

function inline(text) {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") ? (
      <b key={i}>{part.slice(2, -2)}</b>
    ) : (
      <span key={i}>{part}</span>
    )
  );
}

function ApprovalGate({ po, onDecide, decided }) {
  const [note, setNote] = useState("");
  return (
    <div className={"gate" + (decided ? " gate-done" : "")}>
      <div className="gate-head">
        <span className="gate-badge">Approval required</span>
        <span className="gate-total">
          ${Number(po.total).toLocaleString(undefined, {
            minimumFractionDigits: 2,
          })}
        </span>
      </div>
      <div className="gate-body">
        <div className="gate-grid">
          <div><span>Order</span><b>{po.po_id}</b></div>
          <div><span>Supplier</span><b>{po.supplier_name}</b></div>
          <div><span>Quantity</span><b>{po.qty.toLocaleString()} units</b></div>
          <div><span>Unit price</span><b>${po.unit_price}</b></div>
          <div><span>Need by</span><b>{po.need_by}</b></div>
          <div><span>Promised</span><b>{po.promised_date}</b></div>
        </div>
        {decided ? (
          <p className="muted">Decision recorded: {decided}.</p>
        ) : (
          <>
            <input
              className="note"
              placeholder="Optional note to the agent…"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
            <div className="gate-actions">
              <button className="btn approve" onClick={() => onDecide(true, note)}>
                Approve and place
              </button>
              <button className="btn reject" onClick={() => onDecide(false, note)}>
                Reject
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/* --- app --------------------------------------------------------------- */

const STAGES = ["Investigate", "Decide", "Act", "Validate"];

function stageFromEvents(events) {
  const names = events.filter((e) => e.type === "tool_call").map((e) => e.name);
  if (names.includes("validate_order")) return 3;
  if (names.includes("create_purchase_order")) return 2;
  if (names.includes("rank_suppliers")) return 1;
  return 0;
}

export default function App() {
  const [scenario, setScenario] = useState(null);
  const [request, setRequest] = useState("");
  const [events, setEvents] = useState([]);
  const [running, setRunning] = useState(false);
  const [runId, setRunId] = useState(null);
  const [decided, setDecided] = useState(null);
  // Returned with the approval so a suspended run survives a cold start
  // on a stateless host, where the next request hits a fresh instance.
  const [resumeToken, setResumeToken] = useState(null);
  const bottom = useRef(null);

  useEffect(() => {
    fetch("/api/scenario")
      .then((r) => r.json())
      .then((s) => {
        setScenario(s);
        setRequest(s.default_request);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [events]);

  const push = (e) => setEvents((prev) => [...prev, e]);

  async function run() {
    setEvents([]);
    setDecided(null);
    setResumeToken(null);
    setRunning(true);
    try {
      await streamNdjson("/api/run", { request }, (e) => {
        if (e.type === "status" && e.run_id) setRunId(e.run_id);
        if (e.type === "awaiting_approval" && e.resume_token)
          setResumeToken(e.resume_token);
        push(e);
      });
    } catch (err) {
      push({ type: "error", message: String(err) });
    }
    setRunning(false);
  }

  async function decide(approved, note) {
    setDecided(approved ? "approved" : "rejected");
    setRunning(true);
    try {
      await streamNdjson(
        "/api/approve",
        { run_id: runId, approved, note, resume_token: resumeToken },
        push
      );
    } catch (err) {
      push({ type: "error", message: String(err) });
    }
    setRunning(false);
  }

  async function reset() {
    await fetch("/api/reset", { method: "POST" });
    setEvents([]);
    setRunId(null);
    setDecided(null);
    setResumeToken(null);
  }

  const stage = stageFromEvents(events);
  const started = events.length > 0;

  return (
    <div className="app">
      <header>
        <div>
          <h1>Purchasing Agent</h1>
          <p className="sub">
            Replenishment sourcing under supplier disruption — investigate,
            decide, act, validate.
          </p>
        </div>
        <div className="header-right">
          {scenario && <span className="model">{scenario.model}</span>}
          <button className="btn ghost" onClick={reset} disabled={running}>
            Reset data
          </button>
        </div>
      </header>

      <main>
        <section className="left">
          <div className="request-box">
            <label>Buyer request</label>
            <textarea
              rows={3}
              value={request}
              onChange={(e) => setRequest(e.target.value)}
              disabled={running}
            />
            <button className="btn primary" onClick={run} disabled={running || !request}>
              {running ? "Agent working…" : "Run agent"}
            </button>
          </div>

          {started && (
            <div className="stages">
              {STAGES.map((s, i) => (
                <div
                  key={s}
                  className={
                    "stage" + (i < stage ? " past" : i === stage ? " current" : "")
                  }
                >
                  {s}
                </div>
              ))}
            </div>
          )}

          <div className="trace">
            {events.map((e, i) => {
              if (e.type === "thought") return <Thought key={i} text={e.text} />;
              if (e.type === "message") return <Prose key={i} text={e.text} />;
              if (e.type === "tool_call")
                return <ToolCall key={i} name={e.name} args={e.args} />;
              if (e.type === "tool_result")
                return <ToolResult key={i} name={e.name} result={e.result} />;
              if (e.type === "awaiting_approval")
                return (
                  <ApprovalGate
                    key={i}
                    po={e.po}
                    decided={decided}
                    onDecide={decide}
                  />
                );
              if (e.type === "notice")
                return (
                  <div key={i} className="ev notice-box">
                    {e.text}
                  </div>
                );
              if (e.type === "error")
                return (
                  <div key={i} className="ev result error-box">
                    {e.message}
                  </div>
                );
              return null;
            })}
            {running && <div className="working">agent is working…</div>}
            <div ref={bottom} />
          </div>
        </section>

        <aside className="right">
          {scenario && (
            <>
              <div className="panel">
                <h3>Approval threshold</h3>
                <p className="big">
                  ${Number(scenario.approval_threshold).toLocaleString()}
                </p>
                <p className="muted">
                  Orders at or above this are drafted, not placed, and wait for
                  a human.
                </p>
              </div>

              <div className="panel">
                <h3>Inventory</h3>
                <table className="mini">
                  <tbody>
                    {scenario.inventory.map((it) => (
                      <tr key={it.sku}>
                        <td>
                          <b>{it.sku}</b>
                          <div className="muted small">{it.name}</div>
                        </td>
                        <td className="num">
                          <span className={it.on_hand < it.reorder_point ? "low" : ""}>
                            {it.on_hand.toLocaleString()}
                          </span>
                          <div className="muted small">
                            rop {it.reorder_point.toLocaleString()}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="panel">
                <h3>Approved suppliers</h3>
                {scenario.suppliers.map((s) => (
                  <div key={s.id} className="supplier">
                    <div className="supplier-head">
                      <b>{s.name}</b>
                      <span className="rel">{(s.reliability * 100).toFixed(0)}%</span>
                    </div>
                    <div className="muted small">{s.notes}</div>
                  </div>
                ))}
              </div>
            </>
          )}
        </aside>
      </main>
    </div>
  );
}
