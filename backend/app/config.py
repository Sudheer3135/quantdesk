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

    # ---- Angel One SmartAPI, live price feed only ----------------------
    #
    # Deliberately *not* a value of `broker`. Angel is layered on top of
    # whichever broker is configured, as the preferred source of the live
    # spot price and nothing else: candles, the option chain and every
    # analytical input still come from the existing adapter, so switching
    # this on cannot move the 5-minute pipeline underneath the strategy.
    #
    # These are read here and nowhere else. Nothing below serialises them,
    # and no API response carries them — see `tests/test_angel_isolation.py`.
    angel_enabled: bool = False
    angel_api_key: str | None = None
    angel_client_code: str | None = None
    # MPIN on newer accounts, the login password on older ones. Angel takes
    # whichever the account uses in the same field.
    angel_password: str | None = None
    angel_mpin: str | None = None
    angel_totp_secret: str | None = None

    # NIFTY 50 on NSE cash. Angel identifies instruments by numeric token,
    # not by name, and 99926000 is the index token in their instrument
    # master. Exposed because a token that silently changes would subscribe
    # the desk to the wrong instrument and every price would simply be
    # somebody else's — verify it against the master before trusting it.
    angel_nifty_token: str = "99926000"
    angel_exchange_type: int = 1        # 1 = NSE_CM in SmartWebSocketV2

    # A push feed proves it is alive by pushing. Past this with no tick
    # during market hours the feed is treated as down and the free-data
    # poller takes over. Angel's index feed prints about once a second, so
    # this is roughly ten missed ticks — long enough not to flap on a hiccup.
    angel_stale_seconds: float = 10.0

    # The floor between two published prices. The feed is push, so this is
    # not a poll interval: it caps how often a burst of ticks can reach
    # Redis and every open browser. 250ms is four updates a second, which is
    # past what an eye reads off a dashboard anyway.
    angel_min_publish_ms: int = 250

    # Reconnect backoff. Starts fast because most drops are momentary, and
    # tops out well under a session so a feed that recovers at lunchtime
    # does not sit waiting until the close.
    angel_reconnect_min_seconds: float = 2.0
    angel_reconnect_max_seconds: float = 60.0

    # Nothing places a real order unless this is explicitly true.
    live_trading: bool = False

    capital: float = 100_000.0
    risk_per_trade_pct: float = 1.0
    max_trades_per_day: int = 2
    min_risk_reward: float = 2.0
    lot_size: int = 75

    # The rest of the risk rulebook. These existed only as RiskConfig
    # defaults, which meant the README documented a kill switch nobody could
    # reach without editing code. A limit you cannot configure is a limit you
    # cannot turn on when you need it most.
    max_daily_loss_pct: float = 3.0
    max_consecutive_losses: int = 2
    max_open_positions: int = 1
    max_capital_deployed_pct: float = 20.0

    # Blocks every new entry while true. Flip it in .env and restart.
    kill_switch: bool = False

    archive_candles: bool = True

    # Capture an option-chain snapshot on every agent tick. This is the only
    # way option history ever comes to exist — NSE publishes a live snapshot,
    # not a tape, and nobody sells the history at a retail price — so a bar
    # not captured today cannot be recovered tomorrow. Turn this off only if
    # you are certain you will never backtest options.
    archive_option_chain: bool = True

    # How often to poll the option chain. This must be shorter than the bar
    # width (`watch_timeframe`) or every bar is a single observation whose
    # high and low equal its close — a range that is fiction. At 60s into
    # 5-minute buckets each bar aggregates about five observations.
    #
    # Measured against NSE on 17-Aug-2026: 6 consecutive chain requests at
    # this interval, alongside the 10s price ticker, all succeeded at ~1.6s
    # latency with no throttling. Do not lower it without repeating that
    # measurement — NSE's limits are undocumented and they do block.
    option_snapshot_interval_seconds: int = 60

    # Below this share of the option snapshots a session should have
    # produced, the data is treated as unusable for backtesting rather than
    # merely thin. This is a placeholder pending an agreed figure — nothing
    # has derived 90%, and it is exposed here so it can be argued with
    # rather than discovered in a constant.
    option_coverage_min_backtest_pct: float = 90.0

    # The same floor for index candles. Separate from the option figure on
    # purpose: the index archive backfills from Yahoo, so a gap there is
    # recoverable and a stricter bar is affordable, while option history is
    # gone the moment it is missed. Both are placeholders pending an agreed
    # figure — nothing has derived either one.
    index_coverage_min_backtest_pct: float = 90.0

    agent_interval_minutes: int = 5
    # How often the price ticker polls.
    #
    # Measured against Yahoo's quote endpoint on 20-Aug-2026, mid-session,
    # at three rates for 40s each: 5s, 2s and 1s all returned 200 on every
    # request with no throttling. But the quote's own age behind the market
    # was statistically identical at all three (median 2.2s at 5s polling,
    # 3.7s at 1s) — because the lag lives in the source's refresh, not in
    # how often we ask. Polling faster than this buys nothing measurable and
    # spends five times the requests, so 5s is the floor worth having.
    ticker_interval_seconds: int = 5
    watch_symbol: str = "NIFTY"
    watch_timeframe: str = "5m"

    cors_origins: str = "http://localhost:5173"

    # Shared secret for the endpoints that write to the database. Unset
    # means the write endpoints are open, which is tolerable on localhost
    # and a startup failure when ENVIRONMENT=prod. Supply it through the
    # environment only — a key with a default value is not a key.
    api_key: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
