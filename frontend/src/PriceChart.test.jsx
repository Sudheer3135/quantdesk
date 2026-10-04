/* The chart component's wiring.

   lightweight-charts draws to a canvas and jsdom has no canvas, so the
   renderer is mocked and what is asserted here is the wiring: which series
   get created, what data reaches them, that zoom survives a data change,
   and that panning left pages the archive without hammering it.

   The shaping and the paging arithmetic are pure and tested directly in
   chart-data.test.jsx. */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/* ---- the mocked renderer ------------------------------------------- */

const made = {
  charts: [],
  series: [],
};

function makeSeries(kind, options) {
  const s = {
    kind, options,
    data: null,
    priceLines: [],
    setData: vi.fn(function (d) { this.data = d; }),
    updates: [],
    update: vi.fn(function (bar) {
      /* lightweight-charts' real contract, enforced here on purpose.
         `update()` may only touch the last bar or append after it; handed
         anything earlier the library throws

             Cannot update oldest data, last time=…, new time=…

         and with no error boundary above it that unmounts the whole desk
         and leaves a black page. This double used to accept a backwards
         update silently, which is exactly why a suite of 276 green tests
         sat alongside a dashboard that crashed on every refresh. A test
         double that is more forgiving than the real thing does not test
         the real thing. */
      const last = this.data && this.data.length
        ? this.data[this.data.length - 1] : null;
      if (last && bar.time < last.time) {
        throw new Error(
          `Cannot update oldest data, last time=${last.time}, `
          + `new time=${bar.time}`);
      }
      this.updates.push(bar);
      if (!this.data) return;
      const i = this.data.findIndex((b) => b.time === bar.time);
      if (i >= 0) this.data[i] = bar; else this.data.push(bar);
    }),
    createPriceLine: vi.fn(function (l) {
      this.priceLines.push(l); return l;
    }),
    removePriceLine: vi.fn(function (l) {
      this.priceLines = this.priceLines.filter((x) => x !== l);
    }),
  };
  made.series.push(s);
  return s;
}

function makeChart() {
  const rangeHandlers = new Set();
  const crosshairHandlers = new Set();
  const chart = {
    removed: false,
    rangeHandlers,
    crosshairHandlers,
    series: [],
    addSeries: vi.fn((kind, options) => {
      const s = makeSeries(kind, options);
      chart.series.push(s);
      return s;
    }),
    removeSeries: vi.fn((s) => {
      chart.series = chart.series.filter((x) => x !== s);
    }),
    subscribeCrosshairMove: vi.fn((h) => crosshairHandlers.add(h)),
    timeScale: vi.fn(() => ({
      subscribeVisibleLogicalRangeChange: vi.fn((h) => rangeHandlers.add(h)),
      unsubscribeVisibleLogicalRangeChange: vi.fn((h) => rangeHandlers.delete(h)),
    })),
    remove: vi.fn(() => { chart.removed = true; }),
  };
  made.charts.push(chart);
  return chart;
}

vi.mock("lightweight-charts", () => ({
  createChart: vi.fn(() => makeChart()),
  CandlestickSeries: "CandlestickSeries",
  LineSeries: "LineSeries",
  ColorType: { Solid: "solid" },
  CrosshairMode: { Normal: 0 },
  LineStyle: { Solid: 0, Dotted: 1, Dashed: 2 },
}));

const { createChart } = await import("lightweight-charts");

const getJSON = vi.fn();
vi.mock("./api.js", () => ({ getJSON: (...a) => getJSON(...a) }));

const { default: PriceChart, RECENT_REFRESH_MS } = await import("./PriceChart.jsx");

/* ---- fixtures ------------------------------------------------------- */

function bars(n, startIso = "2026-08-28T03:45:00+00:00") {
  const t0 = new Date(startIso).getTime();
  return Array.from({ length: n }, (_, i) => ({
    timestamp: new Date(t0 + i * 5 * 60_000).toISOString(),
    open: 24100 + i, high: 24110 + i, low: 24090 + i, close: 24105 + i,
    vwap: 24100, ema20: 24102, ema50: 24101, ema200: 24099, atr14: 12.5,
  }));
}

const page = (rows, over = {}) => ({
  candles: rows,
  has_more: true,
  oldest: rows.length ? rows[0].timestamp : null,
  ...over,
});

/* The first archive fetch resolves after mount, so its state update lands
   outside React's act() unless the render is awaited inside one. */
async function renderChart(ui) {
  let utils;
  await act(async () => { utils = render(ui); });
  return utils;
}

const chart = () => made.charts[0];
const priceSeries = () =>
  chart().series.find((s) => s.kind === "CandlestickSeries"
                          || (s.kind === "LineSeries" && s.options.lineWidth === 2));

beforeEach(() => {
  made.charts = [];
  made.series = [];
  getJSON.mockReset();
  getJSON.mockResolvedValue(page(bars(60)));
});

afterEach(cleanup);

/* ---- tests ---------------------------------------------------------- */

describe("axis labels", () => {
  it("shows the date only at a day boundary, not on every tick", async () => {
    // lightweight-charts hands tickMarkFormatter a TickMarkType: Year=0,
    // Month=1, DayOfMonth=2, Time=3, TimeWithSeconds=4 — and gives a tick
    // DayOfMonth precisely when it is the first bar of a new day on an
    // intraday chart. Wiring anything narrower than `<= 2` meant that case
    // never fired: every tick rendered bare HH:MM, and two sessions side
    // by side on the axis read as one clock running backwards.
    await renderChart(<PriceChart candles={[]} signal={null} />);
    const options = createChart.mock.calls[0][1];
    const format = options.timeScale.tickMarkFormatter;
    const epoch = Math.floor(new Date("2026-08-28T03:45:00+00:00").getTime() / 1000);

    expect(format(epoch, 2)).toMatch(/^\d{2} [A-Za-z]{3}$/);  // DayOfMonth
    expect(format(epoch, 3)).toBe("09:15");                    // Time
  });
});

describe("mounting", () => {
  it("creates exactly one chart", async () => {
    await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(made.charts).toHaveLength(1));
  });

  it("loads a first window from the archive rather than waiting for props", async () => {
    await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(getJSON).toHaveBeenCalled());
    expect(getJSON.mock.calls[0][0]).toContain("/market/candles/history");
  });

  it("asks for the newest window first, with no cursor", async () => {
    await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(getJSON).toHaveBeenCalled());
    expect(getJSON.mock.calls[0][0]).not.toContain("before=");
  });

  it("destroys the chart on unmount", async () => {
    const { unmount } = await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(made.charts).toHaveLength(1));
    const c = chart();
    unmount();
    expect(c.removed).toBe(true);
  });

  it("says so when the archive is empty instead of drawing nothing", async () => {
    getJSON.mockResolvedValue(page([], { has_more: false, oldest: null }));
    await renderChart(<PriceChart candles={[]} signal={null} />);
    expect(await screen.findByText(/no candles archived yet/i)).toBeTruthy();
  });

  it("keeps the panel alive when the archive request fails", async () => {
    // A failed page must leave the chart showing what it has, not replace
    // it with an error.
    getJSON.mockRejectedValue(new Error("boom"));
    await renderChart(<PriceChart candles={bars(10)} signal={null} />);
    await waitFor(() => expect(priceSeries()?.data?.length).toBe(10));
  });
});

describe("series", () => {
  it("draws candlesticks by default", async () => {
    await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(priceSeries()?.kind).toBe("CandlestickSeries"));
  });

  it("feeds the price series the archived bars", async () => {
    await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(priceSeries()?.data).toHaveLength(60));
  });

  it("adds the four overlays", async () => {
    await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => {
      const overlays = chart().series.filter(
        (s) => s.kind === "LineSeries" && s.options.lastValueVisible === false);
      expect(overlays).toHaveLength(4);
    });
  });

  it("merges live bars on top of the archive", async () => {
    const { rerender } = await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(priceSeries()?.data).toHaveLength(60));
    // One new bar beyond the archived window.
    const live = bars(1, "2026-08-28T08:45:00+00:00");
    rerender(<PriceChart candles={live} signal={null} />);
    await waitFor(() => expect(priceSeries().data.length).toBe(61));
  });

  it("does not duplicate a bar the archive and the live prop share", async () => {
    const shared = bars(60);
    getJSON.mockResolvedValue(page(shared));
    await renderChart(<PriceChart candles={shared} signal={null} />);
    await waitFor(() => expect(priceSeries()?.data).toHaveLength(60));
  });
});

describe("the recent-window refresh", () => {
  /* The bug this covers: mergeLive strips indicators from any bar the
     archive-loaded window has not seen, and nothing re-fetched that window
     after mount. The overlays showed "—" and stopped drawing wherever a
     browser tab had been open longer than one bar's worth of time. */
  beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: true }); });
  afterEach(() => { vi.useRealTimers(); });

  // ema20 is the only overlay drawn dashed (see OVERLAYS in PriceChart.jsx),
  // so its LineStyle.Dashed value (2, from the mocked module) picks it out
  // without depending on series creation order.
  const ema20Series = () => chart().series.find(
    (s) => s.kind === "LineSeries" && s.options.lineStyle === 2);

  it("fills in indicators once the archive catches up to a live-only bar", async () => {
    const { rerender } = await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(priceSeries()?.data).toHaveLength(60));

    // A bar the archive has not enriched yet — exactly what mergeLive hands
    // a forming candle: OHLC only, no vwap/ema/atr.
    const newIso = "2026-08-28T08:45:00+00:00";
    const newTime = Math.floor(new Date(newIso).getTime() / 1000);
    rerender(<PriceChart candles={[{
      timestamp: newIso, open: 24200, high: 24210, low: 24190, close: 24205,
    }]} signal={null} />);
    await waitFor(() => expect(priceSeries().data.length).toBe(61));

    // Confirmed gap: nothing plotted for the new bar on the overlay yet.
    expect(ema20Series().data.some((p) => p.time === newTime)).toBe(false);

    // The archive has since caught up and enriched that bar — what the
    // next `GET /candles/history` (no `before`) would now answer.
    getJSON.mockResolvedValue(page([...bars(60), {
      timestamp: newIso, open: 24200, high: 24210, low: 24190, close: 24205,
      vwap: 24201, ema20: 24202, ema50: 24203, ema200: 24204, atr14: 13,
    }]));
    await act(async () => { await vi.advanceTimersByTimeAsync(RECENT_REFRESH_MS); });

    await waitFor(() => expect(
      ema20Series().data.find((p) => p.time === newTime)?.value).toBe(24202));
  });

  it("does not ask again before the interval elapses", async () => {
    await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(getJSON).toHaveBeenCalledTimes(1));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(RECENT_REFRESH_MS - 1_000);
    });
    expect(getJSON).toHaveBeenCalledTimes(1);
  });

  it("stops refreshing once the chart unmounts", async () => {
    const { unmount } = await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(getJSON).toHaveBeenCalledTimes(1));
    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(RECENT_REFRESH_MS * 3);
    });
    expect(getJSON).toHaveBeenCalledTimes(1);
  });
});

describe("the style toggle", () => {
  it("swaps to a line series without rebuilding the chart", async () => {
    // Rebuilding would reset the user's zoom, which is the point of the
    // whole rewrite.
    await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(priceSeries()?.kind).toBe("CandlestickSeries"));

    fireEvent.click(screen.getByRole("button", { name: "LINE" }));

    await waitFor(() => {
      const line = chart().series.find(
        (s) => s.kind === "LineSeries" && s.options.lineWidth === 2);
      expect(line).toBeTruthy();
    });
    expect(made.charts).toHaveLength(1);
    expect(chart().removed).toBe(false);
  });

  it("removes the old price series rather than stacking them", async () => {
    await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(priceSeries()).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "LINE" }));
    await waitFor(() => expect(chart().removeSeries).toHaveBeenCalled());
  });

  it("honours an initial style of line", async () => {
    await renderChart(<PriceChart candles={[]} signal={null} style="line" />);
    await waitFor(() => {
      expect(chart().series.some(
        (s) => s.kind === "LineSeries" && s.options.lineWidth === 2)).toBe(true);
    });
  });

  it("falls back to candles for an unknown style", async () => {
    await renderChart(<PriceChart candles={[]} signal={null} style="hollow" />);
    await waitFor(() => expect(priceSeries()?.kind).toBe("CandlestickSeries"));
  });
});

describe("plan levels", () => {
  it("draws entry, stop and target as price lines", async () => {
    await renderChart(<PriceChart candles={[]}
                       signal={{ plan: { entry: 24160, stop: 24140, target: 24220 } }} />);
    await waitFor(() =>
      expect(priceSeries()?.priceLines.map((l) => l.title))
        .toEqual(["entry", "stop", "target"]));
  });

  it("omits a level the plan does not have", async () => {
    await renderChart(<PriceChart candles={[]} signal={{ plan: { entry: 24160 } }} />);
    await waitFor(() =>
      expect(priceSeries()?.priceLines.map((l) => l.title)).toEqual(["entry"]));
  });

  it("draws none when there is no signal", async () => {
    await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(priceSeries()).toBeTruthy());
    expect(priceSeries().priceLines).toEqual([]);
  });

  it("replaces the lines when the plan changes", async () => {
    const { rerender } = render(
      <PriceChart candles={[]} signal={{ plan: { entry: 24160 } }} />);
    await waitFor(() => expect(priceSeries()?.priceLines).toHaveLength(1));

    rerender(<PriceChart candles={[]}
                         signal={{ plan: { entry: 24200, stop: 24180 } }} />);
    await waitFor(() =>
      expect(priceSeries().priceLines.map((l) => l.price)).toEqual([24200, 24180]));
  });
});

describe("paging the archive while panning", () => {
  const pan = async (from) => {
    await act(async () => {
      for (const h of chart().rangeHandlers) await h({ from, to: from + 100 });
    });
  };

  it("fetches older bars when the view nears the left edge", async () => {
    await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(getJSON).toHaveBeenCalledTimes(1));

    getJSON.mockResolvedValue(page(bars(60, "2026-08-27T03:45:00+00:00")));
    await pan(2);

    await waitFor(() => expect(getJSON).toHaveBeenCalledTimes(2));
    expect(getJSON.mock.calls[1][0]).toContain("before=");
  });

  it("does not fetch while the view is well inside the data", async () => {
    await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(getJSON).toHaveBeenCalledTimes(1));
    await pan(400);
    expect(getJSON).toHaveBeenCalledTimes(1);
  });

  it("pages from the oldest bar it holds", async () => {
    const first = bars(60);
    getJSON.mockResolvedValue(page(first));
    await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(getJSON).toHaveBeenCalledTimes(1));

    getJSON.mockResolvedValue(page(bars(60, "2026-08-27T03:45:00+00:00")));
    await pan(2);

    await waitFor(() => expect(getJSON).toHaveBeenCalledTimes(2));
    expect(getJSON.mock.calls[1][0])
      .toContain(`before=${encodeURIComponent(first[0].timestamp)}`);
  });

  it("prepends the fetched page beneath what is on screen", async () => {
    await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(priceSeries()?.data).toHaveLength(60));

    getJSON.mockResolvedValue(page(bars(60, "2026-08-27T03:45:00+00:00")));
    await pan(2);

    await waitFor(() => expect(priceSeries().data.length).toBe(120));
  });

  it("stops asking once the archive says it has nothing older", async () => {
    // Otherwise the start of history costs one request per pan event, for
    // as long as the tab is open.
    await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(getJSON).toHaveBeenCalledTimes(1));

    getJSON.mockResolvedValue(
      page(bars(10, "2026-08-27T03:45:00+00:00"), { has_more: false }));
    await pan(2);
    await waitFor(() => expect(getJSON).toHaveBeenCalledTimes(2));

    await pan(1);
    await pan(0);
    await pan(-5);
    expect(getJSON).toHaveBeenCalledTimes(2);
  });

  it("does not fire a second request while one is in flight", async () => {
    await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(getJSON).toHaveBeenCalledTimes(1));

    let release;
    getJSON.mockReturnValue(new Promise((r) => { release = r; }));
    await pan(2);
    await pan(1);
    await pan(0);
    expect(getJSON).toHaveBeenCalledTimes(2);

    // Settle the held request inside act, so its state update is not
    // reported as unwrapped after the assertion has already passed.
    await act(async () => {
      release(page(bars(10, "2026-08-27T03:45:00+00:00")));
    });
  });
});

describe("the readout", () => {
  it("shows the last bar before the pointer has moved", async () => {
    await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(priceSeries()?.data).toHaveLength(60));
    // 24105 + 59
    expect(screen.getByText(/C\s+24164/)).toBeTruthy();
  });

  it("counts the bars it holds", async () => {
    await renderChart(<PriceChart candles={[]} signal={null} />);
    expect(await screen.findByText(/60 bars/)).toBeTruthy();
  });

  it("marks the start of the archive", async () => {
    getJSON.mockResolvedValue(page(bars(10), { has_more: false }));
    await renderChart(<PriceChart candles={[]} signal={null} />);
    expect(await screen.findByText(/start of archive/)).toBeTruthy();
  });

  it("follows the crosshair when the pointer moves", async () => {
    await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(priceSeries()?.data).toHaveLength(60));

    const series = priceSeries();
    const seriesData = new Map([[series, { close: 23999 }]]);
    act(() => {
      for (const h of chart().crosshairHandlers) {
        h({ time: 1787888700, seriesData });
      }
    });
    await waitFor(() => expect(screen.getByText(/C\s+23999/)).toBeTruthy());
  });

  it("falls back to the last bar when the pointer leaves the chart", async () => {
    await renderChart(<PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(priceSeries()?.data).toHaveLength(60));

    act(() => {
      for (const h of chart().crosshairHandlers) h({ time: null });
    });
    await waitFor(() => expect(screen.getByText(/C\s+24164/)).toBeTruthy());
  });
});


describe("the forming candle moves with the feed", () => {
  const liveTick = (price, iso) =>
    ({ price, source_time: iso, market_open: true });

  /* bars() starts at 03:45Z and steps 5m, so 60 bars ends at 08:40Z. */
  const LAST_BAR = "2026-08-28T08:40:00+00:00";

  it("updates the last bar instead of redrawing the series", async () => {
    // A full setData per tick would replace 540 bars four times a second.
    const { rerender } = await renderChart(
      <PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(priceSeries()?.data).toHaveLength(60));

    const before = priceSeries().setData.mock.calls.length;
    rerender(<PriceChart candles={[]} signal={null}
                         price={liveTick(24999, LAST_BAR)} />);

    await waitFor(() => expect(priceSeries().updates.length).toBe(1));
    expect(priceSeries().setData.mock.calls.length).toBe(before);
  });

  it("moves the close of the bar at the right edge", async () => {
    const { rerender } = await renderChart(
      <PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(priceSeries()?.data).toHaveLength(60));

    rerender(<PriceChart candles={[]} signal={null}
                         price={liveTick(24999, LAST_BAR)} />);
    await waitFor(() => {
      const bar = priceSeries().updates.at(-1);
      expect(bar.close).toBe(24999);
      expect(bar.high).toBe(24999);
    });
  });

  it("appends a new bar when the tick crosses into the next bucket", async () => {
    const { rerender } = await renderChart(
      <PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(priceSeries()?.data).toHaveLength(60));

    rerender(<PriceChart candles={[]} signal={null}
                         price={liveTick(24999, "2026-08-28T08:46:00+00:00")} />);
    await waitFor(() => expect(priceSeries().data.length).toBe(61));
  });

  it("does not move the candle while the market is shut", async () => {
    const { rerender } = await renderChart(
      <PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(priceSeries()?.data).toHaveLength(60));

    rerender(<PriceChart candles={[]} signal={null}
                         price={{ price: 24999, source_time: LAST_BAR,
                                  market_open: false }} />);
    await new Promise((r) => setTimeout(r, 0));
    expect(priceSeries().updates).toHaveLength(0);
  });

  it("keeps the forming bar when the polled rows arrive underneath it", async () => {
    // The 60-second poll calls setData. Without carrying the forming bar
    // across it, the right edge jumps backwards once a minute.
    const { rerender } = await renderChart(
      <PriceChart candles={[]} signal={null} />);
    await waitFor(() => expect(priceSeries()?.data).toHaveLength(60));

    rerender(<PriceChart candles={[]} signal={null}
                         price={liveTick(24999, LAST_BAR)} />);
    await waitFor(() => expect(priceSeries().updates.length).toBe(1));

    rerender(<PriceChart candles={bars(60)} signal={null}
                         price={liveTick(24999, LAST_BAR)} />);
    await waitFor(() => {
      const drawn = priceSeries().data.at(-1);
      expect(drawn.close).toBe(24999);
    });
  });

  it("feeds the line series a value rather than a candle", async () => {
    const { rerender } = await renderChart(
      <PriceChart candles={[]} signal={null} style="line" />);
    await waitFor(() => expect(priceSeries()?.data).toHaveLength(60));

    rerender(<PriceChart candles={[]} signal={null} style="line"
                         price={liveTick(24999, LAST_BAR)} />);
    await waitFor(() =>
      expect(priceSeries().updates.at(-1)).toEqual(
        { time: expect.any(Number), value: 24999 }));
  });
});

describe("a last bar that does not sit on the bucket grid", () => {
  /* The black-screen crash, reproduced.

     A source can hand back a still-forming bar stamped at the instant of
     its own refresh rather than at its bucket start. When the live tick
     merge then emitted the bucket start, it asked lightweight-charts to
     move its last bar *backwards* — and the library refuses:

         Cannot update oldest data, last time=…, new time=…

     Nothing caught it, so the throw unmounted the whole dashboard and left
     a black page until the bucket rolled over a few minutes later and the
     condition cleared on its own. Reported from the browser console on
     15-Sep-2026 at PriceChart.jsx:316, the `series.update()` call below.

     The stray stamp is corrected at the source now (see the bucket
     snapping in `brokers/freedata.py`), this keeps the renderer safe if
     one ever gets through again, and `PanelBoundary` keeps a chart throw
     from costing the desk. Belt, braces, and a net — the failure was
     total and silent, and one guard is not enough for that. */
  it("merges in place instead of asking the renderer to go backwards", async () => {
    const FIVE = 300_000;
    const bucket = Date.parse("2026-09-15T07:40:00Z");
    const rows = [
      { timestamp: new Date(bucket - 2 * FIVE).toISOString(),
        open: 100, high: 101, low: 99, close: 100 },
      { timestamp: new Date(bucket - FIVE).toISOString(),
        open: 100, high: 101, low: 99, close: 100.5 },
      // the forming bar, stamped 3m12s into its own bucket
      { timestamp: new Date(bucket + 192_000).toISOString(),
        open: 100.5, high: 102, low: 100, close: 101.5 },
    ];

    const { rerender } = render(
      <PriceChart candles={rows} signal={null} price={null} />);
    await act(async () => {});

    const series = made.series.find((s) => s.kind === "CandlestickSeries");
    const before = series.data[series.data.length - 1].time;

    // a tick inside that same bucket, a few seconds later
    rerender(<PriceChart candles={rows} signal={null} price={{
      price: 101.75, market_open: true,
      source_time: new Date(bucket + 240_000).toISOString(),
    }} />);
    await act(async () => {});

    const after = series.data[series.data.length - 1];
    expect(after.time).toBe(before);       // never moved backwards
    expect(after.close).toBe(101.75);      // and the tick still landed
  });
});
