import { describe, expect, it } from "vitest";
import { sameOIPicture, spotBand } from "./render-keys.js";

const rows = (over = {}) => [
  { strike: 24_150, call_oi: 100, put_oi: 900, call_ltp: 12, ...over },
  { strike: 24_200, call_oi: 500, put_oi: 500, call_ltp: 30 },
];
const props = (over = {}) => ({
  strikes: rows(), spot: 24_201.05, span: 14,
  summary: { max_pain: 24_200, max_pain_distance_pct: 0.004 }, ...over,
});

describe("spotBand", () => {
  it("groups spots between two 25-point lines", () => {
    expect(spotBand(24_200.05)).toBe(spotBand(24_224.95));
  });

  it("puts a spot exactly on a strike with the spots just below it", () => {
    // OIProfile marks a strike above spot with `strike >= spot`, so 24,200.00
    // belongs with 24,199.95, not with 24,200.05.
    expect(spotBand(24_200)).toBe(spotBand(24_199.95));
    expect(spotBand(24_200)).not.toBe(spotBand(24_200.05));
  });

  it("separates spots on either side of a midpoint", () => {
    expect(spotBand(24_224.95)).not.toBe(spotBand(24_225.05));
  });

  it("has no band for a missing or impossible spot", () => {
    expect(spotBand(null)).toBeNull();
    expect(spotBand(undefined)).toBeNull();
    expect(spotBand(0)).toBeNull();
    expect(spotBand("junk")).toBeNull();
  });
});

describe("sameOIPicture", () => {
  it("ignores a tick that stays in the band", () => {
    expect(sameOIPicture(props(), props({ spot: 24_203.40 }))).toBe(true);
  });

  it("redraws when the spot crosses into another band", () => {
    expect(sameOIPicture(props(), props({ spot: 24_196.00 }))).toBe(false);
  });

  it("ignores a pushed chain whose premiums moved but whose OI did not", () => {
    // New array, new objects, new LTPs — the same bars.
    expect(sameOIPicture(props(), props({ strikes: rows({ call_ltp: 99 }) })))
      .toBe(true);
  });

  it("redraws when open interest changes", () => {
    expect(sameOIPicture(props(), props({ strikes: rows({ call_oi: 101 }) })))
      .toBe(false);
    expect(sameOIPicture(props(), props({ strikes: rows({ put_oi: 901 }) })))
      .toBe(false);
  });

  it("redraws when the strike set changes", () => {
    expect(sameOIPicture(props(), props({ strikes: rows().slice(0, 1) }))).toBe(false);
    expect(sameOIPicture(props(), props({ strikes: rows({ strike: 24_100 }) })))
      .toBe(false);
  });

  it("redraws when max pain moves", () => {
    expect(sameOIPicture(props(), props({
      summary: { max_pain: 24_250, max_pain_distance_pct: 0.004 } }))).toBe(false);
  });

  it("redraws when the printed max-pain distance changes, and only then", () => {
    const at = (pct) => props({ summary: { max_pain: 24_200, max_pain_distance_pct: pct } });
    expect(sameOIPicture(at(0.004), at(0.0041))).toBe(true);    // both "0.00"
    expect(sameOIPicture(at(0.004), at(0.012))).toBe(false);    // "0.00" vs "0.01"
  });

  it("redraws when the chain appears or disappears", () => {
    expect(sameOIPicture(props({ strikes: undefined }), props())).toBe(false);
    expect(sameOIPicture(props(), props({ strikes: null }))).toBe(false);
  });

  it("redraws when the span changes", () => {
    expect(sameOIPicture(props(), props({ span: 20 }))).toBe(false);
  });
});
