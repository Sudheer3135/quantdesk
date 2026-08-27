/* The terminal panels.

   Presentation only. Nothing here fetches, decides, or derives a trading
   view of its own — every panel is handed what the backend said and renders
   it. That separation is the reason these can be tested without a socket.

   Two rules run through the file and are worth stating once:

   **Never show a value the backend did not send.** A missing input renders
   as "Unavailable" or an em dash. On a price screen a fabricated zero is
   indistinguishable from a real reading, and the eye cannot catch it.

   **The decision must be readable across a room.** ENTER / WAIT / NO TRADE
   and the risk verdict carry their own colour and weight, because those are
   the two things a desk acts on and everything else is context for them.
*/
import {
  BIAS_LABEL, ENTRY_LABEL, ENTRY_TONE, REGIME_LABEL, RISK_TONE, RISK_VERDICT,
  UNAVAILABLE, dirOf, istStamp, istTime, num, pct, plural, signed,
} from "./format.js";

/* ------------------------------------------------------------------ bits */

/** A label/value pair — the unit the whole terminal is built from. */
export function Cell({ label, value, tone = "", title, mono = true }) {
  return (
    <div className={`cell${tone ? ` tone-${tone}` : ""}`} title={title}>
      <span className="cell-label">{label}</span>
      <span className={`cell-value${mono ? " mono" : ""}`}>{value}</span>
    </div>
  );
}

/** A quote in the top bar. `quote` null means the desk has no feed for it. */
export function QuoteCell({ label, quote, flash = "" }) {
  if (!quote || quote.price === null || quote.price === undefined) {
    return (
      <div className="quote quote-absent" title={`No ${label} feed is wired up`}>
        <span className="quote-name">{label}</span>
        <span className="quote-price mono">{UNAVAILABLE}</span>
      </div>
    );
  }
  const dir = dirOf(quote.change);
  return (
    <div className={`quote dir-${dir}`}>
      <span className="quote-name">{label}</span>
      <span className={`quote-price mono${flash ? ` flash-${flash}` : ""}`}>
        {num(quote.price)}
      </span>
      <span className={`quote-change mono dir-${dir}`}>
        {quote.change === null || quote.change === undefined
          ? "—" : signed(quote.change)}
      </span>
    </div>
  );
}

/* ------------------------------------------------------- 2. performance */

/** Reduce the per-signal outcome rows to the averages the strip needs.

    Computed here rather than asked of the backend because the backend
    already sends every row it would compute from; deriving the mean of a
    list the caller is holding is display work, not analysis. Returns nulls
    — never zeros — when there is nothing to average. */
export function performanceFrom(report) {
  const overall = report?.overall;
  if (!overall || !overall.n) return null;

  const rows = report.outcomes || [];
  const rs = rows.map((r) => r.r_multiple).filter((r) => Number.isFinite(r));
  const winsR = rs.filter((r) => r > 0);
  const lossesR = rs.filter((r) => r <= 0);
  const mean = (list) =>
    list.length ? list.reduce((a, b) => a + b, 0) / list.length : null;

  return {
    trades: overall.n,
    wins: overall.wins,
    // Resolved and not won. Taken as a subtraction rather than by counting
    // stop-outs, so time-capped and session-end exits are not quietly
    // dropped out of the denominator the win rate is read against.
    losses: overall.resolved - overall.wins,
    winRate: overall.win_rate,
    totalR: overall.total_r,
    avgR: overall.avg_r,
    // Null when the caller did not request per-signal rows. The strip shows
    // "Unavailable" rather than implying the average of an empty list is 0.
    avgWinR: mean(winsR),
    avgLossR: mean(lossesR),
    unresolved: overall.unresolved,
  };
}

export function PerformanceStrip({ perf }) {
  if (!perf) {
    return (
      <section className="perf-strip perf-empty">
        <p className="muted">
          No evaluated signals yet. The strip fills once stored signals have
          candles to be replayed against.
        </p>
      </section>
    );
  }
  const rTone = (v) => (v === null ? "" : v > 0 ? "up" : v < 0 ? "down" : "");
  return (
    <section className="perf-strip">
      <Cell label="Trades" value={perf.trades} />
      <Cell label="Wins" value={perf.wins} tone="up" />
      <Cell label="Losses" value={perf.losses} tone="down" />
      <Cell label="Win rate" value={pct(perf.winRate, 1)} />
      <Cell label="Net R" value={signed(perf.totalR, 1)} tone={rTone(perf.totalR)} />
      <Cell
        label="Avg win"
        value={perf.avgWinR === null ? UNAVAILABLE : `${signed(perf.avgWinR)}R`}
        tone={perf.avgWinR === null ? "" : "up"}
      />
      <Cell
        label="Avg loss"
        value={perf.avgLossR === null ? UNAVAILABLE : `${signed(perf.avgLossR)}R`}
        tone={perf.avgLossR === null ? "" : "down"}
      />
      <Cell label="Avg R" value={signed(perf.avgR)} tone={rTone(perf.avgR)} />
      {perf.unresolved > 0 && (
        <Cell label="Open" value={perf.unresolved} tone="wait" />
      )}
    </section>
  );
}

/* ---------------------------------------------------- 4. active decision */

/** The decision's own clock, in the 12-hour form the desk has always used.
    Distinct from `istTime`, which is 24-hour for dense value columns. */
const signalClock = (iso) => {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return "—";
  return new Date(ms).toLocaleTimeString("en-IN", {
    timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit",
  });
};


/** The plain-English sentence, assembled from the layers rather than stored.

    The desk's own words for what it is looking at. Built from the two
    labels plus the trigger the entry layer named, so it can never say
    something the layers do not. */
export function explainPlan(bias, entry) {
  if (!bias || !entry) return null;
  const dir = {
    BULLISH: "Market direction is bullish",
    BEARISH: "Market direction is bearish",
    NEUTRAL: "The higher timeframe has no clear direction",
  }[bias.label] ?? `Bias is ${bias.label}`;

  const level = Number.isFinite(entry.trigger_level)
    ? ` toward ${num(entry.trigger_level)}` : "";

  const advice = {
    ENTER_NOW: "Conditions line up — this is the moment.",
    WAIT_PULLBACK: `but price is extended. Wait for a pullback${level}.`,
    WAIT_BREAKOUT: `but price is inside the range. Wait for a break${level}.`,
    NO_ENTRY: "and this bar is not a place to act.",
  }[entry.state] ?? "";

  return entry.state === "ENTER_NOW" ? `${dir}. ${advice}` : `${dir}, ${advice}`;
}

function Verdict({ tone, kicker, headline, sub, extra = "" }) {
  return (
    <div className={`verdict verdict-${tone}${extra ? ` ${extra}` : ""}`}>
      <span className="verdict-kicker">{kicker}</span>
      <strong className="verdict-headline">{headline}</strong>
      {sub && <span className="verdict-sub">{sub}</span>}
    </div>
  );
}

export function DecisionPanel({ signal, regime, riskNow, marketOpen }) {
  const plan = signal?.plan;
  const bias = plan?.bias ?? null;
  const entry = plan?.entry ?? null;

  /* Two verdicts, and the difference between them is the point. `risk` is
     what the desk decided when it published this plan; `riskNow` is what it
     would decide against the journal as it stands. They diverge the moment a
     position is opened. The loud one is the current one, because that is the
     one you act on. */
  const atSignal = signal?.risk ?? null;
  const live = riskNow ?? null;
  const shown = live ?? atSignal;
  const riskState = shown?.state ?? "missing";
  const approved = riskState === "approved";
  const changed = Boolean(live && atSignal && live.state !== atSignal.state);

  const entryTone = entry ? (ENTRY_TONE[entry.state] ?? "flat") : "flat";
  const hasPlan = signal && signal.action !== "HOLD";
  const sentence = explainPlan(bias, entry);

  return (
    <section className="panel decision-panel" aria-label="Active decision">
      <div className="panel-head">
        <h2>Active decision</h2>
        <span className="panel-note mono">
          {signal && <span className={`chip act-${signal.action}`}>{signal.action}</span>}
          {" "}
          <span className="dim">Last signal</span>{" "}
          {signal?.timestamp ? signalClock(signal.timestamp) : "—"}
        </span>
      </div>

      {/* The three-step read, in the order the desk actually reasons:
          what kind of market, which way, and is this the moment. */}
      <div className="decision-chain">
        <div className="chain-step">
          <span className="chain-label">Regime</span>
          <span className={`chain-value regime-${regime?.day?.label ?? "none"}`}>
            {regime?.day
              ? (REGIME_LABEL[regime.day.label] ?? regime.day.label)
              : UNAVAILABLE}
          </span>
          <span className="chain-conf mono">
            {regime?.day ? pct(regime.day.confidence) : ""}
          </span>
        </div>
        <div className="chain-step">
          <span className="chain-label">Bias</span>
          <span className={`chain-value bias-${bias?.label ?? "none"}`}>
            {bias ? (BIAS_LABEL[bias.label] ?? bias.label) : UNAVAILABLE}
          </span>
          <span className="chain-conf mono">{bias ? pct(bias.confidence) : ""}</span>
        </div>
        <div className="chain-step">
          <span className="chain-label">Entry state</span>
          <span className={`chain-value entry-${entry?.state ?? "none"}`}>
            {entry ? (ENTRY_LABEL[entry.state] ?? entry.state) : UNAVAILABLE}
          </span>
          <span className="chain-conf mono">{entry ? pct(entry.confidence) : ""}</span>
        </div>
      </div>

      {/* The two things a desk acts on, side by side and impossible to
          miss: whether the model says go, and whether risk allows it. */}
      <div className="verdict-row">
        <Verdict
          tone={entryTone}
          kicker="Model"
          headline={entry ? (ENTRY_LABEL[entry.state] ?? entry.state) : "NO READ"}
          sub={signal ? `${signal.action} · ${pct(signal.confidence)} confidence` : null}
        />
        {/* Only where a trade was actually proposed. On a HOLD there is
            nothing to approve or refuse, and a badge reading "NO TRADE"
            beside a decision that already says HOLD is a second answer to a
            question nobody asked.

            `risk-verdict` and `risk-<state>` are a contract, not decoration:
            the desk's tests read the verdict off those classes so a redesign
            cannot quietly stop showing whether a trade was allowed. */}
        {hasPlan && <Verdict
          tone={RISK_TONE[riskState] ?? "stop"}
          extra={`risk-verdict risk-${riskState}`}
          kicker={live ? "Risk now" : "Risk at signal"}
          headline={RISK_VERDICT[riskState] ?? RISK_VERDICT.missing}
          sub={shown?.evaluated
            ? (approved ? "position sized below" : "no size")
            : null}
        />}
      </div>

      {sentence && <p className="decision-sentence">{sentence}</p>}

      {/* The trigger in the entry layer's own words. `explainPlan` above is
          the desk's paraphrase; this is the level it actually named, and the
          two are kept apart so a paraphrase can never stand in for a
          number. */}
      {entry?.trigger_note && Number.isFinite(entry.trigger_level) && (
        <p className="plan-trigger mono">
          {entry.trigger_note} {num(entry.trigger_level)}
        </p>
      )}

      {atSignal && (
        <p className={`risk-history${changed ? " risk-history-changed" : ""}`}>
          At signal {istTime(atSignal.evaluated_at)}:{" "}
          {RISK_VERDICT[atSignal.state] ?? RISK_VERDICT.missing}
          {changed && " — the journal has moved since"}
        </p>
      )}

      {!marketOpen && (
        <p className="warn-line">
          Market is closed — this reads the final candle of the session, not a
          tradeable setup. Overnight gaps will invalidate these levels.
        </p>
      )}

      {hasPlan ? (
        <dl className="levels-dl">
          <div className="kv"><dt>Entry</dt><dd>{num(signal.entry)}</dd></div>
          <div className="kv"><dt>Stop</dt>
            <dd className="down">{num(signal.stop_loss)}</dd></div>
          <div className="kv"><dt>Target</dt>
            <dd className="up">{num(signal.target)}</dd></div>
          <div className="kv"><dt>Reward:risk</dt>
            <dd>1:{num(signal.risk_reward)}</dd></div>
          <div className="kv"><dt>Invalidation</dt>
            <dd>{Number.isFinite(signal.stop_loss)
              ? `Close beyond ${num(signal.stop_loss)}` : UNAVAILABLE}</dd></div>
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
      ) : (
        <p className="muted-body">
          Confidence is under the threshold of{" "}
          {pct(signal?.context?.threshold_used ?? 0.35)}. No plan is generated,
          because a setup you cannot describe is a setup you should not take.
        </p>
      )}

      {!approved && shown?.evaluated && shown.potential?.rupees_at_risk > 0 && (
        <p className="risk-potential">
          Had it been allowed: {plural(shown.potential.quantity, shown.potential.lots)},
          risking {num(shown.potential.rupees_at_risk, 0)}.
        </p>
      )}

      {!approved && shown?.reasons?.length > 0 && (
        <ul className="safety-checks">
          {shown.reasons.map((reason, i) => (
            <li key={i} className="check-fail">{reason}</li>
          ))}
        </ul>
      )}

      {signal && !bias && !entry && (
        <p className="muted-body plan-absent">
          No two-layer read for this signal. The bias needs closed 15-minute
          and hourly bars, which the first minutes of a session do not have.
        </p>
      )}

      {/* The layers' own reasons, kept below the decision rather than
          replacing it. Direction and timing fail independently, so the two
          lists stay two lists. */}
      {(bias || entry) && (
        <div className="reason-columns">
          {bias && (
            <div className="reason-col">
              <p className="eyebrow">Layer 1 · higher timeframe</p>
              <ul className="plan-reasons">
                {(bias.reasons || []).slice(0, 4).map((r, i) => <li key={i}>{r}</li>)}
              </ul>
            </div>
          )}
          {entry && (
            <div className="reason-col">
              <p className="eyebrow">Layer 2 · this bar</p>
              <ul className="plan-reasons">
                {(entry.reasons || []).slice(0, 4).map((r, i) => <li key={i}>{r}</li>)}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

/* ---------------------------------------------------------- 5. feed */

export function SignalFeed({ rows, outcomes }) {
  /* Outcomes arrive from a different endpoint keyed by signal id, so the
     join happens here. A row with no outcome is shown as open rather than
     given a neutral-looking dash that reads as "flat". */
  const byId = new Map((outcomes || []).map((o) => [o.signal_id, o]));

  if (!rows?.length) {
    return (
      <section className="panel feed-panel">
        <div className="panel-head"><h2>Signal feed</h2></div>
        <p className="muted-body">
          No signals recorded yet. The agent publishes one every five minutes
          while the market is open.
        </p>
      </section>
    );
  }

  return (
    <section className="panel feed-panel" aria-label="Signal feed">
      <div className="panel-head">
        <h2>Signal feed</h2>
        <span className="panel-note mono">{rows.length} recent</span>
      </div>
      <div className="feed-scroll">
        <table className="feed-table">
          <thead>
            <tr>
              <th>Time</th><th>Action</th><th>Bias</th><th>Entry</th>
              <th>Regime</th><th className="ralign">Conf</th>
              <th>Risk</th><th>Outcome</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const done = byId.get(r.id);
              return (
                <tr key={r.id}>
                  <td className="mono dim">{istStamp(r.created_at)}</td>
                  <td><span className={`chip act-${r.action}`}>{r.action}</span></td>
                  <td className={`bias-${r.bias ?? "none"}`}>
                    {r.bias ? (BIAS_LABEL[r.bias] ?? r.bias) : "—"}
                  </td>
                  <td className={`entry-${r.entry_state ?? "none"}`}>
                    {r.entry_state ? (ENTRY_LABEL[r.entry_state] ?? r.entry_state) : "—"}
                  </td>
                  <td className="dim">
                    {r.regime_day ? (REGIME_LABEL[r.regime_day] ?? r.regime_day) : "—"}
                  </td>
                  <td className="mono ralign">{pct(r.confidence)}</td>
                  <td>
                    <span className={`chip risk-${r.risk_state ?? "missing"}`}>
                      {RISK_VERDICT[r.risk_state] ?? "—"}
                    </span>
                  </td>
                  <td className="mono">
                    {done
                      ? <span className={done.won ? "up" : "down"}>
                          {done.outcome} {signed(done.r_multiple)}R
                        </span>
                      : <span className="dim">open</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/* -------------------------------------------------------- 6. safety */

/** One monitor line. `state` is go | wait | stop | flat. */
function Monitor({ label, state, value, detail }) {
  return (
    <div className={`monitor monitor-${state}`}>
      <i className="dot" />
      <span className="monitor-label">{label}</span>
      <span className="monitor-value mono">{value}</span>
      {detail && <span className="monitor-detail">{detail}</span>}
    </div>
  );
}

export function SafetyMonitor({
  ageState, priceAge, market, quality, scheduler, riskNow, coverage, feed,
}) {
  const AGE_STATE = { live: "go", delayed: "wait", stale: "stop", closed: "flat", unknown: "wait" };
  const AGE_TEXT = {
    live: "Live", delayed: "Delayed", stale: "STALE",
    closed: "Session closed", unknown: "Unknown",
  };

  const optionFinding = (quality?.findings || []).find(
    (f) => f.check === "option_collector_stalled" || f.check === "option_collector_silent");
  const optionCoverage = quality?.options?.coverage;
  const riskState = riskNow?.state ?? "missing";

  const schedulerProblems = scheduler?.problems ?? [];
  const schedulerKnown = scheduler && scheduler.healthy !== undefined;

  return (
    <section className="panel safety-panel" aria-label="Safety monitor">
      <div className="panel-head"><h2>Safety monitor</h2></div>

      <Monitor
        label="Data freshness" state={AGE_STATE[ageState] ?? "wait"}
        value={AGE_TEXT[ageState] ?? ageState}
        detail={Number.isFinite(priceAge) && ageState !== "closed"
          ? `${Math.max(0, Math.round(priceAge))}s behind` : null}
      />
      <Monitor
        label="Market session" state={market?.open ? "go" : "flat"}
        value={market?.session ?? UNAVAILABLE}
      />
      {/* Which source the price on this screen came from. A desk reading a
          four-second age wants to know whether it is looking at the push
          feed or at the poller that took over when the feed went quiet —
          the number alone does not say, and the two fail differently. */}
      <Monitor
        label="Price feed"
        state={!feed ? "wait" : feed.transport === "stream" ? "go" : "wait"}
        value={!feed ? UNAVAILABLE : feed.live_price_source?.toUpperCase()}
        detail={!feed ? null
          : feed.transport === "stream" ? "streaming"
          : feed.angel?.enabled ? "polling — Angel feed is quiet"
          : "polling"}
      />
      <Monitor
        label="Option collector"
        state={optionFinding ? (optionFinding.severity === "error" ? "stop" : "wait") : "go"}
        value={optionFinding ? optionFinding.severity.toUpperCase() : "Collecting"}
        detail={optionFinding ? optionFinding.check.replace(/_/g, " ") : null}
      />
      <Monitor
        label="Scheduler"
        state={!schedulerKnown ? "wait" : scheduler.healthy ? "go" : "stop"}
        value={!schedulerKnown ? UNAVAILABLE : scheduler.healthy ? "Healthy" : "DEGRADED"}
        detail={schedulerProblems.length ? schedulerProblems[0] : null}
      />
      {/* The verdict only. The reasons are spelled out once, in the
          decision panel, and repeating them here would put the same
          sentence on screen twice under two different headings. */}
      <Monitor
        label="Risk manager" state={RISK_TONE[riskState] ?? "stop"}
        value={RISK_VERDICT[riskState] ?? RISK_VERDICT.missing}
        detail={riskNow?.reasons?.length
          ? `${riskNow.reasons.length} blocking condition(s)` : null}
      />
      <Monitor
        label="Option coverage"
        state={optionCoverage === undefined ? "wait"
          : optionCoverage >= 90 ? "go" : optionCoverage >= 50 ? "wait" : "stop"}
        value={optionCoverage === undefined ? UNAVAILABLE : `${optionCoverage.toFixed(1)}%`}
        detail={quality?.options?.verdict ?? null}
      />
      <Monitor
        label="Index archive"
        state={coverage?.rows ? "go" : "wait"}
        value={coverage?.rows
          ? `${coverage.rows.toLocaleString("en-IN")} bars` : UNAVAILABLE}
        detail={coverage?.sessions ? `${coverage.sessions} sessions` : null}
      />

      {(quality?.findings || [])
        .filter((f) => f.severity === "error" || f.severity === "warning")
        .slice(0, 3)
        .map((f, i) => (
          <p key={i} className={`safety-finding sev-${f.severity}`}>
            <b>{f.severity}</b> {f.summary}
          </p>
        ))}
    </section>
  );
}

/* ---------------------------------------------------------- 7. news */

/* Tone is shown as a word and a colour, never as a bare number. The score is
   a lexicon count over a headline, and rendering it as "0.62" would lend it
   a precision it does not have. */
const SENTIMENT_LABEL = {
  positive: "Positive", negative: "Negative", neutral: "Neutral",
};

export function NewsPanel({ news }) {
  const items = news?.items ?? [];
  const tone = news?.sentiment ?? null;

  if (!news || !news.available || !items.length) {
    return (
      <section className="panel news-panel" aria-label="News and sentiment">
        <div className="panel-head">
          <h2>News &amp; sentiment</h2>
          <span className="panel-note">unavailable</span>
        </div>
        <div className="placeholder">
          <p className="placeholder-title">No headlines right now</p>
          <p className="muted-body">
            {news?.note ??
              "The news feeds did not answer. This panel is context only — " +
              "nothing else on the desk depends on it."}
          </p>
          {/* Which publisher failed and why. A dark panel that will not say
              what went wrong is indistinguishable from a quiet news day. */}
          {(news?.sources || []).some((s) => s.problem) && (
            <ul className="source-status">
              {news.sources.filter((s) => s.problem).map((s, i) => (
                <li key={i}><b>{s.source}</b> {s.problem}</li>
              ))}
            </ul>
          )}
        </div>
      </section>
    );
  }

  return (
    <section className="panel news-panel" aria-label="News and sentiment">
      <div className="panel-head">
        <h2>News &amp; sentiment</h2>
        <span className="panel-note mono">{items.length} headlines</span>
      </div>

      {tone && (
        <div className={`tone-bar tone-${tone.label}`}>
          <span className="tone-label">
            {SENTIMENT_LABEL[tone.label] ?? tone.label}
          </span>
          {/* Counts rather than the mean. "7 up, 5 down, 12 flat" is a
              reading a person can check against the list below; a single
              averaged decimal is not. */}
          <span className="tone-counts mono">
            <span className="up">{tone.counts.positive}+</span>
            <span className="down">{tone.counts.negative}−</span>
            <span className="dim">{tone.counts.neutral}=</span>
          </span>
          <span className="tone-note dim">
            {tone.scored} of {tone.total} scored
          </span>
        </div>
      )}

      <ul className="news-list">
        {items.map((n) => (
          <li key={n.id} className={`news-item sent-${n.sentiment}`}>
            <div className="news-meta mono">
              <span className="news-source">{n.source}</span>
              <span>{istTime(n.published_at)}</span>
              {n.label && <span className="news-label">{n.label}</span>}
              <span className={`news-sent sent-${n.sentiment}`}
                    title={n.sentiment_reason}>
                {SENTIMENT_LABEL[n.sentiment] ?? n.sentiment}
              </span>
            </div>
            {/* The link opens the publisher's own page. `noreferrer` because
                the desk should not announce itself to every site it reads. */}
            {n.url
              ? <a className="news-headline" href={n.url} target="_blank"
                   rel="noreferrer noopener">{n.headline}</a>
              : <p className="news-headline">{n.headline}</p>}
          </li>
        ))}
      </ul>

      <p className="news-foot muted">
        Tone is a lexicon count over the headline only — not a view on NIFTY,
        and not an input to any signal. Hover a tag to see which words scored
        it.
      </p>
    </section>
  );
}

/* ------------------------------------------------- 8. market overview */

export function MarketOverview({ signal, chain, regime, vix, price }) {
  const ctx = signal?.context ?? {};
  const sum = chain?.summary ?? ctx.option_chain ?? null;
  const indiaVix = vix ?? ctx.india_vix ?? null;
  const fvgs = (ctx.unfilled_fvgs || []).length;
  const pools = ctx.liquidity_pools || [];

  return (
    <section className="panel overview-panel" aria-label="Market overview">
      <div className="panel-head">
        <h2>Market overview</h2>
        <span className="panel-note mono">
          {sum?.atm_strike ? `ATM ${num(sum.atm_strike, 0)}` : ""}
        </span>
      </div>
      <div className="overview-grid">
        <Cell label="VWAP" value={num(ctx.vwap)} />
        <Cell label="ATR 14" value={num(ctx.atr14)} />
        <Cell
          label="Trend" mono={false}
          value={ctx.trend ?? UNAVAILABLE}
          tone={ctx.trend === "bullish" ? "up" : ctx.trend === "bearish" ? "down" : ""}
        />
        {/* Regime is deliberately absent here. The decision chain states it
            and the regime panel explains it; a third copy in a numbers grid
            would be the same word in three places on one screen. */}
        <Cell label="PCR (OI)" value={sum ? num(sum.pcr_oi) : UNAVAILABLE} />
        <Cell label="Max pain" value={sum ? num(sum.max_pain, 0) : UNAVAILABLE} />
        <Cell
          label="Chain reads" mono={false}
          value={sum?.bias ?? UNAVAILABLE}
          tone={sum?.bias === "bullish" ? "up" : sum?.bias === "bearish" ? "down" : ""}
        />
        <Cell label="India VIX" value={num(indiaVix)} />
        <Cell label="Unfilled gaps" value={fvgs} />
        <Cell
          label="IV skew"
          value={sum && Number.isFinite(sum.iv_skew) ? num(sum.iv_skew) : UNAVAILABLE}
        />
      </div>

      <div className="levels-columns">
        <div>
          <p className="eyebrow">Resistance</p>
          <ul className="level-list">
            {(sum?.resistance_strikes || []).length
              ? sum.resistance_strikes.map((s, i) => (
                  <li key={i} className="lvl-res mono">{num(s, 0)}</li>))
              : <li className="dim">{UNAVAILABLE}</li>}
          </ul>
        </div>
        <div>
          <p className="eyebrow">Support</p>
          <ul className="level-list">
            {(sum?.support_strikes || []).length
              ? sum.support_strikes.map((s, i) => (
                  <li key={i} className="lvl-sup mono">{num(s, 0)}</li>))
              : <li className="dim">{UNAVAILABLE}</li>}
          </ul>
        </div>
        <div>
          <p className="eyebrow">Liquidity</p>
          <ul className="level-list">
            {pools.length
              ? pools.slice(0, 4).map((p, i) => (
                  <li key={i} className={`mono${p.swept ? " swept" : ""}`}>
                    {p.side} {num(p.level, 0)}
                  </li>))
              : <li className="dim">{UNAVAILABLE}</li>}
          </ul>
        </div>
      </div>
    </section>
  );
}
