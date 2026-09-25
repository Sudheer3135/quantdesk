"""NSE public data client.

NSE's website serves JSON to its own front-end. Those endpoints are open,
but they are not a documented API and they defend themselves:

  - A bare request with no cookie gets a 401 or an empty body. You have to
    load a normal page first so NSE hands you a session cookie, then reuse
    it. That is what `_warm_up` does.
  - Browser-like headers are required. A plain `python-requests` user agent
    gets refused.
  - Hammer it and you get blocked for a while. Every call here is rate
    limited and cached.

Because this is undocumented, it can break without warning. Treat it as a
convenience, not a foundation. If it stops working, the fix is usually a
fresh cookie, not a code change.

Use it for your own analysis only. Do not redistribute the data.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx
import pandas as pd

from .. import net

log = logging.getLogger(__name__)

BASE = "https://www.nseindia.com"

# NSE stamps its payloads in IST without an offset.
IST = timezone(timedelta(hours=5, minutes=30))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE}/option-chain",
    "Connection": "keep-alive",
}

# The index derivatives NSE publishes a chain for. Same reasoning as the
# Yahoo allowlist: `symbol` arrives from a query parameter and is
# interpolated into an outbound query string, so an unrecognised value must
# be refused rather than forwarded.
NSE_INDEX_SYMBOLS = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"})

MIN_SECONDS_BETWEEN_CALLS = 1.5
COOKIE_MAX_AGE_SECONDS = 240

# One HTTP round trip. Shorter than the 12s it replaces, because the number
# that matters is now the caller's budget and this is only the point at
# which a single hung socket stops being worth waiting on. NSE answers a
# healthy chain request in about 1.6s.
REQUEST_TIMEOUT_SECONDS = 8.0

# What a standalone call gets when nobody upstream set a budget — an API
# request handler, a diagnostic script. Scheduled work always arrives with
# its own, sized from its interval; see `net.budget_for`.
DEFAULT_BUDGET_SECONDS = 30.0

# Backoff between attempts within one `get_json`.
RETRY_BACKOFF_SECONDS = 1.5

# How many published expiries a chain fetch will try before giving up.
#
# NSE lists every weekly and monthly expiry it carries, a dozen or more, and
# the loop below used to try all of them. A healthy fetch succeeds on the
# first — the list arrives nearest-first and that is the one being collected
# — so trying the rest only ever happens when something is already wrong,
# and it turns one failing call into twelve. Two spares is enough to survive
# an expiry rolling over mid-session, which is the case the loop was written
# for.
MAX_CHAIN_EXPIRIES = 3

# NSE moves this path without notice. Confirmed on 05-Aug-2026: the old
# /api/option-chain-indices returns 404 while /api/allIndices still works,
# so the session is fine and only the path changed.
#
# Rather than hardcode one path and break again, the client tries each of
# these in order and remembers whichever answers. Add new ones to the front
# as you find them; scripts/probe_nse.py reports which are alive.
CANDIDATE_CHAIN_PATHS = [
    "/api/option-chain-v3?type=Indices&symbol={symbol}",
    "/api/option-chain-indices?symbol={symbol}",
    "/api/option-chain-equities?symbol={symbol}",
    "/api/option-chain?symbol={symbol}",
    "/api/live-analysis-oi-spurts-underlyings?symbol={symbol}",
]


def resolve_nse_symbol(symbol: str) -> str:
    """Validate an index symbol and return it URL-encoded."""
    from .base import UnknownSymbol

    sym = symbol.strip().upper()
    if sym not in NSE_INDEX_SYMBOLS:
        raise UnknownSymbol(
            f"{symbol!r} is not an index NSE publishes a chain for. "
            f"Supported: {', '.join(sorted(NSE_INDEX_SYMBOLS))}."
        )
    return quote(sym, safe="")


class NSEClient:
    """One long-lived session. Create it once and reuse it."""

    def __init__(self, timeout: float = REQUEST_TIMEOUT_SECONDS):
        # A per-request default. Every call site below overrides it with a
        # slice of the caller's remaining budget, so this only covers a path
        # that forgot to — a floor, not the policy.
        self.client = httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True)
        self._cookie_time: float = 0.0
        self._last_call: float = 0.0
        # One instance is shared by the agent, the price ticker, the option
        # collector and every API request thread, so the rate limit below is
        # enforced across all of them or not at all. See `_throttle`.
        self._lock = threading.Lock()
        self._chain_expiry: str | None = None
        self._chain_expiry_time: float = 0.0

    # ---- session handling ----------------------------------------------
    def _warm_up(self, force: bool = False,
                 deadline: net.Deadline | None = None) -> None:
        """Refresh the session cookie, once, however many threads ask.

        Held under the same lock as the throttle so a cold start does not
        send every waiting thread to fetch its own cookie. The freshness
        check is repeated inside the lock because the thread that was
        blocked may find the work already done.

        Two page loads, and both of them spend from the caller's budget.
        This is the step that made a slow network catastrophic rather than
        merely slow: `get_json` warms up before *every* attempt, so an
        unbounded warm-up multiplied by attempts, by candidate expiries.

        A warm-up cut short by the budget deliberately does not stamp
        `_cookie_time`. Recording a half-finished handshake as fresh would
        leave the client believing it holds a cookie it never received, and
        the next call would fail on a 401 it could have avoided.
        """
        with self._lock:
            if not force and time.monotonic() - self._cookie_time < COOKIE_MAX_AGE_SECONDS:
                return
            for path in ("/", "/option-chain"):
                try:
                    timeout = (deadline.slice(REQUEST_TIMEOUT_SECONDS)
                               if deadline is not None else REQUEST_TIMEOUT_SECONDS)
                except net.BudgetExhausted as exc:
                    log.warning("NSE warm-up abandoned before %s: %s", path, exc)
                    return
                try:
                    self.client.get(f"{BASE}{path}", timeout=timeout)
                except httpx.HTTPError as exc:
                    log.warning("NSE warm-up on %s failed: %s", path, exc)
            self._cookie_time = time.monotonic()

    def _throttle(self, deadline: net.Deadline | None = None) -> None:
        """Space outbound calls by at least MIN_SECONDS_BETWEEN_CALLS.

        Audit finding M-2: this used to read `_last_call`, sleep, then write
        it, with no lock. Concurrent callers all measured the same gap, all
        slept the same amount and all fired together — observed in
        production as bursts of five requests inside 68ms, which is exactly
        the pattern this module's own docstring warns gets the IP blocked.

        The whole read-sleep-write is one critical section, so the sleep of
        a waiting thread starts from the *updated* deadline rather than the
        stale one. Serialising here is the point: the limit is per-source,
        not per-thread.

        Uses a monotonic clock so an NTP correction cannot make the gap look
        negative and release a burst.

        With a `deadline`, a caller that cannot afford the full spacing is
        refused rather than released early. Shortening the gap to fit a
        budget would fire the burst this whole method exists to prevent, and
        an NSE block costs option snapshots that cannot be re-collected —
        strictly worse than the skipped poll that refusing costs.
        """
        with self._lock:
            gap = time.monotonic() - self._last_call
            if gap < MIN_SECONDS_BETWEEN_CALLS:
                # Clamped to the interval itself. A monotonic clock cannot
                # run backwards, but a corrupted or hand-set `_last_call`
                # would otherwise compute a sleep of arbitrary length and
                # wedge the scheduler thread indefinitely. Waiting one full
                # interval is the worst this can now cost.
                wait = min(MIN_SECONDS_BETWEEN_CALLS - gap,
                           MIN_SECONDS_BETWEEN_CALLS)
                if deadline is not None:
                    deadline.wait(wait)
                else:
                    time.sleep(wait)
            self._last_call = time.monotonic()

    def get_json(self, path: str, attempts: int = 3,
                 deadline: net.Deadline | None = None) -> Any:
        """GET a JSON endpoint, refreshing the cookie if NSE rejects us.

        Every wait in here — warm-up, rate limit, request, backoff — comes
        out of one budget, so the total is bounded by it however the
        attempts fall. Without that the retry structure was multiplicative:
        three attempts, each preceded by a two-page warm-up, at twelve
        seconds a socket.

        `BudgetExhausted` propagates rather than being retried. There is
        nothing to retry with.
        """
        deadline = deadline or net.deadline_or(DEFAULT_BUDGET_SECONDS,
                                               label=f"NSE {path}")
        last_error: Exception | None = None
        for attempt in range(attempts):
            self._warm_up(force=attempt > 0, deadline=deadline)
            self._throttle(deadline)
            try:
                response = self.client.get(
                    f"{BASE}{path}", timeout=deadline.slice(REQUEST_TIMEOUT_SECONDS))
                if response.status_code == 200 and response.text.strip():
                    return response.json()
                last_error = RuntimeError(
                    f"NSE returned {response.status_code} for {path}"
                )
            except net.BudgetExhausted:
                raise
            except Exception as exc:
                last_error = exc
            # Only *between* attempts. The old code slept after the last one
            # too, spending four and a half seconds on its way to raising.
            if attempt + 1 < attempts:
                deadline.wait(RETRY_BACKOFF_SECONDS * (attempt + 1))
        raise RuntimeError(f"NSE request failed after {attempts} attempts: {last_error}")

    def close(self) -> None:
        self.client.close()

    # ---- endpoints ------------------------------------------------------
    def contract_info(self, symbol: str = "NIFTY",
                      deadline: net.Deadline | None = None) -> dict:
        return self.get_json(
            f"/api/option-chain-contract-info?symbol={resolve_nse_symbol(symbol)}",
            attempts=3, deadline=deadline)

    def _chain_url(self, symbol: str, expiry: str | None = None) -> str:
        url = f"/api/option-chain-v3?type=Indices&symbol={resolve_nse_symbol(symbol)}"
        if expiry:
            url += f"&expiry={quote(expiry, safe='')}"
        return url

    def _select_chain_expiry(self, symbol: str, expiry: str | None = None,
                             deadline: net.Deadline | None = None) -> str | None:
        if expiry:
            return expiry

        if self._chain_expiry and time.time() - self._chain_expiry_time < 6 * 3600:
            return self._chain_expiry

        contract = self.contract_info(symbol, deadline=deadline)
        dates = contract.get("expiryDates") or []
        chosen = dates[0] if dates else None
        self._chain_expiry = chosen
        self._chain_expiry_time = time.time()
        return chosen

    def raw_option_chain(self, symbol: str = "NIFTY", expiry: str | None = None,
                         deadline: net.Deadline | None = None) -> dict:
        """Fetch the chain using the same flow as NSE's own page.

        NSE's option-chain page first loads contract metadata, then requests
        `/api/option-chain-v3` with the selected expiry. Reusing that flow is
        more stable than probing bare candidate URLs.

        The metadata request and every chain attempt share one budget. They
        did not before, and this method is where the 25-Aug-2026 data loss
        was manufactured: one call, unbounded, fanning out across every
        expiry NSE publishes, on a job scheduled every sixty seconds.
        """
        sym = symbol.strip().upper()
        resolve_nse_symbol(sym)          # refuse early, before any network call
        deadline = deadline or net.deadline_or(DEFAULT_BUDGET_SECONDS,
                                               label="NSE option chain")

        contract = self.contract_info(sym, deadline=deadline)
        expiry_dates = [expiry] if expiry else []
        for candidate in contract.get("expiryDates") or []:
            if candidate not in expiry_dates:
                expiry_dates.append(candidate)

        errors: list[str] = []
        for candidate_expiry in expiry_dates[:MAX_CHAIN_EXPIRIES]:
            path = self._chain_url(sym, candidate_expiry)
            try:
                payload = self.get_json(path, attempts=2, deadline=deadline)
                if isinstance(payload, dict) and payload.get("records", {}).get("data"):
                    self._chain_expiry = candidate_expiry
                    self._chain_expiry_time = time.time()
                    log.info("NSE option chain endpoint resolved to %s", path)
                    return payload
                errors.append(f"{path}: 200 but no strike rows")
            except net.BudgetExhausted as exc:
                # Out of time, not out of endpoints. Trying the next expiry
                # would only add another line to the error list.
                #
                # Re-raised as itself rather than folded into the RuntimeError
                # below, because the two say different things to the caller
                # and lead to different fixes: "NSE refused us" is a source
                # problem, "we ran out of the time our schedule allows" is
                # ours. The collector logs them differently for that reason.
                raise net.BudgetExhausted(
                    f"NSE option chain ran out of budget: {exc}"
                    + (f" (after {'; '.join(errors)})" if errors else "")
                ) from exc
            except Exception as exc:
                errors.append(f"{path}: {exc}")

        raise RuntimeError(
            "No NSE option chain endpoint responded. Tried:\n  "
            + "\n  ".join(errors)
            + "\nRun scripts/probe_nse.py, or read the real request off the "
              "Network tab at https://www.nseindia.com/option-chain."
        )

    def all_indices(self, deadline: net.Deadline | None = None) -> dict:
        return self.get_json("/api/allIndices", deadline=deadline)


# --------------------------------------------------------------------------
# parsing — kept separate from the network so it can be tested with fixtures
# --------------------------------------------------------------------------

def _field(block: dict | None, key: str) -> float:
    """One numeric field from a CE/PE block, NaN when it is not there.

    NaN rather than 0 because the two mean different things downstream: a
    zero is a recorded fact about the contract, a NaN is the absence of one.
    Unparseable values ("-", "", None) are absences too.
    """
    if not block or key not in block:
        return float("nan")
    try:
        return float(block[key])
    except (TypeError, ValueError):
        return float("nan")


def parse_option_chain(payload: dict, expiry: str | None = None) -> tuple[pd.DataFrame, float]:
    """Turn NSE's nested JSON into the platform's flat chain shape.

    NSE gives one record per strike containing optional `CE` and `PE` blocks.
    Returns (chain, spot). Strikes with neither side are dropped.
    """
    records = payload.get("records", {})
    rows_in = records.get("data", [])
    if not rows_in:
        raise ValueError("NSE payload contained no option rows")

    spot = float(records.get("underlyingValue") or 0)
    chosen = expiry or (records.get("expiryDates") or [None])[0]

    # The v3 endpoint takes the expiry as a query parameter, so it has
    # already filtered server-side and its rows may carry no expiryDate at
    # all — or carry it in a different format. Filtering again on a field
    # that is not there silently discards every strike, which looks exactly
    # like "NSE returned nothing" and is very hard to spot.
    #
    # So only filter when the rows actually carry the field, and never let
    # filtering empty a non-empty payload.
    rows_have_expiry = any("expiryDate" in r for r in rows_in)
    if chosen and rows_have_expiry:
        matching = [r for r in rows_in if r.get("expiryDate") == chosen]
        if matching:
            rows_in = matching
        elif expiry is not None:
            # The caller named this expiry, so a miss is their mistake, not a
            # format drift. Fail loudly rather than quietly returning a
            # different expiry than the one that was asked for.
            raise ValueError(
                f"expiry {expiry!r} is not in this payload; it carries "
                f"{sorted({r.get('expiryDate') for r in rows_in})[:5]}"
            )
        else:
            log.warning(
                "no rows matched expiry %r; the payload carries %r. Keeping "
                "all %s rows — the endpoint most likely filtered already.",
                chosen,
                sorted({r.get("expiryDate") for r in rows_in})[:3],
                len(rows_in),
            )

    rows: list[dict] = []
    for record in rows_in:
        ce, pe = record.get("CE"), record.get("PE")
        if not ce and not pe:
            continue
        rows.append({
            "strike": float(record["strikePrice"]),
            # Absent is NaN, never 0 (OC-5). A strike NSE lists without a CE
            # block has no call OI to report; writing 0 there made a
            # one-sided chain read as a put/call ratio of zero. A 0 that NSE
            # actually sent is kept as the genuine zero it is.
            "call_oi": _field(ce, "openInterest"),
            "put_oi": _field(pe, "openInterest"),
            "call_oi_change": _field(ce, "changeinOpenInterest"),
            "put_oi_change": _field(pe, "changeinOpenInterest"),
            "call_volume": _field(ce, "totalTradedVolume"),
            "put_volume": _field(pe, "totalTradedVolume"),
            "call_iv": _field(ce, "impliedVolatility"),
            "put_iv": _field(pe, "impliedVolatility"),
            "call_ltp": _field(ce, "lastPrice"),
            "put_ltp": _field(pe, "lastPrice"),
            # The touch and its depth, where the payload carries them. The
            # archive held no bid/ask at all because they were never read
            # out of a payload that had them.
            "call_bid": _field(ce, "bidprice"),
            "call_ask": _field(ce, "askPrice"),
            "call_bid_qty": _field(ce, "bidQty"),
            "call_ask_qty": _field(ce, "askQty"),
            "put_bid": _field(pe, "bidprice"),
            "put_ask": _field(pe, "askPrice"),
            "put_bid_qty": _field(pe, "bidQty"),
            "put_ask_qty": _field(pe, "askQty"),
        })

    if not rows:
        raise ValueError(
            f"parsed 0 usable strikes from {len(rows_in)} rows "
            f"(expiry {chosen!r}). Every row lacked both CE and PE blocks."
        )

    chain = pd.DataFrame(rows).sort_values("strike").reset_index(drop=True)

    # Which expiry these strikes actually describe. A caller passing None
    # asks for the nearest one and until now had no way to learn what it
    # got. That is fine for a live reading and unacceptable for an archive:
    # a snapshot filed under a guessed expiry silently merges two different
    # contracts into one premium series, and the result looks plausible.
    chain.attrs["expiry"] = chosen

    # When the exchange last printed this chain, as NSE itself reports it.
    #
    # On a holiday NSE keeps serving the previous session's chain, so the
    # payload looks entirely normal — same strikes, same prices, HTTP 200 —
    # and the only thing distinguishing it from live data is this stamp.
    # Carrying it through means the archive can refuse a replay on evidence
    # rather than on a holiday list that might be wrong.
    chain.attrs["source_time"] = parse_nse_timestamp(records.get("timestamp"))

    if not spot:                       # fall back to the ATM crossover
        diff = (chain["call_ltp"] - chain["put_ltp"]).abs()
        spot = float(chain.loc[diff.idxmin(), "strike"])

    return chain, spot


def parse_nse_timestamp(raw: str | None) -> str | None:
    """Turn NSE's `"20-Aug-2026 10:34"` into an absolute UTC instant.

    NSE stamps its payloads in IST with minute resolution and no offset, so
    the string alone is ambiguous the moment it crosses a timezone. Parsing
    it here — once, next to the payload it came from — means everything
    downstream compares absolute instants instead of re-deriving a timezone
    from a display string.

    Minute resolution is the honest limit: a price stamped 10:34 was printed
    somewhere in that minute, so an age derived from it can be up to 60s
    optimistic. That is a reason to prefer a source with a finer timestamp,
    not a reason to invent precision here.
    """
    if not raw:
        return None
    try:
        naive = datetime.strptime(raw.strip(), "%d-%b-%Y %H:%M")
    except (ValueError, TypeError):
        log.debug("could not parse NSE timestamp %r", raw)
        return None
    return naive.replace(tzinfo=IST).astimezone(UTC).isoformat()


def parse_index_value(payload: dict, name: str) -> float | None:
    """Pull one index's last price out of /api/allIndices."""
    for row in payload.get("data", []):
        if row.get("index", "").strip().upper() == name.upper():
            value = row.get("last")
            return float(value) if value is not None else None
    return None


def expiry_dates(payload: dict) -> list[str]:
    return list(payload.get("records", {}).get("expiryDates", []))
