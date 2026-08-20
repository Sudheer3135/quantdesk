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

import App from "./App.jsx";

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
