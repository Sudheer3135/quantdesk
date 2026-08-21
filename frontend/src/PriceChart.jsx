import { useMemo } from "react";
import {
  Area, ComposedChart, Line, ReferenceLine, ResponsiveContainer,
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

   Deliberately not a candlestick chart. At 5-minute resolution across a
   session, candle bodies become slivers a couple of pixels wide and the
   thing you actually want to read - where price sits relative to VWAP and
   to your own levels - gets lost in them. A line with the VWAP band shaded
   behind it says that in one glance.

   The axis is categorical rather than a time scale, which is the usual
   choice for an intraday chart: a time scale would render the seventeen
   hours the exchange is shut as seventeen hours of empty width. The cost is
   that the gap between sessions is invisible, so it is drawn explicitly. */
export default function PriceChart({ candles, signal, bars = 120 }) {
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

  const plan = signal && signal.action !== "HOLD";
  // The first bar opens the window rather than a new day within it, so it
  // gets no divider — there is nothing to its left to divide it from.
  const dividers = data.filter((d, i) => d.sessionStart && i > 0);
  const spanned = new Set(data.map((d) => d.session)).size;

  return (
    <div className="panel chart-panel">
      <div className="panel-head">
        <h3>Price · VWAP</h3>
        <span className="panel-note">
          last {data.length} bars
          {spanned > 1 && ` · ${spanned} sessions`}
          {" · "}{dayLabel(data[0].ms)}–{dayLabel(data[data.length - 1].ms)} IST
        </span>
      </div>
      <ResponsiveContainer width="100%" height={280}>
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
          <Line
            dataKey="close" stroke="#dfe6ef" strokeWidth={1.6} dot={false}
            isAnimationActive={false}
          />

          {/* Where one session ends and the next begins. Without this the
              overnight step in price looks like a five-minute move. */}
          {dividers.map((d) => (
            <ReferenceLine key={d.ms} x={d.ms} stroke="#232d3a" strokeWidth={1} />
          ))}

          {plan && (
            <>
              <ReferenceLine y={signal.entry} stroke="#dfe6ef" strokeDasharray="2 4"
                label={{ value: "entry", position: "right", fill: "#7c8899", fontSize: 10 }} />
              <ReferenceLine y={signal.stop_loss} stroke="#d9614c"
                label={{ value: "stop", position: "right", fill: "#d9614c", fontSize: 10 }} />
              <ReferenceLine y={signal.target} stroke="#45b880"
                label={{ value: "target", position: "right", fill: "#45b880", fontSize: 10 }} />
            </>
          )}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
