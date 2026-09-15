/* The dashboard against the desk's own live responses.

   Every other test here uses a fixture, and a fixture is a statement about
   what the author believed the API returns. This one replays payloads
   captured from the running backend, so a field the frontend reads but the
   backend never sends — or spells differently — fails here rather than
   appearing as a blank panel on a trading screen.

   That gap is not hypothetical: this suite caught the chart looking for
   `fvg.mid` when the engine emits `fvg.midpoint`, which rendered as silence.

   Refresh the fixtures with scripts/capture_live_payloads.py when the API
   changes shape. If they go missing the file skips rather than fails — a
   checkout without a running backend should still be able to run the suite.
*/
import { act, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import fs from "node:fs";
import path from "node:path";

import App from "./App.jsx";

const DIR = path.resolve(__dirname, "../fixtures/live");
const present = fs.existsSync(path.join(DIR, "signals_live.json"));
const read = (name) =>
  JSON.parse(fs.readFileSync(path.join(DIR, `${name}.json`), "utf8"));

/* Routes the app's `fetch` calls onto the captured bodies. Anything the app
   asks for that was never captured resolves empty rather than throwing, so a
   new request shows up as a blank panel here instead of a crashed suite. */
function routeFetch() {
  const table = [
    ["/signals/live", "signals_live"], ["/signals/history", "history"],
    ["/signals/outcomes", "outcomes"], ["/market/status", "market_status"],
    ["/market/price", "market_price"], ["/market/regime", "market_regime"],
    ["/market/candles", "candles"], ["/market/option-chain", "chain"],
    ["/market/vix", "vix"], ["/data/quality", "quality"],
    ["/data/coverage", "coverage"], ["/health/scheduler", "scheduler"],
    ["/news", "news"],
  ];
  return vi.fn((url) => {
    const hit = table.find(([prefix]) => String(url).includes(prefix));
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve(hit ? read(hit[1]) : {}),
    });
  });
}

class DeadSocket {
  constructor() { setTimeout(() => this.onclose?.(), 0); }
  close() {}
}

/* Recharts' ResponsiveContainer observes its box, and jsdom has no
   ResizeObserver. The other suites never reach this because they render no
   candles; this one does, which is the point of it. The stub reports nothing,
   so the chart mounts and lays out at zero — enough to prove it does not
   throw on real data, which is all a chart can be asserted on here. */
class NoopResizeObserver {
  observe() {} unobserve() {} disconnect() {}
}

describe.skipIf(!present)("the terminal against live payloads", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.stubGlobal("ResizeObserver", NoopResizeObserver);
    vi.stubGlobal("WebSocket", DeadSocket);
    vi.stubGlobal("fetch", routeFetch());
  });
  afterEach(() => {
    vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks();
  });

  /* The desk prefers its socket and only falls back to polling after two
     failed attempts and their backoff — which is the path a browser takes
     when the backend is up but the socket is not. Advancing the clock here
     exercises exactly that, rather than reaching past it. */
  async function mount() {
    render(<App />);
    for (const step of [0, 1_100, 2_200, 0]) {
      await act(async () => { vi.advanceTimersByTime(step); });
      await act(async () => { await Promise.resolve(); });
    }
  }

  it("renders the top bar from the real price and VIX", async () => {
    await mount();
    const price = read("market_price").price;
    if (price) {
      expect(screen.getAllByText(
        price.toLocaleString("en-IN", { minimumFractionDigits: 2,
                                        maximumFractionDigits: 2 })).length)
        .toBeGreaterThan(0);
    }
    expect(screen.getByText("NIFTY 50")).toBeTruthy();
    expect(screen.getByText("INDIA VIX")).toBeTruthy();
  });

  it("fills the performance strip from the real outcome study", async () => {
    await mount();
    const overall = read("outcomes").overall;
    expect(screen.getByText("Trades")).toBeTruthy();
    expect(screen.getAllByText(String(overall.n)).length).toBeGreaterThan(0);
    // The averages are derived from the per-signal rows, so a backend that
    // stopped sending them would show "Unavailable" here.
    expect(screen.queryAllByText("Unavailable").length).toBeLessThan(12);
  });

  it("renders the decision chain from the real plan", async () => {
    await mount();
    // Scoped: the feed table has its own "Regime" and "Bias" column headers,
    // which are a different thing from the decision's own labels.
    const decision = within(screen.getByLabelText("Active decision"));
    expect(decision.getByText("Regime")).toBeTruthy();
    expect(decision.getByText("Bias")).toBeTruthy();
    expect(decision.getByText("Entry state")).toBeTruthy();
    // Whatever the desk currently reads, it is one of the known labels and
    // never a raw enum leaking through.
    expect(screen.queryByText("BULLISH")).toBeNull();
    expect(screen.queryByText("WAIT_PULLBACK")).toBeNull();
  });

  it("populates the signal feed with the real journal", async () => {
    await mount();
    const rows = read("history");
    expect(screen.getByText(/\d+ recent/)).toBeTruthy();
    expect(rows.length).toBeGreaterThan(0);
    // The fields the redesign added to /signals/history must actually be
    // there — this is the assertion that would have caught them missing.
    expect(rows.some((r) => "bias" in r && "entry_state" in r
      && "regime_day" in r && "risk_state" in r)).toBe(true);
  });

  it("reports real safety state, including a degraded subsystem", async () => {
    await mount();
    for (const label of ["Data freshness", "Option collector", "Scheduler",
                         "Option coverage", "Index archive"]) {
      expect(screen.getByText(label)).toBeTruthy();
    }
    const cov = read("quality").options?.coverage;
    if (cov !== undefined) {
      expect(screen.getByText(`${cov.toFixed(1)}%`)).toBeTruthy();
    }
  });

  it("shows the market overview from the real option chain", async () => {
    await mount();
    const s = read("chain").summary;
    expect(screen.getByText("PCR (OI)")).toBeTruthy();
    if (s?.max_pain) {
      expect(screen.getAllByText(
        s.max_pain.toLocaleString("en-IN", { maximumFractionDigits: 0 })).length)
        .toBeGreaterThan(0);
    }
  });

  it("builds the strike ladder from the real chain", async () => {
    /* The ladder reads per-strike fields the aggregates above never touch:
       call_ltp, put_ltp, call_oi_change, call_iv. A backend that renamed
       any of them would leave the PCR intact and the ladder full of
       dashes, which is exactly the silent-blank failure this file exists
       to catch. */
    await mount();
    const chain = read("chain");
    expect(screen.getByText("Strike ladder")).toBeTruthy();
    expect(screen.getByText(new RegExp(`of ${chain.strikes.length} strikes`))).toBeTruthy();

    // Named by its caption — the desk renders several tables and this must
    // pick the ladder, not the signal journal.
    const table = within(screen.getByRole("table", { name: /option chain, calls left/i }));
    const near = [...chain.strikes]
      .sort((a, b) => Math.abs(a.strike - chain.summary.spot)
                    - Math.abs(b.strike - chain.summary.spot))[0];
    expect(table.getAllByText(
      Math.round(near.strike).toLocaleString("en-IN")).length).toBeGreaterThan(0);
    expect(table.getAllByText(
      near.call_ltp.toLocaleString("en-IN",
        { minimumFractionDigits: 2, maximumFractionDigits: 2 })).length)
      .toBeGreaterThan(0);

    // The capture is a polled NSE snapshot, so both optional columns are
    // carried and must be drawn.
    expect(screen.getAllByText("ΔOI")).toHaveLength(2);
    expect(screen.getAllByText("IV")).toHaveLength(2);
  });

  it("renders real headlines with real tone readings", async () => {
    await mount();
    const news = read("news");
    if (news.available && news.items.length) {
      expect(screen.getByText(news.items[0].headline)).toBeTruthy();
      expect(screen.getByText(/\d+ headlines/)).toBeTruthy();
      expect(screen.getByText(/\d+ of \d+ scored/)).toBeTruthy();
    } else {
      expect(screen.getByText(/No headlines right now/)).toBeTruthy();
    }
  });

  it("renders no NaN, undefined or [object Object] anywhere", async () => {
    /* The catch-all. Every panel formats backend values, and a shape change
       usually surfaces as one of these three strings rather than a crash. */
    await mount();
    const text = document.body.textContent;
    expect(text).not.toMatch(/NaN/);
    expect(text).not.toMatch(/undefined/);
    expect(text).not.toMatch(/\[object Object\]/);
  });
});
