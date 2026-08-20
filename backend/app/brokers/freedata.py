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

import pandas as pd
from curl_cffi import requests as curl_requests

from .base import Broker
from .nse import NSEClient, parse_index_value, parse_nse_timestamp, parse_option_chain

log = logging.getLogger(__name__)

YAHOO_SYMBOLS = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "SENSEX": "^BSESN",
    "INDIAVIX": "^INDIAVIX",
}

# Yahoo's own caps. Asking for more silently returns less, which is worse
# than an error, so we clamp and warn instead.
MAX_DAYS = {"1m": 7, "2m": 59, "5m": 59, "15m": 59, "30m": 59, "60m": 729, "1d": 3650}

# A live quote is worthless late. Fail fast and let the next poll try rather
# than blocking the ticker behind one slow request.
QUOTE_TIMEOUT_SECONDS = 8

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
        ticker = YAHOO_SYMBOLS.get(symbol.upper(), symbol)
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
        response = curl_requests.get(url, impersonate="chrome120", timeout=20)
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

        return df.sort_values("timestamp").reset_index(drop=True)

    def _yahoo_quote(self, symbol: str) -> dict:
        """Yahoo's chart metadata — one small request carrying a real clock.

        `range=1d&interval=1m` is asked for because the metadata block is
        what we are after, not the bars: it holds `regularMarketPrice` and
        `regularMarketTime`, the exchange's own epoch stamp for that price.
        The response is about 7 KB, against roughly 300 rows for a candle
        fetch, so this is the cheaper call as well as the fresher one.
        """
        ticker = YAHOO_SYMBOLS.get(symbol.upper(), symbol)
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
               f"?range=1d&interval=1m")
        response = curl_requests.get(url, impersonate="chrome120",
                                     timeout=QUOTE_TIMEOUT_SECONDS)
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
        except Exception as exc:
            log.debug("Yahoo quote failed, trying NSE: %s", exc)

        try:
            indices = self.nse.all_indices()
            name = "NIFTY 50" if symbol.upper() == "NIFTY" else symbol.upper()
            value = parse_index_value(indices, name)
            if value:
                return {"last_price": value, "source": "nse",
                        "source_time": parse_nse_timestamp(indices.get("timestamp"))}
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
