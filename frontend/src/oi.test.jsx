import { describe, expect, it } from "vitest";

import { heaviest, oiValue } from "./oi.js";
import { sameOIPicture } from "./render-keys.js";

describe("oiValue", () => {
  it("keeps missing as null and zero as zero", () => {
    expect(oiValue(null)).toBeNull();
    expect(oiValue(undefined)).toBeNull();
    expect(oiValue("")).toBeNull();
    expect(oiValue(Number.NaN)).toBeNull();
    expect(oiValue(0)).toBe(0);
    expect(oiValue("1500")).toBe(1500);
  });
});

describe("heaviest", () => {
  it("returns null when nothing is recorded, and never a missing row", () => {
    expect(heaviest([{ oi: null }, { oi: undefined }], (r) => r.oi)).toBeNull();
    const rows = [{ k: 1, oi: null }, { k: 2, oi: 0 }, { k: 3, oi: null }];
    expect(heaviest(rows, (r) => r.oi).k).toBe(2);
  });
});

describe("render keys", () => {
  it("treat a change from missing to zero as a change", () => {
    const a = { strikes: [{ strike: 1, call_oi: null, put_oi: null }], spot: 24_000 };
    const b = { strikes: [{ strike: 1, call_oi: 0, put_oi: 0 }], spot: 24_000 };
    expect(sameOIPicture(a, b)).toBe(false);
  });
});
