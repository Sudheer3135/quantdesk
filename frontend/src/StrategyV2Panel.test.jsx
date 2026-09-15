/* Strategy v2's panel: paper must read as paper, and absence as absence. */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import StrategyV2Panel from "./StrategyV2Panel.jsx";

const base = (over = {}) => ({
  strategy: "v2", mode: "paper", running: true, at: "2026-09-17T05:00:00Z",
  account: { starting_capital: 350000, equity: 351234.5, realised_total: 1234.5,
             realised_today: -420, trades_today: 1, consecutive_losses: 1,
             max_trades_per_day: 2 },
  position: null,
  gates: { market_open: true, entry_window: true, expiry_day: false,
           expiry: "2026-09-22", chain_quotes: 42, feed_live: true, kill_switch: false,
           vix: { ok: true, code: "ok", live: 12.3, percentile: 41, spike_pct: 2.5 } },
  last_decision: { at: "2026-09-17T05:00:00Z", action: "BUY", outcome: "rejected",
                   code: "vix_spiking", detail: "VIX is up 12% on the day" },
  decisions_today: { no_directional_signal: 12, vix_spiking: 1 },
  ...over,
});

describe("StrategyV2Panel", () => {
  it("says it is waiting rather than inventing an account", () => {
    render(<StrategyV2Panel state={null} />);
    expect(screen.getByText(/waiting for the paper trader/i)).toBeTruthy();
    expect(screen.queryByText(/₹/)).toBeNull();
  });

  it("labels the account as simulated and shows the last refusal in words", () => {
    render(<StrategyV2Panel state={base()} />);
    expect(screen.getByText("Strategy v2 · paper")).toBeTruthy();
    expect(screen.getByText(/simulated ₹3,50,000/)).toBeTruthy();
    expect(screen.getByText("VIX spiking")).toBeTruthy();
    expect(screen.getByText(/VIX is up 12% on the day/)).toBeTruthy();
    expect(screen.getByText("₹−420")).toBeTruthy();
  });

  it("marks an open position PAPER and shows its live P&L", () => {
    const position = {
      id: 7, contract: "NIFTY 22SEP26 24050 CE", lots: 2, lot_size: 65,
      premium_entry: 132.5, premium_stop: 110.2, premium_target: 180.1,
      index_stop: 23950, index_target: 24110, risk_amount: 2899,
      opened_at: "2026-09-17T05:00:00Z",
      live: { bid: 140.0, unrealised: 975, r: 0.34 },
    };
    render(<StrategyV2Panel state={base({ position })} />);
    expect(screen.getByText("NIFTY 22SEP26 24050 CE")).toBeTruthy();
    expect(screen.getByText("PAPER")).toBeTruthy();
    expect(screen.getByText(/₹\+975 \(\+0\.34R\)/)).toBeTruthy();
  });

  it("never renders a missing bid as a number", () => {
    const position = {
      id: 7, contract: "NIFTY 22SEP26 24050 CE", lots: 1, lot_size: 65,
      premium_entry: 132.5, premium_stop: 110.2, premium_target: 180.1,
      index_stop: 23950, index_target: 24110, risk_amount: 1450,
      opened_at: "2026-09-17T05:00:00Z", live: { bid: null },
    };
    render(<StrategyV2Panel state={base({ position })} />);
    expect(screen.getAllByText("Unavailable").length).toBeGreaterThanOrEqual(2);
  });

  it("shows a blocked VIX gate with its reason and no live value it lacks", () => {
    const gates = { ...base().gates,
      vix: { ok: false, code: "vix_history_insufficient", live: null } };
    render(<StrategyV2Panel state={base({ gates })} />);
    expect(screen.getByText("VIX history missing")).toBeTruthy();
  });
});
