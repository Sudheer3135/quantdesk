import { Fragment, useMemo, useState } from "react";

import { chainSource, ageText } from "./chain-source.js";
import { heaviest, oiValue } from "./oi.js";
import { spotBand } from "./render-keys.js";

/* The strike ladder.

   Everything the desk already showed of the chain was an aggregate — a put/
   call ratio, a max pain, a bar chart of open interest. Aggregates are what
   you read to form a view. The ladder is what you read to place a trade:
   which strike, at what premium, against how much written interest. It was
   the one part of the chain that had never been on screen.

   Layout is the exchange's, so it reads the same way as any chain elsewhere
   on the desk: calls on the left, puts on the right, strikes climbing down
   the middle. In-the-money cells sit on a lifted ground on each side — the
   two shaded blocks meet at the money, which is what makes spot findable
   without reading a single number.

   Two columns appear only when the source carries them. The polled NSE
   snapshot has the day's change in open interest and an implied volatility
   per strike; the streamed chain has neither — the feed publishes no IV,
   and deriving one here would put a modelled number in the same row as
   observed ones with nothing to tell them apart. Rendering those columns as
   a wall of dashes would read as breakage; leaving them out and saying why
   is the honest version. What the stream has instead is a live bid and ask,
   which is on the premium cell as a tooltip.
*/

// Missing OI prints as a dash, never as 0.0L (OC-5).
const lakh = (v) => (oiValue(v) === null ? "—" : `${(Math.abs(Number(v)) / 1e5).toFixed(1)}L`);
const strikeLabel = (v) => Math.round(Number(v)).toLocaleString("en-IN");

const premium = (v) =>
  v === null || v === undefined || Number.isNaN(Number(v)) || Number(v) === 0
    ? "—"
    : Number(v).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/* NSE publishes 0 for an untraded strike's implied volatility rather than
   omitting it. Zero is not a reading, so it formats as an absence. */
const iv = (v) =>
  Number(v) > 0 ? `${Number(v).toFixed(1)}` : "—";

/* The greeks, each at the precision it is actually read to.

   Gamma needs four places because an ATM NIFTY weekly runs about
   0.0010 and the digit that matters is the fourth; delta needs two and
   would be noise at four. Formatting them all alike would either bury
   gamma in rounding or print six meaningless places on delta. */
const g2 = (v) =>
  v === null || v === undefined || !Number.isFinite(Number(v))
    ? "—" : Number(v).toFixed(2);
const g4 = (v) =>
  v === null || v === undefined || !Number.isFinite(Number(v))
    ? "—" : Number(v).toFixed(4);

/* Greeks columns, outermost first, so calls read right-to-left into the
   strike and puts read left-to-right out of it. Declared once and
   mirrored rather than written twice: two hand-written orders is how
   the delta column ends up above the gamma header on one side only. */
const GREEKS = [
  { key: "rho", label: "Rho", fmt: g2 },
  { key: "vega", label: "Vega", fmt: g2 },
  { key: "gamma", label: "Gamma", fmt: g4 },
  { key: "theta", label: "Theta", fmt: g2 },
  { key: "delta", label: "Delta", fmt: g2 },
];

const oiChange = (v) => {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  if (n === 0) return "0";
  return `${n > 0 ? "+" : "−"}${lakh(n)}`;
};

/* How many strikes either side of the money to show. Twelve is about a
   screen without scrolling and comfortably covers where NIFTY weeklies
   actually trade; the wider settings are for reading the wings on an
   expiry day. */
const SPANS = [12, 20, 40];

function spreadTitle(bid, ask) {
  const b = Number(bid), a = Number(ask);
  if (!(b > 0) || !(a > 0)) return undefined;
  return `bid ${b.toFixed(2)} · ask ${a.toFixed(2)} · spread ${(a - b).toFixed(2)}`;
}

export default function OptionChain({ chain, spot, nowMs = Date.now(), skewMs = 0 }) {
  const [span, setSpan] = useState(SPANS[0]);
  /* Start with the core chain; the existing control reveals all Greeks. */
  const [showGreeks, setShowGreeks] = useState(false);

  const source = chainSource(chain, nowMs, skewMs);

  /* Spot decides which half of every row is in the money, so it has to come
     from somewhere. The live price is preferred because it is newer than
     the chain; the chain's own spot is the fallback, and without either the
     ladder still renders — just with no shading and no anchor, rather than
     with a made-up centre. */
  const anchor = Number.isFinite(Number(spot)) && Number(spot) > 0
    ? Number(spot)
    : Number(chain?.summary?.spot) || null;

  /* Keyed on the spot's *band*, not the spot.

     Which strikes are nearest can only change where the spot crosses a
     strike or the midpoint between two. Keyed on the raw price this
     sorted forty strikes twice on every tick — several times a second —
     to produce the identical list. `anchor` is still what does the
     sorting; the band only decides when the sorting is worth redoing,
     and every spot inside one band yields the same order. */
  const band = spotBand(anchor);
  const rows = useMemo(() => {
    const all = chain?.strikes ?? [];
    if (!all.length) return [];
    if (!anchor) return [...all].sort((a, b) => a.strike - b.strike).slice(0, span * 2);
    return [...all]
      .sort((a, b) => Math.abs(a.strike - anchor) - Math.abs(b.strike - anchor))
      .slice(0, span * 2)
      .sort((a, b) => a.strike - b.strike);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chain, band, span]);

  if (!chain || !rows.length) {
    return (
      <div className="panel">
        <div className="panel-head">
          <h3>Option chain</h3>
          <span className={`pill tone-${source.tone}`}>{source.label}</span>
        </div>
        <p className="muted-body">
          {source.kind === "none"
            ? "No option chain loaded. The backend serves one at /market/option-chain."
            : "The chain arrived with no strikes in it."}
        </p>
      </div>
    );
  }

  /* Columns the source does not carry are not rendered at all. Tested
     against the rows on screen rather than the payload, so widening the
     span can reveal a column and narrowing it cannot hide one that the
     visible rows are using. */
  const hasChange = rows.some((r) =>
    Number.isFinite(Number(r.call_oi_change)) || Number.isFinite(Number(r.put_oi_change)));
  const hasIV = rows.some((r) => Number(r.call_iv) > 0 || Number(r.put_iv) > 0);
  /* Derived from the premium, so they arrive with IV or not at all. */
  const hasGreeks = rows.some((r) => Number.isFinite(Number(r.call_delta))
                                  || Number.isFinite(Number(r.put_delta)));
  const greeks = hasGreeks && showGreeks;

  /* The scale for the open-interest bars. One scale across both sides and
     every visible row, because a bar that rescales per column would make a
     small put wall look like a large one. */
  const widest = Math.max(1, ...rows.flatMap((r) => [oiValue(r.call_oi), oiValue(r.put_oi)])
    .filter((v) => v !== null));

  /* A strike with no recorded OI is never the wall (OC-5). */
  const callWall = heaviest(rows, (r) => r.call_oi);
  const putWall = heaviest(rows, (r) => r.put_oi);

  /* The row the money is nearest. Spot almost never lands on a 50-point
     strike, so this is the closest one rather than an equality test. */
  const atm = anchor
    ? rows.reduce((best, r) =>
        Math.abs(r.strike - anchor) < Math.abs(best.strike - anchor) ? r : best, rows[0])
    : null;
  const maxPain = Number(chain?.summary?.max_pain) || null;

  const bar = (value) => (oiValue(value) === null ? {}
    : { "--oi-fill": `${(oiValue(value) / widest) * 100}%` });

  /* Counted once. Both sides carry the same columns by construction, so
     a single number keeps the two `colSpan`s from drifting apart when a
     column is toggled. */
  const sideCols = (greeks ? GREEKS.length : 0) + (hasIV ? 1 : 0)
    + (hasChange ? 1 : 0) + 2;

  const expiry = chain.expiry
    ? new Date(chain.expiry).toLocaleDateString("en-IN",
        { timeZone: "Asia/Kolkata", day: "2-digit", month: "short" })
    : null;

  return (
    <div className="panel chain-panel">
      <div className="panel-head">
        <h3>Option chain</h3>
        <span className={`pill tone-${source.tone}`} title={
          source.streamed
            ? "Assembled from Angel websocket ticks. Each strike carries its own age; the badge shows the oldest print on screen."
            : "A single NSE snapshot, fetched over HTTP and cached."
        }>
          {source.label} · {source.detail}
        </span>
      </div>

      <div className="chain-meta">
        <span>{expiry ? `Expiry ${expiry}` : "Expiry unstated"}</span>
        <span>{rows.length} of {chain.strikes.length} strikes</span>
        {anchor && <span className="mono">Spot {strikeLabel(anchor)}</span>}
        {source.streamed && Number.isFinite(chain.contracts) && (
          <span>{chain.contracts} contracts live</span>
        )}
        {source.streamed && chain.dropped_stale > 0 && (
          <span className="dim">{chain.dropped_stale} dropped as stale</span>
        )}
        <span className="chain-spans">
          {hasGreeks && (
            <button
              type="button"
              className={`span-btn greek-btn${showGreeks ? " on" : ""}`}
              aria-pressed={showGreeks}
              onClick={() => setShowGreeks((v) => !v)}
              title="Delta, gamma, theta, vega and rho, derived from the traded premium"
            >Greeks</button>
          )}
          {SPANS.map((n) => (
            <button
              key={n} type="button"
              className={`span-btn${n === span ? " on" : ""}`}
              aria-pressed={n === span}
              onClick={() => setSpan(n)}
            >±{n}</button>
          ))}
        </span>
      </div>

      <div className="chain-guide">
        <span><i className="itm-key" /> In the money</span>
        <span><i className="atm-key" /> ATM · nearest strike</span>
        <span>OI & ΔOI in lakhs · premiums in ₹ · IV in %</span>
      </div>
      <div className="chain-scroll" tabIndex={0} role="region" aria-label="Option chain table, scroll for more strikes and columns">

        <table className={`chain-ladder${greeks ? " with-greeks" : ""}`}>
          <caption className="sr-only">
            NIFTY option chain, calls left and puts right, by strike
          </caption>
          <thead>
            <tr className="chain-sides">
              <th colSpan={sideCols} scope="colgroup" className="side-call">Calls · CE</th>
              <th className="side-strike">Strike</th>
              <th colSpan={sideCols} scope="colgroup" className="side-put">Puts · PE</th>
            </tr>
            <tr>
              {/* Calls read outward-in: the greeks furthest from the
                  strike, premium against it. Puts mirror it, so the two
                  books open away from the money and the eye compares
                  like against like across the centre. */}
              {greeks && GREEKS.map((g) => (
                <th key={`c-${g.key}`} className="num greek">{g.label}</th>
              ))}
              {hasIV && <th className="num greek">IV</th>}
              <th className="num">OI</th>
              {hasChange && <th className="num">ΔOI</th>}
              <th className="num">LTP</th>
              <th scope="col" className="side-strike">Price</th>
              <th className="num">LTP</th>
              {hasChange && <th className="num">ΔOI</th>}
              <th className="num">OI</th>
              {hasIV && <th className="num greek">IV</th>}
              {greeks && [...GREEKS].reverse().map((g) => (
                <th key={`p-${g.key}`} className="num greek">{g.label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => {
              const isAtm = atm && r.strike === atm.strike;
              const callItm = anchor ? r.strike < anchor : false;
              const putItm = anchor ? r.strike > anchor : false;
              /* Spot almost never lands on a strike, so the live price is
                 drawn *between* the two it sits between rather than on
                 either. Without it the ladder tells you where the money
                 is only to the nearest fifty points, which on a weekly
                 is most of a delta. */
              const prev = rows[i - 1];
              const crossesSpot = anchor && prev
                && prev.strike < anchor && r.strike >= anchor;
              return (
                <Fragment key={r.strike}>
                {crossesSpot && (
                  <tr className="chain-spot" aria-hidden="true">
                    <td colSpan={sideCols * 2 + 1}>
                      <span className="chain-spot-badge mono">
                        {"Spot "}
                        {Number(anchor).toLocaleString("en-IN",
                          { minimumFractionDigits: 2,
                            maximumFractionDigits: 2 })}
                      </span>
                    </td>
                  </tr>
                )}
                <tr className={isAtm ? "row-atm" : ""}>
                  {greeks && GREEKS.map((g) => (
                    <td key={`c-${g.key}`}
                        className={`num greek${callItm ? " itm" : ""}`}>
                      {g.fmt(r[`call_${g.key}`])}
                    </td>
                  ))}
                  {hasIV && <td className={`num greek${callItm ? " itm" : ""}`}>{iv(r.call_iv)}</td>}
                  <td className={`num oi${callItm ? " itm" : ""}${r.strike === callWall?.strike ? " wall" : ""}`}
                      style={bar(r.call_oi)}>
                    <span>{lakh(r.call_oi)}</span>
                  </td>
                  {hasChange && (
                    <td className={`num ${Number(r.call_oi_change) > 0 ? "down" : Number(r.call_oi_change) < 0 ? "up" : ""}${callItm ? " itm" : ""}`}>
                      {oiChange(r.call_oi_change)}
                    </td>
                  )}
                  <td className={`num ltp${callItm ? " itm" : ""}`}
                      title={spreadTitle(r.call_bid, r.call_ask)}>
                    {premium(r.call_ltp)}
                  </td>

                  <td className="strike">
                    {strikeLabel(r.strike)}
                    {isAtm && <em className="strike-flag">ATM</em>}
                    {!isAtm && maxPain === r.strike && <em className="strike-flag pain">pain</em>}
                  </td>

                  <td className={`num ltp${putItm ? " itm" : ""}`}
                      title={spreadTitle(r.put_bid, r.put_ask)}>
                    {premium(r.put_ltp)}
                  </td>
                  {hasChange && (
                    <td className={`num ${Number(r.put_oi_change) > 0 ? "up" : Number(r.put_oi_change) < 0 ? "down" : ""}${putItm ? " itm" : ""}`}>
                      {oiChange(r.put_oi_change)}
                    </td>
                  )}
                  <td className={`num oi put-oi${putItm ? " itm" : ""}${r.strike === putWall?.strike ? " wall" : ""}`}
                      style={bar(r.put_oi)}>
                    <span>{lakh(r.put_oi)}</span>
                  </td>
                  {hasIV && <td className={`num greek${putItm ? " itm" : ""}`}>{iv(r.put_iv)}</td>}
                  {greeks && [...GREEKS].reverse().map((g) => (
                    <td key={`p-${g.key}`}
                        className={`num greek${putItm ? " itm" : ""}`}>
                      {g.fmt(r[`put_${g.key}`])}
                    </td>
                  ))}
                </tr>
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>

      <p className="chain-foot">
        {callWall
          ? <>Heaviest call OI <b className="mono">{strikeLabel(callWall.strike)}</b> ({lakh(callWall.call_oi)})</>
          : <>Call OI <b>unavailable</b></>}
        {" · "}
        {putWall
          ? <>heaviest put OI <b className="mono">{strikeLabel(putWall.strike)}</b> ({lakh(putWall.put_oi)})</>
          : <>put OI <b>unavailable</b></>}
        {". "}Shaded cells are in the money. Rising call OI is written resistance,
        rising put OI written support — this is positioning, not prediction.
      </p>

      {source.streamed && (
        <p className="chain-foot dim">
          The websocket carries no day-change in open interest and no implied
          volatility, so those two columns are absent rather than blank. Hover a
          premium for its live bid and ask. Each strike is timed separately; the
          oldest print on screen is {ageText(source.ageSeconds)} old.
        </p>
      )}
    </div>
  );
}
