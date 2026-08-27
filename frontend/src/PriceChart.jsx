import { useMemo, useState } from "react";
import {
  Area, Bar, ComposedChart, Line, ReferenceLine, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";

/* Asia/Kolkata is a *display* choice and nothing more. Every value the
   chart reasons about is an epoch in milliseconds, which carries no zone at
   all; IST is applied at the last moment, to render a label. */
const TZ = "Asia/Kolkata";

const TIME_IST = new Intl.DateTimeFormat("en-IN", {
  timeZone: TZ, hour: "2-digit", minute: "2-digit",
});
const DAY_IST = new Intl.DateTimeFormat("en-IN", {
  timeZone: TZ, day: "2-digit", month: "short",
});
const FULL_IST = new Intl.DateTimeFormat("en-IN", {
  timeZone: TZ, weekday: "short", day: "2-digit", month: "short",
  hour: "2-digit", minute: "2-digit",
});
/* en-CA renders as YYYY-MM-DD, which sorts and compares as a plain string.
   This is the session identity: the *IST calendar date* of the bar, not the
   UTC one. A 09:15 IST bar is 03:45 UTC the same day, but a 15:30 IST bar in
   a hypothetical later session would still need the IST date to group with
   its own morning. Grouping on the UTC date would split sessions apart. */
const DATE_KEY_IST = new Intl.DateTimeFormat("en-CA", {
  timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
});

export const hhmm = (ms) => TIME_IST.format(new Date(ms));
export const dayLabel = (ms) => DAY_IST.format(new Date(ms));
export const fullStamp = (ms) => FULL_IST.format(new Date(ms));
export const sessionOf = (ms) => DATE_KEY_IST.format(new Date(ms));

/* Candles as a strictly chronological plot series.

   The chart used to trust the order of the array it was handed and label
   every bar with the time of day alone. Across a two-day window that reads
   as a fault even when the data is perfect: the last bar of Wednesday is
   15:25 and the first bar of Thursday is 09:15, so the axis appears to run
   backwards with nothing on screen saying a day has passed.

   So: sort on the absolute instant, never on anything formatted, and keep
   the session each bar belongs to so the boundary can be drawn.

   `ms` is the sort key and the plot key. `timestamp` is left untouched
   beside it — the full date and offset the backend sent, kept as-is so
   nothing downstream has to reconstruct a zone it was never told about. */
export function toSeries(candles, bars = 120) {
  if (!candles?.length) return [];

  const parsed = [];
  for (const c of candles) {
    const ms = Date.parse(c.timestamp);
    // An unparseable stamp yields NaN, and NaN compares false against
    // everything — it would neither sort nor throw, just quietly settle
    // wherever the input happened to put it. Drop it instead.
    if (!Number.isFinite(ms)) continue;
    parsed.push({ ...c, ms });
  }

  parsed.sort((a, b) => a.ms - b.ms);

  // One instant, one bar. A window that overlaps a previous fetch can
  // repeat the boundary candle, and the later copy is the settled one.
  const unique = [];
  for (const row of parsed) {
    if (unique.length && unique[unique.length - 1].ms === row.ms) {
      unique[unique.length - 1] = row;
    } else {
      unique.push(row);
    }
  }

  // Trim *after* sorting. Slicing first would keep the last N of whatever
  // arbitrary order arrived, which is not the last N bars.
  const window = unique.slice(-bars);

  let previousSession = null;
  return window.map((row) => {
    const session = sessionOf(row.ms);
    const sessionStart = session !== previousSession;
    previousSession = session;
    return {
      ...row,
      session,
      sessionStart,
      band: row.vwap_lower != null && row.vwap_upper != null
        ? [row.vwap_lower, row.vwap_upper]
        : null,
      /* The candle plots as a range, low to high, and the open/close body
         is drawn inside that rectangle. A bar missing either extreme is
         given no range at all, so it is skipped rather than drawn against
         an invented one. */
      range: Number.isFinite(row.low) && Number.isFinite(row.high)
        ? [row.low, row.high]
        : null,
    };
  });
}

/* Which bars get an axis label.

   Recharts picks its own ticks from a category axis, and its only lever is
   a minimum pixel gap — which cannot be told that one particular boundary
   matters more than the rest. Choosing the ticks here guarantees every
   session opening is labelled, which is the one label that explains the
   apparent jump backwards. */
export function pickTicks(rows, target = 9) {
  if (!rows.length) return [];

  const step = Math.max(1, Math.ceil(rows.length / target));
  const startIndexes = rows.reduce(
    (acc, row, i) => (row.sessionStart ? [...acc, i] : acc), []);
  const clearance = Math.max(2, step / 2);
  const chosen = new Set(startIndexes);

  rows.forEach((row, i) => {
    if (row.sessionStart || i % step !== 0) return;
    // Keep a routine tick clear of a session opening, so the date label
    // and the time beside it do not collide.
    const crowded = startIndexes.some((s) => Math.abs(s - i) < clearance);
    if (!crowded) chosen.add(i);
  });

  // Emitted in row order, so the tick array is chronological by
  // construction — the axis cannot be handed a sequence the data does not
  // already have.
  return rows.filter((_, i) => chosen.has(i)).map((r) => r.ms);
}

/* Which structure lines the chart draws, and how many.

   A pure function so it can be tested as the decision it is: the chart's own
   SVG is not rendered under jsdom, and a marker rule verified only by eye is
   a rule that quietly stops firing.

   Only what the engine actually reported. An absent context yields no lines
   — never a level the chart invented to have something to draw. Swept pools
   are dropped because a pool that has been taken is not a level any more,
   and drawing it would mark support where support has already gone. */
export function structureMarkers(signal, limit = 3) {
  const context = signal?.context ?? {};
  return {
    gaps: (context.unfilled_fvgs || [])
      .filter((g) => Number.isFinite(g.midpoint))
      .slice(0, limit),
    pools: (context.liquidity_pools || [])
      .filter((p) => Number.isFinite(p.level) && !p.swept)
      .slice(0, limit),
  };
}

/* Candle geometry, as arithmetic rather than as SVG.

   Recharts has no candlestick series, so the candle is a Bar carrying the
   [low, high] range with a custom shape drawn into the rectangle recharts
   sizes for it. That rectangle is the only scale information the shape is
   given: its top edge is the high, its bottom edge is the low, and every
   other price has to be placed between them by hand.

   Which is exactly why this is a function. A shape that miscalculates one
   pixel conversion draws a body outside its own wick, and the SVG is not
   rendered under jsdom — so a chart verified only by eye is a chart whose
   candles can quietly start lying about which way the bar closed. */
const MIN_BODY_PX = 1;
const MAX_BODY_PX = 11;
/* Recharts' own category gap is a percentage, which at 120 bars leaves well
   under a pixel between bands — the bodies fuse into one slab and the chart
   stops reading as candles at all. Two pixels taken off each band keeps them
   visibly separate at this density without shaving the wick, which stays on
   the band's centre line. */
const BODY_INSET_PX = 2;

export function candleGeometry(bar, box) {
  const { open, close, low, high } = bar ?? {};
  if (![open, close, low, high].every(Number.isFinite)) return null;

  const { x, y, width, height } = box ?? {};
  if (![x, y, width, height].every(Number.isFinite)) return null;
  if (width <= 0) return null;

  const span = high - low;
  /* A bar that never moved still happened. Falling back to the top edge
     draws it as a single mark instead of dividing by zero and writing NaN
     into the SVG, which renders as nothing at all and reads as a gap in
     the data. */
  const pxFor = (price) =>
    span > 0 && height > 0 ? y + ((high - price) * height) / span : y;

  const top = pxFor(Math.max(open, close));
  const bottom = pxFor(Math.min(open, close));
  /* A doji has no body to speak of, and a zero-height rect is invisible.
     One pixel says the bar closed where it opened; nothing says the bar
     is missing. */
  const bodyHeight = Math.max(MIN_BODY_PX, bottom - top);
  const bodyWidth = Math.min(MAX_BODY_PX, Math.max(1, width - BODY_INSET_PX));

  return {
    centre: x + width / 2,
    bodyX: x + (width - bodyWidth) / 2,
    bodyWidth,
    bodyY: top,
    bodyHeight,
    wickTop: y,
    wickBottom: y + Math.max(0, height),
    // Against the open, not against the previous close. This is what the
    // body of the candle means and nothing else.
    tone: close > open ? "up" : close < open ? "down" : "flat",
  };
}

const CANDLE_COLOUR = { up: "#2ec27e", down: "#f0616d", flat: "#8b98a9" };

function Candle(props) {
  const geo = candleGeometry(props.payload, props);
  if (!geo) return null;
  const colour = CANDLE_COLOUR[geo.tone];
  return (
    <g className={`candle candle-${geo.tone}`}>
      <line
        x1={geo.centre} x2={geo.centre} y1={geo.wickTop} y2={geo.wickBottom}
        stroke={colour} strokeWidth={1} shapeRendering="crispEdges"
      />
      <rect
        x={geo.bodyX} y={geo.bodyY} width={geo.bodyWidth} height={geo.bodyHeight}
        fill={colour} shapeRendering="crispEdges"
      />
    </g>
  );
}

function Callout({ active, payload }) {
  if (!active || !payload?.length) return null;
  const bar = payload[0].payload;
  return (
    <div className="callout">
      {/* The full date, not the time alone. Reading a bar off a two-session
          chart means knowing which session it came from. */}
      <div className="callout-time">{fullStamp(bar.ms)} IST</div>
      <div className="callout-row"><span>O</span><b>{bar.open?.toFixed(2)}</b></div>
      <div className="callout-row"><span>H</span><b>{bar.high?.toFixed(2)}</b></div>
      <div className="callout-row"><span>L</span><b>{bar.low?.toFixed(2)}</b></div>
      <div className="callout-row"><span>C</span><b>{bar.close?.toFixed(2)}</b></div>
      {bar.vwap != null && (
        <div className="callout-row"><span>VWAP</span><b>{bar.vwap.toFixed(2)}</b></div>
      )}
    </div>
  );
}

/* Price against session VWAP, with the current trade plan drawn on.

   Candles are the default series. A close-only line says where price sits
   relative to VWAP and to your own levels in one glance, which is why this
   panel started as one — but it throws away the open, the high and the low,
   and those are what a rejection wick or an engulfing bar is *made of*. At
   120 bars in this column a body is roughly seven pixels wide, which is
   enough to read direction and range at a glance, so the line is kept as a
   switch rather than as the only option.

   The axis is categorical rather than a time scale, which is the usual
   choice for an intraday chart: a time scale would render the seventeen
   hours the exchange is shut as seventeen hours of empty width. The cost is
   that the gap between sessions is invisible, so it is drawn explicitly. */
export const PRICE_STYLES = ["candles", "line"];

export default function PriceChart({ candles, signal, bars = 120, style = "candles" }) {
  const [mode, setMode] = useState(PRICE_STYLES.includes(style) ? style : "candles");
  const data = useMemo(() => toSeries(candles, bars), [candles, bars]);
  const ticks = useMemo(() => pickTicks(data), [data]);

  if (!data.length) {
    return <div className="panel chart-panel"><h3>Price</h3>
      <p className="muted-body">No candles loaded.</p></div>;
  }

  const lows = data.map((d) => d.low).filter(Number.isFinite);
  const highs = data.map((d) => d.high).filter(Number.isFinite);
  const levels = [signal?.entry, signal?.stop_loss, signal?.target].filter(Number.isFinite);
  const floor = Math.min(...lows, ...levels);
  const ceiling = Math.max(...highs, ...levels);
  const pad = (ceiling - floor) * 0.06 || 10;

  const last = data[data.length - 1];
  // The readout's swatch stands for the newest thing on the chart, so in
  // candle mode it takes that candle's colour rather than the white of a
  // line that is no longer being drawn.
  const lastTone = last.close > last.open ? "up"
    : last.close < last.open ? "down" : "flat";
  const plan = signal && signal.action !== "HOLD";

  /* Structure the backend already found, drawn rather than listed. */
  const { gaps, pools } = structureMarkers(signal);
  // The first bar opens the window rather than a new day within it, so it
  // gets no divider — there is nothing to its left to divide it from.
  const dividers = data.filter((d, i) => d.sessionStart && i > 0);
  const spanned = new Set(data.map((d) => d.session)).size;

  return (
    <div className="panel chart-panel">
      <div className="panel-head">
        <h3>NIFTY 50 · 5m</h3>
        <div className="chart-head-right">
          <span className="panel-note mono">
            last {data.length} bars
            {spanned > 1 && ` · ${spanned} sessions`}
            {" · "}{dayLabel(data[0].ms)}–{dayLabel(data[data.length - 1].ms)} IST
          </span>
          <div className="chart-styles" role="group" aria-label="Price style">
            {PRICE_STYLES.map((option) => (
              <button
                key={option} type="button" aria-pressed={mode === option}
                className={mode === option ? "chart-style is-on" : "chart-style"}
                onClick={() => setMode(option)}
              >{option}</button>
            ))}
          </div>
        </div>
      </div>

      {/* Current readings off the newest bar, so the chart states its own
          numbers instead of making the eye estimate them off an axis. */}
      <div className="chart-readout mono">
        <span className={`rd rd-close ${mode === "candles" ? `rd-${lastTone}` : ""}`}>
          C {last.close?.toFixed(2) ?? "—"}
        </span>
        <span className="rd rd-vwap">VWAP {last.vwap?.toFixed(2) ?? "—"}</span>
        <span className="rd rd-ema20">EMA20 {last.ema20?.toFixed(2) ?? "—"}</span>
        <span className="rd rd-ema50">EMA50 {last.ema50?.toFixed(2) ?? "—"}</span>
        <span className="rd rd-ema200">EMA200 {last.ema200?.toFixed(2) ?? "—"}</span>
        <span className="rd rd-atr">ATR {last.atr14?.toFixed(2) ?? "—"}</span>
      </div>
      <ResponsiveContainer width="100%" height={300}>
        <ComposedChart data={data} margin={{ top: 8, right: 52, bottom: 4, left: 0 }}>
          <XAxis
            dataKey="ms" type="category" ticks={ticks} interval={0}
            tickFormatter={(ms) => {
              const row = data.find((d) => d.ms === ms);
              return row?.sessionStart ? dayLabel(ms) : hhmm(ms);
            }}
            tick={{ fill: "#7c8899", fontSize: 11 }}
            axisLine={{ stroke: "#232d3a" }} tickLine={false}
          />
          <YAxis
            domain={[floor - pad, ceiling + pad]} orientation="right" width={64}
            tickFormatter={(v) => v.toFixed(0)}
            tick={{ fill: "#7c8899", fontSize: 11 }}
            axisLine={false} tickLine={false}
          />
          <Tooltip content={<Callout />} cursor={{ stroke: "#7c8899", strokeWidth: 1 }} />

          <Area
            dataKey="band" stroke="none" fill="#e8a33d" fillOpacity={0.07}
            isAnimationActive={false} connectNulls
          />
          <Line
            dataKey="vwap" stroke="#e8a33d" strokeWidth={1} dot={false}
            strokeDasharray="3 3" isAnimationActive={false} connectNulls
          />
          {mode === "candles" && (
            <Bar
              dataKey="range" shape={<Candle />} isAnimationActive={false}
              maxBarSize={13}
            />
          )}
          {/* Over the candles, and slowest first, so a 1px average is never
              buried under a body or under a faster average. */}
          <Line
            dataKey="ema200" stroke="#8b5cf6" strokeWidth={1} dot={false}
            isAnimationActive={false} connectNulls
          />
          <Line
            dataKey="ema50" stroke="#3b82f6" strokeWidth={1} dot={false}
            isAnimationActive={false} connectNulls
          />
          <Line
            dataKey="ema20" stroke="#eab308" strokeWidth={1} dot={false}
            isAnimationActive={false} connectNulls
          />
          {mode === "line" && (
            <Line
              dataKey="close" stroke="#e6edf6" strokeWidth={1.6} dot={false}
              isAnimationActive={false}
            />
          )}

          {/* Where one session ends and the next begins. Without this the
              overnight step in price looks like a five-minute move. */}
          {dividers.map((d) => (
            <ReferenceLine key={d.ms} x={d.ms} stroke="#232d3a" strokeWidth={1} />
          ))}

          {/* Unfilled gaps and unswept liquidity — support and resistance
              the engine identified, at the level it identified them. */}
          {pools.map((p, i) => (
            <ReferenceLine
              key={`pool-${i}`} y={p.level} stroke="#64748b"
              strokeDasharray="1 5" strokeWidth={1}
              label={{ value: p.side, position: "left", fill: "#64748b", fontSize: 9 }}
            />
          ))}
          {gaps.map((g, i) => (
            Number.isFinite(g.midpoint) && (
              <ReferenceLine
                key={`fvg-${i}`} y={g.midpoint} stroke="#0ea5e9"
                strokeDasharray="4 4" strokeWidth={1}
                label={{ value: "FVG", position: "left", fill: "#0ea5e9", fontSize: 9 }}
              />
            )
          ))}

          {plan && (
            <>
              <ReferenceLine y={signal.entry} stroke="#e6edf6" strokeDasharray="2 4"
                label={{ value: "entry", position: "right", fill: "#8b98a9", fontSize: 10 }} />
              <ReferenceLine y={signal.stop_loss} stroke="#f0616d"
                label={{ value: "stop", position: "right", fill: "#f0616d", fontSize: 10 }} />
              <ReferenceLine y={signal.target} stroke="#2ec27e"
                label={{ value: "target", position: "right", fill: "#2ec27e", fontSize: 10 }} />
            </>
          )}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
