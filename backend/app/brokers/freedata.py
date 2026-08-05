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

import pandas as pd
from curl_cffi import requests as curl_requests

from .base import Broker
from .nse import NSEClient, parse_index_value, parse_option_chain

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

    def quote(self, symbol: str = "NIFTY") -> dict:
        """Prefer NSE's live index value; fall back to the last candle."""
        try:
            indices = self.nse.all_indices()
            name = "NIFTY 50" if symbol.upper() == "NIFTY" else symbol.upper()
            value = parse_index_value(indices, name)
            if value:
                return {"last_price": value, "source": "nse"}
        except Exception as exc:
            log.debug("NSE quote failed, falling back to candles: %s", exc)

        last = self.candles(symbol, "5m", days=5).iloc[-1]
        return {"last_price": float(last["close"]), "source": "yahoo"}

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
