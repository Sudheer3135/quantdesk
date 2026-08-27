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
import { beforeAll, describe, expect, it } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";

import PriceChart, {
  candleGeometry, structureMarkers,
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
  // The moving averages and ATR the candles endpoint already carries. Named
  // exactly as the backend spells them, so a rename there fails a test here
  // rather than silently drawing a flat line at zero.
  ema20: close - 3, ema50: close - 8, ema200: close - 25, atr14: 19.5,
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


/* --- the terminal readout -------------------------------------------------

   The chart gained the moving averages a desk actually reads a 5-minute
   NIFTY chart against, plus a numeric readout of the newest bar. The readout
   matters more than it looks: an axis tells you roughly where a line is, and
   "roughly" is not a level you can place a stop against.
*/
describe("indicator readout", () => {
  it("states the newest bar's own numbers", () => {
    render(<PriceChart candles={THU} signal={null} />);
    const last = THU[THU.length - 1];
    expect(screen.getByText(`C ${last.close.toFixed(2)}`)).toBeTruthy();
    expect(screen.getByText(`VWAP ${last.vwap.toFixed(2)}`)).toBeTruthy();
    expect(screen.getByText(`EMA20 ${last.ema20.toFixed(2)}`)).toBeTruthy();
    expect(screen.getByText(`EMA50 ${last.ema50.toFixed(2)}`)).toBeTruthy();
    expect(screen.getByText(`EMA200 ${last.ema200.toFixed(2)}`)).toBeTruthy();
    expect(screen.getByText("ATR 19.50")).toBeTruthy();
  });

  it("dashes an indicator the feed did not send, never a zero", () => {
    /* EMA200 needs 200 bars. Early in an archive it is genuinely absent, and
       a chart that drew it at 0 would put a line at the bottom of the axis
       and call it a moving average. */
    const thin = THU.map(({ ema200, ...rest }) => rest);
    render(<PriceChart candles={thin} signal={null} />);
    expect(screen.getByText("EMA200 —")).toBeTruthy();
    expect(screen.queryByText("EMA200 0.00")).toBeNull();
  });

  it("titles itself as the instrument and timeframe it is", () => {
    render(<PriceChart candles={THU} signal={null} />);
    expect(screen.getByText("NIFTY 50 · 5m")).toBeTruthy();
  });

  it("renders with structure present without throwing", () => {
    render(<PriceChart candles={THU} signal={{
      action: "BUY", entry: 24_380, stop_loss: 24_350, target: 24_440,
      context: {
        unfilled_fvgs: [{ midpoint: 24_360 }],
        liquidity_pools: [{ level: 24_330, side: "buyside", swept: false }],
      },
    }} />);
    expect(screen.getByText("NIFTY 50 · 5m")).toBeTruthy();
  });
});

/* --- which structure gets drawn ------------------------------------------

   Tested as a function rather than through the SVG. Recharts does not render
   its children under jsdom's zero-width container, so an assertion on a
   `<ReferenceLine>` label passes and fails for reasons that have nothing to
   do with the rule being checked.
*/
describe("structure markers", () => {
  it("draws nothing when the signal reported no structure", () => {
    expect(structureMarkers(null)).toEqual({ gaps: [], pools: [] });
    expect(structureMarkers({ action: "HOLD" })).toEqual({ gaps: [], pools: [] });
    expect(structureMarkers({ context: {} })).toEqual({ gaps: [], pools: [] });
  });

  it("drops a pool that has already been swept", () => {
    /* A pool that has been taken is not a level any more, and drawing it
       would mark support where support has already gone. */
    const { pools } = structureMarkers({ context: { liquidity_pools: [
      { level: 24_180, side: "buyside", swept: true },
      { level: 24_320, side: "sellside", swept: false },
    ] } });
    expect(pools).toHaveLength(1);
    expect(pools[0].level).toBe(24_320);
  });

  it("ignores a gap with no usable midpoint", () => {
    const { gaps } = structureMarkers({ context: { unfilled_fvgs: [
      { midpoint: null }, { top: 1, bottom: 0 }, { midpoint: 24_232.05 },
    ] } });
    expect(gaps).toHaveLength(1);
    expect(gaps[0].midpoint).toBe(24_232.05);
  });

  it("caps how many it will draw, so the chart stays readable", () => {
    const many = Array.from({ length: 9 }, (_, i) => ({ midpoint: 24_000 + i }));
    expect(structureMarkers({ context: { unfilled_fvgs: many } }).gaps)
      .toHaveLength(3);
  });
});


/* The candles.

   Recharts hands a custom bar shape one rectangle and nothing else: top
   edge = high, bottom edge = low. Every other price on the candle has to be
   placed inside it by arithmetic, and jsdom renders none of the resulting
   SVG — so if the conversion is wrong, a green body appears on a bar that
   closed down and no test would notice. These read the arithmetic directly.

   The box below is deliberately easy to check by hand: 100 pixels tall
   spanning 100 points of price, so one point is one pixel and every
   expected coordinate can be counted rather than recomputed. */
const BOX = { x: 200, y: 50, width: 9, height: 100 };
const priceAt = (px) => 24_200 - (px - BOX.y);   // y=50 is the high, 24,200

describe("candle geometry", () => {
  it("places the body between open and close, inside its own wick", () => {
    // low 24,100 .. high 24,200; opened at 24,120 and closed at 24,180.
    const geo = candleGeometry(
      { open: 24_120, close: 24_180, low: 24_100, high: 24_200 }, BOX);

    expect(geo.wickTop).toBe(50);            // the high
    expect(geo.wickBottom).toBe(150);        // the low
    expect(geo.bodyY).toBe(70);              // close, 20 points below the high
    expect(geo.bodyY + geo.bodyHeight).toBe(130);   // open

    expect(priceAt(geo.bodyY)).toBe(24_180);
    expect(priceAt(geo.bodyY + geo.bodyHeight)).toBe(24_120);

    // The body may touch the wick's ends but must never escape them.
    expect(geo.bodyY).toBeGreaterThanOrEqual(geo.wickTop);
    expect(geo.bodyY + geo.bodyHeight).toBeLessThanOrEqual(geo.wickBottom);
  });

  it("colours the body against the open, not against anything else", () => {
    const up = candleGeometry(
      { open: 24_120, close: 24_180, low: 24_100, high: 24_200 }, BOX);
    const down = candleGeometry(
      { open: 24_180, close: 24_120, low: 24_100, high: 24_200 }, BOX);

    expect(up.tone).toBe("up");
    expect(down.tone).toBe("down");

    /* The same two bars, swapped. A conversion that reads the rectangle
       rather than the values would give both candles the same body, since
       the pixel rect for [min, max] is identical either way. */
    expect(down.bodyY).toBe(up.bodyY);
    expect(down.bodyHeight).toBe(up.bodyHeight);
  });

  it("draws a doji as a mark rather than as nothing", () => {
    const geo = candleGeometry(
      { open: 24_150, close: 24_150, low: 24_100, high: 24_200 }, BOX);

    expect(geo.tone).toBe("flat");
    // A zero-height rect renders nothing at all, which reads as missing data.
    expect(geo.bodyHeight).toBeGreaterThanOrEqual(1);
    expect(geo.bodyY).toBe(100);
  });

  it("survives a bar that never moved", () => {
    /* high === low is a divide by zero. NaN in an SVG coordinate draws
       nothing and throws nothing, so this is exactly the failure that hides. */
    const geo = candleGeometry(
      { open: 24_150, close: 24_150, low: 24_150, high: 24_150 }, BOX);

    for (const v of Object.values(geo)) {
      if (typeof v === "number") expect(Number.isFinite(v)).toBe(true);
    }
    expect(geo.bodyHeight).toBeGreaterThanOrEqual(1);
  });

  it("centres the body on the wick", () => {
    const geo = candleGeometry(
      { open: 24_120, close: 24_180, low: 24_100, high: 24_200 }, BOX);

    expect(geo.centre).toBe(BOX.x + BOX.width / 2);
    expect(geo.bodyX + geo.bodyWidth / 2).toBe(geo.centre);
    // Narrower than the band it sits in, so neighbouring candles stay
    // visibly separate at 120 bars rather than fusing into a slab.
    expect(geo.bodyWidth).toBeLessThan(BOX.width);
  });

  it("keeps a body on the tightest band it will ever be given", () => {
    // A very dense window shrinks the band below the inset. The body must
    // narrow to a hairline, never to zero or to a negative width.
    for (const width of [1, 1.5, 2, 2.5, 3]) {
      const geo = candleGeometry(
        { open: 24_120, close: 24_180, low: 24_100, high: 24_200 },
        { ...BOX, width });
      expect(geo.bodyWidth).toBeGreaterThanOrEqual(1);
      expect(geo.bodyX + geo.bodyWidth / 2).toBeCloseTo(geo.centre, 10);
    }
  });

  it("caps the body width, so a short window does not draw slabs", () => {
    // Twelve bars across a wide panel gives recharts a very fat band.
    const geo = candleGeometry(
      { open: 24_120, close: 24_180, low: 24_100, high: 24_200 },
      { ...BOX, width: 90 });

    expect(geo.bodyWidth).toBeLessThanOrEqual(13);
    expect(geo.bodyX + geo.bodyWidth / 2).toBe(geo.centre);
  });

  it("draws nothing at all when the bar is incomplete", () => {
    // Not a flat candle at zero — no candle. A missing high is unknown
    // range, and a chart must not invent one to have something to draw.
    expect(candleGeometry(
      { open: 24_120, close: 24_180, low: 24_100, high: null }, BOX)).toBeNull();
    expect(candleGeometry({ open: 24_120 }, BOX)).toBeNull();
    expect(candleGeometry(null, BOX)).toBeNull();
    // Recharts sizes bars from the container, which is zero-width on the
    // first paint before the ResizeObserver fires.
    expect(candleGeometry(
      { open: 24_120, close: 24_180, low: 24_100, high: 24_200 },
      { ...BOX, width: 0 })).toBeNull();
  });
});

describe("the plotted series", () => {
  it("carries the high-low range each candle is drawn into", () => {
    const rows = toSeries(WED.slice(0, 3));
    expect(rows[0].range).toEqual([WED[0].low, WED[0].high]);
  });

  it("gives a bar with no range no range to draw", () => {
    const broken = { ...WED[0], high: null };
    const rows = toSeries([broken]);
    expect(rows[0].range).toBeNull();
  });

  it("offers candles and a line, and opens on candles", () => {
    render(<PriceChart candles={WED} signal={null} />);
    const group = screen.getByRole("group", { name: /price style/i });

    const candles = within(group).getByRole("button", { name: "candles" });
    const line = within(group).getByRole("button", { name: "line" });

    expect(candles.getAttribute("aria-pressed")).toBe("true");
    expect(line.getAttribute("aria-pressed")).toBe("false");

    fireEvent.click(line);
    expect(within(group).getByRole("button", { name: "line" })
      .getAttribute("aria-pressed")).toBe("true");
    expect(within(group).getByRole("button", { name: "candles" })
      .getAttribute("aria-pressed")).toBe("false");
  });
});
