/* Shared test environment.

   lightweight-charts renders to a canvas and measures it through
   `matchMedia` and a real 2D context. jsdom implements neither, so any test
   that mounts the dashboard brings the whole suite down inside the charting
   library rather than in anything this project wrote.

   The renderer is stubbed here, once, for every suite. The chart's own
   wiring is asserted against a richer mock in PriceChart.test.jsx, which
   declares its own `vi.mock` and so overrides this; the shaping and paging
   arithmetic is pure and tested directly in chart-data.test.jsx. What is
   left uncovered is the drawing itself, which is TradingView's code, not
   ours, and which no jsdom test could exercise anyway. */
import { vi } from "vitest";

/* fancy-canvas, which lightweight-charts uses for device-pixel sizing,
   installs a matchMedia listener at construction. */
if (!window.matchMedia) {
  window.matchMedia = (query) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  });
}

/* Recharts' ResponsiveContainer measures itself with ResizeObserver, which
   jsdom also lacks. No test rendered the open-interest chart until the
   render-cost suite, so nothing had needed it. */
if (!window.ResizeObserver) {
  window.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

vi.mock("lightweight-charts", () => {
  const series = () => ({
    setData: vi.fn(),
    /* The chart folds every live tick into the last bar through this. */
    update: vi.fn(),
    createPriceLine: vi.fn(() => ({})),
    removePriceLine: vi.fn(),
  });
  return {
    createChart: vi.fn(() => ({
      addSeries: vi.fn(series),
      removeSeries: vi.fn(),
      subscribeCrosshairMove: vi.fn(),
      timeScale: vi.fn(() => ({
        subscribeVisibleLogicalRangeChange: vi.fn(),
        unsubscribeVisibleLogicalRangeChange: vi.fn(),
      })),
      remove: vi.fn(),
    })),
    CandlestickSeries: "CandlestickSeries",
    LineSeries: "LineSeries",
    ColorType: { Solid: "solid" },
    CrosshairMode: { Normal: 0 },
    LineStyle: { Solid: 0, Dotted: 1, Dashed: 2 },
  };
});
