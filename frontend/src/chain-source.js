/* Where the option chain on screen came from, and how far behind it is.

   Two different things answer `/market/option-chain` and they are not
   interchangeable. The polled one is a single NSE snapshot, fetched at one
   moment and served from a cache for a while afterwards. The streamed one
   is a composite assembled from websocket ticks, where every strike carries
   its own age and there is no single moment at all.

   A dashboard that renders them identically is telling the reader that a
   sixty-second-old snapshot and a four-hundred-millisecond stream are the
   same data. They are not, and which one is on screen changes what the
   numbers are worth. So provenance is derived here, once, from fields the
   payload actually carries — never guessed from how the numbers look.

   Lifted out of the component so it can be tested without a DOM.
*/

import { istTime } from "./format.js";

/* Mirrors CHAIN_TTL_SECONDS in backend/app/api/market.py. A polled chain
   older than its own cache entry means the request that should have
   refreshed it either did not happen or did not return. The line is not
   invented here — it is where the backend already put it. */
export const POLL_FRESH_SECONDS = 120;

/* The cache TTL plus two whole browser refresh cycles. Past this the
   dashboard has asked twice since the answer should have changed and been
   handed the same thing both times, which is a stalled loop rather than a
   slow one. Derived from CHAIN_TTL_SECONDS + 2 × the poll interval below,
   so it moves if either of those does. */
export const POLL_STALE_SECONDS = POLL_FRESH_SECONDS + 2 * 60;

/* The streamed chain's headline age is its *oldest* print, and a far strike
   legitimately goes quiet for long stretches — the backend tolerates
   ANGEL_OPTIONS_MAX_AGE_SECONDS (900) before dropping a contract from the
   chain entirely. Colouring against that tolerance, rather than against the
   price feed's much tighter thresholds, stops one quiet wing painting a
   perfectly healthy socket red. */
export const STREAM_TOLERATED_SECONDS = 900;

/* Chosen, not derived. Nothing has yet measured how long an ordinary NIFTY
   option strike goes between prints, so this is a guess at "worth
   noticing", in the same way ANGEL_STALE_SECONDS was a guess before
   `tick_gap_ms` started measuring it. Treat an amber badge here as a
   prompt to go and measure, not as a fault. */
export const STREAM_FRESH_SECONDS = 60;

/** Age in seconds of an absolute instant, corrected for this browser's
    clock offset from the server. Null when there is nothing to measure. */
export function ageOf(iso, nowMs = Date.now(), skewMs = 0) {
  if (!iso) return null;
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return null;
  return Math.max(0, (nowMs - skewMs - ms) / 1000);
}

/** A short age. Deliberately keeps one decimal under ten seconds: the whole
    argument for the streamed chain is that it lands in fractions of a
    second, and a formatter that rounds that to "just now" throws away the
    only number that makes the case. */
export function ageText(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "—";
  const s = Math.max(0, Number(seconds));
  if (s < 10) return `${s.toFixed(1)}s`;
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

/** Provenance and freshness of a chain payload.

    Returns `{ kind, label, detail, tone, ageSeconds, streamed }` where
    `kind` is one of:

      none    nothing loaded
      stream  assembled from the Angel websocket
      poll    fetched from NSE while the market is open
      last    the final chain of a finished session, served from cache

    `tone` maps onto the desk's three colours and is `flat` — never an
    alarm — for `last`. A closed market has nothing newer to offer, so an
    age counter running away from the close would be an alarm that cannot
    clear, which is the same mistake the price-age pill already had. */
export function chainSource(chain, nowMs = Date.now(), skewMs = 0) {
  if (!chain) {
    return { kind: "none", label: "No chain", detail: "nothing loaded",
             tone: "flat", ageSeconds: null, streamed: false };
  }

  if (chain.transport === "stream") {
    const age = Number.isFinite(chain.oldest_age_seconds)
      ? Number(chain.oldest_age_seconds) : null;
    return {
      kind: "stream", label: "Stream", streamed: true, ageSeconds: age,
      detail: age === null ? "no prints yet" : `oldest print ${ageText(age)}`,
      tone: age === null ? "flat"
        : age <= STREAM_FRESH_SECONDS ? "go"
        : age <= STREAM_TOLERATED_SECONDS ? "wait" : "stop",
    };
  }

  const age = ageOf(chain.fetched_at, nowMs, skewMs);

  if (chain.live === false) {
    return {
      kind: "last", label: "Last chain", streamed: false, ageSeconds: age,
      detail: chain.fetched_at ? `close ${istTime(chain.fetched_at)}` : "session finished",
      tone: "flat",
    };
  }

  return {
    kind: "poll", label: "Poll", streamed: false, ageSeconds: age,
    detail: age === null ? "age unknown" : `fetched ${ageText(age)} ago`,
    tone: age === null ? "flat"
      : age <= POLL_FRESH_SECONDS ? "go"
      : age <= POLL_STALE_SECONDS ? "wait" : "stop",
  };
}
