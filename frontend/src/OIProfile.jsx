import { useMemo } from "react";
import {
  Bar, BarChart, Cell, ReferenceLine, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";

const lakh = (v) => (Math.abs(v) >= 1e5 ? `${(v / 1e5).toFixed(1)}L` : Math.round(v));

function Callout({ active, payload }) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload;
  return (
    <div className="callout">
      <div className="callout-time">{row.strike}</div>
      <div className="callout-row"><span>Call OI</span><b>{lakh(row.callOI)}</b></div>
      <div className="callout-row"><span>Put OI</span><b>{lakh(row.putOI)}</b></div>
    </div>
  );
}

/* Where option writers have committed capital.

   Calls drawn left of centre, puts right — the standard desk layout, so it
   reads the same way as any chain you will see elsewhere. The tall call
   bars above spot are the ceiling sellers are defending; the tall put bars
   below are the floor. Max pain and spot are marked because the distance
   between them is usually more informative than either alone.

   This is positioning, not prediction. Crowds are sometimes right. */
export default function OIProfile({ strikes, summary, spot, span = 14 }) {
  const data = useMemo(() => {
    if (!strikes?.length || !spot) return [];
    const near = [...strikes]
      .sort((a, b) => Math.abs(a.strike - spot) - Math.abs(b.strike - spot))
      .slice(0, span)
      .sort((a, b) => b.strike - a.strike);

    return near.map((s) => ({
      strike: s.strike,
      callOI: s.call_oi || 0,
      putOI: s.put_oi || 0,
      call: -(s.call_oi || 0),   // negative so it draws to the left
      put: s.put_oi || 0,
      aboveSpot: s.strike >= spot,
    }));
  }, [strikes, spot, span]);

  if (!data.length) {
    return <div className="panel"><h3>Open interest</h3>
      <p className="muted-body">No option chain loaded.</p></div>;
  }

  const widest = Math.max(...data.flatMap((d) => [d.callOI, d.putOI]));

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Open interest</h3>
        <span className="panel-note">calls left · puts right</span>
      </div>
      <ResponsiveContainer width="100%" height={300}>
        <BarChart data={data} layout="vertical" barCategoryGap={2}
                  margin={{ top: 4, right: 8, bottom: 4, left: 0 }}>
          <XAxis
            type="number" domain={[-widest, widest]} tickFormatter={(v) => lakh(Math.abs(v))}
            tick={{ fill: "#7c8899", fontSize: 10 }} axisLine={false} tickLine={false}
          />
          <YAxis
            type="category" dataKey="strike" width={54}
            tick={{ fill: "#7c8899", fontSize: 10 }} axisLine={false} tickLine={false}
          />
          <Tooltip content={<Callout />} cursor={{ fill: "#ffffff08" }} />
          <ReferenceLine x={0} stroke="#232d3a" />

          <Bar dataKey="call" isAnimationActive={false}>
            {data.map((d, i) => (
              <Cell key={i} fill="#d9614c" fillOpacity={d.aboveSpot ? 0.85 : 0.35} />
            ))}
          </Bar>
          <Bar dataKey="put" isAnimationActive={false}>
            {data.map((d, i) => (
              <Cell key={i} fill="#45b880" fillOpacity={d.aboveSpot ? 0.35 : 0.85} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>

      {summary && (
        <div className="chain-strip">
          <div><span>PCR</span><b>{summary.pcr_oi?.toFixed(2)}</b></div>
          <div><span>Max pain</span><b>{Math.round(summary.max_pain)}</b></div>
          <div><span>vs spot</span>
            <b className={summary.max_pain_distance_pct > 0 ? "down" : "up"}>
              {summary.max_pain_distance_pct?.toFixed(2)}%
            </b></div>
          <div><span>Bias</span>
            <b className={`tag ${summary.bias}`}>{summary.bias}</b></div>
        </div>
      )}
    </div>
  );
}