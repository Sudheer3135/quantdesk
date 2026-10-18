import { memo, useMemo } from "react";

import { divergence, pathFor, toSeries } from "./engine-risk.js";

/* Signal engine against risk manager, on one axis.

   Drawn as raw SVG rather than through the charting library on purpose:
   it is two polylines and a rule, it repaints whenever a signal lands,
   and a canvas renderer plus its data layer is a great deal of machinery
   — and a second thing that can throw inside an effect — for something
   the browser can do in one pass. It also stays crisp at any zoom, which
   a canvas does not.

   No fills under either line. An area fill on a 0..1 axis reads as
   accumulation, and neither of these series accumulates: one is a score
   at an instant, the other is a yes or no. Filling them would imply a
   quantity that does not exist. */

const PAD = 4;
const VIEW_W = 600;          /* a viewBox, not pixels — the path scales */
const VIEW_H = 100;

function EngineRiskView({ history, height = 104 }) {
  const points = useMemo(() => toSeries(history), [history]);

  const { engine, risk, split } = useMemo(() => {
    const box = { width: VIEW_W, height: VIEW_H, pad: PAD };
    return {
      engine: pathFor(points, "confidence", box),
      risk: pathFor(points, "gate", box),
      split: divergence(points),
    };
  }, [points]);

  if (!points.length) {
    return (
      <div className="panel">
        <div className="panel-head"><h3>Engine · Risk</h3></div>
        <p className="muted-body">No stored signals yet.</p>
      </div>
    );
  }

  const mid = PAD + 0.5 * (VIEW_H - PAD * 2);

  return (
    <div className="panel er">
      <div className="panel-head">
        <h3>Engine · Risk</h3>
        <span className="panel-note mono">{points.length} signals</span>
      </div>

      <div className="er-legend">
        <span className="er-key er-key-engine">Signal engine</span>
        <span className="er-key er-key-risk">Risk manager</span>
      </div>

      <svg
        className="er-plot"
        viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
        preserveAspectRatio="none"
        style={{ height }}
        role="img"
        aria-label={
          `Signal confidence against the risk gate over ${points.length} `
          + `signals. ${split.blocked} blocked, ${split.approved} approved.`}
      >
        {/* Rules at 0, 0.5 and 1. `crispEdges` because a horizontal
            hairline landing on a half-pixel antialiases into a 2px grey
            smear, which at this density reads as a third series. */}
        <g shapeRendering="crispEdges" className="er-grid">
          <line x1="0" x2={VIEW_W} y1={PAD} y2={PAD} />
          <line x1="0" x2={VIEW_W} y1={mid} y2={mid} className="er-grid-mid" />
          <line x1="0" x2={VIEW_W} y1={VIEW_H - PAD} y2={VIEW_H - PAD} />
        </g>

        {/* `non-scaling-stroke` is what keeps these hairlines hairlines.
            The viewBox is stretched to the panel width, and without it
            the horizontal scale factor is applied to the stroke too, so
            the line thickens as the panel grows. */}
        <path className="er-line er-risk" d={risk}
              vectorEffect="non-scaling-stroke" />
        <path className="er-line er-engine" d={engine}
              vectorEffect="non-scaling-stroke" />
      </svg>

      <div className="er-axis mono">
        <span>blocked</span><span>0.5</span><span>approved</span>
      </div>

      {/* The finding, said plainly. The lines show it; this makes it
          unmissable to somebody glancing at the desk. */}
      <p className="er-read mono">
        <span className="num">{split.asked}</span> judged ·{" "}
        <span className="num er-blocked">{split.blocked}</span> blocked ·{" "}
        <span className="num er-approved">{split.approved}</span> approved
        {split.blockedWithConviction > 0 && (
          <>
            {" · "}
            <span className="num er-blocked">
              {split.blockedWithConviction}
            </span>{" "}
            blocked at conviction ≥ 0.50
          </>
        )}
      </p>
    </div>
  );
}

/* Streaming guard.

   This panel sits on a desk that repaints on every price tick — several
   a second — and it depends on none of that. It redraws only when the
   stored-signal history actually changes, which is once every few
   minutes. Without this the two paths are rebuilt a few hundred times
   for every one time they differ. */
export default memo(EngineRiskView, (a, b) =>
  a.history === b.history && a.height === b.height);
