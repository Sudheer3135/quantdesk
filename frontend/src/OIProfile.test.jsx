import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import OIProfile, { profileRows } from "./OIProfile.jsx";

/* Pass 2C, OC-5: open interest the source did not send is unavailable, and
   the panel must say so rather than draw a number. */
const strikes = [
  { strike: 24_150, call_oi: null, put_oi: 900 },
  { strike: 24_200, call_oi: null, put_oi: 500 },
];

describe("OIProfile with unavailable open interest", () => {
  it("says the PCR is unavailable instead of printing nothing or zero", () => {
    const { container } = render(<OIProfile strikes={strikes} spot={24_201}
      summary={{ pcr_oi: null, max_pain: null, bias: "unavailable",
                 oi_status: "unavailable" }} />);
    const pcr = [...container.querySelectorAll(".chain-strip > div")]
      .find((cell) => cell.querySelector("span")?.textContent === "PCR");
    expect(pcr.querySelector("b").textContent).toBe("unavailable");
  });

  it("never renders a missing max pain as strike 0", () => {
    const { container } = render(<OIProfile strikes={strikes} spot={24_201}
      summary={{ pcr_oi: null, max_pain: null, bias: "unavailable" }} />);
    const strip = container.querySelector(".chain-strip");
    expect(strip.textContent).not.toMatch(/Max pain0/);
    expect(strip.textContent).toMatch(/Max pain—/);
  });

  it("still prints a real reading, genuine zero included", () => {
    const { container } = render(<OIProfile strikes={strikes} spot={24_201}
      summary={{ pcr_oi: 0, max_pain: 24_200, bias: "bearish" }} />);
    const strip = container.querySelector(".chain-strip");
    expect(strip.textContent).toMatch(/PCR0\.00/);
    expect(strip.textContent).toMatch(/Max pain24,200/);
  });
});

/* Pass 2C.1: missing OI stayed missing in the strip but became 0 in the
   chart data and the captions — "heaviest call OI … (0.0L)" on a chain that
   sent no call OI at all. */
const caption = (container) => container.querySelector(".chart-caption").textContent;

describe("OIProfile keeps missing open interest missing", () => {
  it("names no wall when every strike's OI is missing", () => {
    const none = [
      { strike: 24_150, call_oi: null, put_oi: null },
      { strike: 24_200, call_oi: undefined, put_oi: null },
    ];
    const { container } = render(<OIProfile strikes={none} spot={24_180}
      summary={{ pcr_oi: null, max_pain: null, bias: "unavailable" }} />);
    const text = caption(container);
    expect(text).toMatch(/Call OI unavailable/);
    expect(text).toMatch(/put OI unavailable/);
    expect(text).not.toMatch(/0\.0L/);
    expect(text).not.toMatch(/Heaviest/i);
  });

  it("never picks a missing strike as a wall when other strikes are recorded", () => {
    const partial = [
      { strike: 24_100, call_oi: null, put_oi: 300_000 },
      { strike: 24_150, call_oi: 120_000, put_oi: null },
      { strike: 24_200, call_oi: null, put_oi: null },
    ];
    const { container } = render(<OIProfile strikes={partial} spot={24_160}
      summary={{ pcr_oi: 2.5, max_pain: 24_150, bias: "bullish" }} />);
    const text = caption(container);
    expect(text).toMatch(/Heaviest call OI at 24,150 \(1\.2L\)/);
    expect(text).toMatch(/heaviest put OI at 24,100 \(3\.0L\)/);
  });

  it("shows a genuine zero as a zero reading, distinct from missing", () => {
    const zero = [
      { strike: 24_150, call_oi: 0, put_oi: 0 },
      { strike: 24_200, call_oi: null, put_oi: null },
    ];
    const { container } = render(<OIProfile strikes={zero} spot={24_180}
      summary={{ pcr_oi: null, max_pain: 24_150, bias: "neutral" }} />);
    const text = caption(container);
    expect(text).toMatch(/Heaviest call OI at 24,150 \(0\.0L\)/);
    expect(text).toMatch(/heaviest put OI at 24,150 \(0\.0L\)/);
    expect(text).not.toMatch(/24,200/);
  });
});

describe("OIProfile chart rows", () => {
  it("give the chart null, not a zero-height bar, for a missing reading", () => {
    const rows = profileRows([
      { strike: 24_150, call_oi: null, put_oi: 0 },
      { strike: 24_200, call_oi: 5_000 },
    ], 24_180);
    const missing = rows.find((r) => r.strike === 24_150);
    const partial = rows.find((r) => r.strike === 24_200);
    expect(missing.call).toBeNull();
    expect(missing.callOI).toBeNull();
    expect(missing.put).toBe(0);          // genuine zero stays a reading
    expect(partial.put).toBeNull();       // absent key is missing too
    expect(partial.call).toBe(-5_000);
  });
});
