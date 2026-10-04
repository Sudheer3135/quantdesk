/* Shaping archive rows into what a TradingView chart wants, and deciding
   when to go and get more of them.

   Kept apart from the chart component on purpose. lightweight-charts draws
   to a canvas, which jsdom does not implement, so anything living inside
   the component can only be tested through a mock. Everything here is a
   pure function over plain data and is tested directly. */

/* lightweight-charts keys every point by UNIX seconds. The archive speaks
   ISO 8601 with an offset. */
export function toEpochSeconds(iso) {
  if (iso === null || iso === undefined) return null;
  const ms = new Date(iso).getTime();
  return Number.isNaN(ms) ? null : Math.floor(ms / 1000);
}

/* A bar is only drawable if it has all four prices and a time. A partial
   bar is not a small problem: lightweight-charts throws on a null in an
   OHLC field and takes the whole panel down with it. */
function drawable(row) {
  return row
    && Number.isFinite(row.open) && Number.isFinite(row.high)
    && Number.isFinite(row.low) && Number.isFinite(row.close)
    && toEpochSeconds(row.timestamp) !== null;
}

/* The series must be strictly ascending and free of duplicate timestamps —
   lightweight-charts asserts on both. Pages fetched while panning can
   overlap at the seam if the cursor is ever inclusive, so this is the one
   place that guarantee is enforced rather than assumed. */
export function sortDedupe(points) {
  const byTime = new Map();
  for (const p of points) {
    if (p && p.time !== null && p.time !== undefined) byTime.set(p.time, p);
  }
  return [...byTime.values()].sort((a, b) => a.time - b.time);
}

export function toCandleSeries(candles) {
  if (!Array.isArray(candles)) return [];
  return sortDedupe(candles.filter(drawable).map((row) => ({
    time: toEpochSeconds(row.timestamp),
    open: row.open, high: row.high, low: row.low, close: row.close,
  })));
}

export function toLineSeries(candles, key) {
  if (!Array.isArray(candles)) return [];
  /* Nulls are dropped rather than zeroed. An EMA200 that has not warmed up
     yet is absent, and plotting it as 0 would draw a line from the floor of
     the chart to the price — which reads as a crash. */
  return sortDedupe(candles
    .filter((row) => row && Number.isFinite(row[key])
                  && toEpochSeconds(row.timestamp) !== null)
    .map((row) => ({ time: toEpochSeconds(row.timestamp), value: row[key] })));
}

/* Close-only, for the LINE mode. */
export function toCloseSeries(candles) {
  return toLineSeries(candles, "close");
}

/* Seconds per bar, by the timeframe names this desk uses. */
export const BAR_SECONDS = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600 };

/* The bar a given instant belongs to. NIFTY's 5-minute bars start on the
   clock — 09:15, 09:20 — so flooring the epoch is the right boundary. */
export function bucketStart(epochSeconds, seconds = 300) {
  return Math.floor(epochSeconds / seconds) * seconds;
}

/* Fold one live tick into the bar it belongs to.

   This is what makes the last candle move. Without it the forming bar only
   redraws when the 60-second poll returns, so the price in the header ticks
   four times a second above a candle that is up to a minute stale — the
   chart looks frozen next to its own price.

   Returns the updated bar, or null when the tick should be ignored. Null
   rather than the unchanged bar so the caller can skip the redraw.

   `last` is the bar currently at the right edge. A tick in the same bucket
   extends it: the close follows the tick, the high and low ratchet, and the
   open never moves — it was set by the first trade of the bar and is not
   the live feed's to revise. A tick in a new bucket opens a bar at the tick
   price, which is exactly what the first print of a bar means. */
export function applyTick(last, tick, seconds = 300) {
  if (!tick) return null;

  /* `Number(null)` is 0, and so is `Number("")` — both finite, so coercing
     first and checking after would let a missing price through as a close
     of zero and put the candle on the floor of the chart. Nothing at zero
     is a real print, so the value is rejected before and after coercion. */
  if (tick.price === null || tick.price === undefined) return null;
  const price = Number(tick.price);
  if (!Number.isFinite(price) || price <= 0) return null;

  /* A closed market still publishes — the last traded price, forever. Left
     ungated it would keep opening empty bars through the night, and by
     morning the chart would show a flat plateau that never traded. */
  if (tick.market_open === false) return null;

  const at = toEpochSeconds(tick.source_time || tick.at);
  if (at === null) return null;

  const slot = bucketStart(at, seconds);

  /* `last` is not guaranteed to sit on the bucket grid itself — a source
     has been caught handing back a "current" bar stamped at the instant of
     its own last refresh rather than at the bucket it belongs to (Yahoo,
     for the still-forming candle). Comparing against *its* bucket rather
     than its raw time is what stops that from reading as a bar from the
     future and silently swallowing every tick until the real bucket rolls
     over — measured live on 15-Sep-2026, a forming bar stamped a few
     minutes past its own bucket start froze the chart until the next
     5-minute boundary. */
  const lastSlot = last ? bucketStart(last.time, seconds) : null;

  /* Out-of-order ticks happen on a reconnect, when the feed replays. */
  if (last && slot < lastSlot) return null;

  if (last && slot === lastSlot) {
    /* `last.time`, not `slot`.

       The comparison above is on buckets so an off-grid bar cannot swallow
       every tick, but the *emitted* time has to stay where the renderer
       already has the bar. Returning `slot` here looked tidier — it
       quietly pulled a stray bar back onto the grid — and it asked
       lightweight-charts to move its last bar backwards, from 07:43:12 to
       07:40:00, which it refuses outright:

           Cannot update oldest data, last time=…, new time=…

       Uncaught, that unmounted the entire dashboard and left a black page
       until the bucket rolled over. Merging in place keeps the bar moving
       without ever going backwards, and the source is where an off-grid
       stamp gets corrected — see the bucket snapping in
       `brokers/freedata.py`. */
    return {
      time: last.time,
      open: last.open,
      high: Math.max(last.high, price),
      low: Math.min(last.low, price),
      close: price,
    };
  }
  return { time: slot, open: price, high: price, low: price, close: price };
}

/* Merge two sets of rows by timestamp, ascending.

   `winner` decides which copy of a shared bar survives, and it is a required
   argument rather than a consequence of the argument order — getting it
   backwards is silent and its symptom is subtle, so it should not be
   possible to do by accident. See mergeOlder and mergeLive below. */
function merge(a, b, winner) {
  const first = Array.isArray(a) ? a : [];
  const second = Array.isArray(b) ? b : [];
  const byTime = new Map();
  /* Map.set means the later write wins, so the loser is laid down first. */
  const [loser, victor] = winner === "a" ? [second, first] : [first, second];
  for (const row of [...loser, ...victor]) {
    const t = toEpochSeconds(row?.timestamp);
    if (t !== null) byTime.set(t, row);
  }
  return [...byTime.entries()]
    .sort((x, y) => x[0] - y[0])
    .map(([, row]) => row);
}

/* A freshly fetched older page, spliced beneath what is already loaded.

   What is loaded wins at the seam: it may carry a live update for a bar the
   archive also holds, and the archive's copy of a forming candle is by
   definition the older one. */
export function mergeOlder(existing, older) {
  return merge(existing, older, "a");
}

/* What the live feed is authoritative for. Everything else on a row —
   vwap, ema20/50/200, atr14, rvol — is derived from a window of history. */
const PRICE_FIELDS = ["timestamp", "open", "high", "low", "close", "volume"];

function priceOnly(row) {
  const out = {};
  for (const f of PRICE_FIELDS) {
    if (row[f] !== undefined) out[f] = row[f];
  }
  return out;
}

/* The live feed's prices, over what is loaded — but only its prices.

   The last bar of a session is still forming: the archive holds whatever it
   looked like when it was written, the live feed holds what it looks like
   now, so the live OHLC must win or the right edge of the chart freezes
   mid-session.

   Its *indicators* must not win, and this is the subtle half. The live
   endpoint serves two days — about 86 bars — and `indicators.ema` seeds
   from the first bar it is given, so an EMA200 computed over 86 bars is the
   mean of those 86 bars wearing the name of a 200-period average. Measured
   against the warmed archive at the same timestamp: 24,109 against 24,149,
   a forty-point disagreement. Letting those values through draws a cliff in
   the overlay exactly where the live window starts.

   So a bar the archive already knows keeps the archive's derived columns
   and takes the live prices. A bar newer than the archive carries prices
   alone: `toLineSeries` drops points with no finite value, so the overlays
   simply stop one bar short of the forming candle rather than lying about
   where they are. */
export function mergeLive(existing, live) {
  const base = Array.isArray(existing) ? existing : [];
  const fresh = Array.isArray(live) ? live : [];

  const byTime = new Map();
  for (const row of base) {
    const t = toEpochSeconds(row?.timestamp);
    if (t !== null) byTime.set(t, row);
  }
  for (const row of fresh) {
    const t = toEpochSeconds(row?.timestamp);
    if (t === null) continue;
    const known = byTime.get(t);
    byTime.set(t, known ? { ...known, ...priceOnly(row) } : priceOnly(row));
  }

  return [...byTime.entries()]
    .sort((x, y) => x[0] - y[0])
    .map(([, row]) => row);
}

/* The archive's recent window, replacing whatever this chart is holding at
   the same timestamps.

   `mergeLive` promises the overlays stop "one bar short of the forming
   candle" — true only while the archive-loaded window keeps pace with the
   clock. It does not: the initial load runs once, on mount, and after that
   nothing ever asks the archive for its recent end again. Every bar formed
   since then arrives solely through the live feed and is stripped to
   prices by `priceOnly`, so a browser tab left open for an hour accumulates
   an hour of indicator-less bars — not one, and the gap only grows. The
   archive itself is current the whole time; nothing was fetching it.

   `recent` is the answer to `GET /candles/history` with no `before`, which
   is fully enriched. It wins outright — full row, not a field-by-field
   patch like `mergeLive` — because there is nothing to protect it from:
   this is exactly the data `mergeLive`'s "known" branch is trying to
   approximate, arriving late instead of never. */
export function mergeRecent(existing, recent) {
  return merge(existing, recent, "b");
}

/* How close to the left edge the user must pan before older bars are
   fetched. In bars, against lightweight-charts' logical range — which
   counts series indices, and goes negative once the view runs off the
   start of the data. */
export const LOAD_MORE_THRESHOLD_BARS = 20;

/* Whether panning has gone far enough left to warrant a request.

   Every guard here is a way the chart could otherwise hammer the API: no
   range yet on first paint, a request already in flight, and an archive
   that has already said it has nothing older. That last one matters most —
   without it, reaching the start of history means asking for what is not
   there once per pan event, forever. */
export function shouldLoadOlder({ range, loading, hasMore, oldest }) {
  if (loading || !hasMore || !oldest) return false;
  if (!range || !Number.isFinite(range.from)) return false;
  return range.from < LOAD_MORE_THRESHOLD_BARS;
}

/* Render a UTC instant in exchange time.

   The desk is IST and the archive is UTC. lightweight-charts formats in UTC
   unless told otherwise, so without this a 09:15 open reads as 03:45 — and
   looks like a data fault rather than a display one. The timestamps
   themselves are left alone: shifting them to fake a local clock would put
   every bar an hour and a half from where it happened. */
const IST = "Asia/Kolkata";

function istParts(epochSeconds) {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: IST, hour12: false,
    year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  }).formatToParts(new Date(epochSeconds * 1000))
    .reduce((acc, p) => { acc[p.type] = p.value; return acc; }, {});
}

/* Which IST trading day an instant belongs to. Sessions are what VWAP
   resets on, so this is what decides where its line breaks. */
export function istDateKey(epochSeconds) {
  const p = istParts(epochSeconds);
  return `${p.year}-${p.month}-${p.day}`;
}

/* Axis labels: the time of day is what a 5-minute chart is read by, and the
   date only needs saying when it changes. */
export function formatTickIST(epochSeconds, isNewDay = false) {
  const p = istParts(epochSeconds);
  return isNewDay ? `${p.day} ${p.month}` : `${p.hour}:${p.minute}`;
}

/* Crosshair label: unambiguous, so a bar read off the chart can be found in
   the journal. */
export function formatStampIST(epochSeconds) {
  const p = istParts(epochSeconds);
  return `${p.day} ${p.month} ${p.year}  ${p.hour}:${p.minute} IST`;
}

/* A series that breaks between sessions rather than drawing across them.

   VWAP is anchored to the session open, so it restarts every morning. Drawn
   as one continuous line, that restart renders as a vertical stroke — 142
   points on 20-Aug — which reads as a price move that never happened.

   lightweight-charts breaks a line at a whitespace point: an entry carrying
   a time and no value. There is no slot *between* two adjacent bars to put
   one in, so the session's first bar becomes the break. The cost is one
   bar of VWAP per morning; the alternative is a chart that draws a
   140-point move out of an accounting reset. */
export function toSessionSeries(candles, key) {
  if (!Array.isArray(candles)) return [];
  let session = null;
  const points = [];
  for (const row of candles) {
    const time = toEpochSeconds(row?.timestamp);
    if (time === null) continue;
    const day = istDateKey(time);
    const opened = session !== null && day !== session;
    session = day;
    if (opened || !Number.isFinite(row[key])) points.push({ time });
    else points.push({ time, value: row[key] });
  }
  return sortDedupe(points);
}

/* The levels a signal wants drawn across the chart. Only finite ones: a
   plan with no target must not draw a line at zero. */
export function priceLines(signal) {
  const plan = signal?.plan || signal || {};
  return [
    { key: "entry", price: plan.entry, color: "#d8dee9", style: "dotted" },
    { key: "stop", price: plan.stop, color: "#e06c75", style: "solid" },
    { key: "target", price: plan.target, color: "#4ec9a0", style: "solid" },
  ].filter((l) => Number.isFinite(l.price));
}
