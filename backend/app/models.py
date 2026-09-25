"""Database tables."""
from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# Deferred-loading groups for columns added in migration 0009. See the note
# on `SignalRecord` for why they are deferred.
PROVENANCE_GROUP = "provenance_0009"
CAPTURE_GROUP = "capture_clocks_0009"


def utc_now() -> datetime:
    """The default for every `created_at`, timezone-aware.

    These columns defaulted to a *naive* UTC value going into a TIMESTAMP
    WITH TIME ZONE. Postgres then reads such a value in the session
    timezone, so the stored instant was correct only because that session
    happens to be UTC. Point `TimeZone` at Asia/Kolkata, an entirely
    reasonable thing to do on an Indian trading system, and every row shifts
    five and a half hours — including `TradeRecord.created_at`, which is
    what the daily trade cap counts.

    The old spelling was also deprecated as of Python 3.12 and scheduled for
    removal.

    Right by construction now rather than by configuration.
    """
    return datetime.now(UTC)


class SignalRecord(Base):
    """Every signal the engine produces, saved whether or not it was traded.
    This is what makes the system auditable after the fact."""
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    action: Mapped[str] = mapped_column(String(8), index=True)
    confidence: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    target: Mapped[float | None] = mapped_column(Float, nullable=True)
    checks: Mapped[dict] = mapped_column(JSON, default=dict)
    context: Mapped[dict] = mapped_column(JSON, default=dict)

    # The risk decision this signal was published with: approved or blocked,
    # the reasons, the sizing, and the day state it was judged against.
    #
    # Its own column rather than a key inside `context`. `context` is the
    # market reading the signal engine produced — VWAP, ATR, trend — and the
    # dashboard reads it as such; folding a governance record into it would
    # leave neither column meaning one thing. Nullable because every row
    # written before this existed has no decision to report, and "unknown"
    # is the honest answer for those.
    risk: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # The two-layer read that runs alongside the BUY/SELL/HOLD verdict:
    # where the higher timeframe is pointing, and whether this is the moment
    # to act on it. Two indexed columns and a JSON body, for the same reason
    # `risk` is shaped that way — these two labels are what a study groups
    # by, and the justification behind them is what a human reads afterwards.
    #
    # Nullable with no backfill. Every row written before this existed had
    # no bias and no entry state, and computing one now from today's code
    # and calling it what the desk said at the time would be a fabricated
    # record. The replay in `evaluation.two_layer` recomputes them
    # explicitly, as a study, and says so.
    bias: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    entry_state: Mapped[str | None] = mapped_column(
        String(16), nullable=True, index=True)
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # ---- deferred, on purpose ------------------------------------------
    #
    # Every column below is `deferred`: a plain `select(SignalRecord)` does
    # not name it. Research reads frozen archives that predate these columns
    # — the audit snapshot, and the frozen comparator that replays it with
    # audit-time code — and a SELECT naming a column the archive lacks fails
    # outright. Readers that want them ask through `data.schema.loadable`,
    # which loads them in the same query when the database has them.
    # ---- the decision clocks (TC-2) ------------------------------------
    #
    # Persisted as columns rather than reconstructed from `context.timing`.
    # The JSON copy is kept for readers that already use it, but a clock that
    # only exists inside a blob can be rebuilt by any later code with any
    # later idea of what it should have said, and nothing would show it had
    # been. A column written at decision time cannot.
    #
    #   bar_open_time           the source bar's own open
    #   bar_close_time          when that bar completed
    #   available_at            when the completed bar could first be read
    #   received_at             when Quant Desk actually received it — never
    #                           derived from exchange time; NULL if unknown
    #   decision_at             when the decision was made
    #   earliest_execution_time the first instant an order could be working
    #
    # All nullable, with no backfill. `clock_basis` is NULL on every row
    # written before these existed; guessing their timestamps now and storing
    # them as if observed would be a fabricated record, which is exactly
    # what the columns exist to prevent.
    bar_open_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, deferred=True,
        deferred_group=PROVENANCE_GROUP)
    bar_close_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, deferred=True,
        deferred_group=PROVENANCE_GROUP)
    available_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, deferred=True,
        deferred_group=PROVENANCE_GROUP)
    received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, deferred=True,
        deferred_group=PROVENANCE_GROUP)
    decision_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, deferred=True,
        deferred_group=PROVENANCE_GROUP)
    earliest_execution_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, deferred=True,
        deferred_group=PROVENANCE_GROUP)
    # "persisted" on rows written with the columns above; NULL on legacy
    # rows, whose timing can only be inferred from `context`.
    clock_basis: Mapped[str | None] = mapped_column(
        String(24), nullable=True, index=True, deferred=True,
        deferred_group=PROVENANCE_GROUP)

    # ---- what produced it (RP-2) ---------------------------------------
    #
    # On the signal, not only on a backtest's metadata. One row has to be
    # enough to say which strategy, which parameters, which inputs and which
    # code made it — otherwise two signals from different configurations
    # sit in the same table indistinguishable from each other.
    strategy_version: Mapped[str | None] = mapped_column(String(32), nullable=True, deferred=True,
        deferred_group=PROVENANCE_GROUP)
    parameter_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True, deferred=True,
        deferred_group=PROVENANCE_GROUP)
    input_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True, deferred=True,
        deferred_group=PROVENANCE_GROUP)
    data_source: Mapped[str | None] = mapped_column(String(64), nullable=True, deferred=True,
        deferred_group=PROVENANCE_GROUP)
    code_id: Mapped[str | None] = mapped_column(String(96), nullable=True, deferred=True,
        deferred_group=PROVENANCE_GROUP)
    provenance: Mapped[dict | None] = mapped_column(JSON, nullable=True, deferred=True,
        deferred_group=PROVENANCE_GROUP)


class TradeRecord(Base):
    """The trade journal. Fill the review fields after the close, not during."""
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    # Entry and realisation can belong to different trading days. Older
    # journal rows have no recorded close time; do not invent one on upgrade.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True,
                                                      index=True)
    symbol: Mapped[str] = mapped_column(String(48), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    entry: Mapped[float] = mapped_column(Float)
    exit: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float] = mapped_column(Float)
    target: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    setup: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan_followed: Mapped[bool | None] = mapped_column(nullable=True)
    mistakes: Mapped[str | None] = mapped_column(Text, nullable=True)
    lesson: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CandleRecord(Base):
    """Archived index candles.

    Free data sources cap how far back you can look. This table is how you
    beat that cap: every fetch is stored, so your own history grows from the
    day you switch it on. Nobody can revoke it or start charging for it.

    Every row can answer three questions about itself: where it came from
    (`source`), when we captured it (`ingested_at`), and whether anything
    about it is derived rather than observed (`volume_is_synthetic`). A
    dataset that cannot answer those is a dataset you cannot defend a
    backtest with.
    """
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "timestamp", name="uq_candle"),
        Index("ix_candle_session", "symbol", "timeframe", "session_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)

    # No default. It defaulted to "free" once, and a caller that forgot to
    # pass it labelled 19,000 mock candles as real data — the one column
    # that separated trustworthy rows from junk became useless exactly when
    # it was needed. A missing source must fail, not guess.
    source: Mapped[str] = mapped_column(String(16), nullable=False)

    # The IST trading date. Denormalised from `timestamp` on purpose: gap
    # detection and per-session queries otherwise tz-convert every row in
    # the table on every check.
    session_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)

    # When we wrote the row, as distinct from when the bar happened. This is
    # what tells a bulk backfill apart from a live capture months later.
    ingested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    # Yahoo reports zero volume for Indian index tickers and the free broker
    # substitutes a constant so VWAP does not divide by zero. Those rows are
    # otherwise indistinguishable from real ones, and anything derived from
    # volume is measuring nothing at all. Provenance belongs on the row.
    volume_is_synthetic: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False)

    # The version number of the values on this row. Bumped only when a
    # re-import actually changes them; the superseded values are kept in
    # `candle_revisions` rather than destroyed. It used to bump on every
    # re-import whether anything changed or not, which is how 5,818 of
    # 6,750 archived bars came to carry a revision with nothing to show for
    # it.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class CandleRevision(Base):
    """A superseded version of an archived candle (TC-6).

    Append-only. When a source restates a bar, the values that were on the
    row until then are copied here before the row is overwritten, with the
    window during which they were the known truth. A replay of a decision
    made at 10:02 can then ask for the bar as it was known at 10:02 — the
    original 100 — instead of the 101 the source published at 10:07, which
    the decision could not have seen.

    Nothing is ever updated or deleted here. A revision history that can be
    rewritten is not a history.
    """
    __tablename__ = "candle_revisions"
    __table_args__ = (
        Index("ix_candle_revision_bar", "symbol", "timeframe", "timestamp"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(8))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The version these values were. The live row carries a higher one.
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    volume_is_synthetic: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False)
    # When these values became known, and when they stopped being the latest.
    # Together they bound the moments at which a replay should see them.
    known_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    superseded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OptionContract(Base):
    """One tradable option: an underlying, an expiry, a strike, a side.

    Split from its price history so that "which strikes existed for this
    expiry?" is a query rather than a string-parse over millions of bars.
    The missing-strike diagnostic depends on this separation.
    """
    __tablename__ = "option_contracts"
    __table_args__ = (
        UniqueConstraint("underlying", "expiry_date", "strike", "option_type",
                         name="uq_option_contract"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    underlying: Mapped[str] = mapped_column(String(32), index=True)
    expiry_date: Mapped[date] = mapped_column(Date, index=True)
    strike: Mapped[float] = mapped_column(Float, index=True)
    option_type: Mapped[str] = mapped_column(String(2))       # CE | PE
    lot_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tradingsymbol: Mapped[str | None] = mapped_column(String(64), nullable=True)

    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16), nullable=False)


class OptionCandle(Base):
    """Option premium history, one row per contract per bar.

    Read `bar_kind` before trusting the range on any row. NSE's public
    endpoint publishes a snapshot, not a tape, so bars folded from polling
    it have an open/high/low/close derived from sampled last-traded prices.
    Those understate the true range, and a strategy tested on them will look
    calmer than the market was.
    """
    __tablename__ = "option_candles"
    __table_args__ = (
        UniqueConstraint("contract_id", "timeframe", "timestamp",
                         name="uq_option_candle"),
        Index("ix_option_candle_time", "timeframe", "timestamp"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(
        ForeignKey("option_contracts.id"), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)

    open_interest: Mapped[float | None] = mapped_column(Float, nullable=True)
    oi_change: Mapped[float | None] = mapped_column(Float, nullable=True)
    iv: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Nullable because NSE's public chain publishes no depth. They exist so
    # that the day a source does provide them, slippage becomes a measured
    # quantity instead of an assumed one.
    bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Depth at the touch, where the source publishes it. NULL is
    # "unavailable", never zero.
    bid_size: Mapped[float | None] = mapped_column(Float, nullable=True, deferred=True,
        deferred_group=CAPTURE_GROUP)
    ask_size: Mapped[float | None] = mapped_column(Float, nullable=True, deferred=True,
        deferred_group=CAPTURE_GROUP)

    # ---- three clocks, never one (OC-1) --------------------------------
    #
    #   exchange_time  the source's own timestamp for the quote, if it
    #                  publishes one. NULL when it does not.
    #   capture_time   when Quant Desk received the latest poll folded into
    #                  this bar — the moment the stored close became known.
    #   first_seen     when this bar was first recorded. Immutable: written
    #                  on insert and never in an upsert's update set, so a
    #                  re-import cannot make an old observation look newer
    #                  or older than it was.
    #
    # `timestamp` above is the bucket start and is a grouping key only. It
    # used to double as availability, which let a quote captured at 10:04
    # count as known at 10:00. NULL on legacy rows, which are read under the
    # old bucket-close rule and labelled as such.
    exchange_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, deferred=True,
        deferred_group=CAPTURE_GROUP)
    capture_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, deferred=True,
        deferred_group=CAPTURE_GROUP)
    first_seen: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, deferred=True,
        deferred_group=CAPTURE_GROUP)

    # The index level at this same bar. Stored alongside the premium because
    # without it, greeks and implied volatility cannot be recomputed later:
    # the spot series and the option series drift out of alignment as soon
    # as either has a gap.
    underlying_close: Mapped[float | None] = mapped_column(Float, nullable=True)

    bar_kind: Mapped[str] = mapped_column(String(16), nullable=False)   # ohlc | snapshot
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    session_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    ingested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # How many raw snapshots were folded into this bar. One sample means the
    # high and low are the same number as the close and the range is fiction.
    samples: Mapped[int | None] = mapped_column(Integer, nullable=True)


class DatasetVersion(Base):
    """A content hash of the exact rows one backtest read.

    A backtest result without this is an anecdote: you cannot tell months
    later whether a number changed because you improved the strategy or
    because the underlying data was re-fetched, restated, or extended.

    The hash is content-addressed, so identical data collides on purpose and
    a single changed tick produces a different hash.
    """
    __tablename__ = "dataset_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    first_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    row_count: Mapped[int] = mapped_column(Integer)
    session_count: Mapped[int] = mapped_column(Integer)
    source_mix: Mapped[dict] = mapped_column(JSON, default=dict)
    volume_is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now)


class MarketRegime(Base):
    """What kind of market each bar happened in.

    Stored rather than recomputed on demand for two reasons. The cheap one is
    speed: splitting an outcome study by regime otherwise re-derives the
    whole archive's features on every request. The real one is that a
    classifier changes. `engine_version` is on every row so a table holding
    two generations is detectable instead of quietly mixed — a regime split
    computed across two different definitions of TREND_UP would look like a
    finding.

    Both levels live on one row because they describe the same bar and are
    always read together: the day answers "is this a trend day", the hour
    answers "is it still trending right now", and the interesting bars are
    the ones where those disagree.

    `reasons` and `features` are stored, not just the label. A regime label
    with no justification is exactly the black box this platform keeps
    refusing to build — and without the features, a threshold change months
    from now cannot be argued about against the bars it would have moved.
    """
    __tablename__ = "market_regimes"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "timestamp", name="uq_market_regime"),
        Index("ix_regime_session", "symbol", "timeframe", "session_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    session_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)

    day_regime: Mapped[str] = mapped_column(String(16), index=True)
    day_confidence: Mapped[float] = mapped_column(Float)
    day_reasons: Mapped[list] = mapped_column(JSON, default=list)

    hour_regime: Mapped[str] = mapped_column(String(16), index=True)
    hour_confidence: Mapped[float] = mapped_column(Float)
    hour_reasons: Mapped[list] = mapped_column(JSON, default=list)

    # The numbers behind both verdicts: efficiency, ATR ratio, VWAP side, the
    # gap, the opening range. Kept so a verdict can be re-argued without the
    # candles.
    features: Mapped[dict] = mapped_column(JSON, default=dict)

    engine_version: Mapped[str] = mapped_column(String(16), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now)


class VixDaily(Base):
    """India VIX, one row per session.

    Its own table rather than rows in `candles`: a daily bar is stamped at
    midnight, which the candle validator correctly rejects as outside the
    session, and a volatility index is not a price anything is traded at.
    Strategy v2 ranks the live VIX against this history before buying
    premium, so the table has to exist before the gate can say anything.
    """
    __tablename__ = "vix_daily"
    __table_args__ = (UniqueConstraint("session_date", name="uq_vix_daily"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now)


class PaperPosition(Base):
    """One simulated option position opened by strategy v2.

    Paper, and labelled so in the table name: nothing here ever reached a
    broker. Every level the position was opened against is stored at entry,
    so a restart resumes monitoring the same stop and target rather than
    recomputing them from a later price.
    """
    __tablename__ = "paper_positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(8), index=True)   # open | closed
    session_date: Mapped[date] = mapped_column(Date, index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    direction: Mapped[str] = mapped_column(String(8))            # BUY | SELL on the index
    contract: Mapped[str] = mapped_column(String(48))
    token: Mapped[str | None] = mapped_column(String(16), nullable=True)
    option_type: Mapped[str] = mapped_column(String(2))
    strike: Mapped[float] = mapped_column(Float)
    expiry: Mapped[date] = mapped_column(Date)
    lot_size: Mapped[int] = mapped_column(Integer)
    lots: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)

    index_entry: Mapped[float] = mapped_column(Float)
    index_stop: Mapped[float] = mapped_column(Float)
    index_target: Mapped[float] = mapped_column(Float)
    premium_entry: Mapped[float] = mapped_column(Float)
    premium_stop: Mapped[float] = mapped_column(Float)
    premium_target: Mapped[float] = mapped_column(Float)
    risk_amount: Mapped[float] = mapped_column(Float)

    last_premium: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_premium: Mapped[float | None] = mapped_column(Float, nullable=True)
    worst_premium: Mapped[float | None] = mapped_column(Float, nullable=True)

    premium_exit: Mapped[float | None] = mapped_column(Float, nullable=True)
    index_exit: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
    gross_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    costs: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)

    # The selection, the gates, the risk verdict and the signal it came
    # from — everything needed to argue with the trade later.
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class PaperDecision(Base):
    """Every signal strategy v2 looked at, and what it did about it.

    Rejections are stored, not just entries. A paper record that only kept
    the trades it took would report the win rate of whatever it happened to
    accept, with nothing showing how much it refused or why.
    """
    __tablename__ = "paper_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy: Mapped[str] = mapped_column(String(16), index=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    session_date: Mapped[date] = mapped_column(Date, index=True)
    signal_time: Mapped[str | None] = mapped_column(String(40), nullable=True)
    action: Mapped[str] = mapped_column(String(8))
    outcome: Mapped[str] = mapped_column(String(8), index=True)    # entered | rejected
    code: Mapped[str] = mapped_column(String(40), index=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
