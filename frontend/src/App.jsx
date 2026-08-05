import { useCallback, useEffect, useState } from "react";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";
const REFRESH_MS = 60_000;

const num = (v, d = 2) =>
  v === null || v === undefined ? "—" : Number(v).toLocaleString("en-IN", {
    minimumFractionDigits: d, maximumFractionDigits: d,
  });

async function getJSON(path) {
  const res = await fetch(`${API}${path}`);
  if (!res.ok) throw new Error(`${path} returned ${res.status}`);
  return res.json();
}

/* The ledger is the whole point of this screen: every check the engine ran,
   how far it pushed the decision, and the sentence explaining why. */
function Ledger({ checks }) {
  const widest = Math.max(...checks.map((c) => Math.abs(c.contribution)), 0.01);
  const ordered = [...checks].sort(
    (a, b) => Math.abs(b.contribution) - Math.abs(a.contribution)
  );

  return (
    <section className="ledger">
      <p className="eyebrow">Why this call</p>
      <div className="ledger-rail">
        {ordered.map((c) => {
          const pct = (Math.abs(c.contribution) / widest) * 48;
          const cls = c.contribution > 0 ? "pos" : c.contribution < 0 ? "neg" : "zero";
          return (
            <div key={c.name}>
              <div className="ledger-row">
                <div className="ledger-name">{c.name.replace(/_/g, " ")}</div>
                <div className="ledger-bar-track">
                  <div
                    className={`ledger-bar ${cls}`}
                    style={cls === "zero" ? undefined : { width: `${pct}%` }}
                  />
                </div>
                <p className="ledger-reason">{c.reason}</p>
              </div>
            </div>
          );
        })}
      </div>
      <div className="ledger-scale">
        <span>bearish pull</span>
        <span>neutral</span>
        <span>bullish pull</span>
      </div>
    </section>
  );
}

function StructurePanel({ data }) {
  if (!data) return null;
  const pools = data.liquidity_pools || [];
  return (
    <div className="panel">
      <h3>Structure</h3>
      <dl>
        <div className="kv">
          <dt>Trend</dt>
          <dd><span className={`tag ${data.structure.trend}`}>{data.structure.trend}</span></dd>
        </div>
        <div className="kv">
          <dt>Last swing high</dt>
          <dd>{num(data.structure.last_swing_high?.price)}</dd>
        </div>
        <div className="kv">
          <dt>Last swing low</dt>
          <dd>{num(data.structure.last_swing_low?.price)}</dd>
        </div>
        <div className="kv">
          <dt>Unfilled gaps</dt>
          <dd>{data.fair_value_gaps?.length ?? 0}</dd>
        </div>
      </dl>
      {pools.length > 0 && (
        <>
          <p className="eyebrow" style={{ marginTop: 16 }}>Liquidity</p>
          <ul className="levels">
            {pools.slice(0, 5).map((p, i) => (
              <li key={i} className={p.swept ? "swept" : ""}>
                <span>{p.side}</span>
                <span>{num(p.level)}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function ChainPanel({ summary, vix }) {
  if (!summary) return (
    <div className="panel">
      <h3>Option chain</h3>
      <p style={{ color: "var(--muted)", fontSize: 13 }}>
        No chain loaded. The mock broker supplies one; a live chain needs a Kite session.
      </p>
    </div>
  );
  return (
    <div className="panel">
      <h3>Option chain</h3>
      <dl>
        <div className="kv"><dt>Reading</dt>
          <dd><span className={`tag ${summary.bias}`}>{summary.bias}</span></dd></div>
        <div className="kv"><dt>PCR (OI)</dt><dd>{num(summary.pcr_oi, 2)}</dd></div>
        <div className="kv"><dt>Max pain</dt><dd>{num(summary.max_pain, 0)}</dd></div>
        <div className="kv"><dt>Spot vs max pain</dt>
          <dd>{num(summary.max_pain_distance_pct, 2)}%</dd></div>
        <div className="kv"><dt>Resistance</dt>
          <dd>{(summary.resistance_strikes || []).map((s) => num(s, 0)).join(" · ") || "—"}</dd></div>
        <div className="kv"><dt>Support</dt>
          <dd>{(summary.support_strikes || []).map((s) => num(s, 0)).join(" · ") || "—"}</dd></div>
        <div className="kv"><dt>India VIX</dt><dd>{num(vix, 2)}</dd></div>
      </dl>
    </div>
  );
}

function PlanPanel({ signal }) {
  if (signal.action === "HOLD") {
    return (
      <div className="panel">
        <h3>Trade plan</h3>
        <p style={{ color: "var(--muted)", fontSize: 13, lineHeight: 1.6, margin: 0 }}>
          Confidence is under the threshold of{" "}
          {Math.round((signal.context?.threshold_used ?? 0.35) * 100)}%. No plan is
          generated, because a setup you cannot describe is a setup you should not take.
        </p>
      </div>
    );
  }
  const risk = signal.risk;
  return (
    <div className="panel">
      <h3>Trade plan</h3>
      <dl>
        <div className="kv"><dt>Entry</dt><dd>{num(signal.entry)}</dd></div>
        <div className="kv"><dt>Stop</dt><dd>{num(signal.stop_loss)}</dd></div>
        <div className="kv"><dt>Target</dt><dd>{num(signal.target)}</dd></div>
        <div className="kv"><dt>Reward:risk</dt><dd>1:{num(signal.risk_reward, 2)}</dd></div>
        {risk && (
          <>
            <div className="kv"><dt>Size</dt>
              <dd>{risk.approved ? `${risk.quantity} (${risk.lots} lots)` : "blocked"}</dd></div>
            <div className="kv"><dt>Rupees at risk</dt><dd>{num(risk.risk_amount, 0)}</dd></div>
          </>
        )}
      </dl>
      {risk && !risk.approved && (
        <p style={{ color: "var(--bear)", fontSize: 12, marginTop: 12, lineHeight: 1.6 }}>
          Risk manager blocked this: {risk.reasons.join(" ")}
        </p>
      )}
    </div>
  );
}

export default function App() {
  const [signal, setSignal] = useState(null);
  const [structure, setStructure] = useState(null);
  const [vix, setVix] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const [updated, setUpdated] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [sig, str, v] = await Promise.all([
        getJSON("/signals/live?symbol=NIFTY&timeframe=5m"),
        getJSON("/market/structure?symbol=NIFTY&interval=5m"),
        getJSON("/market/vix").catch(() => ({ india_vix: null })),
      ]);
      setSignal(sig);
      setStructure(str);
      setVix(v.india_vix);
      setUpdated(new Date());
      setError(null);
    } catch (err) {
      setError(`Could not reach the backend at ${API}. Start it with docker compose up.`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, REFRESH_MS);
    return () => clearInterval(id);
  }, [load]);

  return (
    <div className="shell">
      <header className="masthead">
        <h1 className="wordmark">Quant<span>Desk</span></h1>
        <div className="masthead-meta">
          <span>NIFTY · 5m</span>
          <span>{updated ? `updated ${updated.toLocaleTimeString("en-IN")}` : "loading"}</span>
          <button onClick={load} disabled={loading}>
            {loading ? "reading" : "refresh"}
          </button>
        </div>
      </header>

      {error && <p className="notice error">{error}</p>}

      {signal && (
        <>
          <section className="verdict">
            <div>
              <div className={`action ${signal.action}`}>{signal.action}</div>
              <div className="confidence-label">
                {Math.round(signal.confidence * 100)}% confidence
              </div>
            </div>
            <div className="price-row">
              <div>
                <div className="stat-label">Last</div>
                <div className="stat-value">{num(signal.price)}</div>
              </div>
              <div>
                <div className="stat-label">VWAP</div>
                <div className="stat-value">{num(signal.context?.vwap)}</div>
              </div>
              <div>
                <div className="stat-label">ATR 14</div>
                <div className="stat-value">{num(signal.context?.atr14)}</div>
              </div>
              <div>
                <div className="stat-label">Trend</div>
                <div className="stat-value">{signal.context?.trend ?? "—"}</div>
              </div>
            </div>
          </section>

          <Ledger checks={signal.checks} />

          <div className="grid">
            <PlanPanel signal={signal} />
            <StructurePanel data={structure} />
            <ChainPanel summary={signal.context?.option_chain} vix={vix} />
          </div>
        </>
      )}

      <p className="notice">
        This is an analysis tool. It reads the market and shows its working — it does
        not know the future and it is not advice. Every number on this screen comes
        from a rule you can read in <code>backend/app/analytics</code>. Change the rule,
        re-run the backtest, and only then change how you trade.
      </p>
    </div>
  );
}
