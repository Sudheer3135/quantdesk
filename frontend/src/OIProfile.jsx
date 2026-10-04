import { useMemo } from "react";
import {
  Bar, BarChart, Cell, ReferenceLine, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";
import { heaviest, oiValue } from "./oi.js";
import { THEME } from "./theme.js";

/* Consistent units. The old formatter only switched to lakhs above 1e5, so
   one axis carried "74893" and "2.2L" side by side and the reader had to
   convert between them mid-glance. Everything is lakhs now, small end
   included. */
const lakh = (v) => (oiValue(v) === null ? "—" : `${(Math.abs(v) / 1e5).toFixed(1)}L`);
// The axis ticks are always numbers; only readings can be missing.
const tickLakh = (v) => `${(Math.abs(v) / 1e5).toFixed(1)}L`;
// Missing is not zero (OC-5). `Math.round(null)` is 0, so an unavailable max
// pain used to render as strike 0 — a reading nobody took.
const strikeLabel = (v) =>
  v === null || v === undefined || !Number.isFinite(Number(v))
    ? "—" : Math.round(v).toLocaleString("en-IN");

function Callout({ active, payload }) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload;
  const heavier = row.callOI === null || row.putOI === null ? "comparison unavailable"
    : row.callOI > row.putOI ? "calls heavier" : "puts heavier";
  return (
    <div className="callout">
      <div className="callout-time">{strikeLabel(row.strike)}</div>
      <div className="callout-row"><span>Call OI</span><b>{lakh(row.callOI)}</b></div>
      <div className="callout-row"><span>Put OI</span><b>{lakh(row.putOI)}</b></div>
      <div className="callout-row">
        <span>{row.aboveSpot ? "Above spot" : "Below spot"}</span>
        <b>{heavier}</b>
      </div>
    </div>
  );
}

/* The chart rows. Exported so the missing-versus-zero rule can be tested
   on the data the chart is given, not only on the captions around it. */
export function profileRows(strikes, spot, span = 14) {
  if (!strikes?.length || !spot) return [];
  const near = [...strikes]
    .sort((a, b) => Math.abs(a.strike - spot) - Math.abs(b.strike - spot))
    .slice(0, span)
    .sort((a, b) => b.strike - a.strike);

  // Missing stays null all the way to the chart: recharts draws no bar
  // for a null, where a 0 would draw a genuine-looking empty reading.
  return near.map((s) => {
    const callOI = oiValue(s.call_oi);
    const putOI = oiValue(s.put_oi);
    return {
      strike: s.strike,
      callOI,
      putOI,
      call: callOI === null ? null : -callOI,   // negative so it draws to the left
      put: putOI,
      aboveSpot: s.strike >= spot,
    };
  });
}

/* Where option writers have committed capital.

   Calls left of centre, puts right — the standard desk layout, so it reads
   the same way as any chain you will see elsewhere.

   Two things this file used to claim and not do. The comment said spot and
   max pain were marked; only a zero line was drawn, so a reader had no
   anchor and could not tell which strikes sat above the money. And the fade
   on each bar encodes something real — calls written above spot are the
   ceiling being defended, puts written below are the floor, and those are
   the halves that matter — but nothing on screen said so, which made it
   look like an inconsistent render.

   Both are now drawn and both are labelled.

   This is positioning, not prediction. Crowds are sometimes right. */
export default function OIProfile({ strikes, summary, spot, span = 14 }) {
  const data = useMemo(() => profileRows(strikes, spot, span), [strikes, spot, span]);

  if (!data.length) {
    return <div className="panel"><h3>Open interest</h3>
      <p className="muted-body">No option chain loaded.</p></div>;
  }

  const recorded = data.flatMap((d) => [d.callOI, d.putOI]).filter((v) => v !== null);
  // One unit wide when nothing (or only zeros) was recorded, so the axis
  // still has a domain; no bar is drawn for a missing reading either way.
  const widest = Math.max(1, ...recorded);

  // The strike nearest spot, so the chart has an anchor. Spot itself never
  // lands exactly on a 50-point strike, and a reference line on a category
  // axis has to match a category to draw at all.
  const atm = data.reduce((best, d) =>
    Math.abs(d.strike - spot) < Math.abs(best.strike - spot) ? d : best, data[0]);

  const maxPain = summary?.max_pain;
  const maxPainInView = data.some((d) => d.strike === maxPain);

  // The two levels a reader actually acts on: the heaviest committed
  // capital on each side. A strike with no recorded OI is never a wall.
  const callWall = heaviest(data, (d) => d.callOI);
  const putWall = heaviest(data, (d) => d.putOI);

  // The backend computes (spot - max_pain) / spot, so a negative reading
  // means max pain sits *above* spot. "vs spot −0.26%" left the reader to
  // work out which way round that was. Say it in words.
  const distance = summary?.max_pain_distance_pct;
  const pull = distance == null ? null
    : distance > 0 ? `${Math.abs(distance).toFixed(2)}% below spot`
    : distance < 0 ? `${Math.abs(distance).toFixed(2)}% above spot`
    : "level with spot";

  const ticks = [-widest, -widest / 2, 0, widest / 2, widest];

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Open interest</h3>
        <span className="panel-note">calls left · puts right</span>
      </div>

      <p className="chart-legend">
        Solid bars are the side that matters: calls written above spot are the
        ceiling sellers are defending, puts written below are the floor. Faded
        bars sit on the other side of the money.
      </p>

      <ResponsiveContainer width="100%" height={300}>
        <BarChart data={data} layout="vertical" barCategoryGap={2}
                  margin={{ top: 4, right: 8, bottom: 4, left: 0 }}>
          <XAxis
            type="number" domain={[-widest, widest]} ticks={ticks}
            tickFormatter={tickLakh}
            tick={{ fill: THEME.dim, fontSize: 10 }} axisLine={false} tickLine={false}
          />
          <YAxis
            type="category" dataKey="strike" width={54}
            tickFormatter={strikeLabel}
            tick={{ fill: THEME.dim, fontSize: 10 }} axisLine={false} tickLine={false}
          />
          <Tooltip content={<Callout />} cursor={{ fill: THEME.raise }} />
          <ReferenceLine x={0} stroke={THEME.grid} />

          <ReferenceLine
            y={atm.strike} stroke={THEME.wait} strokeDasharray="3 3"
            label={{ value: `spot ${strikeLabel(spot)}`, position: "insideTopRight",
                     fill: THEME.wait, fontSize: 9 }}
          />
          {maxPainInView && maxPain !== atm.strike && (
            <ReferenceLine
              y={maxPain} stroke={THEME.dim} strokeDasharray="2 4"
              label={{ value: "max pain", position: "insideTopRight",
                       fill: THEME.dim, fontSize: 9 }}
            />
          )}

          <Bar dataKey="call" isAnimationActive={false}>
            {data.map((d, i) => (
              <Cell key={i} fill={THEME.down} fillOpacity={d.aboveSpot ? 0.85 : 0.3} />
            ))}
          </Bar>
          <Bar dataKey="put" isAnimationActive={false}>
            {data.map((d, i) => (
              <Cell key={i} fill={THEME.up} fillOpacity={d.aboveSpot ? 0.3 : 0.85} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>

      <p className="chart-caption">
        {callWall
          ? <>Heaviest call OI at <b>{strikeLabel(callWall.strike)}</b> ({lakh(callWall.callOI)})</>
          : <>Call OI <b>unavailable</b></>}
        {" · "}
        {putWall
          ? <>heaviest put OI at <b>{strikeLabel(putWall.strike)}</b> ({lakh(putWall.putOI)})</>
          : <>put OI <b>unavailable</b></>}
        {maxPain && !maxPainInView && (
          <> {" · "}max pain {strikeLabel(maxPain)} is outside this range</>
        )}
      </p>

      {summary && (
        <div className="chain-strip">
          <div><span>PCR</span><b>{summary.pcr_oi == null
            ? "unavailable" : summary.pcr_oi.toFixed(2)}</b></div>
          <div><span>Max pain</span><b>{strikeLabel(summary.max_pain)}</b></div>
          <div><span>Max pain sits</span>
            <b className={distance > 0 ? "down" : distance < 0 ? "up" : ""}>
              {pull ?? "—"}
            </b></div>
          <div><span>Bias</span>
            <b className={`tag ${summary.bias}`}>{summary.bias}</b></div>
        </div>
      )}
    </div>
  );
}
