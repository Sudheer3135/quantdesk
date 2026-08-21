/* The dashboard's half of the latency fix.

   Two claims are worth testing here and neither can be tested on the
   backend: that a pushed price repaints the screen with nobody clicking
   anything, and that the age beside it is the age of the *price* rather
   than the age of the last frame of any kind.

   The second one is the subtle one. A heartbeat every twenty seconds used
   to refresh a shared "updated" clock, so a ticker that had been dead for
   ten minutes still read "3s ago" — the socket was alive, so the desk
   claimed the data was. */
import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App, { classifyAge, formatAge } from "./App.jsx";

/* A WebSocket the test drives by hand. */
class FakeSocket {
  static last = null;

  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    FakeSocket.last = this;
  }

  open() {
    this.readyState = 1;
    this.onopen?.();
  }

  deliver(message) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }

  close() {
    this.readyState = 3;
    this.onclose?.();
  }
}

const iso = (offsetSeconds) =>
  new Date(Date.now() - offsetSeconds * 1000).toISOString();

const priceFrame = (price, ageSeconds) => ({
  type: "price",
  price: {
    symbol: "NIFTY", price, previous: price - 1, change: 1,
    direction: "up", source: "yahoo",
    source_time: iso(ageSeconds), at: iso(ageSeconds),
    age_seconds: ageSeconds, freshness: "live", market_open: true,
  },
});

const signalFrame = (timestamp) => ({
  type: "signal",
  signal: {
    timestamp, action: "BUY", confidence: 0.61, price: 24_200,
    entry: 24_200, stop_loss: 24_150, target: 24_320, risk_reward: 2.4,
    checks: [], context: { vwap: 24_190, atr14: 32, trend: "up" },
  },
  market: { open: true, session: "open", server_time: iso(0) },
});

let fetchMock;

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  FakeSocket.last = null;
  vi.stubGlobal("WebSocket", FakeSocket);
  fetchMock = vi.fn(() =>
    Promise.resolve({ ok: true, json: () => Promise.resolve({}) }));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/* Advance both the fake clock and React's work queue. */
async function tick(ms) {
  await act(async () => {
    vi.advanceTimersByTime(ms);
  });
}

describe("live price delivery", () => {
  it("shows a pushed price without any refresh or fetch", async () => {
    render(<App />);
    await act(async () => { FakeSocket.last.open(); });

    expect(screen.queryByText("24,223.35")).toBeNull();

    await act(async () => {
      FakeSocket.last.deliver(priceFrame(24_223.35, 2));
    });

    expect(screen.getByText("24,223.35")).toBeTruthy();

    // The price arrived on its own. Candles and the option chain have their
    // own slow loop and are allowed to fetch; what must never be requested
    // is the price or the signal, because those are pushed.
    const requested = fetchMock.mock.calls.map(([url]) => url);
    expect(requested.some((url) => url.includes("/market/price"))).toBe(false);
    expect(requested.some((url) => url.includes("/signals/live"))).toBe(false);
  });

  it("repaints on each subsequent push", async () => {
    render(<App />);
    await act(async () => { FakeSocket.last.open(); });

    for (const value of [24_223.35, 24_225.1, 24_219.85]) {
      await act(async () => {
        FakeSocket.last.deliver(priceFrame(value, 1));
      });
    }

    expect(screen.getByText("24,219.85")).toBeTruthy();
    expect(screen.queryByText("24,223.35")).toBeNull();
  });

  it("renders the price before any signal has ever arrived", async () => {
    render(<App />);
    await act(async () => { FakeSocket.last.open(); });
    await act(async () => {
      FakeSocket.last.deliver(priceFrame(24_223.35, 2));
    });

    // The tape used to be gated behind the first signal, so a working feed
    // showed nothing for up to five minutes after a restart.
    expect(screen.getByText("24,223.35")).toBeTruthy();
    expect(screen.getByText(/awaiting first signal/i)).toBeTruthy();
  });
});

describe("stale data detection", () => {
  it("calls a fresh price live", async () => {
    render(<App />);
    await act(async () => { FakeSocket.last.open(); });
    await act(async () => { FakeSocket.last.deliver(priceFrame(24_223.35, 2)); });

    expect(screen.getByText(/data live/i)).toBeTruthy();
  });

  it("ages a price that stops arriving, with no new frames at all", async () => {
    render(<App />);
    await act(async () => { FakeSocket.last.open(); });
    await act(async () => { FakeSocket.last.deliver(priceFrame(24_223.35, 1)); });

    expect(screen.getByText(/data live/i)).toBeTruthy();

    await tick(30_000);
    await waitFor(() => expect(screen.getByText(/data delayed/i)).toBeTruthy());

    await tick(120_000);
    await waitFor(() => expect(screen.getByText(/data stale/i)).toBeTruthy());

    // The price itself is still on screen — it is labelled, not hidden.
    expect(screen.getByText("24,223.35")).toBeTruthy();
  });

  it("a heartbeat does not make a stale price look fresh", async () => {
    render(<App />);
    await act(async () => { FakeSocket.last.open(); });
    await act(async () => { FakeSocket.last.deliver(priceFrame(24_223.35, 1)); });

    await tick(150_000);
    await waitFor(() => expect(screen.getByText(/data stale/i)).toBeTruthy());

    // Heartbeats keep arriving because the socket is perfectly healthy.
    for (let i = 0; i < 5; i += 1) {
      await act(async () => {
        FakeSocket.last.deliver({
          type: "heartbeat",
          market: { open: true, session: "open", server_time: iso(0) },
        });
      });
    }

    // This is the regression that mattered: the socket is alive, the feed
    // is not, and the desk must say the feed is not.
    expect(screen.getByText(/data stale/i)).toBeTruthy();
    expect(screen.queryByText(/data live/i)).toBeNull();
  });

  it("says so when the source gave no timestamp", async () => {
    render(<App />);
    await act(async () => { FakeSocket.last.open(); });

    const frame = priceFrame(24_223.35, 2);
    frame.price.source_time = null;
    await act(async () => { FakeSocket.last.deliver(frame); });

    expect(screen.getByText(/data age unknown/i)).toBeTruthy();
    expect(screen.queryByText(/data live/i)).toBeNull();
  });
});

describe("price and signal are independent", () => {
  it("keeps the signal time fixed while the price keeps moving", async () => {
    render(<App />);
    await act(async () => { FakeSocket.last.open(); });

    const signalAt = new Date("2026-08-20T04:50:00Z").toISOString();  // 10:20 IST
    await act(async () => { FakeSocket.last.deliver(signalFrame(signalAt)); });

    expect(screen.getByText("10:20 am")).toBeTruthy();

    // Four minutes of price ticks underneath the same signal.
    for (const value of [24_223.35, 24_225.1, 24_219.85]) {
      await act(async () => {
        FakeSocket.last.deliver(priceFrame(value, 1));
      });
      await tick(1_000);
    }

    // The acceptance test: the current price has moved on, the signal has not.
    expect(screen.getByText("24,219.85")).toBeTruthy();
    expect(screen.getByText("10:20 am")).toBeTruthy();
    expect(screen.getByText(/data live/i)).toBeTruthy();
  });

  it("does not present the current price as the signal's price", async () => {
    render(<App />);
    await act(async () => { FakeSocket.last.open(); });

    const signalAt = new Date("2026-08-20T04:50:00Z").toISOString();
    await act(async () => { FakeSocket.last.deliver(signalFrame(signalAt)); });
    await act(async () => { FakeSocket.last.deliver(priceFrame(24_223.35, 2)); });

    // Two distinct labels, two distinct numbers.
    expect(screen.getByText(/current price/i)).toBeTruthy();
    expect(screen.getByText(/last signal/i)).toBeTruthy();
    expect(screen.getByText("24,223.35")).toBeTruthy();
  });
});

describe("clock handling", () => {
  it("uses the server clock, so a skewed browser still reads correctly", async () => {
    // This machine's clock is four minutes fast. Naive arithmetic would
    // report a live feed as four minutes stale.
    const skewMs = 4 * 60 * 1000;
    const realNow = Date.now();
    vi.setSystemTime(new Date(realNow + skewMs));

    render(<App />);
    await act(async () => { FakeSocket.last.open(); });

    const serverNow = new Date(realNow).toISOString();
    await act(async () => {
      FakeSocket.last.deliver({
        type: "price",
        price: {
          symbol: "NIFTY", price: 24_223.35, previous: 24_222, change: 1.35,
          direction: "up", source: "yahoo",
          source_time: serverNow, at: serverNow,
          age_seconds: 0.5, freshness: "live", market_open: true,
        },
      });
    });

    expect(screen.getByText(/data live/i)).toBeTruthy();
    expect(screen.queryByText(/data stale/i)).toBeNull();
  });
});

describe("degraded delivery still keeps the price fresh", () => {
  it("polls the price on its own fast timer when the socket never connects", async () => {
    /* The regression this guards.

       The live socket started requiring a key; the browser had none, the
       handshake was rejected, and the dashboard fell back to a single 60s
       timer shared with /signals/live. The feed was healthy the whole time
       and the desk still read "data delayed 44s ago".

       The price now has its own timer against the cheap Redis-backed
       endpoint, so a socket outage costs seconds rather than a minute. */
    fetchMock.mockImplementation((url) => {
      if (url.includes("/market/price")) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({
            symbol: "NIFTY", price: 24_231.1, previous: 24_230, change: 1.1,
            direction: "up", source: "yahoo", source_time: iso(2), at: iso(2),
            age_seconds: 2, freshness: "live", market_open: true,
          }),
        });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });

    render(<App />);
    // Socket is created but never opens, then closes — the rejected case.
    await act(async () => { FakeSocket.last.close(); });
    await act(async () => { FakeSocket.last?.close?.(); });

    await tick(6_000);

    const priceCalls = fetchMock.mock.calls
      .map(([url]) => url)
      .filter((url) => url.includes("/market/price"));
    expect(priceCalls.length).toBeGreaterThan(0);

    await waitFor(() => expect(screen.getByText("24,231.10")).toBeTruthy());
    expect(screen.getByText(/data live/i)).toBeTruthy();
  });

  it("does not poll the expensive signal endpoint on the fast timer", async () => {
    /* Freshness must not be bought with broker load: /signals/live rebuilds
       a signal from the feed, so it stays on the slow timer. */
    render(<App />);
    // Polling only starts after the second failed attempt, matching the
    // component's reconnect policy.
    await act(async () => { FakeSocket.last.close(); });
    await tick(2_000);
    await act(async () => { FakeSocket.last?.close?.(); });
    await tick(20_000);

    const urls = fetchMock.mock.calls.map(([u]) => u);
    const priceCalls = urls.filter((u) => u.includes("/market/price")).length;
    const signalCalls = urls.filter((u) => u.includes("/signals/live")).length;

    expect(priceCalls).toBeGreaterThan(signalCalls);
  });
});

describe("session clock", () => {
  /* The dashboard formats what the backend decided and decides nothing
     itself, so these feed it backend-shaped payloads. */
  const marketPayload = (over = {}) => ({
    open: false, session: "closed", reason: "before-open",
    server_time: iso(0),
    next_open: "2026-08-21T09:15:00+05:30",
    next_boundary: "2026-08-21T09:15:00+05:30",
    boundary_direction: "opens",
    seconds_to_boundary: 33180,
    calendar_provisional: true,
    ...over,
  });

  async function mount(market) {
    render(<App />);
    await act(async () => { FakeSocket.last.open(); });
    await act(async () => {
      FakeSocket.last.deliver({ type: "heartbeat", market });
    });
  }

  it("shows MARKET CLOSED at 00:02, never pre-open", async () => {
    /* The reported screenshot: midnight read "pre-open" and counted toward
       an open nine hours away as though it were minutes. */
    vi.setSystemTime(new Date("2026-08-21T00:02:00+05:30"));
    await mount(marketPayload());

    expect(screen.getAllByText(/market closed/i).length).toBeGreaterThan(0);
    expect(screen.queryByText(/pre-open/i)).toBeNull();
    expect(screen.getByText(/opens in 9h 13m/i)).toBeTruthy();
  });

  it("shows PRE-OPEN inside the window", async () => {
    vi.setSystemTime(new Date("2026-08-21T08:59:00+05:30"));
    await mount(marketPayload({
      session: "pre-open", reason: null, seconds_to_boundary: 960,
    }));

    expect(screen.getAllByText(/pre-open/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/opens in 16m/i)).toBeTruthy();
  });

  it("counts down to the close while open", async () => {
    vi.setSystemTime(new Date("2026-08-21T10:18:00+05:30"));
    await mount(marketPayload({
      open: true, session: "open", reason: null,
      next_boundary: "2026-08-21T15:30:00+05:30",
      boundary_direction: "closes",
    }));

    expect(screen.getAllByText(/market open/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/closes in 5h 12m/i)).toBeTruthy();
  });

  it("names the next session after Friday's close", async () => {
    vi.setSystemTime(new Date("2026-08-21T16:00:00+05:30"));
    await mount(marketPayload({
      reason: "after-close",
      next_open: "2026-08-24T09:15:00+05:30",
      next_boundary: "2026-08-24T09:15:00+05:30",
    }));

    expect(screen.getByText(/session finished/i)).toBeTruthy();
    expect(screen.getByText(/next session Mon/i)).toBeTruthy();
    expect(screen.getByText(/opens in 65h 15m/i)).toBeTruthy();
  });

  it("says weekend on a Saturday", async () => {
    vi.setSystemTime(new Date("2026-08-22T12:00:00+05:30"));
    await mount(marketPayload({
      reason: "weekend",
      next_open: "2026-08-24T09:15:00+05:30",
      next_boundary: "2026-08-24T09:15:00+05:30",
    }));

    expect(screen.getByText(/weekend/i)).toBeTruthy();
    expect(screen.getByText(/next session Mon/i)).toBeTruthy();
  });

  it("the countdown falls as time passes and never climbs", async () => {
    /* The reported symptom was a timer that increased. */
    vi.setSystemTime(new Date("2026-08-21T00:02:00+05:30"));
    await mount(marketPayload());

    const read = () =>
      screen.getByText(/opens in/i).textContent.match(/(\d+)h (\d+)m/).slice(1, 3)
        .map(Number).reduce((h, m) => h * 60 + m);

    const first = read();
    await tick(120_000);
    const second = read();

    expect(second).toBeLessThan(first);
  });

  it("a heartbeat does not resurrect a stale session state", async () => {
    /* The state must come from the payload, never from the browser's own
       reading of the clock. */
    vi.setSystemTime(new Date("2026-08-22T12:00:00+05:30"));
    await mount(marketPayload({
      reason: "weekend",
      next_open: "2026-08-24T09:15:00+05:30",
      next_boundary: "2026-08-24T09:15:00+05:30",
    }));

    for (let i = 0; i < 4; i += 1) {
      await act(async () => {
        FakeSocket.last.deliver({
          type: "heartbeat",
          market: marketPayload({
            reason: "weekend",
            next_open: "2026-08-24T09:15:00+05:30",
            next_boundary: "2026-08-24T09:15:00+05:30",
          }),
        });
      });
    }

    expect(screen.queryByText(/market open/i)).toBeNull();
    expect(screen.queryByText(/pre-open/i)).toBeNull();
  });
});

/* The age counter kept running after the bell.

   At 00:48 on a Friday the desk read "DATA STALE 557m 37s ago" in the same
   colour it uses for a broken ticker, and the number was still climbing —
   it was measuring the length of the night. By Monday morning it would have
   read four figures.

   Nothing about the feed was wrong. The closing print is the freshest
   reading that exists while the exchange is shut, so there is nothing to be
   behind by, and an alarm that cannot clear teaches the desk to ignore it.
   The staleness detection itself still has to work during the session,
   which is the other half of what is tested here. */
describe("data age outside the session", () => {
  const marketPayload = (over = {}) => ({
    open: false, session: "closed", reason: "before-open",
    server_time: new Date().toISOString(),
    next_open: "2026-08-21T09:15:00+05:30",
    next_boundary: "2026-08-21T09:15:00+05:30",
    boundary_direction: "opens", seconds_to_boundary: 30_780,
    ...over,
  });

  /* The reported moment: 00:48 IST, the last print being Thursday's 15:30
     close, 557 minutes earlier. */
  const CLOSE_IST = "2026-08-20T15:30:00+05:30";

  /* The clock is set before the payload is built, not after. `server_time`
     is stamped at construction, and a payload built against the real clock
     then read under a fake one looks like ten hours of skew — which the
     dashboard duly corrects for, reporting a stale price as live. */
  async function mountAt(nowIso, marketOver = {}, sourceTime = CLOSE_IST) {
    vi.setSystemTime(new Date(nowIso));
    const market = marketPayload(marketOver);
    render(<App />);
    await act(async () => { FakeSocket.last.open(); });
    await act(async () => {
      FakeSocket.last.deliver({ type: "heartbeat", market });
    });
    await act(async () => {
      FakeSocket.last.deliver({
        type: "price",
        price: {
          symbol: "NIFTY", price: 24_231.85, previous: 24_231.85, change: 0,
          direction: "flat", source: "yahoo",
          /* Two different clocks, and the difference is the whole point.
             `source_time` is when the exchange printed this number;
             `at` is when the backend published it, which is now. Setting
             them equal would tell the dashboard the browser is running
             fast, and its skew correction would erase the very age being
             tested. */
          source_time: sourceTime, at: nowIso,
          age_seconds: 33_450, freshness: "stale",
          market_open: market.open,
        },
      });
    });
  }

  it("shows the closing print, not a running counter", async () => {
    await mountAt("2026-08-21T00:48:00+05:30");

    expect(screen.queryByText(/557m/)).toBeNull();
    expect(screen.queryByText(/data stale/i)).toBeNull();
    expect(screen.getByText(/last print/i)).toBeTruthy();
    expect(screen.getAllByText(/03:30 pm/).length).toBeGreaterThan(0);
  });

  it("the reading does not move as the night passes", async () => {
    await mountAt("2026-08-21T00:48:00+05:30");

    const read = () => document.querySelector(".data-age").textContent;
    const first = read();

    await tick(60_000);
    expect(read()).toBe(first);
    await tick(30 * 60_000);
    expect(read()).toBe(first);
  });

  it("is not styled as a fault", async () => {
    await mountAt("2026-08-21T00:48:00+05:30");
    const line = document.querySelector(".data-age");
    expect(line.className).toContain("age-closed");
    expect(line.className).not.toContain("age-stale");
  });

  it("treats pre-open the same way — no ticks are due yet", async () => {
    await mountAt("2026-08-21T08:59:00+05:30", {
      session: "pre-open", reason: null, seconds_to_boundary: 960,
    });
    expect(screen.queryByText(/data stale/i)).toBeNull();
    expect(screen.getByText(/last print/i)).toBeTruthy();
  });

  it("still raises a stale alarm while the market is open", async () => {
    /* The regression that matters. Suppressing the alarm out of hours must
       not suppress it during them — a ticker that dies at 11am is exactly
       what this display exists to catch. */
    await mountAt(
      "2026-08-21T11:00:00+05:30",
      {
        open: true, session: "open", reason: null,
        next_boundary: "2026-08-21T15:30:00+05:30",
        boundary_direction: "closes",
      },
      "2026-08-21T10:50:00+05:30",      // ten minutes behind, mid-session
    );

    expect(screen.getByText(/data stale/i)).toBeTruthy();
    // The masthead pill and the age line both carry it.
    expect(screen.getAllByText(/10m 00s ago/).length).toBe(2);
    expect(document.querySelector(".data-age").className).toContain("age-stale");
  });

  it("reports a live price normally while the market is open", async () => {
    await mountAt(
      "2026-08-21T11:00:00+05:30",
      {
        open: true, session: "open", reason: null,
        next_boundary: "2026-08-21T15:30:00+05:30",
        boundary_direction: "closes",
      },
      "2026-08-21T10:59:57+05:30",
    );

    expect(screen.getByText(/data live/i)).toBeTruthy();
    expect(screen.getAllByText(/3s ago/).length).toBe(2);
  });

  it("keeps the alarm live before the session state is known", async () => {
    /* A status request that has not landed must not silence a real fault. */
    vi.setSystemTime(new Date("2026-08-21T11:00:00+05:30"));
    render(<App />);
    await act(async () => { FakeSocket.last.open(); });
    await act(async () => {
      FakeSocket.last.deliver({
        type: "price",
        price: {
          symbol: "NIFTY", price: 24_231.85, previous: 24_231.85, change: 0,
          direction: "flat", source: "yahoo",
          source_time: "2026-08-21T10:50:00+05:30",
          at: "2026-08-21T11:00:00+05:30",
          age_seconds: 600, freshness: "stale", market_open: true,
        },
      });
    });

    expect(screen.getByText(/data stale/i)).toBeTruthy();
  });
});

describe("elapsed time stays legible past an hour", () => {
  it("rolls minutes into hours instead of counting to 557", () => {
    expect(formatAge(33_450)).toBe("9h 17m ago");
    expect(formatAge(3_600)).toBe("1h 00m ago");
    expect(formatAge(3_599)).toBe("59m 59s ago");
    expect(formatAge(90)).toBe("1m 30s ago");
    expect(formatAge(9)).toBe("9s ago");
  });

  it("classifies by the session, not by the elapsed count alone", () => {
    expect(classifyAge(3, true)).toBe("live");
    expect(classifyAge(600, true)).toBe("stale");
    expect(classifyAge(600, false)).toBe("closed");
    expect(classifyAge(3, false)).toBe("closed");
    expect(classifyAge(null, false)).toBe("unknown");
  });
});

/* The risk verdict on the trade plan.

   Audit finding H-4: the agent published signals with no risk block, and
   `PlanPanel` guarded its risk rows with `{risk && ...}` — so a refused
   trade rendered as a clean plan with entry, stop and target and nothing
   saying it had been refused. The absence had no visible form.

   These tests are about that: the verdict is always on screen, it comes
   from the backend rather than being inferred here, and a payload that
   somehow arrives without one says so loudly instead of silently dropping
   a row. */
describe("risk verdict", () => {
  const withRisk = (risk) => ({
    type: "signal",
    signal: {
      timestamp: new Date().toISOString(), action: "BUY", confidence: 0.61,
      price: 24_200, entry: 24_200, stop_loss: 24_190, target: 24_225,
      risk_reward: 2.5, checks: [], context: { trend: "up" },
      ...(risk === undefined ? {} : { risk }),
    },
    market: { open: true, session: "open", server_time: new Date().toISOString() },
  });

  const approved = {
    state: "approved", evaluated: true, approved: true,
    reasons: ["Risking 1.0% (1000) at 10.00 per unit.", "Reward:risk 1:2.50."],
    quantity: 75, lots: 1, rupees_at_risk: 1000, risk_amount: 1000,
    potential: { quantity: 75, lots: 1, rupees_at_risk: 1000 },
    evaluated_at: "2026-08-21T04:30:00+00:00",
    risk_per_unit: 10, risk_reward: 2.5,
    day_state: { trading_day: "2026-08-21", trades_taken: 0, realised_pnl: 0,
                 consecutive_losses: 0, open_positions: 0 },
  };

  const blocked = {
    state: "blocked", evaluated: true, approved: false,
    reasons: ["Kill switch is on — no new entries."],
    quantity: 0, lots: 0, rupees_at_risk: 0, risk_amount: 0,
    potential: { quantity: 0, lots: 0, rupees_at_risk: 0 },
    evaluated_at: "2026-08-21T04:30:00+00:00",
    risk_per_unit: 0, risk_reward: null,
    day_state: { trading_day: "2026-08-21", trades_taken: 0, realised_pnl: 0,
                 consecutive_losses: 0, open_positions: 0 },
  };

  async function mount(frame) {
    render(<App />);
    await act(async () => { FakeSocket.last.open(); });
    await act(async () => { FakeSocket.last.deliver(frame); });
    return document.querySelector(".risk-verdict");
  }

  it("shows APPROVED on an approved trade", async () => {
    const badge = await mount(withRisk(approved));

    expect(badge).toBeTruthy();
    expect(badge.textContent).toMatch(/Risk/);
    expect(badge.textContent).toMatch(/APPROVED/);
    expect(badge.className).toContain("risk-approved");
    expect(screen.getByText("75 (1 lot)")).toBeTruthy();
    expect(screen.getByText("1,000")).toBeTruthy();
  });

  it("shows BLOCKED and the reason on a refused trade", async () => {
    const badge = await mount(withRisk(blocked));

    expect(badge.textContent).toMatch(/BLOCKED/);
    expect(badge.className).toContain("risk-blocked");
    expect(screen.getByText(/Kill switch is on/)).toBeTruthy();
    expect(screen.getByText("blocked")).toBeTruthy();
  });

  it("never shows APPROVED for a blocked trade", async () => {
    const badge = await mount(withRisk(blocked));
    expect(badge.textContent).not.toMatch(/APPROVED/);
  });

  it("says so loudly when a payload carries no decision", async () => {
    /* The regression itself. This used to render a clean, complete-looking
       trade plan with the risk rows simply absent. */
    const badge = await mount(withRisk(undefined));

    expect(badge).toBeTruthy();
    expect(badge.textContent).toMatch(/NOT REPORTED/);
    expect(badge.className).toContain("risk-missing");
    expect(screen.queryByText(/Rupees at risk/)).toBeNull();
  });

  it("labels a signal the backend marked unevaluated", async () => {
    const badge = await mount(withRisk({
      state: "unevaluated", evaluated: false, approved: false,
      reasons: ["This signal was published without a risk decision."],
      quantity: 0, lots: 0, rupees_at_risk: 0,
    }));

    expect(badge.textContent).toMatch(/NOT EVALUATED/);
    expect(screen.getByText(/published without a risk decision/)).toBeTruthy();
  });

  it("takes the verdict from the backend, never from the numbers", async () => {
    /* A payload whose fields would look approvable but which the backend
       refused must still read BLOCKED. The dashboard does not get a vote. */
    const badge = await mount(withRisk({
      ...blocked, potential: { quantity: 75, lots: 1, rupees_at_risk: 1000 },
      reasons: ["Daily trade cap reached (2)."],
    }));

    expect(badge.textContent).toMatch(/BLOCKED/);
    expect(screen.getByText(/Daily trade cap reached/)).toBeTruthy();
  });

  it("shows no verdict badge on a HOLD, which proposes no trade", async () => {
    render(<App />);
    await act(async () => { FakeSocket.last.open(); });
    await act(async () => {
      FakeSocket.last.deliver({
        type: "signal",
        signal: {
          timestamp: new Date().toISOString(), action: "HOLD",
          confidence: 0.2, price: 24_200, checks: [], context: {},
          risk: { state: "not-applicable", evaluated: false, approved: false,
                  reasons: ["No trade proposed — nothing to size."] },
        },
        market: { open: true, session: "open", server_time: new Date().toISOString() },
      });
    });

    expect(document.querySelector(".risk-verdict")).toBeNull();
    expect(screen.getByText(/No plan\s+is generated/)).toBeTruthy();
  });
});

/* Actual exposure, potential exposure, and the difference between the
   verdict then and the verdict now.

   Two defects sat in the first version of this panel. A refused trade
   reported "Size: blocked" beside "Rupees at risk: 1,000" — the sizing
   arithmetic, rendered as though it were money on the table. And the stored
   verdict was presented as the live one, so a browser reconnecting to a
   fifteen-minute-old cached signal read "APPROVED" for a trade whose
   position it was already holding. */
describe("risk exposure and freshness", () => {
  const at = "2026-08-21T04:30:00+00:00";          // 10:00 IST

  const approvedNow = {
    state: "approved", evaluated: true, approved: true, evaluated_at: at,
    reasons: ["Risking 1.0% (1000) at 10.00 per unit."],
    quantity: 75, lots: 1, rupees_at_risk: 1000, risk_amount: 1000,
    potential: { quantity: 75, lots: 1, rupees_at_risk: 1000 },
  };

  const blockedByPosition = {
    state: "blocked", evaluated: true, approved: false,
    evaluated_at: "2026-08-21T04:32:00+00:00",     // 10:02 IST
    reasons: ["Already holding 1 position(s)."],
    quantity: 0, lots: 0, rupees_at_risk: 0, risk_amount: 0,
    potential: { quantity: 75, lots: 1, rupees_at_risk: 1000 },
  };

  const signalWith = (risk) => ({
    timestamp: new Date().toISOString(), action: "BUY", confidence: 0.61,
    price: 24_200, entry: 24_200, stop_loss: 24_190, target: 24_225,
    risk_reward: 2.5, checks: [], context: { trend: "up" }, risk,
  });

  const marketOpen = () =>
    ({ open: true, session: "open", server_time: new Date().toISOString() });

  async function mount(frames) {
    render(<App />);
    await act(async () => { FakeSocket.last.open(); });
    for (const frame of frames) {
      await act(async () => { FakeSocket.last.deliver(frame); });
    }
    return {
      badge: document.querySelector(".risk-verdict"),
      history: document.querySelector(".risk-history"),
      potential: document.querySelector(".risk-potential"),
    };
  }

  it("shows zero exposure on a blocked trade, not the hypothetical", async () => {
    /* The regression: this row used to read 1,000 on a trade nobody may
       take. */
    await mount([{ type: "signal", signal: signalWith(blockedByPosition),
                   market: marketOpen() }]);

    const rows = [...document.querySelectorAll(".kv")].map((r) => r.textContent);
    expect(rows).toContain("Rupees at risk0");
    expect(rows).not.toContain("Rupees at risk1,000");
    expect(rows.some((r) => r.startsWith("Sizeblocked"))).toBe(true);
  });

  it("still explains what the trade would have been", async () => {
    const { potential } = await mount([
      { type: "signal", signal: signalWith(blockedByPosition), market: marketOpen() },
    ]);

    expect(potential).toBeTruthy();
    expect(potential.textContent).toMatch(/Had it been allowed/);
    expect(potential.textContent).toMatch(/75 \(1 lot\)/);
    expect(potential.textContent).toMatch(/1,000/);
  });

  it("shows real exposure on an approved trade", async () => {
    const { potential } = await mount([
      { type: "signal", signal: signalWith(approvedNow), market: marketOpen() },
    ]);

    const rows = [...document.querySelectorAll(".kv")].map((r) => r.textContent);
    expect(rows).toContain("Rupees at risk1,000");
    expect(rows).toContain("Size75 (1 lot)");
    expect(potential).toBeNull();      // nothing hypothetical about it
  });

  it("says one lot, not one lots", async () => {
    await mount([{ type: "signal", signal: signalWith(approvedNow),
                   market: marketOpen() }]);
    expect(screen.getByText("75 (1 lot)")).toBeTruthy();
    expect(screen.queryByText("75 (1 lots)")).toBeNull();
  });

  it("pluralises correctly above one lot", async () => {
    await mount([{ type: "signal", signal: signalWith({
      ...approvedNow, quantity: 150, lots: 2,
    }), market: marketOpen() }]);
    expect(screen.getByText("150 (2 lots)")).toBeTruthy();
  });

  /* ---- then versus now ---- */

  it("the reconnect scenario: approved at 10:00, blocked by 10:02", async () => {
    /* Exactly the sequence from the brief. The socket replays a cached
       signal approved at 10:00; a position was opened at 10:01; the
       backend's current verdict says blocked. */
    const { badge, history } = await mount([{
      type: "snapshot",
      signal: signalWith(approvedNow),
      market: marketOpen(),
      risk_now: blockedByPosition,
    }]);

    expect(badge.textContent).toMatch(/Risk now/);
    expect(badge.textContent).toMatch(/BLOCKED/);
    expect(badge.className).toContain("risk-blocked");

    // and the historical decision is still on screen, unchanged
    expect(history.textContent).toMatch(/At signal 10:00/);
    expect(history.textContent).toMatch(/APPROVED/);
    expect(history.textContent).toMatch(/journal has moved since/);
    expect(history.className).toContain("risk-history-changed");
  });

  it("never presents a stale approval as the current verdict", async () => {
    const { badge } = await mount([{
      type: "snapshot", signal: signalWith(approvedNow),
      market: marketOpen(), risk_now: blockedByPosition,
    }]);
    expect(badge.textContent).not.toMatch(/APPROVED/);
  });

  it("keeps the two quiet when they agree", async () => {
    const { badge, history } = await mount([{
      type: "snapshot", signal: signalWith(approvedNow),
      market: marketOpen(), risk_now: approvedNow,
    }]);

    expect(badge.textContent).toMatch(/APPROVED/);
    expect(history.textContent).toMatch(/At signal 10:00.*APPROVED/);
    expect(history.className).not.toContain("risk-history-changed");
  });

  it("updates the current verdict on a heartbeat, with no new signal", async () => {
    /* Risk state changes when a trade is recorded, not when a bar closes —
       so it has to ride the heartbeat or the desk waits five minutes to
       learn it is holding a position. */
    const { badge } = await mount([
      { type: "signal", signal: signalWith(approvedNow), market: marketOpen() },
      { type: "heartbeat", market: marketOpen(), risk_now: blockedByPosition },
    ]);

    expect(badge.textContent).toMatch(/BLOCKED/);
    expect(document.querySelector(".risk-history").textContent)
      .toMatch(/At signal 10:00.*APPROVED/);
  });

  it("labels the verdict as signal-time when no current one is available", async () => {
    /* The backend omits `risk_now` if the journal read failed. Better to say
       which question was answered than to imply the wrong one. */
    const { badge } = await mount([{
      type: "snapshot", signal: signalWith(approvedNow),
      market: marketOpen(), risk_now: null,
    }]);

    expect(badge.textContent).toMatch(/Risk at signal/);
    expect(badge.textContent).toMatch(/APPROVED/);
  });
});
