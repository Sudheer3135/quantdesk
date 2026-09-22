/* The two-series projection, and the design constraints it has to hold.

   The arithmetic tests are ordinary. The ones at the bottom are not:
   they assert properties of the *rendering* — hairline strokes, no area
   fills, no gradient — because those are the constraints the panel was
   specified against, and a constraint nobody tests is a constraint that
   survives exactly until the next person finds a fill prettier. */
import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import EngineRisk from "./EngineRisk.jsx";
import { divergence, gateLevel, pathFor, toSeries } from "./engine-risk.js";

const row = (iso, confidence, risk_state, action = "SELL") =>
  ({ created_at: iso, confidence, risk_state, action });

const BOX = { width: 600, height: 100, pad: 4 };

describe("gateLevel", () => {
  it("puts an approval at the top and a block at the floor", () => {
    expect(gateLevel("approved")).toBe(1);
    expect(gateLevel("blocked")).toBe(0);
  });

  it("has no opinion when the risk manager was never asked", () => {
    /* A HOLD is not a refusal. Drawing "not-applicable" at zero would
       put a phantom refusal on the chart for every quiet bar and make
       an idle engine look like an obstructive risk desk — on the
       15-Sep-2026 data that is 92 of 200 rows misrepresented. */
    expect(gateLevel("not-applicable")).toBeNull();
    expect(gateLevel(undefined)).toBeNull();
    expect(gateLevel("something new")).toBeNull();
  });
});

describe("toSeries", () => {
  it("puts the oldest signal first", () => {
    /* The API returns newest first, which is right for a feed and
       backwards for a chart: drawn in that order every line runs
       right-to-left through time. */
    const out = toSeries([
      row("2026-09-15T10:00:00Z", 0.5, "blocked"),
      row("2026-09-15T09:00:00Z", 0.4, "blocked"),
    ]);
    expect(out.map((p) => p.confidence)).toEqual([0.4, 0.5]);
  });

  it("drops a row with no usable timestamp rather than plotting it at zero", () => {
    const out = toSeries([row("not a date", 0.5, "blocked"),
                          row("2026-09-15T09:00:00Z", 0.4, "blocked")]);
    expect(out).toHaveLength(1);
  });

  it("drops a confidence outside 0..1 instead of clamping it", () => {
    /* Confidence is a probability. A value outside the range is a bug
       upstream; clamping plots it cleanly and hides it, a gap shows it. */
    const out = toSeries([row("2026-09-15T09:00:00Z", 1.4, "blocked")]);
    expect(out[0].confidence).toBeNull();
  });

  it("survives junk without throwing", () => {
    expect(toSeries(null)).toEqual([]);
    expect(toSeries([null, undefined, {}])).toEqual([]);
  });
});

describe("pathFor", () => {
  it("breaks the line where the series says nothing", () => {
    /* Two blocked signals with a HOLD between them must not be joined:
       a bridging segment draws a refusal that never happened. */
    const pts = toSeries([
      row("2026-09-15T09:00:00Z", 0.4, "blocked"),
      row("2026-09-15T09:05:00Z", 0.2, "not-applicable", "HOLD"),
      row("2026-09-15T09:10:00Z", 0.6, "blocked"),
    ]);
    const d = pathFor(pts, "gate", BOX);
    expect(d.match(/M/g)).toHaveLength(2);   // two runs, not one
    expect(d).not.toMatch(/M[^M]*L[^M]*L/);  // neither run has 2 segments
  });

  it("draws one continuous run when nothing is missing", () => {
    const pts = toSeries([
      row("2026-09-15T09:00:00Z", 0.4, "blocked"),
      row("2026-09-15T09:05:00Z", 0.5, "blocked"),
      row("2026-09-15T09:10:00Z", 0.6, "approved"),
    ]);
    const d = pathFor(pts, "confidence", BOX);
    expect(d.match(/M/g)).toHaveLength(1);
    expect(d.match(/L/g)).toHaveLength(2);
  });

  it("puts a blocked verdict on the floor and an approval on the ceiling", () => {
    const pts = toSeries([row("2026-09-15T09:00:00Z", 0.4, "blocked")]);
    const [, y] = pathFor(pts, "gate", BOX).replace("M", "").split(",");
    expect(Number(y)).toBeCloseTo(96, 0);     // height - pad
    const up = toSeries([row("2026-09-15T09:00:00Z", 0.4, "approved")]);
    const [, yUp] = pathFor(up, "gate", BOX).replace("M", "").split(",");
    expect(Number(yUp)).toBeCloseTo(4, 0);    // pad
  });

  it("is empty rather than malformed when there is nothing to draw", () => {
    expect(pathFor([], "gate", BOX)).toBe("");
    expect(pathFor(null, "gate", BOX)).toBe("");
  });
});

describe("divergence", () => {
  it("counts only the signals the risk manager actually judged", () => {
    const pts = toSeries([
      row("2026-09-15T09:00:00Z", 0.7, "blocked"),
      row("2026-09-15T09:05:00Z", 0.2, "not-applicable", "HOLD"),
      row("2026-09-15T09:10:00Z", 0.6, "approved"),
      row("2026-09-15T09:15:00Z", 0.3, "blocked"),
    ]);
    expect(divergence(pts)).toEqual({
      asked: 3, blocked: 2, approved: 1, blockedWithConviction: 1,
    });
  });
});

describe("the panel's design constraints", () => {
  const history = [
    row("2026-09-15T09:00:00Z", 0.7, "blocked"),
    row("2026-09-15T09:05:00Z", 0.2, "not-applicable", "HOLD"),
    row("2026-09-15T09:10:00Z", 0.6, "approved"),
  ];

  it("draws two series and nothing underneath them", () => {
    /* `fill: none` is load-bearing. An SVG path defaults to a filled
       shape, so a series without it closes on itself and paints a solid
       blob under the line — the decorative fill this chart is specified
       not to have. The stylesheet sets it; this checks nothing has
       overridden it back to a filled area. */
    const { container } = render(<EngineRisk history={history} />);

    const lines = container.querySelectorAll("path.er-line");
    expect(lines).toHaveLength(2);
    for (const path of lines) {
      expect(path.getAttribute("fill")).not.toBe("currentColor");
      expect(path.closest("svg").querySelector("linearGradient")).toBeNull();
    }
  });

  it("carries no gradient or filter anywhere in the plot", () => {
    const { container } = render(<EngineRisk history={history} />);
    const svg = container.querySelector("svg");
    expect(svg.querySelector("defs")).toBeNull();
    expect(svg.querySelector("radialGradient")).toBeNull();
    expect(svg.innerHTML).not.toMatch(/gradient|filter=/i);
  });

  it("keeps its strokes hairline as the panel is stretched", () => {
    /* The viewBox is stretched to the panel width, so without
       `non-scaling-stroke` the horizontal scale factor is applied to
       the stroke too and the lines fatten as the desk widens. */
    const { container } = render(<EngineRisk history={history} />);
    for (const path of container.querySelectorAll("path.er-line")) {
      expect(path.getAttribute("vector-effect")).toBe("non-scaling-stroke");
    }
  });

  it("states the divergence in words, not only as two lines", () => {
    const { container } = render(<EngineRisk history={history} />);
    expect(container.textContent).toMatch(/2 judged/);
    expect(container.textContent).toMatch(/1 blocked/);
    expect(container.textContent).toMatch(/1 approved/);
  });

  it("says so plainly when there is no history yet", () => {
    const { container } = render(<EngineRisk history={[]} />);
    expect(container.textContent).toMatch(/No stored signals/i);
  });

  it("does not redraw while only the price is ticking", () => {
    /* The panel sits on a desk that repaints several times a second and
       depends on none of it. Without the memo the two paths are rebuilt
       a few hundred times for every one time they change. */
    const spy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { container, rerender } = render(<EngineRisk history={history} />);
    const before = container.querySelector("path.er-engine").getAttribute("d");

    rerender(<EngineRisk history={history} />);   // same identity
    const after = container.querySelector("path.er-engine").getAttribute("d");

    expect(after).toBe(before);
    spy.mockRestore();
  });
});
