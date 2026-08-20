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
import time
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pandas as pd

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

MIN_SECONDS_BETWEEN_CALLS = 1.5
COOKIE_MAX_AGE_SECONDS = 240

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


class NSEClient:
    """One long-lived session. Create it once and reuse it."""

    def __init__(self, timeout: float = 12.0):
        self.client = httpx.Client(headers=HEADERS, timeout=timeout, follow_redirects=True)
        self._cookie_time: float = 0.0
        self._last_call: float = 0.0
        self._chain_expiry: str | None = None
        self._chain_expiry_time: float = 0.0

    # ---- session handling ----------------------------------------------
    def _warm_up(self, force: bool = False) -> None:
        if not force and time.time() - self._cookie_time < COOKIE_MAX_AGE_SECONDS:
            return
        for path in ("/", "/option-chain"):
            try:
                self.client.get(f"{BASE}{path}")
            except httpx.HTTPError as exc:
                log.warning("NSE warm-up on %s failed: %s", path, exc)
        self._cookie_time = time.time()

    def _throttle(self) -> None:
        gap = time.time() - self._last_call
        if gap < MIN_SECONDS_BETWEEN_CALLS:
            time.sleep(MIN_SECONDS_BETWEEN_CALLS - gap)
        self._last_call = time.time()

    def get_json(self, path: str, attempts: int = 3) -> Any:
        """GET a JSON endpoint, refreshing the cookie if NSE rejects us."""
        last_error: Exception | None = None
        for attempt in range(attempts):
            self._warm_up(force=attempt > 0)
            self._throttle()
            try:
                response = self.client.get(f"{BASE}{path}")
                if response.status_code == 200 and response.text.strip():
                    return response.json()
                last_error = RuntimeError(
                    f"NSE returned {response.status_code} for {path}"
                )
            except Exception as exc:
                last_error = exc
            time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"NSE request failed after {attempts} attempts: {last_error}")

    def close(self) -> None:
        self.client.close()

    # ---- endpoints ------------------------------------------------------
    def contract_info(self, symbol: str = "NIFTY") -> dict:
        return self.get_json(f"/api/option-chain-contract-info?symbol={symbol.upper()}", attempts=3)

    def _chain_url(self, symbol: str, expiry: str | None = None) -> str:
        url = f"/api/option-chain-v3?type=Indices&symbol={symbol.upper()}"
        if expiry:
            url += f"&expiry={expiry}"
        return url

    def _select_chain_expiry(self, symbol: str, expiry: str | None = None) -> str | None:
        if expiry:
            return expiry

        if self._chain_expiry and time.time() - self._chain_expiry_time < 6 * 3600:
            return self._chain_expiry

        contract = self.contract_info(symbol)
        dates = contract.get("expiryDates") or []
        chosen = dates[0] if dates else None
        self._chain_expiry = chosen
        self._chain_expiry_time = time.time()
        return chosen

    def raw_option_chain(self, symbol: str = "NIFTY", expiry: str | None = None) -> dict:
        """Fetch the chain using the same flow as NSE's own page.

        NSE's option-chain page first loads contract metadata, then requests
        `/api/option-chain-v3` with the selected expiry. Reusing that flow is
        more stable than probing bare candidate URLs.
        """
        sym = symbol.upper()

        contract = self.contract_info(sym)
        expiry_dates = [expiry] if expiry else []
        for candidate in contract.get("expiryDates") or []:
            if candidate not in expiry_dates:
                expiry_dates.append(candidate)

        errors: list[str] = []
        for candidate_expiry in expiry_dates:
            path = self._chain_url(sym, candidate_expiry)
            try:
                payload = self.get_json(path, attempts=2)
                if isinstance(payload, dict) and payload.get("records", {}).get("data"):
                    self._chain_expiry = candidate_expiry
                    self._chain_expiry_time = time.time()
                    log.info("NSE option chain endpoint resolved to %s", path)
                    return payload
                errors.append(f"{path}: 200 but no strike rows")
            except Exception as exc:
                errors.append(f"{path}: {exc}")

        raise RuntimeError(
            "No NSE option chain endpoint responded. Tried:\n  "
            + "\n  ".join(errors)
            + "\nRun scripts/probe_nse.py, or read the real request off the "
              "Network tab at https://www.nseindia.com/option-chain."
        )

    def all_indices(self) -> dict:
        return self.get_json("/api/allIndices")


# --------------------------------------------------------------------------
# parsing — kept separate from the network so it can be tested with fixtures
# --------------------------------------------------------------------------

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
            "call_oi": float((ce or {}).get("openInterest", 0)),
            "put_oi": float((pe or {}).get("openInterest", 0)),
            "call_oi_change": float((ce or {}).get("changeinOpenInterest", 0)),
            "put_oi_change": float((pe or {}).get("changeinOpenInterest", 0)),
            "call_volume": float((ce or {}).get("totalTradedVolume", 0)),
            "put_volume": float((pe or {}).get("totalTradedVolume", 0)),
            "call_iv": float((ce or {}).get("impliedVolatility", 0)),
            "put_iv": float((pe or {}).get("impliedVolatility", 0)),
            "call_ltp": float((ce or {}).get("lastPrice", 0)),
            "put_ltp": float((pe or {}).get("lastPrice", 0)),
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
