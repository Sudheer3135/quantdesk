"""Mock broker.

Generates believable NIFTY-like candles and an option chain so the whole
platform runs end to end before you ever connect a real account. This is
the default, on purpose.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .base import Broker

IST = timezone(timedelta(hours=5, minutes=30))


class MockBroker(Broker):
    name = "mock"

    def __init__(self, seed: int = 7, base_price: float = 24_500.0):
        self.rng = np.random.default_rng(seed)
        self.base_price = base_price

    def candles(self, symbol: str = "NIFTY", interval: str = "5minute",
                days: int = 5) -> pd.DataFrame:
        per_day = 75 if interval == "5minute" else 375
        n = per_day * days
        drift = np.linspace(0, self.rng.normal(0, 120), n)
        noise = np.cumsum(self.rng.normal(0, 12, n))
        close = self.base_price + drift + noise

        spread = np.abs(self.rng.normal(9, 4, n))
        open_ = np.r_[close[0], close[:-1]]
        high = np.maximum(open_, close) + spread
        low = np.minimum(open_, close) - spread
        volume = np.abs(self.rng.normal(180_000, 60_000, n)).round()

        start = datetime.now(IST).replace(hour=9, minute=15, second=0, microsecond=0) \
            - timedelta(days=days)
        stamps, cursor, count = [], start, 0
        while len(stamps) < n:
            stamps.append(cursor)
            cursor += timedelta(minutes=5)
            count += 1
            if count % per_day == 0:
                cursor = (cursor + timedelta(days=1)).replace(hour=9, minute=15)

        return pd.DataFrame({
            "timestamp": pd.to_datetime(stamps, utc=True),
            "open": open_.round(2), "high": high.round(2),
            "low": low.round(2), "close": close.round(2), "volume": volume,
        })

    def quote(self, symbol: str = "NIFTY") -> dict:
        return {"last_price": float(self.candles(symbol).iloc[-1]["close"])}

    def option_chain(self, symbol: str = "NIFTY", expiry: str | None = None) -> pd.DataFrame:
        spot = self.quote(symbol)["last_price"]
        atm = round(spot / 50) * 50
        strikes = np.arange(atm - 1000, atm + 1050, 50)
        distance = np.abs(strikes - spot)
        weight = np.exp(-distance / 400)

        return pd.DataFrame({
            "strike": strikes.astype(float),
            "call_oi": (weight * self.rng.uniform(6e5, 2e6, len(strikes))).round(),
            "put_oi": (weight * self.rng.uniform(6e5, 2e6, len(strikes))).round(),
            "call_oi_change": self.rng.normal(0, 1.2e5, len(strikes)).round(),
            "put_oi_change": self.rng.normal(0, 1.2e5, len(strikes)).round(),
            "call_volume": (weight * self.rng.uniform(1e5, 9e5, len(strikes))).round(),
            "put_volume": (weight * self.rng.uniform(1e5, 9e5, len(strikes))).round(),
            "call_iv": self.rng.uniform(11, 19, len(strikes)).round(2),
            "put_iv": self.rng.uniform(12, 21, len(strikes)).round(2),
            "call_ltp": np.maximum(spot - strikes, 0) + self.rng.uniform(10, 90, len(strikes)),
            "put_ltp": np.maximum(strikes - spot, 0) + self.rng.uniform(10, 90, len(strikes)),
        })

    def india_vix(self) -> float | None:
        return round(float(self.rng.uniform(11, 18)), 2)

    def place_order(self, **kwargs) -> dict:
        return {"status": "simulated", "order_id": f"MOCK-{self.rng.integers(1e6):06d}", **kwargs}

    def positions(self) -> list[dict]:
        return []
