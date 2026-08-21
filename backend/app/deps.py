"""Shared dependencies — one place that decides which broker is live."""
from functools import lru_cache

from . import killswitch
from .brokers.base import Broker
from .brokers.mock import MockBroker
from .config import get_settings
from .risk.manager import RiskConfig


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


def risk_config() -> RiskConfig:
    """The risk rulebook, assembled from settings in one place.

    Every limit here is read once at boot and stays put — capital and lot
    size changing underneath a decision would make the system harder to
    reason about, not safer.

    The kill switch is the exception, and it is read live. This docstring
    used to claim the whole rulebook worked that way; it did not, because
    `get_settings()` is cached, and the switch could only be engaged by
    restarting the process. See `app/killswitch.py`.
    """
    s = get_settings()
    return RiskConfig(
        capital=s.capital,
        risk_per_trade_pct=s.risk_per_trade_pct,
        max_trades_per_day=s.max_trades_per_day,
        min_risk_reward=s.min_risk_reward,
        max_daily_loss_pct=s.max_daily_loss_pct,
        max_consecutive_losses=s.max_consecutive_losses,
        max_open_positions=s.max_open_positions,
        lot_size=s.lot_size,
        kill_switch=killswitch.engaged(),
        max_capital_deployed_pct=s.max_capital_deployed_pct,
    )
