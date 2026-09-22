/* The two-series projection behind the Engine/Risk chart.

   Kept pure and separate from the SVG so the arithmetic can be tested
   without a DOM, the same way `chart-data.js` sits under `PriceChart`.

   **What the two lines are, and why this pairing is the one worth
   drawing.** The desk has two authorities and they disagree. The signal
   engine scores conviction on every bar; the risk manager then decides
   whether that conviction may be acted on at all. Plotted alone, either
   line is close to useless — conviction with no gate reads as a strategy
   that never fires, and a gate with no conviction reads as a desk that
   never wanted to trade. The gap between them is the whole diagnosis:
   measured on 15-Sep-2026 across 200 stored signals, the engine produced
   conviction up to 0.75 and the risk manager approved exactly none of
   them. That is not visible in any single number on the desk, and it is
   the first thing somebody should see.

   **Why the risk line breaks instead of dropping to zero.** A HOLD is
   not a rejection — the risk manager was never asked. Drawing it at zero
   would put a hundred phantom refusals on the chart and make a quiet
   engine look like an obstructive risk desk. A gap says "no question was
   put", which is what actually happened. */

/* The risk manager's verdict, on the same 0..1 axis as confidence.
   `null` means it had no opinion and the line should break. */
export function gateLevel(state) {
  if (state === "approved") return 1;
  if (state === "blocked") return 0;
  return null;                       // "not-applicable", missing, unknown
}

const asMs = (iso) => {
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : null;
};

const asUnit = (n) => {
  const v = Number(n);
  if (!Number.isFinite(v)) return null;
  /* Confidence is a probability. Anything outside [0,1] is a bug
     upstream, and clamping it silently would hide that, so it is
     dropped instead — a missing point is visible, a clamped one is a
     lie that plots cleanly. */
  return v >= 0 && v <= 1 ? v : null;
};

/* History rows -> points in time order, oldest first.

   The API returns newest first, which is right for a feed and wrong for
   a chart: drawn in that order every line runs backwards through time. */
export function toSeries(history) {
  if (!Array.isArray(history)) return [];
  return history
    .map((row) => {
      const t = asMs(row?.created_at ?? row?.timestamp);
      if (t === null) return null;
      return {
        t,
        confidence: asUnit(row?.confidence),
        gate: gateLevel(row?.risk_state),
        action: row?.action ?? null,
      };
    })
    .filter(Boolean)
    .sort((a, b) => a.t - b.t);
}

/* An SVG path for one series, with gaps where the value is absent.

   Breaks are real breaks — a new `M` rather than a line to the next
   point — because bridging a gap draws a segment through time the
   series says nothing about. */
export function pathFor(points, key, box) {
  if (!Array.isArray(points) || points.length === 0) return "";
  const { width, height, pad = 0 } = box;
  const t0 = points[0].t;
  const t1 = points[points.length - 1].t;
  const span = t1 - t0;
  const usable = Math.max(1, width - pad * 2);

  const x = (t) => pad + (span === 0 ? usable / 2 : ((t - t0) / span) * usable);
  const y = (v) => pad + (1 - v) * Math.max(1, height - pad * 2);

  let d = "";
  let open = false;
  for (const p of points) {
    const v = p[key];
    if (v === null || v === undefined) { open = false; continue; }
    const cmd = open ? "L" : "M";
    d += `${d ? " " : ""}${cmd}${x(p.t).toFixed(2)},${y(v).toFixed(2)}`;
    open = true;
  }
  return d;
}

/* The headline the chart exists to deliver, in words.

   A chart that has to be interpreted before it says anything gets
   glanced at and skipped, so the divergence is also stated outright. */
export function divergence(points) {
  const asked = points.filter((p) => p.gate !== null);
  const blocked = asked.filter((p) => p.gate === 0);
  const confident = blocked.filter((p) => (p.confidence ?? 0) >= 0.5);
  return {
    asked: asked.length,
    blocked: blocked.length,
    approved: asked.length - blocked.length,
    blockedWithConviction: confident.length,
  };
}
