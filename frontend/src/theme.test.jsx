/* The JS palette must not drift from the stylesheet.

   `terminal.css` is the source of truth, but lightweight-charts paints
   to a canvas and Recharts wants concrete colour strings, so neither
   can read it — `theme.js` mirrors the values out as literals. A mirror
   nobody checks is a second palette with extra steps, which is exactly
   what this codebase already had: three copies that had drifted, with
   the OI bars painting #d9614c against panels that had moved on to
   #E5484D.

   Nobody reports that as a bug. It just makes the desk look subtly
   wrong in a way that is hard to name and harder to find. */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { OVERLAY_COLORS, THEME } from "./theme.js";

const css = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "terminal.css"), "utf8");

/* The value of a custom property as declared in :root. */
function token(name) {
  const m = css.match(new RegExp(`--${name}\\s*:\\s*([^;]+);`));
  return m ? m[1].trim().toLowerCase() : null;
}

/* Each JS key against the CSS token it mirrors. */
const PAIRS = [
  ["bg", "bg"],
  ["panel", "bg-panel"],
  ["raise", "bg-raise"],
  ["sunk", "bg-sunk"],
  ["grid", "line"],
  ["gridSoft", "line-soft"],
  ["text", "text"],
  ["dim", "text-dim"],
  ["mute", "text-mute"],
  ["up", "go"],
  ["down", "stop"],
  ["wait", "wait"],
  ["info", "info"],
  ["violet", "violet"],
];

describe("the JS palette mirrors the stylesheet", () => {
  it.each(PAIRS)("THEME.%s matches --%s", (jsKey, cssName) => {
    const declared = token(cssName);
    expect(declared, `--${cssName} is not declared in terminal.css`).toBeTruthy();
    expect(THEME[jsKey].toLowerCase()).toBe(declared);
  });

  it("uses the palette the desk was specified against", () => {
    /* The three fixed points of the design system. If one of these
       changes it should be a decision, not a drift. */
    expect(THEME.bg).toBe("#0B0E11");
    expect(THEME.panel).toBe("#151A22");
    expect(THEME.grid).toBe("#232A34");
  });

  it("keeps the moving averages out of the semantic accents", () => {
    /* An EMA is a reference line, not a verdict. Letting one borrow the
       green or the red puts a second meaning on the two colours that
       carry direction on this desk, and then a rising EMA20 reads as a
       bullish signal the engine never gave. */
    const semantic = [THEME.up, THEME.down].map((c) => c.toLowerCase());
    for (const [name, colour] of Object.entries(OVERLAY_COLORS)) {
      expect(semantic, `${name} borrows a direction colour`)
        .not.toContain(colour.toLowerCase());
    }
  });

  it("leaves no hard-coded hex in the chart components", () => {
    /* The rule that keeps the mirror singular. A literal here is how a
       fourth palette starts. */
    const here = dirname(fileURLToPath(import.meta.url));
    for (const file of ["PriceChart.jsx", "OIProfile.jsx", "EngineRisk.jsx"]) {
      const src = readFileSync(join(here, file), "utf8");
      const hexes = src.match(/#[0-9a-fA-F]{6}\b/g) || [];
      expect(hexes, `${file} hard-codes ${hexes.join(", ")}`).toEqual([]);
    }
  });
});
