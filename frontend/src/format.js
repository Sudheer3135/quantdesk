/* Formatting shared by the terminal panels.

   Lifted out of App.jsx so a panel can be rendered — and tested — without
   dragging in the socket, the polling loop and the whole dashboard with it.

   One rule runs through all of it: a value that is not there formats as an
   em dash or the word "Unavailable", never as a zero. On a trading screen a
   zero is a reading, and a missing reading rendered as one is worse than a
   blank — it is a lie the eye cannot catch.
*/

/** A number, grouped Indian-style, or an em dash when there is nothing. */
export const num = (v, d = 2) =>
  v === null || v === undefined || Number.isNaN(Number(v))
    ? "—"
    : Number(v).toLocaleString("en-IN", {
        minimumFractionDigits: d, maximumFractionDigits: d,
      });

/** A signed number, for anything that can move either way. */
export const signed = (v, d = 2) => {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  const n = Number(v);
  return `${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n).toLocaleString("en-IN", {
    minimumFractionDigits: d, maximumFractionDigits: d,
  })}`;
};

/** A ratio as a whole-number percentage. `fraction` is 0..1, not 0..100. */
export const pct = (fraction, d = 0) =>
  fraction === null || fraction === undefined || Number.isNaN(Number(fraction))
    ? "—"
    : `${(Number(fraction) * 100).toFixed(d)}%`;

/** The word this codebase uses for an input that is not there at all. */
export const UNAVAILABLE = "Unavailable";

/** Direction class for a signed value: drives the green/red/neutral colour. */
export const dirOf = (v) =>
  v === null || v === undefined || Number.isNaN(Number(v))
    ? "flat" : Number(v) > 0 ? "up" : Number(v) < 0 ? "down" : "flat";

const TZ = "Asia/Kolkata";

/** Wall-clock time of an absolute instant, in IST. */
export const istTime = (iso, withSeconds = false) => {
  if (!iso) return "—";
  const ms = typeof iso === "number" ? iso : Date.parse(iso);
  if (!Number.isFinite(ms)) return "—";
  return new Date(ms).toLocaleTimeString("en-IN", {
    timeZone: TZ, hour: "2-digit", minute: "2-digit",
    ...(withSeconds ? { second: "2-digit" } : {}),
    hour12: false,
  });
};

/** Date and time, for a journal row that may not be from today. */
export const istStamp = (iso) => {
  if (!iso) return "—";
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return "—";
  return new Date(ms).toLocaleString("en-IN", {
    timeZone: TZ, day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
};

/* Labels. Kept as maps rather than inline so the dashboard and its tests
   agree on the exact wording, and so a label the backend adds later shows
   its raw value instead of blank. */

export const REGIME_LABEL = {
  TREND_UP: "Trend up",
  TREND_DOWN: "Trend down",
  RANGE: "Range",
  VOLATILE_CHOP: "Volatile chop",
  SQUEEZE: "Squeeze",
};

export const BIAS_LABEL = {
  BULLISH: "Bullish", BEARISH: "Bearish", NEUTRAL: "Neutral",
};

export const ENTRY_LABEL = {
  ENTER_NOW: "Enter now",
  WAIT_PULLBACK: "Wait for pullback",
  WAIT_BREAKOUT: "Wait for breakout",
  NO_ENTRY: "No entry",
};

/* Which of the three decision colours an entry state earns.

   The desk's whole job is to make "act now" look different from "wait" and
   from "stand down" at a glance, across a room, without reading a word. */
export const ENTRY_TONE = {
  ENTER_NOW: "go",
  WAIT_PULLBACK: "wait",
  WAIT_BREAKOUT: "wait",
  NO_ENTRY: "stop",
};

export const RISK_VERDICT = {
  approved: "APPROVED",
  blocked: "BLOCKED",
  unevaluated: "NOT EVALUATED",
  "not-applicable": "NO TRADE",
  missing: "NOT REPORTED",
};

export const RISK_TONE = {
  approved: "go", blocked: "stop", unevaluated: "wait",
  "not-applicable": "flat", missing: "stop",
};

/** "1 lots" read as a typo every single time. */
export const plural = (quantity, lots) =>
  `${quantity} (${lots} ${lots === 1 ? "lot" : "lots"})`;
