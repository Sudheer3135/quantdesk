import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";

/* Recharts is roughly three quarters of the bundle. Loading it lazily lets
   the verdict, the ledger and the live price paint immediately — those are
   what you actually read first — while the charts arrive a beat later. */
const PriceChart = lazy(() => import("./PriceChart.jsx"));
const OIProfile = lazy(() => import("./OIProfile.jsx"));

function ChartFallback({ label }) {
  return (
    <div className="panel chart-fallback">
      <h3>{label}</h3>
      <p className="muted-body">Loading…</p>
    </div>
  );
}

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";
const WS_URL = API.replace(/^http/, "ws") + "/ws/signals";

const FALLBACK_POLL_MS = 60_000;   // only used if the socket cannot connect
const MAX_RECONNECT_MS = 30_000;

const num = (v, d = 2) =>
  v === null || v === undefined ? "—" : Number(v).toLocaleString("en-IN", {
    minimumFractionDigits: d, maximumFractionDigits: d,
  });

async function getJSON(path) {
  const res = await fetch(`${API}${path}`);
  if (!res.ok) throw new Error(`${path} returned ${res.status}`);
  return res.json();
}

/* Pushed signals over a socket, with polling as a safety net.

   The agent produces one signal every five minutes. Polling on a timer meant
   most requests returned something already on screen, while a genuinely new
   signal could sit unseen for up to a minute. */
function useLiveSignal() {
  const [signal, setSignal] = useState(null);
  const [price, setPrice] = useState(null);
  const [market, setMarket] = useState(null);
  const [link, setLink] = useState("connecting");   // connecting | live | polling
  const [updated, setUpdated] = useState(null);

  const socket = useRef(null);
  const attempts = useRef(0);
  const pollTimer = useRef(null);
  const closed = useRef(false);

  const poll = useCallback(async () => {
    try {
      const [sig, status, tick] = await Promise.all([
        getJSON("/signals/live?symbol=NIFTY&timeframe=5m"),
        getJSON("/market/status").catch(() => null),
        getJSON("/market/price").catch(() => null),
      ]);
      setSignal(sig);
      if (status) setMarket(status);
      if (tick) setPrice(tick);
      setUpdated(new Date());
    } catch {
      /* leave the last good signal on screen rather than blanking it */
    }
  }, []);

  const startPolling = useCallback(() => {
    if (pollTimer.current) return;
    setLink("polling");
    poll();
    pollTimer.current = setInterval(poll, FALLBACK_POLL_MS);
  }, [poll]);

  const stopPolling = useCallback(() => {
    clearInterval(pollTimer.current);
    pollTimer.current = null;
  }, []);

  const connect = useCallback(() => {
    if (closed.current) return;
    let ws;
    try {
      ws = new WebSocket(WS_URL);
    } catch {
      startPolling();
      return;
    }
    socket.current = ws;

    ws.onopen = () => {
      attempts.current = 0;
      stopPolling();
      setLink("live");
    };

    ws.onmessage = (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch {
        return;
      }
      if (msg.market) setMarket(msg.market);
      // Prices arrive every few seconds, signals every few minutes. Handled
      // separately so a price tick does not redraw the whole analysis.
      if (msg.price) {
        setPrice(msg.price);
        setUpdated(new Date());
      }
      if (msg.signal) {
        setSignal(msg.signal);
        setUpdated(new Date());
      }
      if (msg.type === "heartbeat") setUpdated(new Date());
    };

    ws.onclose = () => {
      if (closed.current) return;
      // Back off, but keep the last signal visible and fall back to polling
      // so the dashboard degrades instead of silently freezing.
      const wait = Math.min(1000 * 2 ** attempts.current, MAX_RECONNECT_MS);
      attempts.current += 1;
      if (attempts.current >= 2) startPolling();
      setTimeout(connect, wait);
    };

    ws.onerror = () => ws.close();
  }, [startPolling, stopPolling]);

  useEffect(() => {
    closed.current = false;
    connect();
    return () => {
      closed.current = true;
      stopPolling();
      socket.current?.close();
    };
  }, [connect, stopPolling]);

  return { signal, price, market, link, updated, refresh: poll };
}

/* The live price. Separate from the signal on purpose: the price moves
   every few seconds, the analysis every five minutes. Flashing the whole
   dashboard on every tick would make it unreadable. */
/* Candles and the option chain change on the timeframe, not on every tick,
   so they get their own slower loop rather than riding the price socket. */
function useMarketData(intervalMs = 60_000) {
  const [candles, setCandles] = useState([]);
  const [chain, setChain] = useState(null);

  const load = useCallback(async () => {
    const [c, ch] = await Promise.all([
      getJSON("/market/candles?symbol=NIFTY&interval=5m&days=2").catch(() => null),
      getJSON("/market/option-chain?symbol=NIFTY").catch(() => null),
    ]);
    if (c?.candles) setCandles(c.candles);
    if (ch) setChain(ch);
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, intervalMs);
    return () => clearInterval(id);
  }, [load, intervalMs]);

  return { candles, chain };
}

function Ticker({ price, marketOpen }) {
  const [flash, setFlash] = useState("");
  const last = useRef(null);

  useEffect(() => {
    if (!price || price.price === last.current) return;
    const dir = last.current === null ? ""
      : price.price > last.current ? "up" : "down";
    last.current = price.price;
    if (!dir) return;
    setFlash(dir);
    const id = setTimeout(() => setFlash(""), 600);
    return () => clearTimeout(id);
  }, [price]);

  if (!price) {
    return (
      <div className="ticker">
        <span className="ticker-price muted">—</span>
        <span className="ticker-note">waiting for a price</span>
      </div>
    );
  }

  const change = price.change;
  return (
    <div className="ticker">
      <span className={`ticker-price flash-${flash}`}>{num(price.price)}</span>
      {change !== null && change !== undefined && (
        <span className={`ticker-change ${change > 0 ? "up" : change < 0 ? "down" : ""}`}>
          {change > 0 ? "+" : ""}{num(change)}
        </span>
      )}
      <span className="ticker-note">
        {marketOpen
          ? `${price.symbol} · polled, a few seconds behind`
          : `${price.symbol} · last traded before close`}
      </span>
    </div>
  );
}

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
          const cls = c.disabled ? "zero"
            : c.contribution > 0 ? "pos"
            : c.contribution < 0 ? "neg" : "zero";
          return (
            <div className="ledger-row" key={c.name}>
              <div className={`ledger-name${c.disabled ? " muted" : ""}`}>
                {c.name.replace(/_/g, " ")}
              </div>
              <div className="ledger-bar-track">
                <div
                  className={`ledger-bar ${cls}`}
                  style={cls === "zero" ? undefined : { width: `${pct}%` }}
                />
              </div>
              <p className={`ledger-reason${c.disabled ? " muted" : ""}`}>
                {c.reason}
              </p>
            </div>
          );
        })}
      </div>
      <div className="ledger-scale">
        <span>bearish pull</span><span>neutral</span><span>bullish pull</span>
      </div>
    </section>
  );
}

function PlanPanel({ signal, marketOpen }) {
  if (signal.action === "HOLD") {
    return (
      <div className="panel">
        <h3>Trade plan</h3>
        <p className="muted-body">
          Confidence is under the threshold of{" "}
          {Math.round((signal.context?.threshold_used ?? 0.35) * 100)}%. No plan
          is generated, because a setup you cannot describe is a setup you
          should not take.
        </p>
      </div>
    );
  }
  const risk = signal.risk;
  return (
    <div className="panel">
      <h3>Trade plan</h3>
      {!marketOpen && (
        <p className="warn-line">
          Market is closed — this reads the final candle of the session, not a
          tradeable setup. Overnight gaps will invalidate these levels.
        </p>
      )}
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
        <p className="warn-line">Risk manager blocked this: {risk.reasons.join(" ")}</p>
      )}
    </div>
  );
}

function ChainPanel({ summary, vix }) {
  if (!summary) {
    return (
      <div className="panel">
        <h3>Option chain</h3>
        <p className="muted-body">No chain loaded for this signal.</p>
      </div>
    );
  }
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

function ContextPanel({ context }) {
  if (!context) return null;
  const pools = context.liquidity_pools || [];
  return (
    <div className="panel">
      <h3>Structure</h3>
      <dl>
        <div className="kv"><dt>Trend</dt>
          <dd><span className={`tag ${context.trend}`}>{context.trend}</span></dd></div>
        <div className="kv"><dt>VWAP</dt><dd>{num(context.vwap)}</dd></div>
        <div className="kv"><dt>ATR 14</dt><dd>{num(context.atr14)}</dd></div>
        <div className="kv"><dt>Unfilled gaps</dt>
          <dd>{(context.unfilled_fvgs || []).length}</dd></div>
        <div className="kv"><dt>Checks disabled</dt>
          <dd>{(context.disabled_checks || []).join(", ") || "none"}</dd></div>
      </dl>
      {pools.length > 0 && (
        <>
          <p className="eyebrow" style={{ marginTop: 16 }}>Liquidity</p>
          <ul className="levels">
            {pools.slice(0, 5).map((p, i) => (
              <li key={i} className={p.swept ? "swept" : ""}>
                <span>{p.side}</span><span>{num(p.level)}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

export default function App() {
  const { signal, price, market, link, updated, refresh } = useLiveSignal();
  const { candles, chain } = useMarketData();
  const [clock, setClock] = useState(new Date());

  useEffect(() => {
    const id = setInterval(() => setClock(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  const marketOpen = market?.open ?? false;
  const ago = updated ? Math.round((clock - updated) / 1000) : null;

  return (
    <div className="shell">
      <header className="masthead">
        <h1 className="wordmark">Quant<span>Desk</span></h1>
        <div className="masthead-meta">
          <span className={`pill ${marketOpen ? "on" : "off"}`}>
            {market ? (marketOpen ? "market open" : market.session) : "…"}
          </span>
          <span className={`pill link-${link}`}>
            <i className="dot" />{link}
          </span>
          <span>{ago === null ? "waiting" : `${ago}s ago`}</span>
          <button onClick={refresh}>refresh</button>
        </div>
      </header>

      {!signal && (
        <p className="notice">
          Waiting for the first signal. The agent publishes one every five
          minutes — if this does not clear, check that the backend is running.
        </p>
      )}

      {signal && (
        <>
          <section className="tape">
            <div className="tape-price">
              <Ticker price={price} marketOpen={marketOpen} />
            </div>
            <div className={`tape-verdict ${signal.action}`}>
              <div className={`action ${signal.action}`}>{signal.action}</div>
              <div className="confidence-label">
                {Math.round(signal.confidence * 100)}% confidence
              </div>
            </div>
            <div className="tape-stats">
              <div><span className="stat-label">VWAP</span>
                <span className="stat-value">{num(signal.context?.vwap)}</span></div>
              <div><span className="stat-label">ATR 14</span>
                <span className="stat-value">{num(signal.context?.atr14)}</span></div>
              <div><span className="stat-label">Trend</span>
                <span className="stat-value">{signal.context?.trend ?? "—"}</span></div>
              <div><span className="stat-label">Signal at</span>
                <span className="stat-value">
                  {signal.timestamp
                    ? new Date(signal.timestamp).toLocaleTimeString("en-IN", {
                        hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata",
                      })
                    : "—"}
                </span></div>
            </div>
          </section>

          <div className="desk">
            <Suspense fallback={<ChartFallback label="Price · VWAP" />}>
              <PriceChart candles={candles} signal={signal} />
            </Suspense>
            <PlanPanel signal={signal} marketOpen={marketOpen} />
          </div>

          <Ledger checks={signal.checks || []} />

          <div className="desk">
            <Suspense fallback={<ChartFallback label="Open interest" />}>
              <OIProfile
                strikes={chain?.strikes}
                summary={chain?.summary || signal.context?.option_chain}
                spot={price?.price || signal.price}
              />
            </Suspense>
            <ContextPanel context={signal.context} />
          </div>
        </>
      )}

      <p className="notice">
        This is an analysis tool. It reads the market and shows its working — it
        does not know the future and it is not advice. Every number here comes
        from a rule you can read in <code>backend/app/analytics</code>.
      </p>
    </div>
  );
}