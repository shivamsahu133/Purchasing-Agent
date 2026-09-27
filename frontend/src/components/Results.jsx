import { Fragment, useState } from "react";

const fmt = (n, dp = 2) =>
  Number(n).toLocaleString(undefined, {
    minimumFractionDigits: dp,
    maximumFractionDigits: dp,
  });

function Head({ children }) {
  return <div className="res-head">{children}</div>;
}

export function InventoryResult({ result: r }) {
  return (
    <>
      <Head>
        Stock check — {r.sku} <span className="muted">{r.name}</span>
      </Head>
      <div className="stat-row">
        <div className="stat">
          <span>On hand</span>
          <b className={r.below_reorder_point ? "low" : ""}>
            {r.on_hand.toLocaleString()}
          </b>
        </div>
        <div className="stat">
          <span>Reorder point</span>
          <b>{r.reorder_point.toLocaleString()}</b>
        </div>
        <div className="stat">
          <span>Shortfall</span>
          <b className="low">{r.shortfall_vs_reorder_point.toLocaleString()}</b>
        </div>
        <div className="stat">
          <span>Open POs</span>
          <b>{r.open_purchase_orders.length}</b>
        </div>
      </div>
    </>
  );
}

export function QuotesResult({ result: r }) {
  return (
    <>
      <Head>
        Quotes received — {r.quote_count} suppliers responded for{" "}
        {r.requested_qty.toLocaleString()} units
      </Head>
      <table className="grid">
        <thead>
          <tr>
            <th>Supplier</th>
            <th className="num">Unit</th>
            <th className="num">Available</th>
            <th className="num">Lead</th>
            <th>ETA</th>
          </tr>
        </thead>
        <tbody>
          {r.quotes.map((q) => (
            <tr key={q.supplier_id}>
              <td>
                {q.supplier_name}
                {q.under_contract && <span className="tag">contract</span>}
              </td>
              <td className="num">${fmt(q.unit_price)}</td>
              <td className="num">
                <span className={q.available_qty < q.requested_qty ? "low" : ""}>
                  {q.available_qty.toLocaleString()}
                </span>
              </td>
              <td className="num">{q.lead_time_days}d</td>
              <td>{q.estimated_delivery}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

export function RankingResult({ result: r }) {
  const [openRow, setOpenRow] = useState(null);
  return (
    <>
      <Head>
        Ranked options — {r.feasible_count} of {r.ranked_options.length} viable
      </Head>
      <table className="grid ranking">
        <thead>
          <tr>
            <th>#</th>
            <th>Supplier</th>
            <th className="num">Total</th>
            <th>ETA</th>
            <th className="num">Slack</th>
            <th className="num">Score</th>
          </tr>
        </thead>
        <tbody>
          {r.ranked_options.map((o) => (
            <Fragment key={o.supplier_id}>
              <tr
                className={
                  (o.feasible ? "" : "infeasible ") +
                  (o.rank === 1 && o.feasible ? "winner " : "") +
                  "clickable"
                }
                onClick={() =>
                  setOpenRow(openRow === o.supplier_id ? null : o.supplier_id)
                }
              >
                <td>{o.rank}</td>
                <td>
                  {o.supplier_name}
                  {o.under_contract && <span className="tag">contract</span>}
                  {!o.feasible && <span className="tag bad">blocked</span>}
                </td>
                <td className="num">${fmt(o.total_cost)}</td>
                <td>{o.estimated_delivery}</td>
                <td className="num">
                  <span className={o.days_of_slack < 0 ? "low" : ""}>
                    {o.days_of_slack}d
                  </span>
                </td>
                <td className="num">{fmt(o.score, 3)}</td>
              </tr>
              {openRow === o.supplier_id && (
                <tr className="detail-row">
                  <td colSpan={6}>
                    {o.blocking_reasons.length > 0 && (
                      <div className="blockers">
                        {o.blocking_reasons.map((b, i) => (
                          <div key={i}>• {b}</div>
                        ))}
                      </div>
                    )}
                    <div className="bars">
                      {Object.entries(o.score_breakdown).map(([k, v]) => (
                        <div key={k} className="bar">
                          <span className="bar-label">
                            {k} <em>×{r.weights_used[k]}</em>
                          </span>
                          <div className="bar-track">
                            <div
                              className="bar-fill"
                              style={{ width: Math.round(v * 100) + "%" }}
                            />
                          </div>
                          <span className="bar-val">{fmt(v, 2)}</span>
                        </div>
                      ))}
                    </div>
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
      <p className="rationale">{r.rationale}</p>
    </>
  );
}

export function OrderResult({ result: r }) {
  return (
    <>
      <Head>
        {r.approval_required ? "Order drafted" : "Order placed"} — {r.po_id}
      </Head>
      <div className="stat-row">
        <div className="stat">
          <span>Supplier</span>
          <b>{r.supplier_name}</b>
        </div>
        <div className="stat">
          <span>Quantity</span>
          <b>{r.qty.toLocaleString()}</b>
        </div>
        <div className="stat">
          <span>Unit</span>
          <b>${fmt(r.unit_price, 4)}</b>
        </div>
        <div className="stat">
          <span>Total</span>
          <b>${fmt(r.total)}</b>
        </div>
        <div className="stat">
          <span>Status</span>
          <b>{r.status}</b>
        </div>
      </div>
    </>
  );
}

export function ValidationResult({ result: r }) {
  const ok = r.validation_passed;
  return (
    <>
      <Head>
        <span className={ok ? "pill good" : "pill bad"}>
          {ok ? "Validation passed" : "Validation failed"}
        </span>
        {!ok && (
          <span className="muted">
            {r.discrepancy_count} discrepancy(s) against the order
          </span>
        )}
      </Head>
      <table className="grid compare">
        <thead>
          <tr>
            <th>Field</th>
            <th className="num">Ordered</th>
            <th className="num">Confirmed</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>Unit price</td>
            <td className="num">${fmt(r.ordered.unit_price, 4)}</td>
            <td className="num">${fmt(r.confirmed.unit_price, 4)}</td>
          </tr>
          <tr>
            <td>Quantity</td>
            <td className="num">{r.ordered.qty.toLocaleString()}</td>
            <td className="num">{r.confirmed.qty.toLocaleString()}</td>
          </tr>
          <tr>
            <td>Delivery</td>
            <td className="num">{r.ordered.need_by}</td>
            <td className="num">{r.confirmed.promised_date}</td>
          </tr>
          <tr>
            <td>Total</td>
            <td className="num">${fmt(r.ordered.total)}</td>
            <td className="num">${fmt(r.confirmed.total)}</td>
          </tr>
        </tbody>
      </table>
      {r.discrepancies.map((d, i) => (
        <div key={i} className={"discrepancy sev-" + d.severity}>
          <b>{d.field.replace(/_/g, " ")}</b> {d.detail}
        </div>
      ))}
    </>
  );
}

export function EscalationResult({ result: r }) {
  return (
    <>
      <Head>
        <span className="pill warn">Escalated to buyer</span>
      </Head>
      <p className="escalation-summary">{r.summary}</p>
      <ol className="options">
        {r.options.map((o, i) => (
          <li key={i}>{o}</li>
        ))}
      </ol>
    </>
  );
}

export function RawResult({ result }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button className="thought-toggle" onClick={() => setOpen(!open)}>
        <span className="chev">{open ? "▾" : "▸"}</span> result
      </button>
      {open && <pre className="raw">{JSON.stringify(result, null, 2)}</pre>}
    </>
  );
}
