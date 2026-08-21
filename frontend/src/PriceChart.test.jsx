/* The chart read as though its candles were out of order.

   They were not. `/market/candles?days=2` returns two sessions in perfect
   chronological order, and the chart plotted them in exactly that order —
   but it labelled every bar with the time of day and nothing else, so the
   axis ran

       ... 02:30 pm -> 03:10 pm -> 09:35 am -> 10:15 am ...

   which is Wednesday's close followed by Thursday's open. Correct data,
   and no way to tell from the screen.

   Two things are tested here. That the series is ordered by the absolute
   instant rather than by anything formatted — because sorting on "03:10 pm"
   really would corrupt it — and that a session boundary is visible, because
   an ordering that is right but unreadable is the bug that was reported. */
import { render, screen } from "@testing-library/react";
import { beforeAll, describe, expect, it } from "vitest";

import PriceChart, {
  hhmm, pickTicks, sessionOf, toSeries,
} from "./PriceChart.jsx";

/* jsdom ships no ResizeObserver, and Recharts' ResponsiveContainer asks for
   one on mount. The chart geometry is not what these tests read — the panel
   heading and its date span are — so a stub that never fires is enough. */
beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
});

/* 09:15 IST is 03:45 UTC. Bars are built from the UTC instant so the test
   states the moment it means rather than trusting a local zone. */
const bar = (iso, close = 24_200) => ({
  timestamp: iso,
  open: close - 5, high: close + 8, low: close - 9, close,
  volume: 1, vwap: close - 2, vwap_upper: close + 12, vwap_lower: close - 16,
});

/* One IST session as 5-minute bars, from 09:15. */
function session(date, count, base = 24_200) {
  const open = Date.parse(`${date}T03:45:00+00:00`);
  return Array.from({ length: count }, (_, i) =>
    bar(new Date(open + i * 5 * 60_000).toISOString(), base + i));
}

const WED = session("2026-08-19", 75, 24_200);      // 09:15 -> 15:25 IST
const THU = session("2026-08-20", 76, 24_310);      // 09:15 -> 15:30 IST

const epochs = (rows) => rows.map((r) => r.ms);
const isAscending = (xs) => xs.every((x, i) => i === 0 || xs[i - 1] < x);

describe("chronological ordering", () => {
  it("reproduces the reported sequence and puts it back in order", () => {
    /* The exact shape from the report: a midday bar, a late-afternoon bar,
       and a next-morning bar, delivered out of order. Formatted as HH:mm
       they read 12:15 pm, 03:25 pm, 10:45 am — and any sort that touches
       those strings keeps them in that order. */
    const broken = [
      bar("2026-08-20T05:15:00+00:00", 24_400),   // Thu 10:45 IST
      bar("2026-08-19T06:45:00+00:00", 24_100),   // Wed 12:15 IST
      bar("2026-08-19T09:55:00+00:00", 24_250),   // Wed 15:25 IST
    ];

    const labels = broken.map((b) => hhmm(Date.parse(b.timestamp)));
    expect(labels).toEqual(["10:45 am", "12:15 pm", "03:25 pm"]);

    /* Sorting the labels does not rescue it. A 12-hour clock puts "03:25 pm"
       first and the true first bar second, so the formatted order is wrong
       in its own way and gives no hint that it is wrong. */
    expect([...labels].sort()).toEqual(["03:25 pm", "10:45 am", "12:15 pm"]);

    const rows = toSeries(broken);
    expect(isAscending(epochs(rows))).toBe(true);
    expect(rows.map((r) => hhmm(r.ms)))
      .toEqual(["12:15 pm", "03:25 pm", "10:45 am"]);
    expect(rows.map((r) => r.close)).toEqual([24_100, 24_250, 24_400]);
  });

  it("orders a fully shuffled two-session window by the instant", () => {
    const shuffled = [...WED, ...THU]
      .map((c, i) => ({ c, k: (i * 7919) % 151 }))
      .sort((a, b) => a.k - b.k)
      .map((x) => x.c);

    const rows = toSeries(shuffled, 200);
    expect(rows).toHaveLength(151);
    expect(isAscending(epochs(rows))).toBe(true);
    expect(rows[0].timestamp).toBe(WED[0].timestamp);
    expect(rows[rows.length - 1].timestamp).toBe(THU[THU.length - 1].timestamp);
  });

  it("keeps an already-correct series exactly as it is", () => {
    const rows = toSeries([...WED, ...THU], 200);
    expect(rows.map((r) => r.timestamp))
      .toEqual([...WED, ...THU].map((c) => c.timestamp));
  });

  it("trims to the last N bars after sorting, not before", () => {
    /* Slicing an unordered array keeps the last N of an arbitrary order.
       Reversed input makes that visible: the naive path would return the
       *oldest* bars and call them the newest. */
    const rows = toSeries([...WED, ...THU].reverse(), 10);
    expect(rows).toHaveLength(10);
    expect(epochs(rows)).toEqual(epochs(toSeries([...WED, ...THU], 10)));
    expect(rows[rows.length - 1].timestamp).toBe(THU[THU.length - 1].timestamp);
  });

  it("collapses a repeated instant to the later reading", () => {
    const restated = { ...WED[3], close: 24_999 };
    const rows = toSeries([...WED.slice(0, 5), restated], 50);
    expect(rows).toHaveLength(5);
    expect(isAscending(epochs(rows))).toBe(true);
    expect(rows[3].close).toBe(24_999);
  });

  it("drops a stamp it cannot parse instead of sorting NaN", () => {
    const rows = toSeries([WED[0], bar("not a date"), WED[1]], 50);
    expect(rows).toHaveLength(2);
    expect(isAscending(epochs(rows))).toBe(true);
  });
});

describe("timestamps arrive with a full date and a zone", () => {
  it("accepts the ISO 8601 form the backend now sends", () => {
    const rows = toSeries([bar("2026-08-19T03:45:00+00:00")]);
    expect(rows[0].ms).toBe(Date.parse("2026-08-19T03:45:00Z"));
  });

  it("keeps the original stamp beside the epoch", () => {
    const rows = toSeries([bar("2026-08-19T03:45:00+00:00")]);
    expect(rows[0].timestamp).toBe("2026-08-19T03:45:00+00:00");
  });

  it("reads the same instant identically whatever offset expresses it", () => {
    /* IST only decides how a moment is drawn. It must not decide which
       moment it is. */
    const utc = toSeries([bar("2026-08-19T03:45:00+00:00")])[0];
    const ist = toSeries([bar("2026-08-19T09:15:00+05:30")])[0];
    expect(utc.ms).toBe(ist.ms);
    expect(hhmm(utc.ms)).toBe("09:15 am");
    expect(sessionOf(utc.ms)).toBe(sessionOf(ist.ms));
  });
});

describe("multiple sessions", () => {
  it("groups bars by their IST trading date", () => {
    const rows = toSeries([...WED, ...THU], 200);
    expect(new Set(rows.map((r) => r.session)))
      .toEqual(new Set(["2026-08-19", "2026-08-20"]));
  });

  it("marks exactly one opening per session", () => {
    const rows = toSeries([...WED, ...THU], 200);
    const starts = rows.filter((r) => r.sessionStart);
    expect(starts).toHaveLength(2);
    expect(starts.map((r) => hhmm(r.ms))).toEqual(["09:15 am", "09:15 am"]);
  });

  it("marks the opening even when the window starts mid-session", () => {
    const rows = toSeries([...WED.slice(-20), ...THU.slice(0, 20)], 200);
    const starts = rows.filter((r) => r.sessionStart);
    expect(starts).toHaveLength(2);
    expect(starts[0].ms).toBe(rows[0].ms);          // the window's own edge
    expect(hhmm(starts[1].ms)).toBe("09:15 am");    // the real boundary
  });

  it("labels every session opening with its date", () => {
    const rows = toSeries([...WED, ...THU], 200);
    const ticks = pickTicks(rows);
    for (const start of rows.filter((r) => r.sessionStart)) {
      expect(ticks).toContain(start.ms);
    }
  });

  it("emits ticks in chronological order", () => {
    const ticks = pickTicks(toSeries([...WED, ...THU], 200));
    expect(isAscending(ticks)).toBe(true);
    expect(ticks.length).toBeGreaterThan(2);
  });

  it("handles a single session and an empty window", () => {
    expect(toSeries(WED, 200).filter((r) => r.sessionStart)).toHaveLength(1);
    expect(toSeries([])).toEqual([]);
    expect(toSeries(null)).toEqual([]);
    expect(pickTicks([])).toEqual([]);
  });
});

describe("rendered panel", () => {
  it("reports the window's real date span, not a time of day", () => {
    render(<PriceChart candles={[...WED, ...THU]} signal={null} bars={200} />);
    expect(screen.getByText(/2 sessions/)).toBeTruthy();
    expect(screen.getByText(/19 Aug.*20 Aug.*IST/)).toBeTruthy();
  });

  it("says so plainly when there are no candles", () => {
    render(<PriceChart candles={[]} signal={null} />);
    expect(screen.getByText("No candles loaded.")).toBeTruthy();
  });

  it("plots only the candles it was given", () => {
    /* The live price arrives every few seconds and the archive advances
       every five minutes. Appending the tick as a bar would invent a candle
       that never closed, at a timestamp no session ever produced. */
    const rows = toSeries([...WED, ...THU], 200);
    expect(rows).toHaveLength(151);
    expect(rows[rows.length - 1].close).toBe(THU[THU.length - 1].close);
    expect(rows.every((r) => r.ms % (5 * 60_000) === 0)).toBe(true);
  });
});
