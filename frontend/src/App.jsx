import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";

/* Recharts is roughly three quarters of the bundle. Loading it lazily lets
   the verdict, the ledger and the live price paint immediately — those are
   what you actually read first — while the charts arrive a beat later. */
const PriceChart = lazy(() => import("./PriceChart.jsx"));
const OIProfile = lazy(() => import("./OIProfile.jsx"));

import TopBar from "./TopBar.jsx";
import {
  DecisionPanel, MarketOverview, NewsPanel, PerformanceStrip, SafetyMonitor,
  SignalFeed, performanceFrom,
} from "./panels.jsx";

function ChartFallback({ label }) {
  return (
    <div className="panel chart-fallback">
      <h3>{label}</h3>
      <p className="muted-body">Loading…</p>
    </div>
  );
}

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";

/* Set VITE_API_KEY when the backend has API_KEY set. Reads stay open, so
   this is only needed for the live socket; leaving it unset is correct for
   a localhost desk running without a key. */
const API_KEY = import.meta.env.VITE_API_KEY || "";
const WS_URL = API.replace(/^http/, "ws") + "/ws/signals"
  + (API_KEY ? `?key=${encodeURIComponent(API_KEY)}` : "");

const FALLBACK_POLL_MS = 60_000;   // only used if the socket cannot connect
/* The price gets its own, much faster fallback. /market/price is a Redis
   read costing about 50ms and touches no broker, whereas /signals/live
   rebuilds a signal from the feed — which is why the poll above is slow.
   Sharing one timer meant a dropped socket left the price as stale as the
   analysis, so a working feed still read "delayed 44s ago". */
const PRICE_POLL_MS = 5_000;
const MAX_RECONNECT_MS = 30_000;

/* Mirrors LIVE_SECONDS / DELAYED_SECONDS in backend/app/workers/ticker.py.
   Both ends classify the same way so the dashboard and the API never
   disagree about what "stale" means. */
const LIVE_SECONDS = 15;
const DELAYED_SECONDS = 60;

/* Freshness is a claim about the *feed*, and a feed can only be behind while
   it is supposed to be producing. Outside the session there is nothing newer
   than the closing print to be behind by, so the age counter was measuring
   the length of the night: it read "557m 37s ago" at 00:48 and would have
   reached four figures by Monday, in the same alarm colour the desk uses for
   a genuinely broken ticker. An alarm that cannot clear is not an alarm.

   So the classification takes the session with it. While the market is open
   the thresholds below apply unchanged — that detection is the whole point
   of the live-price work and it is untouched. While it is shut, the reading
   is "closed", and what gets shown is the fixed instant of the last print
   rather than a stopwatch running away from it. */
export function classifyAge(seconds, sessionLive = true) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "unknown";
  if (!sessionLive) return "closed";
  if (seconds <= LIVE_SECONDS) return "live";
  if (seconds <= DELAYED_SECONDS) return "delayed";
  return "stale";
}

export function formatAge(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "—";
  if (seconds < 0) return "just now";          /* clock skew overshoot */
  if (seconds < 1) return "just now";
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  const m = Math.floor(seconds / 60);
  if (m < 60) {
    const rest = Math.floor(seconds % 60);
    return `${m}m ${String(rest).padStart(2, "0")}s ago`;
  }
  // Past an hour the seconds are noise and the minutes stop being legible:
  // nobody reads "557m" as nine and a quarter hours.
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, "0")}m ago`;
}

/* The clock time of an absolute instant, in IST. Used where a fixed moment
   says more than an elapsed count — which, once the session is over, is
   everywhere. */
export function formatClock(iso) {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return "—";
  return new Date(ms).toLocaleTimeString("en-IN", {
    timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit",
  });
}

/* Session copy. The state itself is decided by the backend and only
   formatted here — the dashboard used to have no say in it and must keep
   none, or the two disagree on a holiday. */
const SESSION_LABEL = {
  open: "Market open", "pre-open": "Pre-open", closed: "Market closed",
};

const CLOSED_BECAUSE = {
  weekend: "Weekend", holiday: "Exchange holiday",
  "before-open": "Opens later today", "after-close": "Session finished",
};

function formatCountdown(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "—";
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h >= 1) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m >= 1) return `${m}m`;
  return `${s}s`;
}

/* The risk verdict, as the backend reported it. `missing` is not a state the
   backend sends — it is what the dashboard shows if a payload ever arrives
   without a decision, so the absence is loud instead of invisible. */
const RISK_MISSING = "missing";

/* "1 lots" read as a typo every time. */
const plural = (quantity, lots) =>
  `${quantity} (${lots} ${lots === 1 ? "lot" : "lots"})`;

/* Clock time of an absolute instant, in IST. Used to date the stored
   verdict, so "at signal" names a moment rather than a vague past. */
const clockIST = (iso) => {
  if (!iso) return null;
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return null;
  return new Date(ms).toLocaleTimeString("en-IN", {
    timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit",
  });
};
const RISK_VERDICT = {
  approved: "APPROVED",
  blocked: "BLOCKED",
  unevaluated: "NOT EVALUATED",
  "not-applicable": "NO TRADE",
  [RISK_MISSING]: "NOT REPORTED",
};

const AGE_LABEL = {
  live: "Data live", delayed: "Data delayed", stale: "Data stale",
  closed: "Last print", unknown: "Data age unknown",
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
  /* The risk verdict as of *now*, which is not the same thing as the verdict
     the signal was published with. The journal moves between agent ticks:
     open a position at 10:01 and the 10:00 approval is stale by 10:02. The
     backend recomputes this on connect and on every heartbeat. */
  const [riskNow, setRiskNow] = useState(null);
  /* The market condition the analysis is being formed in. A property of the
     market rather than of the signal, so it arrives on the heartbeat and
     keeps updating between agent ticks. */
  const [regime, setRegime] = useState(null);
  const [link, setLink] = useState("connecting");   // connecting | live | polling
  const [skewMs, setSkewMs] = useState(0);

  const socket = useRef(null);
  const attempts = useRef(0);
  const pollTimer = useRef(null);
  const priceTimer = useRef(null);
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
      const [sig, status, tick, condition] = await Promise.all([
        getJSON("/signals/live?symbol=NIFTY&timeframe=5m"),
        getJSON("/market/status").catch(() => null),
        getJSON("/market/price").catch(() => null),
        getJSON("/market/regime").catch(() => null),
      ]);
      setSignal(sig);
      if (condition) setRegime(condition);
      /* The polled endpoint builds a signal from scratch, so its verdict was
         computed for this request — current by construction. */
      if (sig?.risk) setRiskNow(sig.risk);
      if (status) { setMarket(status); noteServerClock(status.server_time); }
      if (tick && tick.price !== null && tick.price !== undefined) setPrice(tick);
    } catch {
      /* leave the last good signal on screen rather than blanking it */
    }
  }, [noteServerClock]);

  /* Price only. Cheap enough to run at the ticker's own cadence. */
  const pollPrice = useCallback(async () => {
    try {
      const tick = await getJSON("/market/price");
      if (tick && tick.price !== null && tick.price !== undefined) setPrice(tick);
    } catch {
      /* keep the last price and let its age keep climbing */
    }
  }, []);

  const startPolling = useCallback(() => {
    if (pollTimer.current) return;
    setLink("polling");
    poll();
    pollPrice();
    pollTimer.current = setInterval(poll, FALLBACK_POLL_MS);
    priceTimer.current = setInterval(pollPrice, PRICE_POLL_MS);
  }, [poll, pollPrice]);

  const stopPolling = useCallback(() => {
    clearInterval(pollTimer.current);
    clearInterval(priceTimer.current);
    pollTimer.current = null;
    priceTimer.current = null;
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
      if (msg.signal) {
        setSignal(msg.signal);
        /* A signal that has just arrived was judged moments ago, so its own
           verdict is the current one until the next heartbeat says
           otherwise. */
        if (msg.risk_now === undefined && msg.signal.risk) setRiskNow(msg.signal.risk);
      }
      if (msg.risk_now !== undefined) setRiskNow(msg.risk_now);
      /* `undefined` means this frame carried no regime; `null` means the
         backend looked and has none classified. Only the second should clear
         what is on screen. */
      if (msg.regime !== undefined) setRegime(msg.regime);
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

  return { signal, price, market, riskNow, regime, link, skewMs, refresh: poll };
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

/* The slow-moving desk furniture: the journal, the outcome study, the data
   quality report, the scheduler and the archive.

   Deliberately its own loop and a slow one. None of this changes between
   agent ticks, and putting it on the price socket would repaint the whole
   right-hand column every few seconds for no new information.

   Every request is independently guarded. A backend with no scheduler
   endpoint, or a quality report that fails, must cost that one panel its
   reading and not the other six — which is why this is six catches rather
   than one try block. */
function useDeskData(intervalMs = 60_000) {
  const [feed, setFeed] = useState([]);
  const [study, setStudy] = useState(null);
  const [quality, setQuality] = useState(null);
  const [scheduler, setScheduler] = useState(null);
  const [coverage, setCoverage] = useState(null);
  const [vix, setVix] = useState(null);
  const [news, setNews] = useState(null);
  const [priceFeed, setPriceFeed] = useState(null);

  const load = useCallback(async () => {
    // `feedStatus` is the *price* feed — which source is serving — and is
    // deliberately not the same thing as `feed`, the signal journal.
    const [history, outcomes, qual, sched, feedStatus, cov, vixRes, headlines] =
      await Promise.all([
      getJSON("/signals/history?limit=25").catch(() => null),
      getJSON("/signals/outcomes?symbol=NIFTY&include_signals=true").catch(() => null),
      getJSON("/data/quality?symbol=NIFTY").catch(() => null),
      getJSON("/health/scheduler").catch(() => null),
      getJSON("/health/feed").catch(() => null),
      getJSON("/data/coverage?symbol=NIFTY").catch(() => null),
      getJSON("/market/vix").catch(() => null),
      getJSON("/news").catch(() => null),
    ]);
    if (Array.isArray(history)) setFeed(history);
    if (outcomes) setStudy(outcomes);
    if (qual) setQuality(qual);
    if (sched) setScheduler(sched);
    if (feedStatus) setPriceFeed(feedStatus);
    if (cov) setCoverage(cov);
    if (vixRes && typeof vixRes.india_vix === "number") setVix(vixRes.india_vix);
    if (headlines) setNews(headlines);
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, intervalMs);
    return () => clearInterval(id);
  }, [load, intervalMs]);

  return { feed, study, quality, scheduler, priceFeed, coverage, vix, news };
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

/* The session clock.

   Everything shown here comes straight off /market/status: the state, the
   next boundary and its direction. The countdown is recomputed against that
   absolute instant on every tick, so it falls toward the boundary rather
   than climbing away from a stale reading — which is what a midnight
   "pre-open" was doing. */
function SessionClock({ market, secondsToBoundary }) {
  if (!market) {
    return (
      <p className="session-clock">
        <span className="session-state">…</span>
      </p>
    );
  }
  const state = market.session ?? "closed";
  const verb = market.boundary_direction === "closes" ? "Closes" : "Opens";
  const nextOpen = market.next_open
    ? new Date(market.next_open).toLocaleString("en-IN", {
        weekday: "short", hour: "2-digit", minute: "2-digit",
        timeZone: "Asia/Kolkata",
      })
    : null;

  return (
    <p className={`session-clock session-${state}`}>
      <span className="session-state">{SESSION_LABEL[state] ?? state}</span>
      {state !== "open" && market.reason && (
        <span className="session-reason">{CLOSED_BECAUSE[market.reason] ?? market.reason}</span>
      )}
      {state !== "open" && nextOpen && (
        <span className="session-next">Next session {nextOpen}</span>
      )}
      <span className="session-countdown">
        {verb} in {formatCountdown(secondsToBoundary)}
      </span>
      {market.calendar_provisional && (
        <span className="session-note" title="The 2026 NSE holiday list is transcribed but unverified.">
          provisional calendar
        </span>
      )}
    </p>
  );
}

/* How far behind the market the number above actually is.

   Separate from the signal's timestamp on purpose. The price and the
   analysis are two different clocks, and the dashboard used to imply they
   were one: a five-minute-old signal sat beside a live price under a single
   "updated" label, so whichever was staler was the one you could not see. */
function DataAge({ seconds, price, sessionLive }) {
  const state = classifyAge(seconds, sessionLive);
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
      <span className="data-age-value">
        {/* A fixed instant once the session is over. The number stops
            moving because the thing it describes stopped moving. */}
        {state === "closed" ? `${formatClock(price.source_time)} IST` : formatAge(seconds)}
      </span>
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

/* Which condition the desk thinks the market is in, at two levels.

   Separate from the signal on purpose. The regime is a property of the
   market, not of any one decision, and it keeps moving between agent ticks —
   rendering it inside the signal panel would freeze it at whatever it was
   when the last signal fired.

   The two levels are shown together because the interesting case is when
   they disagree: an hour running against the day is either the turn or a
   trap, and the desk has no rule yet that tells those apart. Hiding the
   disagreement behind a single label would hide the one thing worth
   looking at. */
const REGIME_LABEL = {
  TREND_UP: "Trend up",
  TREND_DOWN: "Trend down",
  RANGE: "Range",
  VOLATILE_CHOP: "Volatile chop",
  SQUEEZE: "Squeeze",
};

function RegimeReading({ level, reading }) {
  if (!reading) return null;
  const label = REGIME_LABEL[reading.label] ?? reading.label;
  return (
    <div className={`regime-reading regime-${reading.label}`}>
      <p className="eyebrow">{level}</p>
      <p className="regime-label">
        {label}
        {reading.provisional && <span className="regime-flag">provisional</span>}
      </p>
      <p className="regime-confidence">
        {Math.round((reading.confidence ?? 0) * 100)}% confidence
      </p>
      <ul className="regime-reasons">
        {(reading.reasons || []).map((reason, i) => (
          <li key={i}>{reason}</li>
        ))}
      </ul>
    </div>
  );
}

/* The two layers, shown as two. The whole point of splitting the output is
   that direction and timing fail independently, so a dashboard that fused
   them back into one badge would undo it — "BULLISH, but wait for a pullback
   near 24,180" is the sentence the desk needs, and it needs both halves. */
const BIAS_LABEL = { BULLISH: "Bullish", BEARISH: "Bearish", NEUTRAL: "Neutral" };

const ENTRY_LABEL = {
  ENTER_NOW: "Enter now",
  WAIT_PULLBACK: "Wait for pullback",
  WAIT_BREAKOUT: "Wait for breakout",
  NO_ENTRY: "No entry",
};

function PlanLayers({ plan }) {
  if (!plan || !plan.bias || !plan.entry) {
    return (
      <div className="panel">
        <h2>Bias &amp; entry</h2>
        <p className="muted">
          No two-layer read for this signal. The bias needs closed 15-minute
          and hourly bars, which the first minutes of a session do not have.
        </p>
      </div>
    );
  }

  const { bias, entry } = plan;
  const level =
    entry.trigger_level === null || entry.trigger_level === undefined
      ? null
      : num(entry.trigger_level);

  return (
    <div className="panel">
      <h2>Bias &amp; entry</h2>

      {/* The headline sentence, assembled from both layers rather than from
          either one. */}
      <p className={`plan-headline bias-${bias.label}`}>
        <strong>{BIAS_LABEL[bias.label] ?? bias.label}</strong>
        {" — "}
        <span className={`entry-${entry.state}`}>
          {ENTRY_LABEL[entry.state] ?? entry.state}
        </span>
        {entry.trigger_note && level && (
          <span className="plan-trigger">
            {" "}
            ({entry.trigger_note} {level})
          </span>
        )}
      </p>

      <div className="plan-grid">
        <div className="plan-layer">
          <p className="eyebrow">Layer 1 · higher timeframe</p>
          <p className="plan-value">
            {BIAS_LABEL[bias.label] ?? bias.label}
            <span className="plan-confidence">
              {Math.round((bias.confidence ?? 0) * 100)}%
            </span>
          </p>
          <ul className="plan-reasons">
            {(bias.reasons || []).map((reason, i) => (
              <li key={i}>{reason}</li>
            ))}
          </ul>
        </div>

        <div className="plan-layer">
          <p className="eyebrow">Layer 2 · this bar</p>
          <p className="plan-value">
            {ENTRY_LABEL[entry.state] ?? entry.state}
            <span className="plan-confidence">
              {Math.round((entry.confidence ?? 0) * 100)}%
            </span>
          </p>
          <ul className="plan-reasons">
            {(entry.reasons || []).map((reason, i) => (
              <li key={i}>{reason}</li>
            ))}
          </ul>
        </div>
      </div>

      <p className="muted regime-foot">
        Two layers, deliberately. The bias says where the higher timeframe
        points; the entry state says whether this is the moment. Neither
        places an order, and neither overrides the risk manager.
      </p>
    </div>
  );
}

function RegimePanel({ regime }) {
  if (!regime || (!regime.day && !regime.hour)) {
    return (
      <div className="panel">
        <h2>Market regime</h2>
        <p className="muted">
          {regime?.note ??
            "No regime classified yet. The agent classifies each bar as it is archived."}
        </p>
      </div>
    );
  }

  const disagree =
    regime.day && regime.hour && regime.day.label !== regime.hour.label;

  return (
    <div className="panel">
      <h2>Market regime</h2>
      <div className="regime-grid">
        <RegimeReading level="Session so far" reading={regime.day} />
        <RegimeReading level="Last hour" reading={regime.hour} />
      </div>
      {disagree && (
        <p className="regime-note">
          The hour is reading against the session. That is either the turn or
          a trap — nothing here yet tells those apart.
        </p>
      )}
      <p className="muted regime-foot">
        Classified from ATR against its own baseline, directional efficiency,
        VWAP behaviour and the session&rsquo;s opening range. A description of
        conditions, not a trade instruction.
      </p>
    </div>
  );
}

function PlanPanel({ signal, marketOpen, riskNow }) {
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

  /* Two verdicts, and the difference between them is the point.

     `signal.risk` is what the desk decided when it published this plan.
     `riskNow` is what it would decide about the same levels against today's
     journal as it stands. They diverge the moment a position is opened, and
     a browser that reconnects replays a signal up to fifteen minutes old —
     so presenting the stored verdict as the live one would tell you a trade
     is approved after you have already taken the position that blocks it.

     The loud badge is the current one, because that is the one you act on.
     Neither is inferred here; the backend decides both. */
  const atSignal = signal.risk;
  const live = riskNow ?? null;
  const shown = live ?? atSignal;
  const state = shown?.state ?? RISK_MISSING;
  const approved = state === "approved";
  const stale = Boolean(live && atSignal && live.state !== atSignal.state);

  return (
    <div className="panel">
      <h3>Trade plan</h3>

      <p className={`risk-verdict risk-${state}`}>
        <span className="risk-label">{live ? "Risk now" : "Risk at signal"}</span>
        <b>{RISK_VERDICT[state] ?? RISK_VERDICT[RISK_MISSING]}</b>
      </p>

      {atSignal && (
        <p className={`risk-history${stale ? " risk-history-changed" : ""}`}>
          At signal {clockIST(atSignal.evaluated_at) ?? "—"}:{" "}
          {RISK_VERDICT[atSignal.state] ?? RISK_VERDICT[RISK_MISSING]}
          {stale && " — the journal has moved since"}
        </p>
      )}

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
        {shown?.evaluated && (
          <>
            <div className="kv"><dt>Size</dt>
              <dd>{approved ? plural(shown.quantity, shown.lots) : "blocked"}</dd></div>
            {/* A refused trade has no money on the table. What the sizing
                would have been is shown below as what it is, rather than
                reported here as exposure. */}
            <div className="kv"><dt>Rupees at risk</dt>
              <dd>{num(shown.rupees_at_risk ?? 0, 0)}</dd></div>
          </>
        )}
      </dl>

      {!approved && shown?.evaluated && shown.potential?.rupees_at_risk > 0 && (
        <p className="risk-potential">
          Had it been allowed: {plural(shown.potential.quantity, shown.potential.lots)},
          risking {num(shown.potential.rupees_at_risk, 0)}.
        </p>
      )}

      {!approved && shown?.reasons?.length > 0 && (
        <p className="warn-line">
          {shown.evaluated ? "Risk manager blocked this: " : ""}
          {shown.reasons.join(" ")}
        </p>
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
  const { signal, price, market, riskNow, regime, link, skewMs, refresh } =
    useLiveSignal();
  const { candles, chain } = useMarketData();
  const { feed, study, quality, scheduler, priceFeed, coverage, vix, news } = useDeskData();
  const [clock, setClock] = useState(() => Date.now());

  /* Drives the age counter and the wall clock. It ticks on its own so a
     price that stops arriving visibly gets older, instead of freezing at
     whatever it said when the last frame landed. */
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

  /* Is the feed supposed to be producing right now? Only the backend's
     session decides — never `Date.now()` here, which would put the desk back
     in the business of guessing the market clock for itself.

     Unknown means "assume it is", so a status request that has not landed
     yet cannot silence a real staleness alarm. The test for "known" is the
     `session` field, not the object: a failed status fetch still resolves to
     a bodyless payload, and treating that as "not open" would suppress the
     alarm exactly when the backend is in trouble. */
  const sessionLive = market?.session ? market.session === "open" : true;
  const ageState = classifyAge(priceAge, sessionLive);

  /* Seconds to the next session boundary, recomputed on every tick against
     the absolute instant the backend supplied. Falls as time passes; it
     cannot drift or climb, because nothing here accumulates. */
  const secondsToBoundary = market?.next_boundary
    ? Math.max(0, (new Date(market.next_boundary).getTime() - (clock - skewMs)) / 1000)
    : null;

  const perf = performanceFrom(study);
  const outcomeRows = study?.outcomes ?? [];

  /* What the freshness pill says after the label. A fixed instant once the
     session is over, because the number stops moving when the thing it
     describes stops moving. */
  const ageText = !price ? ""
    : ageState === "closed" ? formatClock(price.source_time)
    : formatAge(priceAge);

  return (
    <div className="shell">
      <TopBar
        price={price} vix={vix} market={market} link={link}
        ageState={ageState} ageText={ageText} clock={clock}
        onRefresh={refresh}
      />

      <PerformanceStrip perf={perf} />

      <main className="terminal">
        {/* Left: the chart is the main visual, with the numbers that
            describe the same market directly under it. */}
        <div className="col col-chart">
          <Suspense fallback={<ChartFallback label="NIFTY 50 · 5m" />}>
            <PriceChart candles={candles} signal={signal} />
          </Suspense>
          <MarketOverview
            signal={signal} chain={chain} regime={regime} vix={vix} price={price}
          />
        </div>

        {/* Centre: what the desk is deciding, and the record of what it
            decided before. This column is the reason the page exists. */}
        <div className="col col-decision">
          {signal ? (
            <DecisionPanel
              signal={signal} regime={regime} riskNow={riskNow}
              marketOpen={marketOpen}
            />
          ) : (
            <section className="panel decision-panel">
              <div className="panel-head">
                <h2>Active decision</h2>
                <span className="panel-note">awaiting first signal</span>
              </div>
              <div className="verdict verdict-flat">
                <span className="verdict-kicker">Model</span>
                <strong className="verdict-headline">—</strong>
                <span className="verdict-sub">no decision yet</span>
              </div>
              <p className="muted-body">
                The agent publishes one every five minutes — if this does not
                clear, check that the backend is running. The price above
                updates independently and is already live.
              </p>
            </section>
          )}
          <SignalFeed rows={feed} outcomes={outcomeRows} />
        </div>

        {/* Right: is the desk itself trustworthy right now. */}
        <div className="col col-status">
          {/* The price itself lives in the top bar and only there. This
              panel is about how far behind the market that number is and
              where the session stands — the two questions the number alone
              cannot answer. */}
          <section className="panel session-panel">
            <div className="panel-head">
              <h2>Current price</h2>
              <span className="panel-note mono">
                {price?.source ? `via ${price.source}` : "no feed"}
              </span>
            </div>
            <DataAge seconds={priceAge} price={price} sessionLive={sessionLive} />
            <SessionClock market={market} secondsToBoundary={secondsToBoundary} />
          </section>
          {/* The market is in some condition whether or not the agent has
              spoken yet, so this is never gated on a signal. */}
          <RegimePanel regime={regime} />
          <SafetyMonitor
            ageState={ageState} priceAge={priceAge} market={market}
            quality={quality} scheduler={scheduler} riskNow={riskNow}
            coverage={coverage} feed={priceFeed}
          />
          <NewsPanel news={news} />
        </div>
      </main>

      {/* Below the fold: the working. Kept off the decision columns because
          it is what you read when you disagree with the call, not what you
          read to act on it. */}
      {signal && (
        <section className="terminal-lower">
          <div className="lower-wide">
            <Ledger
              checks={signal.checks || []}
              context={signal.context}
              confidence={signal.confidence}
              action={signal.action}
            />
          </div>
          <div className="lower-wide">
            <Suspense fallback={<ChartFallback label="Open interest" />}>
              <OIProfile
                strikes={chain?.strikes}
                summary={chain?.summary || signal.context?.option_chain}
                spot={price?.price || signal.price}
              />
            </Suspense>
          </div>
          <ContextPanel context={signal.context} />
        </section>
      )}

      <p className="notice">
        Educational purpose only. This is an analysis tool — it reads the
        market and shows its working, it does not know the future, and it is
        not advice. Every number here comes from a rule you can read in{" "}
        <code>backend/app/analytics</code>. Not SEBI registered.
      </p>
    </div>
  );
}
