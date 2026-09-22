import { render, screen, within, fireEvent } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import OptionChain from "./OptionChain.jsx";
import {
  ageOf, ageText, chainSource, POLL_FRESH_SECONDS, POLL_STALE_SECONDS,
  STREAM_FRESH_SECONDS, STREAM_TOLERATED_SECONDS,
} from "./chain-source.js";

const NOW = Date.parse("2026-08-28T09:45:00Z");

/* A polled NSE payload: has the day's OI change and an IV per strike, and
   is stamped with the one moment it was fetched. */
function polled(overrides = {}) {
  return {
    symbol: "NIFTY",
    summary: { spot: 24334.55, atm_strike: 24350, pcr_oi: 1.08, max_pain: 24300 },
    strikes: [
      { strike: 24250, call_oi: 300000, put_oi: 900000, call_oi_change: 50000,
        put_oi_change: -20000, call_iv: 14.2, put_iv: 15.1, call_ltp: 120.5, put_ltp: 40.25 },
      { strike: 24300, call_oi: 500000, put_oi: 1200000, call_oi_change: 10000,
        put_oi_change: 60000, call_iv: 13.8, put_iv: 14.6, call_ltp: 90.1, put_ltp: 58.7 },
      { strike: 24350, call_oi: 1500000, put_oi: 700000, call_oi_change: 220000,
        put_oi_change: -5000, call_iv: 13.5, put_iv: 14.2, call_ltp: 62.4, put_ltp: 81.0 },
      { strike: 24400, call_oi: 800000, put_oi: 400000, call_oi_change: 90000,
        put_oi_change: -1000, call_iv: 13.9, put_iv: 14.9, call_ltp: 41.2, put_ltp: 110.3 },
    ],
    fetched_at: new Date(NOW - 20_000).toISOString(),
    live: true,
    ...overrides,
  };
}

/* A streamed payload: bid/ask instead of OI change, no IV at all, and an
   age per contract rather than one fetch time. */
function streamed(overrides = {}) {
  return {
    symbol: "NIFTY",
    summary: { spot: 24334.55, atm_strike: 24350, pcr_oi: 1.08, max_pain: 24300 },
    strikes: [
      { strike: 24300, call_oi: 500000, put_oi: 1200000, call_ltp: 90.1, put_ltp: 58.7,
        call_bid: 89.9, call_ask: 90.4, put_bid: 58.5, put_ask: 58.9,
        call_volume: 12000, put_volume: 9000 },
      { strike: 24350, call_oi: 1500000, put_oi: 700000, call_ltp: 62.4, put_ltp: 81.0,
        call_bid: 62.2, call_ask: 62.6, put_bid: 80.8, put_ask: 81.3,
        call_volume: 22000, put_volume: 15000 },
      { strike: 24400, call_oi: 800000, put_oi: 400000, call_ltp: 41.2, put_ltp: 110.3,
        call_bid: 41.0, call_ask: 41.5, put_bid: 110.0, put_ask: 110.6,
        call_volume: 8000, put_volume: 4000 },
    ],
    fetched_at: new Date(NOW).toISOString(),
    live: true,
    transport: "stream",
    expiry: "2026-09-03",
    oldest_age_seconds: 0.42,
    contracts: 82,
    dropped_stale: 0,
    ...overrides,
  };
}

describe("ageText", () => {
  it("keeps a decimal below ten seconds, because that is the whole claim", () => {
    expect(ageText(0.42)).toBe("0.4s");
    expect(ageText(4)).toBe("4.0s");
  });

  it("drops to whole seconds, then minutes, as the number stops being precise", () => {
    expect(ageText(47)).toBe("47s");
    expect(ageText(125)).toBe("2m 05s");
    expect(ageText(3 * 3600 + 240)).toBe("3h 04m");
  });

  it("reports nothing as an em dash, never as a zero", () => {
    expect(ageText(null)).toBe("—");
    expect(ageText(undefined)).toBe("—");
    expect(ageText(NaN)).toBe("—");
  });

  it("clamps a negative age rather than printing one", () => {
    expect(ageText(-3)).toBe("0.0s");
  });
});

describe("ageOf", () => {
  it("measures against the corrected clock", () => {
    const iso = new Date(NOW - 30_000).toISOString();
    expect(ageOf(iso, NOW, 0)).toBeCloseTo(30, 3);
    // A browser clock running 10s fast must not report the data as older.
    expect(ageOf(iso, NOW + 10_000, 10_000)).toBeCloseTo(30, 3);
  });

  it("returns null for anything unparseable", () => {
    expect(ageOf(null, NOW)).toBeNull();
    expect(ageOf("not a date", NOW)).toBeNull();
  });
});

describe("chainSource", () => {
  it("names the streamed chain and reads its age from the oldest print", () => {
    const s = chainSource(streamed(), NOW);
    expect(s.kind).toBe("stream");
    expect(s.streamed).toBe(true);
    expect(s.ageSeconds).toBe(0.42);
    expect(s.detail).toBe("oldest print 0.4s");
    expect(s.tone).toBe("go");
  });

  it("does not use fetched_at for the stream, which is always now", () => {
    // The backend stamps the streamed payload at request time, so a dead
    // socket would still look freshly fetched. Only the print age is real.
    const s = chainSource(streamed({
      fetched_at: new Date(NOW).toISOString(),
      oldest_age_seconds: STREAM_FRESH_SECONDS + 1,
    }), NOW);
    expect(s.tone).toBe("wait");
  });

  it("tolerates a quiet wing up to the backend's own drop threshold", () => {
    expect(chainSource(streamed({ oldest_age_seconds: STREAM_TOLERATED_SECONDS }), NOW).tone)
      .toBe("wait");
    expect(chainSource(streamed({ oldest_age_seconds: STREAM_TOLERATED_SECONDS + 1 }), NOW).tone)
      .toBe("stop");
  });

  it("names the polled chain and ages it from when it was fetched", () => {
    const s = chainSource(polled(), NOW);
    expect(s.kind).toBe("poll");
    expect(s.streamed).toBe(false);
    expect(s.ageSeconds).toBeCloseTo(20, 1);
    expect(s.detail).toBe("fetched 20s ago");
    expect(s.tone).toBe("go");
  });

  it("colours a polled chain against the backend's cache TTL", () => {
    const at = (secs) => polled({ fetched_at: new Date(NOW - secs * 1000).toISOString() });
    expect(chainSource(at(POLL_FRESH_SECONDS), NOW).tone).toBe("go");
    expect(chainSource(at(POLL_FRESH_SECONDS + 1), NOW).tone).toBe("wait");
    expect(chainSource(at(POLL_STALE_SECONDS + 1), NOW).tone).toBe("stop");
  });

  it("never raises an alarm on a closed market's last chain", () => {
    // An age counter running away from the close would be an alarm that
    // cannot clear, which is no alarm at all.
    const s = chainSource(polled({
      live: false, fetched_at: new Date(NOW - 12 * 3600 * 1000).toISOString(),
    }), NOW);
    expect(s.kind).toBe("last");
    expect(s.tone).toBe("flat");
    expect(s.label).toBe("Last chain");
  });

  it("says so when there is no chain at all", () => {
    expect(chainSource(null, NOW).kind).toBe("none");
    expect(chainSource(undefined, NOW).tone).toBe("flat");
  });
});

describe("OptionChain ladder", () => {
  it("renders a row per strike with both sides on it", () => {
    render(<OptionChain chain={polled()} spot={24334.55} nowMs={NOW} />);
    const rows = screen.getAllByRole("row");
    // two header rows plus one per strike
    expect(rows).toHaveLength(2 + 4);
    // Scoped to the table: the footer names the walls by strike too.
    const table = within(screen.getByRole("table"));
    expect(table.getByText("24,350")).toBeTruthy();
    expect(table.getByText("62.40")).toBeTruthy();   // call LTP
    expect(table.getByText("81.00")).toBeTruthy();   // put LTP
  });

  it("shows the OI-change and IV columns for a polled chain", () => {
    render(<OptionChain chain={polled()} spot={24334.55} nowMs={NOW} />);
    expect(screen.getAllByText("ΔOI")).toHaveLength(2);
    expect(screen.getAllByText("IV")).toHaveLength(2);
  });

  it("omits those columns entirely for a streamed chain, rather than dashing them", () => {
    // The websocket carries neither. A wall of em dashes reads as breakage;
    // an absent column plus a sentence saying why does not.
    render(<OptionChain chain={streamed()} spot={24334.55} nowMs={NOW} />);
    expect(screen.queryByText("ΔOI")).toBeNull();
    expect(screen.queryByText("IV")).toBeNull();
    expect(screen.getByText(/carries no day-change in open interest/)).toBeTruthy();
  });

  it("puts the live bid and ask on the premium cell when the stream has them", () => {
    render(<OptionChain chain={streamed()} spot={24334.55} nowMs={NOW} />);
    const cell = screen.getByText("62.40");
    expect(cell.getAttribute("title")).toBe("bid 62.20 · ask 62.60 · spread 0.40");
  });

  it("leaves the tooltip off when there is no book", () => {
    render(<OptionChain chain={polled()} spot={24334.55} nowMs={NOW} />);
    expect(screen.getByText("62.40").getAttribute("title")).toBeNull();
  });

  it("flags the strike nearest spot, not an exact match", () => {
    // Spot is 24334.55 and never lands on a 50-point strike.
    render(<OptionChain chain={polled()} spot={24334.55} nowMs={NOW} />);
    const atm = screen.getByText("ATM").closest("tr");
    expect(within(atm).getByText("24,350")).toBeTruthy();
  });

  it("marks max pain when it is not already the ATM row", () => {
    render(<OptionChain chain={polled()} spot={24334.55} nowMs={NOW} />);
    const pain = screen.getByText("pain").closest("tr");
    expect(within(pain).getByText("24,300")).toBeTruthy();
  });

  it("shades in-the-money cells on the correct side of the money", () => {
    render(<OptionChain chain={polled()} spot={24334.55} nowMs={NOW} />);
    const rows = screen.getAllByRole("row").slice(2);
    const low = rows[0];     // 24250: calls ITM, puts OTM
    const high = rows[3];    // 24400: puts ITM, calls OTM
    expect(within(low).getByText("120.50").className).toContain("itm");
    expect(within(low).getByText("40.25").className).not.toContain("itm");
    expect(within(high).getByText("110.30").className).toContain("itm");
    expect(within(high).getByText("41.20").className).not.toContain("itm");
  });

  it("scales the OI bars against one figure across both sides", () => {
    // A per-column scale would draw a small put wall the same width as a
    // large call wall. The widest visible value here is 1.5L of call OI.
    render(<OptionChain chain={polled()} spot={24334.55} nowMs={NOW} />);
    const heaviestCall = screen.getByText("15.0L").closest("td");
    expect(heaviestCall.style.getPropertyValue("--oi-fill")).toBe("100%");
    const putAtSameStrike = within(heaviestCall.closest("tr")).getByText("7.0L");
    expect(putAtSameStrike.closest("td").style.getPropertyValue("--oi-fill"))
      .toBe(`${(700000 / 1500000) * 100}%`);
  });

  it("bolds the heaviest OI on each side and names both underneath", () => {
    render(<OptionChain chain={polled()} spot={24334.55} nowMs={NOW} />);
    expect(screen.getByText("15.0L").closest("td").className).toContain("wall");
    expect(screen.getByText("12.0L").closest("td").className).toContain("wall");
    expect(screen.getByText(/Heaviest call OI/)).toBeTruthy();
  });

  it("widens and narrows the window without losing the anchor", () => {
    const many = polled({
      strikes: Array.from({ length: 120 }, (_, i) => ({
        strike: 23000 + i * 50, call_oi: 1000 * (i + 1), put_oi: 900 * (i + 1),
        call_oi_change: 0, put_oi_change: 0, call_iv: 12, put_iv: 12,
        call_ltp: 10 + i, put_ltp: 200 - i,
      })),
    });
    render(<OptionChain chain={many} spot={24334.55} nowMs={NOW} />);
    expect(screen.getAllByRole("row")).toHaveLength(2 + 24);
    expect(screen.getByText(/24 of 120 strikes/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "±40" }));
    expect(screen.getAllByRole("row")).toHaveLength(2 + 80);
    // The money is still on screen after widening.
    expect(screen.getByText("ATM")).toBeTruthy();
  });

  it("falls back to the chain's own spot when no live price is passed", () => {
    render(<OptionChain chain={polled()} nowMs={NOW} />);
    const atm = screen.getByText("ATM").closest("tr");
    expect(within(atm).getByText("24,350")).toBeTruthy();
  });

  it("still renders with no spot anywhere, without inventing a centre", () => {
    const noSpot = polled({ summary: { pcr_oi: 1.08 } });
    render(<OptionChain chain={noSpot} nowMs={NOW} />);
    expect(screen.getAllByRole("row")).toHaveLength(2 + 4);
    expect(screen.queryByText("ATM")).toBeNull();
  });

  it("says nothing is loaded rather than rendering an empty table", () => {
    render(<OptionChain chain={null} nowMs={NOW} />);
    expect(screen.getByText(/No option chain loaded/)).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("distinguishes an empty chain from a missing one", () => {
    render(<OptionChain chain={polled({ strikes: [] })} nowMs={NOW} />);
    expect(screen.getByText(/arrived with no strikes/)).toBeTruthy();
  });

  it("shows the source badge, so the reader knows what the numbers are worth", () => {
    const { unmount } = render(<OptionChain chain={streamed()} nowMs={NOW} />);
    expect(screen.getByText("Stream · oldest print 0.4s")).toBeTruthy();
    expect(screen.getByText("82 contracts live")).toBeTruthy();
    unmount();

    render(<OptionChain chain={polled()} nowMs={NOW} />);
    expect(screen.getByText("Poll · fetched 20s ago")).toBeTruthy();
  });

  it("labels a closed market's chain as the last one, not as current", () => {
    render(<OptionChain chain={polled({ live: false })} nowMs={NOW} />);
    expect(screen.getByText(/^Last chain · close/)).toBeTruthy();
  });

  it("formats an untraded strike's zero premium and zero IV as absences", () => {
    // NSE publishes 0 for both rather than omitting them, and on a trading
    // screen a rendered zero is a reading.
    const quiet = polled({
      strikes: [{ strike: 24350, call_oi: 100, put_oi: 100, call_oi_change: 0,
                  put_oi_change: 0, call_iv: 0, put_iv: 14.2,
                  call_ltp: 0, put_ltp: 81 }],
    });
    render(<OptionChain chain={quiet} spot={24350} nowMs={NOW} />);
    const row = screen.getAllByRole("row")[2];
    expect(within(row).getAllByText("—")).toHaveLength(2);   // call LTP and call IV
  });
})

describe("greeks", () => {
  /* The backend derives IV and the greeks from the traded premium (see
     `analytics/chain_greeks.py`), so both transports now carry the same
     columns. What is asserted here is that the ladder mirrors them
     correctly around the strike — calls reading outward-in, puts
     reading inward-out — because a table where delta sits above the
     gamma header on one side only is worse than no greeks at all. */

  const withGreeks = (over = {}) => ({
    ...polled(),
    strikes: polled().strikes.map((r, i) => ({
      ...r,
      call_delta: 0.6 - i * 0.1, call_gamma: 0.0010, call_theta: -13.28,
      call_vega: 12.1, call_rho: 2.0,
      put_delta: -0.4 - i * 0.1, put_gamma: 0.0010, put_theta: -11.2,
      put_vega: 12.1, put_rho: -1.9,
    })),
    ...over,
  });

  const headers = () =>
    [...document.querySelectorAll("thead tr:last-child th")]
      .map((th) => th.textContent.trim());

  it("mirrors the greeks around the strike column", () => {
    render(<OptionChain chain={withGreeks()} spot={24334.55} nowMs={NOW} />);

    fireEvent.click(screen.getByRole("button", { name: /greeks/i }));
    const head = headers();
    const strike = head.indexOf("Price");        // the strike price column
    const left = head.slice(0, strike);
    const right = head.slice(strike + 1);

    // Calls: greeks furthest out, premium against the strike.
    expect(left.slice(0, 5)).toEqual(
      ["Rho", "Vega", "Gamma", "Theta", "Delta"]);
    expect(left[left.length - 1]).toBe("LTP");

    // Puts: the exact mirror.
    expect(right.slice(-5)).toEqual(
      ["Delta", "Theta", "Gamma", "Vega", "Rho"]);
    expect(right[0]).toBe("LTP");
  });

  it("renders a delta on both sides of a strike", () => {
    render(<OptionChain chain={withGreeks()} spot={24334.55} nowMs={NOW} />);
    fireEvent.click(screen.getByRole("button", { name: /greeks/i }));
    const row = [...document.querySelectorAll("tbody tr")]
      .find((tr) => tr.querySelector("td.strike")?.textContent.startsWith("24,350"));
    const cells = within(row).getAllByText(/^-?0\.\d\d$/);
    // one call delta, one put delta
    expect(cells.length).toBeGreaterThanOrEqual(2);
    expect(cells.some((c) => c.textContent.startsWith("-"))).toBe(true);
  });

  it("folds the greeks away on request and gives the width back", () => {
    render(<OptionChain chain={withGreeks()} spot={24334.55} nowMs={NOW} />);
    expect(headers()).not.toContain("Gamma");
    fireEvent.click(screen.getByRole("button", { name: /greeks/i }));
    expect(headers()).toContain("Gamma");

    fireEvent.click(screen.getByRole("button", { name: /greeks/i }));

    expect(headers()).not.toContain("Gamma");
    expect(headers()).toContain("LTP");      // the rest of the ladder stays
  });

  it("shows no greek columns at all when the chain carries none", () => {
    /* Not a row of dashes. A column of "—" costs width and says
       nothing that its absence does not say more quietly. */
    render(<OptionChain chain={polled()} spot={24334.55} nowMs={NOW} />);
    expect(headers()).not.toContain("Delta");
    expect(screen.queryByRole("button", { name: /greeks/i })).toBeNull();
  });
});

describe("the spot marker", () => {
  it("draws the live price between the two strikes it sits between", () => {
    /* Spot almost never lands on a strike. Without this the ladder
       locates the money only to the nearest fifty points, which on a
       weekly is most of a delta. */
    render(<OptionChain chain={polled()} spot={24334.55} nowMs={NOW} />);

    const marker = document.querySelector(".chain-spot");
    expect(marker).toBeTruthy();
    expect(marker.textContent).toContain("24,334.55");

    // It sits after 24,300 and before 24,350 — not on either.
    const rows = [...document.querySelectorAll("tbody tr")];
    const at = rows.indexOf(marker);
    expect(rows[at - 1].textContent).toContain("24,300");
    expect(rows[at + 1].textContent).toContain("24,350");
  });

  it("is not drawn when there is no spot to draw", () => {
    const blind = { ...polled(), summary: {} };
    render(<OptionChain chain={blind} spot={null} nowMs={NOW} />);
    expect(document.querySelector(".chain-spot")).toBeNull();
  });
});
