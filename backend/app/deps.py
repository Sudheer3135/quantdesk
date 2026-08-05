"""Shared dependencies — one place that decides which broker is live."""
from functools import lru_cache

from .brokers.base import Broker
from .brokers.mock import MockBroker
from .config import get_settings


@lru_cache
def get_broker() -> Broker:
    s = get_settings()
    if s.broker == "free":
        from .brokers.freedata import FreeDataBroker
        return FreeDataBroker()
    if s.broker == "kite":
        from .brokers.kite import KiteBroker
        return KiteBroker(
            api_key=s.kite_api_key or "",
            api_secret=s.kite_api_secret or "",
            access_token=s.kite_access_token,
            allow_live_orders=s.live_trading,
        )
    return MockBroker()
