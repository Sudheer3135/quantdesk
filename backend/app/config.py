"""Settings. Everything comes from environment variables — nothing secret
is ever committed. See .env.example at the repo root."""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "QuantDesk"
    environment: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://quant:quant@localhost:5432/quantdesk"
    redis_url: str = "redis://localhost:6379/0"

    broker: Literal["mock", "free", "kite"] = "mock"
    kite_api_key: str | None = None
    kite_api_secret: str | None = None
    kite_access_token: str | None = None

    # Nothing places a real order unless this is explicitly true.
    live_trading: bool = False

    capital: float = 100_000.0
    risk_per_trade_pct: float = 1.0
    max_trades_per_day: int = 2
    min_risk_reward: float = 2.0
    lot_size: int = 75

    archive_candles: bool = True

    # Capture an option-chain snapshot on every agent tick. This is the only
    # way option history ever comes to exist — NSE publishes a live snapshot,
    # not a tape, and nobody sells the history at a retail price — so a bar
    # not captured today cannot be recovered tomorrow. Turn this off only if
    # you are certain you will never backtest options.
    archive_option_chain: bool = True

    agent_interval_minutes: int = 5
    # How often the price ticker polls. Below ~5s you risk being throttled
    # by the free sources, and the data is not tick-level anyway.
    ticker_interval_seconds: int = 10
    watch_symbol: str = "NIFTY"
    watch_timeframe: str = "5m"

    cors_origins: str = "http://localhost:5173"


@lru_cache
def get_settings() -> Settings:
    return Settings()
