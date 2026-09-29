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

    # India VIX on NSE cash. Verified against Angel's instrument master on
    # 14-Sep-2026 ("India VIX", AMXIDX, NSE). Subscribed on the index socket
    # in LTP mode and routed by token, so a VIX print can never be published
    # as the NIFTY price — both arrive on the same segment in the same shape.
    angel_vix_token: str = "99926017"

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

    # How long the feed may sit silent, socket still reporting "connected",
    # before this forces the connection closed rather than waiting for the
    # vendor SDK to notice on its own.
    #
    # angel_stale_seconds only decides when the *poller* takes over; it does
    # not touch the Angel socket at all. That gap is real: measured on
    # 15-Sep-2026, the feed twice went fully silent for minutes — 677s and
    # 365s — with the socket reporting itself open the whole time and
    # neither on_close nor on_error firing, so the supervisor's own reconnect
    # loop never ran. Both times a "Websocket connected" line from the
    # vendor library's own logger, not ours, is what eventually recovered
    # it — the SDK's internal reconnect noticed, on its own clock, which
    # that day took over ten minutes. This is what makes the desk force the
    # issue instead of trusting that clock: past this many seconds of
    # silence during market hours, the socket is closed here, which the
    # supervisor sees as a normal disconnect and reconnects from at its own
    # (much faster) 2-60s backoff.
    #
    # Set comfortably above angel_stale_seconds so the fallback poller is
    # already covering the gap before this fires — this is a ceiling on how
    # long a stall can last, not the trigger for switching to the poller.
    angel_force_reconnect_seconds: float = 30.0

    # Nothing places a real order unless this is explicitly true.
    live_trading: bool = False
    # How often the writer-process lease checks its own connection. Lifecycle
    # monitoring only; 0 turns it off. Write safety comes from the per-
    # transaction schema lock, not from this (Pass 2E-A.2).
    writer_lease_heartbeat_seconds: float = 5.0

    capital: float = 100_000.0
    risk_per_trade_pct: float = 1.0
    max_trades_per_day: int = 2
    min_risk_reward: float = 2.0
    # NIFTY lot size. 65 per Angel's instrument master on 14-Sep-2026 and
    # confirmed by the desk owner; it was 75 until the exchange revised it.
    # Changes by circular — check the master before trusting this.
    lot_size: int = 65

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

    # Consecutive unhealthy checks before the poller actually takes over.
    #
    # Without this the switch is instantaneous: one missed beat past
    # ANGEL_STALE_SECONDS and the very next five-second poll publishes a
    # Yahoo price, so the desk's `source` flips to the slower feed and back
    # again on a single blip. The dashboard shows that flicker, and worse,
    # a 2.2s-old polled quote briefly replaces a 0.4s-old streamed one.
    #
    # At two, a switch needs the feed to be quiet across two whole poll
    # cycles — genuine silence, not one late tick. It costs one extra poll
    # interval of delay on a real outage, which against a five-minute
    # analysis pipeline is nothing.
    angel_fallback_confirmations: int = 2

    # ---- Angel live option chain --------------------------------------
    #
    # Off by default. The polled NSE chain keeps working either way; this
    # replaces where the *live* chain is read from, and a desk that has not
    # opted in should not silently change data source on upgrade.
    angel_options_enabled: bool = False

    # How many strikes either side of spot to subscribe. NIFTY strikes are
    # 50 apart, so 20 is ±1000 points and about 82 contracts — wide enough
    # that ordinary intraday drift never leaves the band, small enough that
    # the socket is not carrying strikes nobody will trade.
    angel_options_band: int = 20

    # Re-centre when fewer than this many strikes of cover remain on one
    # side. Re-subscribing costs a round trip and a gap in the series, so
    # this is deliberately not eager.
    angel_options_refresh_margin: int = 5

    # A contract with nothing newer than this is dropped from the chain
    # rather than reported. Far strikes legitimately go quiet for long
    # stretches; this excludes yesterday's print, not a slow hour.
    angel_options_max_age_seconds: float = 900.0

    # ---- Angel historical backfill ------------------------------------
    #
    # Run by hand, never on a schedule, so these are a command's defaults
    # rather than settings the running desk reads every tick.
    #
    # The window is the vendor's limit, not ours. `getCandleData` caps a
    # FIVE_MINUTE request at 100 days and — measured, not documented —
    # silently truncates anything longer to the most *recent* 100 days
    # while still answering SUCCESS. A 200-day request therefore returns
    # half the data with no error anywhere, which is why the pager walks
    # backwards in windows that never exceed this.
    angel_history_max_window_days: int = 100

    # Seconds between calls. Measured: at 0.34s pacing fifteen consecutive
    # requests all succeeded (~1.6 req/s end to end, each call ~300ms);
    # with no pacing the twelfth was rejected. 0.6s is that measurement
    # with room to spare, and the room is worth having because a
    # rate-limited call comes back as a parse error rather than a clean
    # 429 — easy to mistake for a corrupt response.
    angel_history_pace_seconds: float = 0.6

    # How many times a throttled call is retried before the run fails.
    angel_history_max_retries: int = 3

    # NIFTY 50 on NSE cash — the same token the websocket subscribes to,
    # kept separate so backfilling a different index never means editing
    # the live feed's configuration.
    angel_history_index_token: str = "99926000"

    # ---- Strategy v2, on paper -------------------------------------------
    #
    # Off unless switched on. v2 opens simulated positions only; nothing in
    # it can reach a broker, and `live_trading` is not consulted because
    # there is no order path to gate.
    v2_paper_enabled: bool = False

    # Strikes either side of spot streamed for v2's expiry when it is not
    # the nearest one. v2 buys a 0.45–0.60 delta, which sits within a few
    # strikes of the money; ten is room to drift, and far fewer tokens than
    # the main band.
    v2_options_band: int = 10

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
