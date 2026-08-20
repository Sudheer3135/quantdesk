"""Zerodha Kite adapter.

Two things to know before using this live:

1. Kite access tokens expire every morning. You log in once a day through
   the browser flow and paste the request token. `login_url()` and
   `exchange_token()` handle that; the token is cached in Redis.
2. Kite does not publish a Greek-annotated option chain. This adapter
   builds one from instrument quotes, which is why it needs the
   instruments dump. That call is heavy — it is cached for a day.

Read-only by default. `place_order` raises unless LIVE_TRADING is on.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

from .base import Broker

log = logging.getLogger(__name__)

INTERVAL_MAP = {
    "1m": "minute", "3m": "3minute", "5m": "5minute",
    "15m": "15minute", "30m": "30minute", "1h": "60minute", "1d": "day",
}


class KiteBroker(Broker):
    name = "kite"

    def __init__(self, api_key: str, api_secret: str, access_token: str | None = None,
                 allow_live_orders: bool = False):
        try:
            from kiteconnect import KiteConnect
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("pip install kiteconnect to use the Kite adapter") from exc

        self.api_key = api_key
        self.api_secret = api_secret
        self.allow_live_orders = allow_live_orders
        self.kite = KiteConnect(api_key=api_key)
        if access_token:
            self.kite.set_access_token(access_token)
        self._instruments: pd.DataFrame | None = None
        self._instruments_loaded: datetime | None = None

    # ---- auth -----------------------------------------------------------
    def login_url(self) -> str:
        return self.kite.login_url()

    def exchange_token(self, request_token: str) -> str:
        data = self.kite.generate_session(request_token, api_secret=self.api_secret)
        self.kite.set_access_token(data["access_token"])
        return data["access_token"]

    # ---- instruments ----------------------------------------------------
    def instruments(self, exchange: str = "NFO") -> pd.DataFrame:
        fresh = self._instruments_loaded and \
            datetime.utcnow() - self._instruments_loaded < timedelta(hours=12)
        if self._instruments is None or not fresh:
            self._instruments = pd.DataFrame(self.kite.instruments(exchange))
            self._instruments_loaded = datetime.utcnow()
        return self._instruments

    def _index_token(self, symbol: str) -> int:
        mapping = {"NIFTY": 256265, "BANKNIFTY": 260105, "FINNIFTY": 257801,
                   "INDIAVIX": 264969}
        if symbol.upper() in mapping:
            return mapping[symbol.upper()]
        eq = pd.DataFrame(self.kite.instruments("NSE"))
        match = eq[eq["tradingsymbol"] == symbol.upper()]
        if match.empty:
            raise ValueError(f"unknown symbol: {symbol}")
        return int(match.iloc[0]["instrument_token"])

    # ---- data -----------------------------------------------------------
    def candles(self, symbol: str = "NIFTY", interval: str = "5m", days: int = 5) -> pd.DataFrame:
        token = self._index_token(symbol)
        kite_interval = INTERVAL_MAP.get(interval, interval)
        to_dt = datetime.now()
        from_dt = to_dt - timedelta(days=days)
        raw = self.kite.historical_data(token, from_dt, to_dt, kite_interval)
        df = pd.DataFrame(raw)
        if df.empty:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = df.rename(columns={"date": "timestamp"})
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df[["timestamp", "open", "high", "low", "close", "volume"]]

    def quote(self, symbol: str = "NIFTY") -> dict:
        key = f"NSE:{'NIFTY 50' if symbol.upper() == 'NIFTY' else symbol.upper()}"
        data = self.kite.quote([key])[key]
        # Kite stamps each quote with the exchange's own timestamp. That is
        # the one number that lets the desk tell a live price from a frozen
        # feed, so pass it through rather than re-deriving it from our clock.
        stamped = data.get("timestamp") or data.get("last_trade_time")
        return {
            "last_price": float(data["last_price"]),
            "source": "kite",
            "source_time": stamped.isoformat() if hasattr(stamped, "isoformat") else stamped,
            "raw": data,
        }

    def india_vix(self) -> float | None:
        try:
            return float(self.kite.quote(["NSE:INDIA VIX"])["NSE:INDIA VIX"]["last_price"])
        except Exception as exc:
            log.warning("could not read India VIX: %s", exc)
            return None

    def option_chain(self, symbol: str = "NIFTY", expiry: str | None = None) -> pd.DataFrame:
        """Build a normalised chain from NFO quotes for the nearest expiry."""
        inst = self.instruments("NFO")
        opts = inst[(inst["name"] == symbol.upper()) & (inst["segment"] == "NFO-OPT")].copy()
        if opts.empty:
            raise ValueError(f"no NFO options found for {symbol}")

        opts["expiry"] = pd.to_datetime(opts["expiry"])
        chosen = pd.to_datetime(expiry) if expiry else opts["expiry"].min()
        opts = opts[opts["expiry"] == chosen]

        spot = self.quote(symbol)["last_price"]
        opts = opts[(opts["strike"] - spot).abs() <= 1500]

        keys = [f"NFO:{s}" for s in opts["tradingsymbol"]]
        quotes: dict = {}
        for i in range(0, len(keys), 200):        # Kite caps instruments per call
            quotes.update(self.kite.quote(keys[i:i + 200]))

        rows: dict[float, dict] = {}
        for row in opts.itertuples():
            q = quotes.get(f"NFO:{row.tradingsymbol}")
            if not q:
                continue
            strike = float(row.strike)
            entry = rows.setdefault(strike, {"strike": strike})
            side = "call" if row.instrument_type == "CE" else "put"
            entry[f"{side}_oi"] = float(q.get("oi", 0))
            entry[f"{side}_oi_change"] = float(
                q.get("oi", 0) - q.get("oi_day_high", q.get("oi", 0)))
            entry[f"{side}_volume"] = float(q.get("volume", 0))
            entry[f"{side}_ltp"] = float(q.get("last_price", 0))
            entry[f"{side}_iv"] = 0.0        # Kite does not publish IV; compute if you need it

        chain = pd.DataFrame(rows.values()).sort_values("strike").reset_index(drop=True)
        return chain.fillna(0.0)

    # ---- execution ------------------------------------------------------
    def place_order(self, **kwargs) -> dict:
        if not self.allow_live_orders:
            raise PermissionError(
                "Live orders are disabled. Set LIVE_TRADING=true and restart "
                "only when you have tested the full path in paper mode."
            )
        return {"order_id": self.kite.place_order(**kwargs)}

    def positions(self) -> list[dict]:
        return self.kite.positions().get("net", [])
