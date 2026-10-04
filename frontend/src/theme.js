/* The palette, for the renderers that cannot read CSS.

   `terminal.css` is the source of truth for the desk. This file exists
   because two of the charts draw through libraries that never see a
   stylesheet — lightweight-charts paints to a canvas, and Recharts wants
   concrete colour strings for its cells — so they need the same values
   as literals.

   One mirror, not three. Before this, `PriceChart` and `OIProfile` each
   carried their own hard-coded set and the CSS carried a third, so the
   desk was running three palettes that had already drifted apart: the OI
   bars were still painting #d9614c against panels that had moved to
   #E5484D. Nobody notices that as a bug; it just makes the dashboard
   look slightly wrong in a way that is hard to name.

   **If you change a colour here, change it in `terminal.css` too.** The
   pairing is asserted in `theme.test.jsx`, which reads the stylesheet
   and fails if the two drift. */
export const THEME = {
  /* ground */
  bg:      "#0B0E11",
  panel:   "#151A22",
  raise:   "#1B212B",
  sunk:    "#080A0D",
  grid:    "#232A34",
  gridSoft:"#1A202A",

  /* text */
  text:    "#E4E9F0",
  dim:     "#8A94A3",
  mute:    "#929DAD",

  /* meaning — identical to the accents in terminal.css */
  up:      "#3FB950",
  down:    "#E5484D",
  wait:    "#D29922",
  info:    "#388BFD",
  violet:  "#8957E5",
};

/* The moving averages, kept apart from the semantic accents on purpose.

   An EMA is not bullish or bearish — it is a reference line — so none of
   them may borrow the green or the red, which on this desk mean
   direction and nothing else. They are separated by hue rather than by
   brightness so they stay distinguishable where three of them converge. */
export const OVERLAY_COLORS = {
  vwap:   "#C99A2E",
  ema20:  "#9BA3B4",
  ema50:  "#388BFD",
  ema200: "#8957E5",
};
