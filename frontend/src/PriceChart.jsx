import { useMemo } from "react";
import {
  Area, ComposedChart, Line, ReferenceLine, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";

const IST = { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" };

function hhmm(ts) {
  return new Date(ts).toLocaleTimeString("en-IN", IST);
}

function Callout({ active, payload }) {
  if (!active || !payload?.length) return null;
  const bar = payload[0].payload;
  return (
    <div className="callout">
      <div className="callout-time">{hhmm(bar.timestamp)}</div>
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
   behind it says that in one glance. */
export default function PriceChart({ candles, signal, bars = 120 }) {
  const data = useMemo(() => {
    if (!candles?.length) return [];
    return candles.slice(-bars).map((c) => ({
      ...c,
      band: c.vwap_lower != null && c.vwap_upper != null
        ? [c.vwap_lower, c.vwap_upper]
        : null,
    }));
  }, [candles, bars]);

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

  return (
    <div className="panel chart-panel">
      <div className="panel-head">
        <h3>Price · VWAP</h3>
        <span className="panel-note">last {data.length} bars</span>
      </div>
      <ResponsiveContainer width="100%" height={280}>
        <ComposedChart data={data} margin={{ top: 8, right: 52, bottom: 4, left: 0 }}>
          <XAxis
            dataKey="timestamp" tickFormatter={hhmm} minTickGap={56}
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
