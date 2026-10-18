import React from "react";

/* A panel that fails must not take the desk with it.

   On 15-Sep-2026 a single throw inside the price chart — lightweight-charts
   refusing an out-of-order `update()` — unmounted the entire React tree.
   The dashboard went black: no price, no chain, no signals, no risk, no way
   to see that anything was still running. The feed underneath was perfectly
   healthy the whole time. A charting bug had become a total loss of the
   desk, and the only recovery was that the condition cleared itself a few
   minutes later.

   That is the wrong blast radius. The chart is one panel among a dozen and
   the desk is legible without it, so a failure here is contained to the
   panel that failed and every other panel keeps rendering.

   Deliberately *not* silent: it says what broke and offers a retry, because
   a panel that quietly renders nothing is how a broken feed gets mistaken
   for a quiet market — the failure mode this whole platform is built to
   avoid. `onError` is where the detail goes so a report can carry it. */
export default class PanelBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // Kept on the console: this is a real defect and it should be
    // reportable, not swallowed because the page survived it.
    console.error(`${this.props.label ?? "panel"} failed:`, error, info);
    this.props.onError?.(error, info);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="panel panel-failed">
        <h3>{this.props.label ?? "Panel"}</h3>
        <p className="muted-body">
          This panel stopped drawing. The rest of the desk is unaffected and
          the data feed is unchanged.
        </p>
        <p className="panel-failed-why">{String(error.message || error)}</p>
        <button type="button" onClick={() => this.setState({ error: null })}>
          Try again
        </button>
      </div>
    );
  }
}
