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

/* Mirrors LIVE_SECONDS / DELAYED_SECONDS in backend/app/workers/ticker.py.
   Both ends classify the same way so the dashboard and the API never
   disagree about what "stale" means. */
const LIVE_SECONDS = 15;
const DELAYED_SECONDS = 60;

function classifyAge(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "unknown";
  if (seconds <= LIVE_SECONDS) return "live";
  if (seconds <= DELAYED_SECONDS) return "delayed";
  return "stale";
}

function formatAge(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "—";
  if (seconds < 0) return "just now";          /* clock skew overshoot */
  if (seconds < 1) return "just now";
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  const m = Math.floor(seconds / 60);
  const rest = Math.floor(seconds % 60);
  return `${m}m ${String(rest).padStart(2, "0")}s ago`;
}

const AGE_LABEL = {
  live: "Data live", delayed: "Data delayed",
  stale: "Data stale", unknown: "Data age unknown",
};

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
  const [skewMs, setSkewMs] = useState(0);

  const socket = useRef(null);
  const attempts = useRef(0);
  const pollTimer = useRef(null);
  const closed = useRef(false);

  /* How far this browser's clock sits from the server's.

     Data age is the gap between two clocks — the exchange's, which stamps
     the price, and this one, which renders it. A laptop several minutes off
     would otherwise report a perfectly live feed as badly stale, or worse,
     a frozen one as current. Every frame the server sends carries its own
     send time, so the offset is measurable rather than assumed, and every
     age below is corrected by it. */
  const noteServerClock = useCallback((serverIso) => {
    if (!serverIso) return;
    const server = new Date(serverIso).getTime();
    if (Number.isNaN(server)) return;
    setSkewMs((previous) => {
      const observed = Date.now() - server;
      /* Ignore sub-second wobble; it is network jitter, not clock drift. */
      return Math.abs(observed - previous) > 1000 ? observed : previous;
    });
  }, []);

  const poll = useCallback(async () => {
    try {
      const [sig, status, tick] = await Promise.all([
        getJSON("/signals/live?symbol=NIFTY&timeframe=5m"),
        getJSON("/market/status").catch(() => null),
        getJSON("/market/price").catch(() => null),
      ]);
      setSignal(sig);
      if (status) { setMarket(status); noteServerClock(status.server_time); }
      if (tick && tick.price !== null && tick.price !== undefined) setPrice(tick);
    } catch {
      /* leave the last good signal on screen rather than blanking it */
    }
  }, [noteServerClock]);

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
      if (msg.market) {
        setMarket(msg.market);
        noteServerClock(msg.market.server_time);
      }
      // Prices arrive every few seconds, signals every few minutes. Handled
      // separately so a price tick does not redraw the whole analysis.
      //
      // Note what is deliberately absent: a heartbeat does not touch the
      // price. It used to refresh a shared "last updated" clock, so a dead
      // ticker still read as "3s ago" — the socket was alive, so the screen
      // claimed the data was too. Age now comes from the price's own
      // source timestamp, and nothing but a new price can make it younger.
      if (msg.price && msg.price.price !== null && msg.price.price !== undefined) {
        setPrice(msg.price);
        noteServerClock(msg.price.at);
      }
      if (msg.signal) setSignal(msg.signal);
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
  }, [startPolling, stopPolling, noteServerClock]);

  useEffect(() => {
    closed.current = false;
    connect();
    return () => {
      closed.current = true;
      stopPolling();
      socket.current?.close();
    };
  }, [connect, stopPolling]);

  return { signal, price, market, link, skewMs, refresh: poll };
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
          ? `${price.symbol} · polled from ${price.source ?? "source"}`
          : `${price.symbol} · last traded before close`}
      </span>
    </div>
  );
}

/* How far behind the market the number above actually is.

   Separate from the signal's timestamp on purpose. The price and the
   analysis are two different clocks, and the dashboard used to imply they
   were one: a five-minute-old signal sat beside a live price under a single
   "updated" label, so whichever was staler was the one you could not see. */
function DataAge({ seconds, price }) {
  const state = classifyAge(seconds);
  if (!price) {
    return (
      <p className="data-age age-unknown">
        <i className="dot" />Waiting for the first price
      </p>
    );
  }
  return (
    <p className={`data-age age-${state}`}>
      <i className="dot" />
      <span className="data-age-label">{AGE_LABEL[state]}</span>
      <span className="data-age-value">{formatAge(seconds)}</span>
      {state === "unknown" && (
        <span className="data-age-note">source gave no timestamp</span>
      )}
    </p>
  );
}

function Ledger({ checks, context, confidence, action }) {
  const live = checks.filter((c) => !c.disabled);

  // Bars are scaled against the largest *weight*, not the largest observed
  // contribution. Scaling to the observed maximum made a bar's length mean
  // something different on every refresh — a 0.05 nudge filled the row on a
  // quiet reading and looked identical to a maxed-out 0.22. Against a fixed
  // ceiling, length means the same thing today as it did yesterday.
  const ceiling = Math.max(...checks.map((c) => c.weight ?? 0), 0.01);

  const ordered = [...checks].sort((a, b) => {
    if (a.disabled !== b.disabled) return a.disabled ? 1 : -1;
    return Math.abs(b.contribution) - Math.abs(a.contribution);
  });

  // The same arithmetic the engine does, shown rather than asserted. A
  // disabled check contributes nothing and its weight is shared out, which
  // is why the sum is rescaled instead of simply averaged.
  const sum = live.reduce((total, c) => total + c.contribution, 0);
  const scale = context?.weight_scale ?? 1;
  const liveWeight = scale ? 1 / scale : 1;
  const net = sum * scale;
  const threshold = context?.threshold_used;
  const disabled = checks.filter((c) => c.disabled);

  const signed = (v) => `${v > 0 ? "+" : v < 0 ? "\u2212" : " "}${Math.abs(v).toFixed(3)}`;

  return (
    <section className="ledger">
      <div className="ledger-head">
        <p className="eyebrow">Why this call</p>
        <p className="ledger-head-net">
          <span className="dim">net</span> {signed(net)}
          <span className="dim"> → </span>
          {Math.round(Math.min(Math.abs(net), 1) * 100)}% confidence
        </p>
      </div>

      <div className="ledger-legend">
        <span><i className="swatch cap" /> weight available</span>
        <span><i className="swatch neg" /> bearish pull</span>
        <span><i className="swatch pos" /> bullish pull</span>
      </div>

      <div className="ledger-rail">
        {ordered.map((c) => {
          const weight = c.weight ?? 0;
          const capHalf = (weight / ceiling) * 50;
          const fill = (Math.abs(c.contribution) / ceiling) * 50;
          const dir = c.contribution > 0 ? "pos" : c.contribution < 0 ? "neg" : "flat";
          return (
            <div className={`ledger-row${c.disabled ? " is-off" : ""}`} key={c.name}>
              <div className="ledger-name">{c.name.replace(/_/g, " ")}</div>

              <div className="ledger-nums">
                <span className={`ledger-contrib ${dir}`}>
                  {c.disabled ? "\u2014\u2014\u2014\u2014" : signed(c.contribution)}
                </span>
                <span className="ledger-weight">of {weight.toFixed(2)}</span>
              </div>

              <div className="ledger-plot">
                <span className="ledger-zero" />
                <span
                  className="ledger-cap"
                  style={{ left: `${50 - capHalf}%`, width: `${capHalf * 2}%` }}
                />
                {!c.disabled && dir !== "flat" && (
                  <span
                    className={`ledger-bar ${dir}`}
                    style={dir === "pos"
                      ? { left: "50%", width: `${fill}%` }
                      : { right: "50%", width: `${fill}%` }}
                  />
                )}
              </div>

              <p className="ledger-reason">{c.reason}</p>
            </div>
          );
        })}
      </div>

      <div className="ledger-axis">
        <span />
        <span />
        <span className="ledger-axis-scale">
          <span>−{ceiling.toFixed(2)}</span>
          <span>0</span>
          <span>+{ceiling.toFixed(2)}</span>
        </span>
      </div>

      <dl className="ledger-maths">
        <div><dt>sum of contributions</dt><dd>{signed(sum)}</dd></div>
        <div>
          <dt>
            ÷ live weight {liveWeight.toFixed(2)}
            {disabled.length > 0 && (
              <span className="dim">
                {" "}· {disabled.map((c) => c.name.replace(/_/g, " ")).join(", ")}
                {disabled.length === 1 ? " is" : " are"} unavailable, weight shared out
              </span>
            )}
          </dt>
          <dd>× {scale.toFixed(3)}</dd>
        </div>
        <div className="ledger-maths-total">
          <dt>= net</dt>
          <dd>{signed(net)}</dd>
        </div>
        {threshold !== undefined && threshold !== null && (
          <div>
            <dt>
              {Math.abs(net) >= threshold
                ? `above the ${Math.round(threshold * 100)}% threshold`
                : `below the ${Math.round(threshold * 100)}% threshold`}
            </dt>
            <dd>{action ?? ""}</dd>
          </div>
        )}
      </dl>
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
  const { signal, price, market, link, skewMs, refresh } = useLiveSignal();
  const { candles, chain } = useMarketData();
  const [clock, setClock] = useState(() => Date.now());

  /* Drives the age counter. It ticks on its own so a price that stops
     arriving visibly gets older, instead of freezing at whatever it said
     when the last frame landed. */
  useEffect(() => {
    const id = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const marketOpen = market?.open ?? false;

  /* Age of the price on screen, in seconds, measured between absolute
     instants and corrected for this browser's clock offset. Never derived
     from the rendered HH:MM string — that would fold in the timezone
     conversion and quietly report a 5.5-hour error as fresh data. */
  const priceAge = price?.source_time
    ? (clock - skewMs - new Date(price.source_time).getTime()) / 1000
    : null;

  const signalTime = signal?.timestamp
    ? new Date(signal.timestamp).toLocaleTimeString("en-IN", {
        hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata",
      })
    : "—";

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
          <span className={`pill age-${classifyAge(priceAge)}`}>
            {price ? formatAge(priceAge) : "waiting"}
          </span>
          {/* The desk updates itself; this is here for a forced re-read,
              not because anything requires clicking it. */}
          <button onClick={refresh}>refresh</button>
        </div>
      </header>

      {/* The tape renders whether or not a signal exists yet. The price is
          live market data and the signal is a five-minute decision; gating
          the former on the latter meant a working feed showed nothing at
          all until the agent's first tick. */}
      <section className="tape">
        <div className="tape-price">
          <p className="eyebrow">Current price</p>
          <Ticker price={price} marketOpen={marketOpen} />
          <DataAge seconds={priceAge} price={price} />
        </div>
        {signal ? (
          <div className={`tape-verdict ${signal.action}`}>
            <div className={`action ${signal.action}`}>{signal.action}</div>
            <div className="confidence-label">
              {Math.round(signal.confidence * 100)}% confidence
            </div>
          </div>
        ) : (
          <div className="tape-verdict">
            <div className="action">—</div>
            <div className="confidence-label">awaiting first signal</div>
          </div>
        )}
        <div className="tape-stats">
          <div><span className="stat-label">VWAP</span>
            <span className="stat-value">{num(signal?.context?.vwap)}</span></div>
          <div><span className="stat-label">ATR 14</span>
            <span className="stat-value">{num(signal?.context?.atr14)}</span></div>
          <div><span className="stat-label">Trend</span>
            <span className="stat-value">{signal?.context?.trend ?? "—"}</span></div>
          {/* The decision time, which is not the price time. A signal taken
              at 10:20 stays stamped 10:20 while the price above keeps
              moving — that gap is real and the desk should show it. */}
          <div><span className="stat-label">Last signal</span>
            <span className="stat-value">{signalTime}</span></div>
        </div>
      </section>

      {!signal && (
        <p className="notice">
          Waiting for the first signal. The agent publishes one every five
          minutes — if this does not clear, check that the backend is running.
          The price above updates independently and is already live.
        </p>
      )}

      {signal && (
        <>
          <div className="desk">
            <Suspense fallback={<ChartFallback label="Price · VWAP" />}>
              <PriceChart candles={candles} signal={signal} />
            </Suspense>
            <PlanPanel signal={signal} marketOpen={marketOpen} />
          </div>

          <Ledger
            checks={signal.checks || []}
            context={signal.context}
            confidence={signal.confidence}
            action={signal.action}
          />

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