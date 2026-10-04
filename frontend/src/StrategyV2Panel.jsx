/* Strategy v2 on paper.

   Presentation only, like every other panel: it renders the state the
   backend published and derives nothing that could be mistaken for a
   trading view of its own. The word "paper" is on the panel head and on the
   position, because a simulated P&L that could be read as a real one is the
   single most expensive misunderstanding this screen could cause. */
import { memo } from "react";

import { UNAVAILABLE, istTime, num, signed } from "./format.js";

const CODE_LABEL = {
  entered: "Entered",
  no_directional_signal: "No direction",
  signal_has_no_levels: "No levels",
  signal_too_old: "Signal too old",
  entry_state_not_ready: "Entry not ready",
  bias_disagrees_with_action: "Bias disagrees",
  outside_entry_window: "Outside entry window",
  expiry_day: "Expiry day",
  position_already_open: "Position open",
  kill_switch_engaged: "Kill switch",
  live_feed_not_healthy: "Feed not live",
  no_live_index_price: "No index price",
  index_past_stop: "Index past stop",
  index_past_target: "Index past target",
  vix_history_insufficient: "VIX history missing",
  vix_live_unavailable: "No live VIX",
  vix_percentile_too_high: "VIX too high",
  vix_spiking: "VIX spiking",
  no_expiry_far_enough: "No expiry",
  chain_not_ready: "Chain not ready",
  implied_volatility_unsolvable: "No IV",
  no_strike_in_delta_band: "No strike in delta band",
  quote_too_old: "Quote stale",
  no_two_sided_quote: "No two-sided quote",
  spread_too_wide: "Spread too wide",
  premium_below_floor: "Premium too low",
  premium_risk_not_defined: "Risk undefined",
  risk_manager_vetoed: "Risk veto",
};

const EXIT_LABEL = {
  premium_stop: "Premium stop", index_stop: "Index stop",
  premium_target: "Premium target", index_target: "Index target",
  session_end: "Session end", time_limit: "Time limit", manual: "Closed by hand",
  closed_after_restart: "Closed after restart",
};

const rupees = (v) => (v === null || v === undefined ? "—" : `₹${signed(v, 0)}`);
const tone = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");

function Gate({ label, ok, value, detail }) {
  const state = ok === null || ok === undefined ? "wait" : ok ? "go" : "stop";
  return (
    <div className={`monitor monitor-${state}`}>
      <i className="dot" />
      <span className="monitor-label">{label}</span>
      <span className="monitor-value mono">{value}</span>
      {detail && <span className="monitor-detail">{detail}</span>}
    </div>
  );
}

function Position({ position }) {
  const live = position.live || {};
  const unrealised = live.unrealised;
  return (
    <div className="v2-position" aria-label="Open paper position">
      <div className="v2-position-head">
        <strong className="mono">{position.contract}</strong>
        <span className="pill tone-wait">PAPER</span>
      </div>
      <div className="v2-grid">
        <div className="cell"><span className="cell-label">Size</span>
          <span className="cell-value mono">{position.lots} lot × {position.lot_size}</span></div>
        <div className="cell"><span className="cell-label">Entry</span>
          <span className="cell-value mono">{num(position.premium_entry)}</span></div>
        <div className="cell"><span className="cell-label">Bid now</span>
          <span className="cell-value mono">{live.bid === null || live.bid === undefined
            ? UNAVAILABLE : num(live.bid)}</span></div>
        <div className={`cell${unrealised !== undefined ? ` tone-${tone(unrealised)}` : ""}`}>
          <span className="cell-label">Unrealised</span>
          <span className="cell-value mono">
            {unrealised === undefined ? UNAVAILABLE
              : `${rupees(unrealised)} (${signed(live.r, 2)}R)`}
          </span></div>
        <div className="cell"><span className="cell-label">Stop / target</span>
          <span className="cell-value mono">
            {num(position.premium_stop)} / {num(position.premium_target)}</span></div>
        <div className="cell"><span className="cell-label">Index stop / target</span>
          <span className="cell-value mono">
            {num(position.index_stop)} / {num(position.index_target)}</span></div>
      </div>
      <p className="panel-note">
        Opened {istTime(position.opened_at, true)} · risk ₹{num(position.risk_amount, 0)}
      </p>
    </div>
  );
}

function StrategyV2Panel({ state }) {
  if (!state) {
    return (
      <section className="panel v2-panel" aria-label="Strategy v2">
        <div className="panel-head">
          <h2>Strategy v2 · paper</h2>
          <span className="panel-note">no state yet</span>
        </div>
        <p className="muted-body">Waiting for the paper trader to report.</p>
      </section>
    );
  }

  const { account = {}, gates = {}, position, last_decision: last } = state;
  const vix = gates.vix || {};
  const counts = Object.entries(state.decisions_today || {})
    .sort((a, b) => b[1] - a[1]).slice(0, 4);

  return (
    <section className="panel v2-panel" aria-label="Strategy v2">
      <div className="panel-head">
        <h2>Strategy v2 · paper</h2>
        <span className="panel-note mono">
          {state.running ? "running" : "stopped"} · simulated ₹{num(account.starting_capital, 0)}
        </span>
      </div>

      <div className="v2-grid">
        <div className="cell"><span className="cell-label">Equity</span>
          <span className="cell-value mono">₹{num(account.equity, 0)}</span></div>
        <div className={`cell tone-${tone(account.realised_today)}`}>
          <span className="cell-label">Today</span>
          <span className="cell-value mono">{rupees(account.realised_today)}</span></div>
        <div className="cell"><span className="cell-label">Trades today</span>
          <span className="cell-value mono">
            {account.trades_today ?? "—"} / {account.max_trades_per_day ?? "—"}</span></div>
        <div className={`cell${account.consecutive_losses ? " tone-wait" : ""}`}>
          <span className="cell-label">Loss streak</span>
          <span className="cell-value mono">{account.consecutive_losses ?? "—"}</span></div>
      </div>

      {position ? <Position position={position} /> : (
        <div className="v2-idle">
          <span className="cell-label">No open position</span>
          {last ? (
            <p className="muted-body">
              <b>{CODE_LABEL[last.code] ?? last.code}</b>
              {last.action ? ` on ${last.action}` : ""} at {istTime(last.at)}
              {last.detail ? ` — ${last.detail}` : ""}
            </p>
          ) : <p className="muted-body">No signal considered yet this session.</p>}
        </div>
      )}

      <Gate label="India VIX" ok={vix.ok}
        value={vix.live === null || vix.live === undefined ? UNAVAILABLE : num(vix.live)}
        detail={vix.ok ? `${num(vix.percentile, 0)}th pct · ${signed(vix.spike_pct, 1)}% on day`
          : (CODE_LABEL[vix.code] ?? vix.detail)} />
      <Gate label="Expiry" ok={gates.expiry ? !gates.expiry_day : null}
        value={gates.expiry ?? UNAVAILABLE}
        detail={gates.expiry_day ? "expiry day — no new entries" : null} />
      <Gate label="Entry window" ok={gates.market_open ? gates.entry_window : null}
        value={gates.entry_window ? "Open" : "Closed"} />
      <Gate label="Live quotes" ok={gates.feed_live && gates.chain_quotes > 0}
        value={`${gates.chain_quotes ?? 0} contracts`}
        detail={gates.feed_live ? null : "Angel feed not live"} />
      <Gate label="Kill switch" ok={!gates.kill_switch} value={gates.kill_switch ? "ON" : "Off"} />

      {counts.length > 0 && (
        <p className="panel-note v2-counts">
          Today: {counts.map(([code, n]) => `${CODE_LABEL[code] ?? code} ${n}`).join(" · ")}
        </p>
      )}
    </section>
  );
}

export { CODE_LABEL, EXIT_LABEL };
export default memo(StrategyV2Panel);
