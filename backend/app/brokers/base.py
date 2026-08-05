"""Broker interface.

Everything above this layer talks to `Broker`, never to a vendor SDK.
Swapping Zerodha for another broker means writing one new adapter.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class Broker(ABC):
    name: str = "base"

    @abstractmethod
    def candles(self, symbol: str, interval: str, days: int = 5) -> pd.DataFrame:
        """Return columns: timestamp, open, high, low, close, volume."""

    @abstractmethod
    def quote(self, symbol: str) -> dict:
        """Return at least {'last_price': float}."""

    @abstractmethod
    def option_chain(self, symbol: str, expiry: str | None = None) -> pd.DataFrame:
        """Return the normalised chain shape described in analytics/options.py."""

    @abstractmethod
    def india_vix(self) -> float | None:
        ...

    @abstractmethod
    def place_order(self, **kwargs) -> dict:
        ...

    @abstractmethod
    def positions(self) -> list[dict]:
        ...
