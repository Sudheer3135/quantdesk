import { describe, expect, it } from "vitest";
import {
  LOAD_MORE_THRESHOLD_BARS, applyTick, bucketStart, formatStampIST,
  formatTickIST, istDateKey,
  mergeLive, mergeOlder, priceLines, shouldLoadOlder, sortDedupe,
  toCandleSeries, toCloseSeries, toEpochSeconds, toLineSeries,
  toSessionSeries,
} from "./chart-data.js";

const bar = (ts, over = {}) => ({
  timestamp: ts, open: 100, high: 105, low: 99, close: 104, ...over,
});

describe("toEpochSeconds", () => {
  it("converts an ISO stamp to unix seconds", () => {
    expect(toEpochSeconds("2026-08-28T03:45:00+00:00")).toBe(1787888700);
  });

  it("returns null rather than NaN for junk", () => {
    expect(toEpochSeconds("not a date")).toBeNull();
    expect(toEpochSeconds(null)).toBeNull();
    expect(toEpochSeconds(undefined)).toBeNull();
  });

  it("respects the offset rather than assuming UTC", () => {
    expect(toEpochSeconds("2026-08-28T09:15:00+05:30"))
      .toBe(toEpochSeconds("2026-08-28T03:45:00+00:00"));
  });
});

describe("toCandleSeries", () => {
  it("maps OHLC and keys by time", () => {
    const out = toCandleSeries([bar("2026-08-28T03:45:00+00:00")]);
    expect(out).toEqual([{ time: 1787888700, open: 100, high: 105,
                           low: 99, close: 104 }]);
  });

  it("drops a bar missing any price", () => {
    // lightweight-charts throws on a null in an OHLC field, taking the
    // whole panel down rather than skipping the bar.
    const rows = [bar("2026-08-28T03:45:00+00:00"),
                  bar("2026-08-28T03:50:00+00:00", { high: null }),
                  bar("2026-08-28T03:55:00+00:00", { close: undefined })];
    expect(toCandleSeries(rows)).toHaveLength(1);
  });

  it("drops a bar with an unparseable timestamp", () => {
    expect(toCandleSeries([bar("nonsense")])).toEqual([]);
  });

  it("sorts ascending regardless of input order", () => {
    const out = toCandleSeries([bar("2026-08-28T04:00:00+00:00"),
                                bar("2026-08-28T03:45:00+00:00")]);
    expect(out.map((p) => p.time)).toEqual([1787888700, 1787889600]);
  });

  it("collapses duplicate timestamps to one bar", () => {
    const out = toCandleSeries([bar("2026-08-28T03:45:00+00:00"),
                                bar("2026-08-28T03:45:00+00:00", { close: 111 })]);
    expect(out).toHaveLength(1);
    expect(out[0].close).toBe(111);
  });

  it("survives a non-array", () => {
    expect(toCandleSeries(null)).toEqual([]);
    expect(toCandleSeries(undefined)).toEqual([]);
  });
});

describe("toLineSeries", () => {
  it("omits points where the indicator has not warmed up", () => {
    // Plotting a null EMA200 as 0 draws a line from the floor of the chart
    // to the price, which reads as a crash.
    const rows = [bar("2026-08-28T03:45:00+00:00", { ema200: null }),
                  bar("2026-08-28T03:50:00+00:00", { ema200: 24100 })];
    expect(toLineSeries(rows, "ema200"))
      .toEqual([{ time: 1787889000, value: 24100 }]);
  });

  it("keeps a legitimate zero", () => {
    const rows = [bar("2026-08-28T03:45:00+00:00", { vwap: 0 })];
    expect(toLineSeries(rows, "vwap")).toHaveLength(1);
  });

  it("omits NaN, which pandas produces for an empty window", () => {
    const rows = [bar("2026-08-28T03:45:00+00:00", { rvol: NaN })];
    expect(toLineSeries(rows, "rvol")).toEqual([]);
  });
});

describe("toCloseSeries", () => {
  it("reduces a bar to its close", () => {
    expect(toCloseSeries([bar("2026-08-28T03:45:00+00:00")]))
      .toEqual([{ time: 1787888700, value: 104 }]);
  });
});

describe("sortDedupe", () => {
  it("keeps the last value for a repeated time", () => {
    expect(sortDedupe([{ time: 2, value: "a" }, { time: 1, value: "b" },
                       { time: 2, value: "c" }]))
      .toEqual([{ time: 1, value: "b" }, { time: 2, value: "c" }]);
  });

  it("drops points with no time", () => {
    expect(sortDedupe([{ time: null }, { time: undefined }, { time: 1 }]))
      .toEqual([{ time: 1 }]);
  });
});

describe("mergeOlder", () => {
  it("puts the fetched page beneath what is loaded", () => {
    const loaded = [bar("2026-08-28T03:45:00+00:00")];
    const older = [bar("2026-08-27T03:45:00+00:00")];
    expect(mergeOlder(loaded, older).map((r) => r.timestamp))
      .toEqual(["2026-08-27T03:45:00+00:00", "2026-08-28T03:45:00+00:00"]);
  });

  it("collapses an overlap at the seam", () => {
    const seam = "2026-08-28T03:45:00+00:00";
    const merged = mergeOlder([bar(seam)], [bar(seam)]);
    expect(merged).toHaveLength(1);
  });

  it("prefers the already-loaded row at the seam", () => {
    // The loaded row may carry a live update the archive page does not.
    const seam = "2026-08-28T03:45:00+00:00";
    const merged = mergeOlder([bar(seam, { close: 999 })], [bar(seam)]);
    expect(merged[0].close).toBe(999);
  });

  it("handles either side being empty or absent", () => {
    expect(mergeOlder([], [])).toEqual([]);
    expect(mergeOlder(null, null)).toEqual([]);
    expect(mergeOlder([bar("2026-08-28T03:45:00+00:00")], null)).toHaveLength(1);
  });
});

describe("mergeLive", () => {
  const ts = "2026-08-28T09:50:00+00:00";

  it("lets the live copy of a forming bar overwrite the archived one", () => {
    // The archive holds what the last candle looked like when it was
    // written; the live prop holds what it looks like now. Preferring the
    // archive freezes the right edge of the chart mid-session — the candle
    // stops moving while the price above it keeps ticking.
    const archived = [bar(ts, { close: 100, high: 2 })];
    const live = [bar(ts, { close: 155, high: 9 })];
    const merged = mergeLive(archived, live);
    expect(merged).toHaveLength(1);
    expect(merged[0].close).toBe(155);
    expect(merged[0].high).toBe(9);
  });

  it("is the opposite precedence to mergeOlder on the same inputs", () => {
    const archived = [bar(ts, { close: 100 })];
    const live = [bar(ts, { close: 155 })];
    expect(mergeLive(archived, live)[0].close).toBe(155);
    expect(mergeOlder(archived, live)[0].close).toBe(100);
  });

  it("appends a genuinely new bar", () => {
    const merged = mergeLive([bar("2026-08-28T09:45:00+00:00")], [bar(ts)]);
    expect(merged).toHaveLength(2);
    expect(merged[1].timestamp).toBe(ts);
  });

  it("keeps the loaded rows when the live prop is empty", () => {
    expect(mergeLive([bar(ts)], [])).toHaveLength(1);
    expect(mergeLive([bar(ts)], null)).toHaveLength(1);
  });
});

describe("applyTick — the forming candle", () => {
  const T = "2026-08-31T04:35:00+00:00";          // 10:05 IST
  const slot = bucketStart(toEpochSeconds(T));
  const last = { time: slot, open: 24030, high: 24040, low: 24025, close: 24035 };
  const tick = (price, iso, over = {}) =>
    ({ price, source_time: iso, market_open: true, ...over });

  it("moves the close with the tick", () => {
    expect(applyTick(last, tick(24045, "2026-08-31T04:36:31+00:00")).close)
      .toBe(24045);
  });

  it("never revises the open", () => {
    // The open was set by the first trade of the bar. It is not the live
    // feed's to change.
    expect(applyTick(last, tick(24045, "2026-08-31T04:36:31+00:00")).open)
      .toBe(24030);
  });

  it("ratchets the high", () => {
    const out = applyTick(last, tick(24099, "2026-08-31T04:36:31+00:00"));
    expect(out.high).toBe(24099);
    expect(out.low).toBe(24025);
  });

  it("ratchets the low", () => {
    const out = applyTick(last, tick(23990, "2026-08-31T04:36:31+00:00"));
    expect(out.low).toBe(23990);
    expect(out.high).toBe(24040);
  });

  it("does not shrink the high or low on an inside tick", () => {
    const out = applyTick(last, tick(24032, "2026-08-31T04:36:31+00:00"));
    expect(out.high).toBe(24040);
    expect(out.low).toBe(24025);
  });

  it("opens a new bar when the tick crosses the bucket", () => {
    const out = applyTick(last, tick(24045, "2026-08-31T04:40:02+00:00"));
    expect(out.time).toBe(slot + 300);
    expect(out).toMatchObject({ open: 24045, high: 24045,
                                low: 24045, close: 24045 });
  });

  it("buckets by the timeframe it is given", () => {
    const fifteen = { ...last, time: bucketStart(toEpochSeconds(T), 900) };
    // 04:36 and 04:44 are the same 15-minute bar; 04:46 is the next one.
    expect(applyTick(fifteen, tick(24045, "2026-08-31T04:44:00+00:00"), 900).time)
      .toBe(fifteen.time);
    expect(applyTick(fifteen, tick(24045, "2026-08-31T04:46:00+00:00"), 900).time)
      .toBe(fifteen.time + 900);
  });

  it("opens the first bar when there is nothing yet", () => {
    const out = applyTick(null, tick(24045, "2026-08-31T04:36:31+00:00"));
    expect(out.time).toBe(slot);
    expect(out.open).toBe(24045);
  });

  it("ignores a tick while the market is shut", () => {
    // A closed market still publishes the last traded price, forever. Left
    // ungated it opens empty bars all night and by morning the chart shows
    // a flat plateau that never traded.
    expect(applyTick(last, tick(24045, "2026-08-31T12:00:00+00:00",
                                { market_open: false }))).toBeNull();
  });

  it("ignores a tick older than the bar, as a reconnect replay sends", () => {
    expect(applyTick(last, tick(24045, "2026-08-31T04:20:00+00:00"))).toBeNull();
  });

  it("treats an off-grid last bar as belonging to its own bucket", () => {
    // A source has been caught handing back a "current" bar stamped a few
    // minutes past its own bucket start rather than at the bucket start
    // itself (Yahoo, for the still-forming candle — see freedata.py).
    // Comparing against `last.time` directly there made every tick before
    // the *next* real bucket read as older than a bar from the future, and
    // applyTick returned null until that bucket finally rolled over —
    // measured live on 15-Sep-2026, this froze the chart for minutes at a
    // stretch. It must reason about the bucket `last` belongs to instead.
    const offGrid = { time: slot + 145, open: 24030, high: 24040,
                      low: 24025, close: 24035 };

    const same = applyTick(offGrid, tick(24050, "2026-08-31T04:38:00+00:00"));
    expect(same.time).toBe(slot);            // snapped back onto the grid
    expect(same.open).toBe(24030);
    expect(same.close).toBe(24050);

    const next = applyTick(offGrid, tick(24060, "2026-08-31T04:41:00+00:00"));
    expect(next.time).toBe(slot + 300);      // the following bucket
    expect(next).toMatchObject({ open: 24060, high: 24060,
                                 low: 24060, close: 24060 });
  });

  it("ignores a tick with no usable price or time", () => {
    // `Number(null)` is 0 and finite. Coercing before checking would draw
    // the candle's close at zero, on the floor of the chart.
    expect(applyTick(last, tick(null, "2026-08-31T04:36:31+00:00"))).toBeNull();
    expect(applyTick(last, tick(undefined, "2026-08-31T04:36:31+00:00"))).toBeNull();
    expect(applyTick(last, tick("", "2026-08-31T04:36:31+00:00"))).toBeNull();
    expect(applyTick(last, tick(0, "2026-08-31T04:36:31+00:00"))).toBeNull();
    expect(applyTick(last, tick(-5, "2026-08-31T04:36:31+00:00"))).toBeNull();
    expect(applyTick(last, tick(24045, "nonsense"))).toBeNull();
    expect(applyTick(last, null)).toBeNull();
  });

  it("accepts a numeric string, which JSON sometimes carries", () => {
    expect(applyTick(last, tick("24045.5", "2026-08-31T04:36:31+00:00")).close)
      .toBe(24045.5);
  });

  it("falls back to `at` when there is no source_time", () => {
    const out = applyTick(last, { price: 24045, at: "2026-08-31T04:36:31+00:00",
                                  market_open: true });
    expect(out.close).toBe(24045);
  });
});

describe("bucketStart", () => {
  it("floors to the bar boundary", () => {
    const t = toEpochSeconds("2026-08-31T04:36:31+00:00");
    expect(bucketStart(t)).toBe(toEpochSeconds("2026-08-31T04:35:00+00:00"));
  });

  it("leaves a boundary instant alone", () => {
    const t = toEpochSeconds("2026-08-31T04:35:00+00:00");
    expect(bucketStart(t)).toBe(t);
  });
});

describe("mergeLive keeps the archive's indicators", () => {
  const ts = "2026-08-28T09:50:00+00:00";

  it("takes the live price but not the live EMA", () => {
    // The live endpoint serves ~86 bars and `ema` seeds from the first bar
    // it is given, so its "EMA200" is the mean of 86 bars. Measured against
    // the warmed archive at the same timestamp: 24,109 vs 24,149. Letting
    // it through draws a forty-point cliff where the live window starts.
    const archived = [bar(ts, { close: 100, ema200: 24149, vwap: 5 })];
    const live = [bar(ts, { close: 155, ema200: 24109, vwap: 9 })];
    const merged = mergeLive(archived, live);

    expect(merged[0].close).toBe(155);
    expect(merged[0].ema200).toBe(24149);
    expect(merged[0].vwap).toBe(5);
  });

  it("takes every live price column", () => {
    const archived = [bar(ts, { open: 1, high: 2, low: 3, close: 4 })];
    const live = [bar(ts, { open: 10, high: 20, low: 30, close: 40 })];
    expect(mergeLive(archived, live)[0])
      .toMatchObject({ open: 10, high: 20, low: 30, close: 40 });
  });

  it("carries no indicators on a bar newer than the archive", () => {
    // Better an overlay that stops one bar short than one that lies about
    // where it is.
    const merged = mergeLive([bar("2026-08-28T09:45:00+00:00", { ema200: 1 })],
                             [bar(ts, { ema200: 99999 })]);
    expect(merged).toHaveLength(2);
    expect(merged[1].ema200).toBeUndefined();
    expect(merged[1].close).toBe(104);
  });

  it("drops such a bar from an overlay rather than plotting garbage", () => {
    const merged = mergeLive([bar("2026-08-28T09:45:00+00:00", { ema200: 24149 })],
                             [bar(ts, { ema200: 99999 })]);
    expect(toLineSeries(merged, "ema200")).toEqual([
      { time: toEpochSeconds("2026-08-28T09:45:00+00:00"), value: 24149 },
    ]);
  });

  it("still draws that bar as a candle", () => {
    const merged = mergeLive([bar("2026-08-28T09:45:00+00:00")], [bar(ts)]);
    expect(toCandleSeries(merged)).toHaveLength(2);
  });
});

describe("toSessionSeries", () => {
  const at = (day, hhmm, over) =>
    bar(`2026-08-${day}T${hhmm}:00+00:00`, over);

  it("breaks the line at the session open instead of drawing across it", () => {
    // VWAP restarts every morning. Drawn continuously the reset renders as
    // a vertical stroke — 142 points on 20-Aug — a price move that never
    // happened.
    const rows = [at("19", "09:55", { vwap: 24060 }),
                  at("20", "03:45", { vwap: 24202 }),
                  at("20", "03:50", { vwap: 24205 })];
    const out = toSessionSeries(rows, "vwap");

    expect(out[0].value).toBe(24060);
    expect(out[1].value).toBeUndefined();   // whitespace = the break
    expect(out[2].value).toBe(24205);
  });

  it("does not break inside a session", () => {
    const rows = [at("20", "03:45", { vwap: 1 }), at("20", "03:50", { vwap: 2 }),
                  at("20", "03:55", { vwap: 3 })];
    const out = toSessionSeries(rows, "vwap");
    expect(out.every((p) => p.value !== undefined)).toBe(true);
  });

  it("emits whitespace where the value is missing", () => {
    const rows = [at("20", "03:45", { vwap: 1 }), at("20", "03:50", { vwap: null })];
    expect(toSessionSeries(rows, "vwap")[1].value).toBeUndefined();
  });

  it("uses the IST day, not the UTC day", () => {
    // 18:30 UTC is 00:00 IST the next day. Keying on UTC would split the
    // afternoon session in half.
    expect(istDateKey(toEpochSeconds("2026-08-28T03:45:00+00:00")))
      .toBe(istDateKey(toEpochSeconds("2026-08-28T09:55:00+00:00")));
  });

  it("survives a non-array", () => {
    expect(toSessionSeries(null, "vwap")).toEqual([]);
  });
});

describe("shouldLoadOlder", () => {
  const ok = { range: { from: 3 }, loading: false, hasMore: true,
               oldest: "2026-08-28T03:45:00+00:00" };

  it("fetches once the view nears the left edge", () => {
    expect(shouldLoadOlder(ok)).toBe(true);
  });

  it("does not fetch while the view is well inside the data", () => {
    expect(shouldLoadOlder({ ...ok, range: { from: 500 } })).toBe(false);
  });

  it("does not fetch with a request already in flight", () => {
    expect(shouldLoadOlder({ ...ok, loading: true })).toBe(false);
  });

  it("stops asking once the archive says it has nothing older", () => {
    // Otherwise reaching the start of history means one request per pan
    // event, for as long as the tab is open.
    expect(shouldLoadOlder({ ...ok, hasMore: false })).toBe(false);
  });

  it("does not fetch before the first page has established a cursor", () => {
    expect(shouldLoadOlder({ ...ok, oldest: null })).toBe(false);
  });

  it("does not fetch on the first paint, before there is a range", () => {
    expect(shouldLoadOlder({ ...ok, range: null })).toBe(false);
    expect(shouldLoadOlder({ ...ok, range: {} })).toBe(false);
  });

  it("fetches when the view has run off the start of the data", () => {
    expect(shouldLoadOlder({ ...ok, range: { from: -40 } })).toBe(true);
  });

  it("uses the documented threshold", () => {
    expect(shouldLoadOlder({ ...ok,
      range: { from: LOAD_MORE_THRESHOLD_BARS - 1 } })).toBe(true);
    expect(shouldLoadOlder({ ...ok,
      range: { from: LOAD_MORE_THRESHOLD_BARS } })).toBe(false);
  });
});

describe("IST formatting", () => {
  const open = toEpochSeconds("2026-08-28T03:45:00+00:00");

  it("renders the 03:45 UTC open as 09:15 IST", () => {
    // Shown in UTC this reads 03:45, which looks like a data fault rather
    // than a display one.
    expect(formatTickIST(open)).toBe("09:15");
  });

  it("renders a date tick when the day changes", () => {
    expect(formatTickIST(open, true)).toBe("28 Aug");
  });

  it("gives the crosshair an unambiguous stamp", () => {
    expect(formatStampIST(open)).toBe("28 Aug 2026  09:15 IST");
  });

  it("does not shift the underlying instant", () => {
    // The timestamps stay UTC; only the rendering is IST. Shifting them
    // would put every bar 5.5 hours from where it happened.
    expect(toEpochSeconds("2026-08-28T03:45:00+00:00")).toBe(open);
  });
});

describe("priceLines", () => {
  it("draws entry, stop and target from a plan", () => {
    const out = priceLines({ plan: { entry: 24160, stop: 24140, target: 24220 } });
    expect(out.map((l) => l.key)).toEqual(["entry", "stop", "target"]);
  });

  it("reads levels off the signal itself when there is no plan", () => {
    expect(priceLines({ entry: 24160 }).map((l) => l.key)).toEqual(["entry"]);
  });

  it("omits a level that is absent rather than drawing it at zero", () => {
    const out = priceLines({ plan: { entry: 24160, stop: null } });
    expect(out.map((l) => l.key)).toEqual(["entry"]);
  });

  it("returns nothing for no signal", () => {
    expect(priceLines(null)).toEqual([]);
    expect(priceLines(undefined)).toEqual([]);
    expect(priceLines({})).toEqual([]);
  });
});
