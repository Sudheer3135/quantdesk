/* What a live tick costs the browser.

   The App root holds the clock (1/s), the price (~2.5/s) and the pushed
   option chain (up to 4/s). With nothing memoised, every one of those
   re-rendered the entire terminal — the outcome ledger, the signal feed, the
   decision panel and the Recharts open-interest chart included — none of
   which reads the price. On 14-Sep-2026 that showed up as Chrome and
   WindowServer together holding ~60% of a CPU while the dashboard was open.

   These count renders of the panels that should not care. */
import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const renders = {};
const counted = (name, Real) => {
  renders[name] = 0;
  return function Counted(props) {
    renders[name] += 1;
    return <Real {...props} />;
  };
};

vi.mock("./panels.jsx", async (importOriginal) => {
  const real = await importOriginal();
  return {
    ...real,
    DecisionPanel: counted("DecisionPanel", real.DecisionPanel),
    SignalFeed: counted("SignalFeed", real.SignalFeed),
    PerformanceStrip: counted("PerformanceStrip", real.PerformanceStrip),
    NewsPanel: counted("NewsPanel", real.NewsPanel),
  };
});

vi.mock("./OIProfile.jsx", async (importOriginal) => {
  const real = await importOriginal();
  return { ...real, default: counted("OIProfile", real.default) };
});

/* App applies the memo around what it imports (see the note there), so
   the counter goes on the module's plain view and the real memo sits
   outside it — which is the only arrangement where the count reflects
   the memo's decisions rather than App's render rate. */
vi.mock("./OptionChain.jsx", async (importOriginal) => {
  const real = await importOriginal();
  return { ...real, default: counted("OptionChain", real.default) };
});

const { default: App } = await import("./App.jsx");

class FakeSocket {
  static last = null;
  constructor() { FakeSocket.last = this; }
  open() { this.onopen?.(); }
  deliver(m) { this.onmessage?.({ data: JSON.stringify(m) }); }
  close() { this.onclose?.(); }
}

const iso = (ago = 0) => new Date(Date.now() - ago * 1000).toISOString();

const priceOf = (p) => ({
  symbol: "NIFTY", price: p, previous: p - 0.05, change: 0.05, direction: "up",
  source: "angel", transport: "stream", source_time: iso(), at: iso(),
  age_seconds: 0.4, freshness: "live", market_open: true,
});

const strikes = Array.from({ length: 30 }, (_, i) => {
  const k = 23_450 + i * 50;
  return { strike: k, call_oi: 1000 + i, put_oi: 2000 - i, call_ltp: 10, put_ltp: 10 };
});

const snapshot = {
  type: "snapshot",
  market: { open: true, session: "open", server_time: iso() },
  price: priceOf(24_201.05),
  signal: {
    timestamp: iso(60), action: "HOLD", confidence: 0.3, price: 24_200,
    checks: [{ name: "vwap", score: 0.2, weight: 0.16, reason: "x", contribution: 0.03 }],
    context: { vwap: 24_190, atr14: 20, trend: "up" },
  },
  chain: { symbol: "NIFTY", transport: "stream", live: true, fetched_at: iso(),
           strikes, summary: { atm_strike: 24_200, max_pain: 24_200,
                               max_pain_distance_pct: 0.01, pcr_oi: 1 } },
  risk_now: null,
  regime: null,
};

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal("fetch", vi.fn(() =>
    Promise.resolve({ ok: true, json: () => Promise.resolve({}) })));
  for (const k of Object.keys(renders)) renders[k] = 0;
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function settle() {
  render(<App />);
  await act(async () => { FakeSocket.last.open(); });
  await act(async () => { FakeSocket.last.deliver(snapshot); });
  await act(async () => { vi.advanceTimersByTime(50); });

  /* Wait for the lazy panels to actually arrive before sampling.

     `OIProfile` is a dynamic import behind Suspense, so a fixed number
     of timer ticks is a bet on how fast the module graph resolves —
     and that bet is lost whenever the suite is under load. The counter
     then reads `undefined` and the assertion fails with "actual value
     must be number or bigint", which looks like a render-count
     regression and is really a race in this helper. Poll for the
     component instead of guessing at a duration. */
  for (let i = 0; i < 50 && renders.OIProfile === undefined; i += 1) {
    await act(async () => { await Promise.resolve(); });
    await act(async () => { vi.advanceTimersByTime(10); });
  }

  const before = { ...renders };
  return before;
}

const ticks = async (prices) => {
  for (const p of prices) {
    await act(async () => {
      FakeSocket.last.deliver({ type: "price", price: priceOf(p) });
    });
  }
};

describe("a price tick redraws only what shows the price", () => {
  it("leaves the decision, the feed, the strip and the news alone", async () => {
    const before = await settle();
    expect(before.DecisionPanel).toBeGreaterThan(0);   // they did render once

    await ticks([24_201.10, 24_201.35, 24_202.00, 24_201.80, 24_202.45]);

    for (const name of ["DecisionPanel", "SignalFeed", "PerformanceStrip", "NewsPanel"]) {
      expect(renders[name], `${name} re-rendered on a price tick`).toBe(before[name]);
    }
  });

  it("does not redraw the open-interest chart for a move inside one strike band", async () => {
    // Its picture depends on the spot only through which strikes are nearest
    // and which sit above it — lines 25 points apart. A 1.4-point wobble
    // changes neither, and Recharts is the costliest thing on the page.
    const before = await settle();
    expect(before.OIProfile).toBeGreaterThan(0);

    await ticks([24_201.10, 24_201.90, 24_202.45]);
    expect(renders.OIProfile).toBe(before.OIProfile);
  });

  it("does not redraw the strike ladder for a move inside one strike band", async () => {
    /* The densest panel on the desk — up to eighty cells of streaming
       premium — and until this guard it redrew on every price tick,
       sorting forty strikes twice each time to produce the identical
       list. What the spot decides here is which strikes are listed, in
       what order, and which side is in the money; a 1.4-point wobble
       changes none of the three. */
    const before = await settle();
    expect(before.OptionChain).toBeGreaterThan(0);

    await ticks([24_201.10, 24_201.90, 24_202.45]);
    expect(renders.OptionChain).toBe(before.OptionChain);
  });

  it("does redraw the strike ladder when price crosses a strike", async () => {
    /* The other half of the guard. Suppressing a redraw that shows
       something new is a far worse bug than the cost it saves, so the
       crossing case is pinned too. */
    const before = await settle();
    await ticks([24_196.00]);
    expect(renders.OptionChain).toBeGreaterThan(before.OptionChain);
  });

  it("does redraw the open-interest chart when price crosses a strike", async () => {
    const before = await settle();
    await ticks([24_196.00]);                          // below 24,200
    expect(renders.OIProfile).toBeGreaterThan(before.OIProfile);
  });
});

describe("the one-second clock redraws only what shows time", () => {
  it("leaves the decision, the feed, the strip and the news alone", async () => {
    const before = await settle();
    await act(async () => { vi.advanceTimersByTime(3_000); });
    for (const name of ["DecisionPanel", "SignalFeed", "PerformanceStrip", "NewsPanel"]) {
      expect(renders[name], `${name} re-rendered on a clock tick`).toBe(before[name]);
    }
  });
});
