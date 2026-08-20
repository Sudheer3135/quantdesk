"""Mock broker.

Generates believable NIFTY-like candles and an option chain so the whole
platform runs end to end before you ever connect a real account. This is
the default, on purpose.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .base import Broker

IST = timezone(timedelta(hours=5, minutes=30))


class MockBroker(Broker):
    name = "mock"

    def __init__(self, seed: int = 7, base_price: float = 24_500.0):
        self.rng = np.random.default_rng(seed)
        self.base_price = base_price

    # Bars per NSE session, keyed by every spelling the platform uses.
    # An unrecognised interval used to fall through to 375 bars a day, which
    # produced 22,125 candles for a 59-day request and timestamps two months
    # in the future. Silent, plausible-looking, and completely wrong.
    BARS_PER_SESSION = {
        "1m": 375, "1minute": 375,
        "3m": 125, "3minute": 125,
        "5m": 75, "5minute": 75,
        "15m": 25, "15minute": 25,
        "30m": 13, "30minute": 13,
        "1h": 7, "60m": 7, "60minute": 7,
        "1d": 1, "day": 1,
    }

    def candles(self, symbol: str = "NIFTY", interval: str = "5minute",
                days: int = 5) -> pd.DataFrame:
        if interval not in self.BARS_PER_SESSION:
            raise ValueError(
                f"unknown interval {interval!r}; expected one of "
                f"{sorted(self.BARS_PER_SESSION)}"
            )
        per_day = self.BARS_PER_SESSION[interval]
        step = timedelta(minutes=375 // per_day) if per_day > 1 else timedelta(days=1)
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
            cursor += step
            count += 1
            if count % per_day == 0:
                cursor = (cursor + timedelta(days=1)).replace(hour=9, minute=15)

        return pd.DataFrame({
            "timestamp": pd.to_datetime(stamps, utc=True),
            "open": open_.round(2), "high": high.round(2),
            "low": low.round(2), "close": close.round(2), "volume": volume,
        })

    def quote(self, symbol: str = "NIFTY") -> dict:
        last = self.candles(symbol).iloc[-1]
        return {
            "last_price": float(last["close"]),
            "source": "mock",
            # Simulated data is generated as of now, so it is honestly
            # current. Reporting it keeps every broker's quote the same
            # shape, so the ticker never has to special-case one of them.
            "source_time": datetime.now(UTC).isoformat(),
        }

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
