"""Free data broker — no account, no subscription, no rupees.

Candles come from Yahoo Finance (`^NSEI` for NIFTY), the option chain and
India VIX come from NSE's public endpoints.

What you get:
  - 5-minute NIFTY candles, roughly 60 days back
  - the live option chain with OI, change in OI, volume, IV and LTP
  - India VIX

What you do not get:
  - years of intraday history. Yahoo caps intraday lookback hard.
  - a tick-level websocket feed. Everything here is polled.
  - order placement. This adapter is read-only by design.

The fix for the history limit is the archiver: every fetch is written to
Postgres, so your own history grows from the day you start. Three months
from now you will have three months of clean 5-minute data that nobody can
take away or start charging for.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from urllib.parse import quote

import pandas as pd
from curl_cffi import requests as curl_requests

from .. import net
from ..data.importer import TIMEFRAME_MINUTES
from .base import Broker, UnknownSymbol
from .nse import NSEClient, parse_index_value, parse_nse_timestamp, parse_option_chain

log = logging.getLogger(__name__)

YAHOO_SYMBOLS = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "SENSEX": "^BSESN",
    "INDIAVIX": "^INDIAVIX",
}

def resolve_yahoo_symbol(symbol: str) -> str:
    """Map a platform symbol to its Yahoo ticker, or refuse it.

    This used to be `YAHOO_SYMBOLS.get(symbol.upper(), symbol)` — a lookup
    with a passthrough default, which reads like an allowlist and is not
    one. Anything unrecognised went straight into the outbound URL, so
    `?symbol=AAPL` fetched Apple and the API became a free proxy to Yahoo
    running on this machine's IP.

    The returned value is percent-encoded even though every entry in the map
    is already URL-safe. The encoding is not what makes this safe — the
    allowlist is — but it means a future entry containing a slash or a
    question mark cannot silently reshape the request.
    """
    from ..symbols import validate
    ticker = YAHOO_SYMBOLS.get(validate(symbol))
    if ticker is None:
        raise UnknownSymbol(
            f"{symbol!r} is a supported symbol but this adapter has no Yahoo "
            f"mapping for it. Carried here: {', '.join(sorted(YAHOO_SYMBOLS))}."
        )
    return quote(ticker, safe="")


# Yahoo's own caps. Asking for more silently returns less, which is worse
# than an error, so we clamp and warn instead.
MAX_DAYS = {"1m": 7, "2m": 59, "5m": 59, "15m": 59, "30m": 59, "60m": 729, "1d": 3650}

# A live quote is worthless late. Fail fast and let the next poll try rather
# than blocking the ticker behind one slow request.
QUOTE_TIMEOUT_SECONDS = 8

# A candle fetch is three hundred rows and is allowed to be slower than a
# quote, but not unboundedly so.
CANDLE_TIMEOUT_SECONDS = 20

# What a Yahoo call gets when nobody upstream set a budget. Scheduled work
# always arrives with one, sized from its own interval — see `net.budget_for`
# and the workers that wrap their ticks in it.
DEFAULT_BUDGET_SECONDS = 30.0

INTERVAL_MAP = {
    "1m": "1m", "3m": "5m", "5m": "5m", "15m": "15m",
    "30m": "30m", "1h": "60m", "60m": "60m", "1d": "1d",
}


class FreeDataBroker(Broker):
    name = "free"

    def __init__(self, nse_client: NSEClient | None = None):
        self.nse = nse_client or NSEClient()

    # ---- candles --------------------------------------------------------
    def candles(self, symbol: str = "NIFTY", interval: str = "5m", days: int = 5) -> pd.DataFrame:
        ticker = resolve_yahoo_symbol(symbol)
        yf_interval = INTERVAL_MAP.get(interval, interval)

        cap = MAX_DAYS.get(yf_interval, 59)
        if days > cap:
            log.warning(
                "Yahoo caps %s history at %s days; asked for %s. "
                "Use the archived candles in Postgres for anything longer.",
                yf_interval, cap, days,
            )
            days = cap

        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            f"?range={days}d&interval={yf_interval}&includePrePost=false&events=div%2Csplits"
        )
        deadline = net.deadline_or(DEFAULT_BUDGET_SECONDS, label="Yahoo candles")
        response = curl_requests.get(
            url, impersonate="chrome120",
            timeout=deadline.slice(CANDLE_TIMEOUT_SECONDS))
        if response.status_code != 200:
            raise RuntimeError(
                f"Yahoo returned {response.status_code} for {ticker} at {yf_interval}")

        payload = response.json()
        result = ((payload or {}).get("chart") or {}).get("result") or []
        if not result:
            raise RuntimeError(f"Yahoo returned no data for {ticker} at {yf_interval}")

        chart = result[0]
        timestamps = chart.get("timestamp") or []
        indicators = (chart.get("indicators") or {}).get("quote") or []
        if not timestamps or not indicators:
            raise RuntimeError(f"Yahoo returned no data for {ticker} at {yf_interval}")

        quote = indicators[0]
        df = pd.DataFrame({
            "timestamp": pd.to_datetime(timestamps, unit="s", utc=True),
            "open": quote.get("open", []),
            "high": quote.get("high", []),
            "low": quote.get("low", []),
            "close": quote.get("close", []),
            "volume": quote.get("volume", []),
        }).dropna()

        # Index volume from Yahoo is often zero. VWAP and relative volume
        # both divide by it, so substitute a constant rather than produce
        # silent NaNs that look like working indicators.
        if df["volume"].sum() == 0:
            log.warning(
                "%s reports zero volume; VWAP becomes a plain "
                "typical-price average.", ticker)
            df["volume"] = 1.0

        df = df.sort_values("timestamp").reset_index(drop=True)

        # Yahoo occasionally appends one row beyond its own 5-minute grid:
        # the still-forming bar, stamped at the instant of its last refresh
        # rather than at the bucket it belongs to, and carrying a single
        # print (open=high=low=close) that discards every tick the
        # *properly* bucketed row already holds for that same window.
        #
        # Measured live on 15-Sep-2026: the row for [07:35, 07:40) UTC
        # arrived correctly aligned and evolving (open 23308.90, high
        # 23315.75, low 23303.45), and a second row for the same window
        # followed a few minutes later stamped 07:39:24 with a single print
        # (23307.40 on all four fields) — the real bucket's range gone,
        # replaced by a doji sitting off the 5-minute grid.
        #
        # An off-grid row breaks a live chart's tick merge: the desk buckets
        # a fresh tick by flooring its own timestamp, so a tick inside
        # [07:35, 07:40) floors to 07:35:00 — earlier than the stray row's
        # 07:39:24. The chart then either rejects the tick as belonging to
        # a bar already in the past (the bar freezes until the next
        # boundary) or, once it stopped rejecting it, asks the renderer to
        # move the last bar backwards, which lightweight-charts refuses
        # with "Cannot update oldest data" — an uncaught throw that
        # unmounted the dashboard and left a black page.
        #
        # So the rule is applied to the whole frame, not just to a detected
        # duplicate pair. On a fixed-width timeframe a bar's timestamp *is*
        # its bucket start by definition, and anything else is the source
        # describing itself badly. Snapping every row and aggregating the
        # collisions covers the duplicate case and, importantly, the case
        # that a narrower fix missed: a lone off-grid forming row with no
        # properly aligned twin, which passed straight through. Where a
        # bucket ends up holding several rows the first row's open and the
        # last row's close survive, which is what those fields mean.
        minutes = TIMEFRAME_MINUTES.get(interval)
        if minutes and not df.empty:
            bucket = df["timestamp"].dt.floor(f"{minutes}min")
            if not bucket.equals(df["timestamp"]):
                df = (df.assign(timestamp=bucket)
                        .groupby("timestamp", as_index=False)
                        .agg(open=("open", "first"), high=("high", "max"),
                             low=("low", "min"), close=("close", "last"),
                             volume=("volume", "sum"))
                        .sort_values("timestamp")
                        .reset_index(drop=True))

        df.attrs["volume_is_synthetic"] = True
        return df

    def _yahoo_quote(self, symbol: str) -> dict:
        """Yahoo's chart metadata — one small request carrying a real clock.

        `range=1d&interval=1m` is asked for because the metadata block is
        what we are after, not the bars: it holds `regularMarketPrice` and
        `regularMarketTime`, the exchange's own epoch stamp for that price.
        The response is about 7 KB, against roughly 300 rows for a candle
        fetch, so this is the cheaper call as well as the fresher one.
        """
        ticker = resolve_yahoo_symbol(symbol)
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
               f"?range=1d&interval=1m")
        deadline = net.deadline_or(DEFAULT_BUDGET_SECONDS, label="Yahoo quote")
        response = curl_requests.get(
            url, impersonate="chrome120",
            timeout=deadline.slice(QUOTE_TIMEOUT_SECONDS))
        if response.status_code != 200:
            raise RuntimeError(f"Yahoo returned {response.status_code} for {ticker}")

        result = (((response.json() or {}).get("chart") or {}).get("result") or [])
        if not result:
            raise RuntimeError(f"Yahoo returned no quote for {ticker}")

        meta = result[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None:
            raise RuntimeError(f"Yahoo quote for {ticker} carried no price")

        epoch = meta.get("regularMarketTime")
        return {
            "last_price": float(price),
            "source": "yahoo",
            "source_time": (datetime.fromtimestamp(epoch, UTC).isoformat()
                            if epoch else None),
        }

    def quote(self, symbol: str = "NIFTY") -> dict:
        """The live spot price, freshest source first.

        The order here was measured, not assumed. Sampling both sources
        side by side during the session on 20-Aug-2026:

          - Yahoo chart metadata changed on 23 of 25 polls over 90s and
            carries an exact `regularMarketTime`. Median age behind the
            market: 1.9s.
          - NSE `/api/allIndices` changed *once* in the same 90 seconds. It
            sits behind a CDN that refreshes roughly once a minute, and its
            timestamp is minute-resolution, so downstream code cannot even
            see how stale it is. The two disagreed by 1.8 points on average
            and by as much as 3.8.

        NSE was the primary source, which is why the dashboard could sit a
        minute behind the market while looking perfectly healthy. It is now
        the fallback, and every branch reports `source_time` — the moment
        the *market* printed this price, not the moment we fetched it. A
        caller that cannot tell those apart cannot detect stale data, which
        is the whole point.

        Returns `last_price`, `source`, and `source_time` (ISO-8601 UTC, or
        None when the source will not say — never a guess).
        """
        try:
            return self._yahoo_quote(symbol)
        except net.BudgetExhausted:
            # No time left is not the same failure as a source being down,
            # and only the second one has a useful fallback. Falling through
            # here would start a second and a third request that the caller's
            # schedule has already run out of room for — which is how one
            # slow poll became a skipped tick became a lost session.
            raise
        except Exception as exc:
            log.debug("Yahoo quote failed, trying NSE: %s", exc)

        try:
            indices = self.nse.all_indices()
            name = "NIFTY 50" if symbol.upper() == "NIFTY" else symbol.upper()
            value = parse_index_value(indices, name)
            if value:
                return {"last_price": value, "source": "nse",
                        "source_time": parse_nse_timestamp(indices.get("timestamp"))}
        except net.BudgetExhausted:
            raise
        except Exception as exc:
            log.debug("NSE quote failed, falling back to candles: %s", exc)

        # Last resort. This is a *bar close*, not a live print — it can be
        # most of a bar width behind. It is labelled distinctly and carries
        # the bar's own timestamp so nothing downstream mistakes it for a
        # live quote, which is exactly what used to happen.
        last = self.candles(symbol, "5m", days=5).iloc[-1]
        return {"last_price": float(last["close"]), "source": "yahoo-candle",
                "source_time": last["timestamp"].to_pydatetime().isoformat()}

    # ---- derivatives ----------------------------------------------------
    def option_chain(self, symbol: str = "NIFTY", expiry: str | None = None) -> pd.DataFrame:
        payload = self.nse.raw_option_chain(symbol, expiry)
        chain, _spot = parse_option_chain(payload, expiry)
        return chain

    def chain_with_spot(self, symbol: str = "NIFTY",
                        expiry: str | None = None) -> tuple[pd.DataFrame, float]:
        """NSE hands back the underlying value in the same payload — use it
        rather than making a second call for the spot price."""
        payload = self.nse.raw_option_chain(symbol, expiry)
        return parse_option_chain(payload, expiry)

    def india_vix(self) -> float | None:
        try:
            return parse_index_value(self.nse.all_indices(), "INDIA VIX")
        except Exception as exc:
            log.warning("could not read India VIX from NSE: %s", exc)
            return None

    # ---- execution ------------------------------------------------------
    def place_order(self, **kwargs) -> dict:
        raise PermissionError(
            "The free broker is read-only. To place orders, open a free API "
            "account (Fyers, Angel One SmartAPI, Upstox or Dhan) and write an "
            "adapter against Broker — see docs/FREE_DATA.md."
        )

    def positions(self) -> list[dict]:
        return []
