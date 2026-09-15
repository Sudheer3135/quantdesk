/* The price chart, on TradingView's own renderer.

   This used to be Recharts. Recharts has no candlestick series — the old
   chart drew each candle as a Bar with a custom shape — and, more
   fundamentally, it plots on a *category* axis: every bar is an equal-width
   slot, like months in a sales report. There is no continuous time axis to
   zoom along, so wheel-zoom and drag-pan were not switched off here, they
   were never expressible. The only zoom Recharts offers is a Brush, a range
   slider under the plot.

   lightweight-charts is what TradingView publishes for this. Canvas, a real
   time scale, and zoom/pan/crosshair as native behaviour rather than
   something to reimplement.

   The component is deliberately thin. Everything decidable without a canvas
   — series shaping, the seam between fetched pages, when a pan warrants
   another request, IST formatting — lives in chart-data.js and is tested
   directly. What is left here is wiring, tested through a mocked renderer,
   because jsdom has no canvas and never will. */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  CandlestickSeries, ColorType, CrosshairMode, LineSeries, LineStyle,
  createChart,
} from "lightweight-charts";

import { getJSON } from "./api.js";
import {
  BAR_SECONDS, applyTick, formatStampIST, formatTickIST, mergeLive,
  mergeOlder, priceLines, shouldLoadOlder, toCandleSeries, toCloseSeries,
  toLineSeries, toSessionSeries,
} from "./chart-data.js";

export const PRICE_STYLES = ["candles", "line"];

/* How many bars to pull per request while panning. Large enough that the
   chart does not stutter into a fetch every few seconds of scrolling,
   inside the endpoint's own 1500 ceiling. */
const PAGE_BARS = 500;

/* The terminal's palette, so the chart is part of the desk rather than a
   widget dropped onto it. */
const THEME = {
  up: "#4ec9a0",
  down: "#e06c75",
  grid: "#1c2128",
  text: "#7d8590",
  vwap: "#e3a008",
  ema20: "#d7a13b",
  ema50: "#4a9eff",
  ema200: "#a970ff",
};

const OVERLAYS = [
  /* VWAP is anchored to the session open, so its line breaks each morning
     rather than drawing a vertical stroke across the reset. */
  { key: "vwap", color: THEME.vwap, width: 1, dashed: false, perSession: true },
  { key: "ema20", color: THEME.ema20, width: 1, dashed: true },
  { key: "ema50", color: THEME.ema50, width: 1, dashed: false },
  { key: "ema200", color: THEME.ema200, width: 1, dashed: false },
];

function num(v, d = 2) {
  return v === null || v === undefined || !Number.isFinite(v)
    ? "—" : Number(v).toFixed(d);
}

/* One request for a window of archive older than `before`. Never throws:
   a failed page must leave the chart showing what it already has, not
   replace it with an error. */
async function fetchOlder(before) {
  const q = new URLSearchParams({ symbol: "NIFTY", interval: "5m",
                                  limit: String(PAGE_BARS) });
  if (before) q.set("before", before);
  try {
    return await getJSON(`/market/candles/history?${q}`);
  } catch {
    return null;
  }
}

export default function PriceChart({ candles, signal, price: tick,
                                    timeframe = "5m", style = "candles" }) {
  const [mode, setMode] = useState(
    PRICE_STYLES.includes(style) ? style : "candles");
  const [rows, setRows] = useState([]);
  const [hasMore, setHasMore] = useState(true);
  const [readout, setReadout] = useState(null);
  /* Mirrors the ref below. The ref is what the pan handler reads — a state
     read there would need the handler re-subscribed on every fetch — and
     this is what the header renders. */
  const [busy, setBusy] = useState(false);

  const box = useRef(null);
  const chart = useRef(null);
  const price = useRef(null);
  /* The bar at the right edge, and the live version of it. Held as refs so
     a tick four times a second costs one `update()` call and no React
     render of a 540-bar series. */
  const lastBar = useRef(null);
  const forming = useRef(null);
  const overlays = useRef({});
  const lines = useRef([]);
  const loading = useRef(false);
  const oldest = useRef(null);
  const rowsRef = useRef([]);

  rowsRef.current = rows;

  /* Pull older bars and splice them in beneath what is on screen.
     lightweight-charts keeps the viewport anchored to the data already
     rendered, so prepending does not jump the user's position. */
  const loadOlder = useCallback(async () => {
    if (loading.current) return;
    loading.current = true;
    setBusy(true);
    try {
      const page = await fetchOlder(oldest.current);
      if (!page) return;
      const older = page.candles || [];
      if (older.length) {
        setRows((current) => mergeOlder(current, older));
        oldest.current = page.oldest || oldest.current;
      }
      /* An archive that has said it holds nothing older is believed. Without
         this the chart asks the same empty question on every pan event for
         as long as the tab is open. */
      setHasMore(Boolean(page.has_more) && older.length > 0);
    } finally {
      loading.current = false;
      setBusy(false);
    }
  }, []);

  /* First window, from the archive rather than the live endpoint, so the
     chart opens with something to scroll through. */
  useEffect(() => { loadOlder(); }, [loadOlder]);

  /* The live prop is merged on top rather than replacing: it carries the
     newest bars, the archive carries the depth, and the right edge has to
     keep moving while the market is open. */
  useEffect(() => {
    if (Array.isArray(candles) && candles.length) {
      setRows((current) => mergeLive(current, candles));
    }
  }, [candles]);

  /* Build the chart once. Rebuilding it on every data change would reset
     the user's zoom, which is the one thing this rewrite exists to give
     them. */
  useEffect(() => {
    if (!box.current) return undefined;

    const c = createChart(box.current, {
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: THEME.text,
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: THEME.grid },
        horzLines: { color: THEME.grid },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: THEME.grid },
      timeScale: {
        borderColor: THEME.grid,
        timeVisible: true,
        secondsVisible: false,
        /* Room to drag the last bar off the right edge, the way a real
           terminal lets you see space ahead of price. */
        rightOffset: 6,
        // lightweight-charts' TickMarkType: Year=0, Month=1,
        // DayOfMonth=2, Time=3, TimeWithSeconds=4. The library hands a
        // tick DayOfMonth precisely when it is the coarsest boundary that
        // changed there — the first bar of a new day on an intraday chart
        // — so <= 2 is "show the date"; `< 2` excluded that case and every
        // tick rendered as bare HH:MM, so two sessions side by side read as
        // one clock running backwards (13:30 followed by 09:15) with
        // nothing on the axis saying a day had turned over.
        tickMarkFormatter: (time, tickType) => formatTickIST(time, tickType <= 2),
      },
      localization: { timeFormatter: formatStampIST },
      handleScroll: true,
      handleScale: true,
      autoSize: true,
    });

    chart.current = c;

    /* Reading OHLC off the crosshair is how a chart like this is actually
       used — the header row follows the pointer instead of being frozen on
       the last bar. */
    c.subscribeCrosshairMove((param) => {
      if (!param?.time || !param.seriesData?.size) {
        setReadout(null);
        return;
      }
      const bar = param.seriesData.get(price.current);
      const values = { time: param.time };
      for (const o of OVERLAYS) {
        const s = overlays.current[o.key];
        const point = s && param.seriesData.get(s);
        if (point) values[o.key] = point.value;
      }
      setReadout(bar ? { ...values, ...bar } : values);
    });

    /* The pan handler is registered by the effect below, not here: it has
       to see the current `hasMore`, and a subscription created once would
       close over the value it had on first paint. */

    return () => {
      c.remove();
      chart.current = null;
      price.current = null;
      overlays.current = {};
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* hasMore lives in state but is read inside a subscription created once,
     so the handler is re-registered when it flips rather than closing over
     a stale value forever. */
  useEffect(() => {
    const c = chart.current;
    if (!c) return undefined;
    const onRange = (range) => {
      if (shouldLoadOlder({ range, loading: loading.current,
                            hasMore, oldest: oldest.current })) {
        loadOlder();
      }
    };
    c.timeScale().subscribeVisibleLogicalRangeChange(onRange);
    return () => c.timeScale().unsubscribeVisibleLogicalRangeChange(onRange);
  }, [hasMore, loadOlder]);

  /* Swap the main series when the style toggle moves. Only the price series
     is rebuilt; the overlays and the zoom survive. */
  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    if (price.current) {
      c.removeSeries(price.current);
      price.current = null;
    }
    price.current = mode === "line"
      ? c.addSeries(LineSeries, {
          color: THEME.up, lineWidth: 2, priceLineVisible: false })
      : c.addSeries(CandlestickSeries, {
          upColor: THEME.up, downColor: THEME.down,
          borderUpColor: THEME.up, borderDownColor: THEME.down,
          wickUpColor: THEME.up, wickDownColor: THEME.down });
  }, [mode]);

  /* Feed the series. setData replaces the whole series, which is what a
     prepended page needs, and lightweight-charts diffs it internally rather
     than redrawing from scratch. */
  useEffect(() => {
    const c = chart.current;
    if (!c || !price.current || !rows.length) return;

    const candleSeries = toCandleSeries(rows);
    lastBar.current = candleSeries[candleSeries.length - 1] || null;

    /* A forming bar the polled rows have not caught up with yet must
       survive setData, or the right edge jumps backwards once a minute. */
    const live = forming.current;
    if (live && lastBar.current && live.time >= lastBar.current.time) {
      const i = candleSeries.findIndex((b) => b.time === live.time);
      if (i >= 0) candleSeries[i] = live;
      else candleSeries.push(live);
      lastBar.current = live;
    } else {
      forming.current = null;
    }

    price.current.setData(
      mode === "line"
        ? candleSeries.map((b) => ({ time: b.time, value: b.close }))
        : candleSeries);

    for (const o of OVERLAYS) {
      if (!overlays.current[o.key]) {
        overlays.current[o.key] = c.addSeries(LineSeries, {
          color: o.color,
          lineWidth: o.width,
          lineStyle: o.dashed ? LineStyle.Dashed : LineStyle.Solid,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        });
      }
      overlays.current[o.key].setData(
        o.perSession ? toSessionSeries(rows, o.key) : toLineSeries(rows, o.key));
    }
  }, [rows, mode]);

  /* Every live tick, folded into the bar at the right edge.

     `update()` rather than `setData()`: it touches one bar instead of
     replacing five hundred, which is what lets this run at the feed's own
     rate. The readout is left alone deliberately — it follows the crosshair
     and falls back to the last bar, and repainting it here would fight the
     pointer. */
  useEffect(() => {
    const series = price.current;
    if (!series || !tick) return;

    const next = applyTick(forming.current || lastBar.current, tick,
                           BAR_SECONDS[timeframe] || 300);
    if (!next) return;

    forming.current = next;
    lastBar.current = next;
    series.update(mode === "line"
      ? { time: next.time, value: next.close }
      : next);
  }, [tick, mode, timeframe]);

  /* The plan's levels, as native price lines on the price series so they
     stay put through zoom and pan. */
  useEffect(() => {
    const series = price.current;
    if (!series) return;
    for (const line of lines.current) {
      try { series.removePriceLine(line); } catch { /* series was swapped */ }
    }
    lines.current = priceLines(signal).map((l) => series.createPriceLine({
      price: l.price,
      color: l.color,
      lineWidth: 1,
      lineStyle: l.style === "dotted" ? LineStyle.Dotted : LineStyle.Solid,
      axisLabelVisible: true,
      title: l.key,
    }));
  }, [signal, mode, rows.length]);

  const last = rows.length ? rows[rows.length - 1] : null;
  const shown = readout || last || {};
  const sessions = new Set(
    rows.map((r) => String(r.timestamp).slice(0, 10))).size;

  return (
    <div className="panel chart-panel">
      <div className="panel-head">
        <h3>NIFTY 50 · 5M</h3>
        <span className="panel-note mono">
          {rows.length} bars
          {sessions > 1 && ` · ${sessions} sessions`}
          {busy && " · loading…"}
          {!hasMore && " · start of archive"}
        </span>
        <div className="chart-toggle">
          {PRICE_STYLES.map((s) => (
            <button
              key={s}
              type="button"
              className={mode === s ? "on" : ""}
              onClick={() => setMode(s)}
            >{s.toUpperCase()}</button>
          ))}
        </div>
      </div>

      <div className="chart-readout mono">
        <span className="rd rd-close">C {num(shown.close)}</span>
        <span className="rd rd-vwap">VWAP {num(shown.vwap)}</span>
        <span className="rd rd-ema20">EMA20 {num(shown.ema20)}</span>
        <span className="rd rd-ema50">EMA50 {num(shown.ema50)}</span>
        <span className="rd rd-ema200">EMA200 {num(shown.ema200)}</span>
        <span className="rd rd-atr">ATR {num(last?.atr14)}</span>
      </div>

      {rows.length === 0
        ? <div className="chart-empty mono">no candles archived yet</div>
        : null}
      <div className="chart-canvas" ref={box} data-testid="chart-canvas" />
    </div>
  );
}
