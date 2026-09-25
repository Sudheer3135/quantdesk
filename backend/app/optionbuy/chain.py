"""The option archive, read the way a backtest is allowed to read it.

`backtest/feed.py` solved this problem for index candles: the frame is not
passed around, a cursor is, and it will not hand over a bar the walk has not
reached. This is the same guarantee for option premiums, which need it more.

Index look-ahead is at least *visible* — a strategy that peeks at tomorrow's
close usually posts an absurd equity curve. Option look-ahead is quiet. Ask
for the 24,300 CE at 11:05 and get the 11:10 bar back, and the result is a
strategy that consistently buys a few rupees better than it could have. That
is a small, plausible, permanent edge that no statistic in the output would
flag.

So the store carries its own clock. `advance()` moves it, only forwards, and
every read is checked against it. A read for a moment the walk has not
reached raises rather than returning a number.

Two further rules, both about not passing off one price as another:

  **At or before, never after.** A quote is the last one printed on or
  before the instant you asked about.

  **Stale is not observed.** A bar from forty minutes ago is a fact about
  forty minutes ago. Past `staleness_minutes` — or across a session
  boundary, at any age — the store reports no observation, and the caller
  decides what to do about it. Yesterday's close is not this morning's ask.
"""
from __future__ import annotations

import hashlib
import json
import logging
from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..market_hours import IST
from ..data import schema
from ..models import OptionCandle, OptionContract

log = logging.getLogger(__name__)

# `OptionCandle.bar_kind`, as the collector writes it. `ohlc` is a real tape;
# `snapshot` is polls folded into a bucket, whose high and low are sampled
# last-traded prices rather than the session's true extremes.
OHLC = "ohlc"
SNAPSHOT = "snapshot"

# How old the newest quote may be and still count as an observation. Two
# bucket widths: one poll can be missed without the price becoming a
# different fact about a different market.
DEFAULT_STALENESS_MINUTES = 10

HASH_VERSION = "2"

# How a bar's availability is established.
#   CAPTURED       the capture time Quant Desk recorded when it received the
#                  poll whose price is stored — an observation.
#   LEGACY_BUCKET  rows archived before capture times existed: available at
#                  the bucket's close (or later, if the row says so). An
#                  assumption, and labelled as one.
CAPTURED = "capture_time"
LEGACY_BUCKET = "legacy_bucket_close"

# Which clock a quote's *age* is measured on — a different question from
# when it became available. A quote can be newly received and already old:
# printed at 10:00, captured at 10:09, it is available from 10:09 and nine
# minutes old the moment it arrives.
AGE_EXCHANGE = "exchange_time"
AGE_CAPTURE = "capture_time_no_exchange_stamp"
AGE_LEGACY_BUCKET = "legacy_bucket_start"


class OptionLookaheadError(RuntimeError):
    """Something asked for an option price the walk has not reached.

    Always a bug, never a data problem — the same distinction `feed.py`
    draws. It means a fill was about to be priced with a quote that had not
    printed when the trade claims to have been placed.
    """


@dataclass(frozen=True, order=True)
class ContractKey:
    """One tradable option, as the archive identifies it."""
    expiry: date
    strike: float
    option_type: str            # CE | PE

    def label(self) -> str:
        return f"{self.strike:.0f} {self.option_type} {self.expiry.isoformat()}"


@dataclass(frozen=True)
class ContractMeta:
    contract_id: int
    key: ContractKey
    lot_size: int | None
    tradingsymbol: str | None
    source: str
    first_seen: datetime | None = None


@dataclass(frozen=True)
class OptionBar:
    """One stored option bar, with everything needed to cite it later."""
    row_id: int
    contract_id: int
    key: ContractKey
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    open_interest: float | None
    iv: float | None
    bid: float | None
    ask: float | None
    underlying_close: float | None
    bar_kind: str
    source: str
    samples: int | None
    session_date: date | None
    available_at: datetime | None = None
    # The three clocks (OC-1). None on legacy rows, which are then read under
    # the old bucket-close rule — see `ChainStore.available_from`.
    exchange_time: datetime | None = None
    capture_time: datetime | None = None
    first_seen: datetime | None = None

    @property
    def timestamp_basis(self) -> str:
        """Which clock this bar's availability and age are measured on."""
        return CAPTURED if self.capture_time is not None else LEGACY_BUCKET

    @property
    def observed_at(self) -> datetime:
        """When the stored price was true: the source's own stamp if it gave
        one, else when it was captured. Never the bucket start, which is a
        grouping key and not a time the price existed at."""
        if self.exchange_time is not None:
            return _as_utc(self.exchange_time)
        if self.capture_time is not None:
            return _as_utc(self.capture_time)
        return _as_utc(self.timestamp)

    @property
    def age_basis(self) -> str:
        """The clock `observed_at` came from, named so a fallback is visible."""
        if self.exchange_time is not None:
            return AGE_EXCHANGE
        if self.capture_time is not None:
            return AGE_CAPTURE
        return AGE_LEGACY_BUCKET

    def age(self, moment: datetime) -> timedelta:
        """How old the observation is at `moment`, on the staleness clock."""
        return _as_utc(moment) - self.observed_at

    @property
    def observed_tape(self) -> bool:
        """A real high and low, rather than sampled last-traded prices."""
        return self.bar_kind == OHLC

    def reference(self) -> dict:
        """The citation for a fill priced off this bar.

        A premium in a result is unfalsifiable unless the row it came from
        can be found again. This is that row.
        """
        return {
            "option_candle_id": self.row_id,
            "contract_id": self.contract_id,
            "contract": self.key.label(),
            "bar_timestamp": self.timestamp.isoformat(),
            "available_at": self.available_at.isoformat() if self.available_at else None,
            "exchange_time": self.exchange_time.isoformat() if self.exchange_time else None,
            "capture_time": self.capture_time.isoformat() if self.capture_time else None,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "timestamp_basis": self.timestamp_basis,
            "observed_at": self.observed_at.isoformat(),
            "age_basis": self.age_basis,
            "bar_kind": self.bar_kind,
            "samples": self.samples,
            "source": self.source,
        }


def _as_utc(moment: datetime) -> datetime:
    """SQLite returns naive datetimes; everything here is stored in UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _session_of(moment: datetime) -> date:
    """The IST calendar date. An NSE session never spans midnight."""
    return _as_utc(moment).astimezone(IST).date()


class ChainStore:
    """Option bars, indexed by contract and handed out strictly in the past."""

    def __init__(
        self,
        bars: dict[ContractKey, list[OptionBar]],
        contracts: dict[ContractKey, ContractMeta],
        *,
        underlying: str = "NIFTY",
        timeframe: str = "5m",
        staleness_minutes: int = DEFAULT_STALENESS_MINUTES,
    ) -> None:
        from ..analytics.indicators import TIMEFRAME_MINUTES
        duration = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
        def available(bar):
            listed = (_as_utc(contracts[bar.key].first_seen)
                      if bar.key in contracts and contracts[bar.key].first_seen
                      else _as_utc(bar.timestamp))
            if bar.capture_time is not None:
                # Available when it was captured — no earlier, however early
                # its bucket started. A quote captured at 10:04 is not known
                # at 10:02 because its bucket is stamped 10:00 (OC-1).
                return max(_as_utc(bar.capture_time), listed)
            return max(_as_utc(bar.timestamp) + duration,
                       _as_utc(bar.available_at) if bar.available_at else _as_utc(bar.timestamp),
                       listed)
        self._bars = {k: sorted(v, key=available) for k, v in bars.items()}
        self._stamps = {k: [available(b) for b in v] for k, v in self._bars.items()}
        self._available = available
        self.contracts = contracts
        self.underlying = underlying
        self.timeframe = timeframe
        self.staleness = timedelta(minutes=staleness_minutes)
        self._now: datetime | None = None

    # ---- the clock ----------------------------------------------------

    @property
    def now(self) -> datetime | None:
        """The furthest instant the walk has reached. None before it starts."""
        return self._now

    def advance(self, moment: datetime) -> None:
        """Move the clock forward. Backwards is a bug and says so."""
        moment = _as_utc(moment)
        if self._now is not None and moment < self._now:
            raise OptionLookaheadError(
                f"the option clock cannot go backwards: {moment.isoformat()} "
                f"is before {self._now.isoformat()}. A walk moves one way.")
        self._now = moment

    def seek(self, moment: datetime | None) -> None:
        """Set the clock without walking. For tests and for resuming."""
        self._now = _as_utc(moment) if moment is not None else None

    def available_from(self, bar: OptionBar) -> datetime:
        """When this bar actually became readable, as the store ordered it.

        Not the same as `OptionBar.available_at`, which is only one of the
        three inputs: a bucket stamped 05:25 is not readable until the
        bucket has closed at 05:30, and a contract's `first_seen` can push
        it later still. The citation on a trade used to print the raw field,
        so a fill that was correctly eligible at 05:30 cited a 05:25
        availability and could not be checked against its own execution
        clock. This is the number the store compared.
        """
        return self._available(bar)

    def _guard(self, moment: datetime, what: str) -> datetime:
        moment = _as_utc(moment)
        if self._now is None:
            raise OptionLookaheadError(
                f"{what}: the option clock has not started. Call advance() "
                "with the bar the walk is on before pricing anything.")
        if moment > self._now:
            raise OptionLookaheadError(
                f"{what}: asked for {moment.isoformat()} while the walk is at "
                f"{self._now.isoformat()}. That quote had not printed when "
                "this decision claims to have been made.")
        return moment

    # ---- reading -------------------------------------------------------

    def bar_at(self, key: ContractKey, moment: datetime,
               *, allow_stale: bool = False,
               eligible_from: datetime | None = None) -> OptionBar | None:
        """The last quote printed on or before `moment`, if it is current.

        None means *no observation* and never a modelled stand-in. Deciding
        what to do without one belongs to the pricing policy, which has to
        label the answer; a store that quietly substituted a model would put
        that label out of reach.

        `eligible_from` is the execution clock: the earliest instant an order
        from this decision could have been working. A quote that became
        available before that instant is not a price the order could have
        been given, however recent it is, so it is refused rather than
        returned.

        That is not a latency special case. At zero latency the clock is the
        decision instant itself, and a bucket that became available five
        minutes earlier is exactly as unreachable then as it is under a
        latency. Staleness and eligibility are different questions — a quote
        can be entirely fresh and still have printed before the order
        existed — and only one of them used to be asked.
        """
        moment = self._guard(moment, f"bar_at {key.label()}")
        stamps = self._stamps.get(key)
        if not stamps:
            return None

        position = bisect_right(stamps, moment) - 1
        if position < 0:
            return None
        # Availability ascends, so the candidate above is the newest quote
        # the walk can see. If even that one became available before the
        # order could exist, every earlier one did too and there is no
        # eligible observation to be had.
        if eligible_from is not None and stamps[position] < _as_utc(eligible_from):
            return None
        bar = self._bars[key][position]

        if allow_stale:
            return bar
        # Age from when the price was true — the exchange's stamp, else the
        # capture — not from the bucket start. For a legacy row those are the
        # same thing and nothing changes.
        if bar.age(moment) > self.staleness:
            return None
        # A quote from the previous session is not a stale quote from this
        # one — it is a fact about a market that has since closed, gapped and
        # reopened. Age alone would let an overnight hold price off it.
        if _session_of(bar.observed_at) != _session_of(moment):
            return None
        return bar

    def expiries(self, moment: datetime) -> list[date]:
        """Expiries with at least one quote by `moment`, soonest first.

        Derived from quotes rather than from `option_contracts.first_seen`,
        because a contract row the collector has never priced is not
        something this backtest can trade.
        """
        moment = self._guard(moment, "expiries")
        seen = {
            key.expiry for key, stamps in self._stamps.items()
            if stamps and stamps[0] <= moment
        }
        return sorted(seen)

    def strikes(self, expiry: date, option_type: str, moment: datetime) -> list[float]:
        """The strike ladder that existed at `moment`, ascending."""
        moment = self._guard(moment, "strikes")
        return sorted(
            key.strike for key, stamps in self._stamps.items()
            if key.expiry == expiry and key.option_type == option_type
            and stamps and stamps[0] <= moment
        )

    def sessions(self) -> set[date]:
        """Every IST session the archive holds a quote for."""
        return {b.session_date or _session_of(b.timestamp)
                for bars in self._bars.values() for b in bars}

    def contracts_on(self, session: date) -> int:
        """How many distinct contracts the archive priced that session.

        A session with two strikes stored is not a covered session even if
        every bucket has a bar in it: the chain the strategy needs to choose
        from was not captured, only a corner of it.
        """
        return sum(
            1 for bars in self._bars.values()
            if any((b.session_date or _session_of(b.timestamp)) == session
                   for b in bars)
        )

    def span(self) -> tuple[datetime | None, datetime | None]:
        stamps = [s for group in self._stamps.values() for s in group]
        return (min(stamps), max(stamps)) if stamps else (None, None)

    def __len__(self) -> int:
        return sum(len(v) for v in self._bars.values())

    @property
    def empty(self) -> bool:
        return len(self) == 0

    def bar_kinds(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for bars in self._bars.values():
            for bar in bars:
                counts[bar.bar_kind] = counts.get(bar.bar_kind, 0) + 1
        return counts

    def polls_by_bucket(self, session: date) -> dict[datetime, int]:
        """Snapshots folded into each bucket of one session.

        Shaped for `data.option_coverage.assess_session`, which is already
        the platform's answer to "did the collector run?". The maximum across
        contracts is the right reducer: every contract in one chain snapshot
        is written by the same poll, so summing would count one poll once per
        strike and report several hundred percent coverage.
        """
        polls: dict[datetime, int] = {}
        for bars in self._bars.values():
            for bar in bars:
                if (bar.session_date or _session_of(bar.timestamp)) != session:
                    continue
                bucket = bar.timestamp.astimezone(UTC)
                polls[bucket] = max(polls.get(bucket, 0), bar.samples or 1)
        return polls

    # ---- auditability ---------------------------------------------------

    def fingerprint(self) -> dict:
        """A content hash of exactly the option rows this run could read.

        The index fingerprint in `data/dataset.py` answers "were the candles
        the same?". Without its counterpart, two option backtests over
        identical candles can differ because the chain archive grew
        underneath them, and nothing in either result would say so.
        """
        digest = hashlib.sha256()
        digest.update(f"{HASH_VERSION}|{self.underlying}|{self.timeframe}\n".encode())
        for key in sorted(self._bars):
            meta = self.contracts.get(key)
            contract = {
                "expiry": key.expiry.isoformat(), "strike": float(key.strike),
                "option_type": key.option_type,
                "lot_size": meta.lot_size if meta else None,
                "tradingsymbol": meta.tradingsymbol if meta else None,
                "source": meta.source if meta else None,
            }
            digest.update((json.dumps(contract, sort_keys=True) + "\n").encode())
            for bar in self._bars[key]:
                # Prices alone do not identify a dataset: IV drives sizing,
                # OI/volume/spread drive eligibility, and samples drive coverage.
                # Preserve precision and NULLs; exclude database surrogate IDs.
                row = {name: (float(getattr(bar, name))
                              if getattr(bar, name) is not None else None)
                       for name in ("open", "high", "low", "close", "volume",
                                    "open_interest", "iv", "bid", "ask", "underlying_close")}
                row.update(
                    timestamp=_as_utc(bar.timestamp).astimezone(UTC).isoformat(),
                    bar_kind=bar.bar_kind, source=bar.source, samples=bar.samples,
                    session_date=(bar.session_date or _session_of(bar.timestamp)).isoformat())
                digest.update((json.dumps(row, sort_keys=True) + "\n").encode())

        first, last = self.span()
        return {
            "hash": digest.hexdigest(),
            "underlying": self.underlying,
            "timeframe": self.timeframe,
            "rows": len(self),
            "contracts": len(self._bars),
            "sessions": len(self.sessions()),
            "expiries": sorted(e.isoformat() for e in {k.expiry for k in self._bars}),
            "bar_kinds": self.bar_kinds(),
            "first": first.isoformat() if first else None,
            "last": last.isoformat() if last else None,
        }


def load(
    db: Session,
    *,
    underlying: str = "NIFTY",
    timeframe: str = "5m",
    start: datetime | date | None = None,
    end: datetime | date | None = None,
    staleness_minutes: int = DEFAULT_STALENESS_MINUTES,
) -> ChainStore:
    """Every stored option bar for the window, as a store.

    Loaded whole rather than queried per bar. A backtest issuing one SELECT
    per contract per bar would be slower by orders of magnitude and, worse,
    would put a live database round trip inside the walk — which is exactly
    how a run stops being reproducible.
    """
    stmt = (
        select(OptionCandle, OptionContract)
        .join(OptionContract, OptionCandle.contract_id == OptionContract.id)
        .where(OptionContract.underlying == underlying,
               OptionCandle.timeframe == timeframe)
        .order_by(OptionCandle.timestamp)
    )
    if start is not None:
        stmt = stmt.where(OptionCandle.timestamp >= _window_bound(start))
    if end is not None:
        stmt = stmt.where(OptionCandle.timestamp <= _window_bound(end, end_of_day=True))

    bars: dict[ContractKey, list[OptionBar]] = {}
    contracts: dict[ContractKey, ContractMeta] = {}

    # An archive from before migration 0009 has no capture clocks; read what
    # it has and let those bars fall back to the labelled legacy rule.
    only, _missing = schema.loadable(db, OptionCandle)
    if only:
        stmt = stmt.options(*only)
    has_clocks = not _missing

    # Option bars from a protected prospective holdout session are withheld
    # from strategy access here, like index bars in the repository (2D.1).
    from ..methodology import registry
    rows = db.execute(stmt).all()
    day_of = {}
    if rows:
        stamps = pd.to_datetime([_as_utc(candle.timestamp) for candle, _ in rows], utc=True)
        day_of = dict(zip(range(len(rows)),
                          stamps.tz_convert(IST).date.astype(str), strict=True))
    protected = registry.protected_sessions(db, set(day_of.values())) if rows else set()

    for n, (candle, contract) in enumerate(rows):
        if protected and day_of[n] in protected:
            continue
        key = ContractKey(expiry=contract.expiry_date,
                          strike=float(contract.strike),
                          option_type=contract.option_type)
        contracts.setdefault(key, ContractMeta(
            contract_id=contract.id, key=key, lot_size=contract.lot_size,
            tradingsymbol=contract.tradingsymbol, source=contract.source,
            first_seen=_as_utc(contract.first_seen)))
        bars.setdefault(key, []).append(OptionBar(
            row_id=candle.id,
            contract_id=candle.contract_id,
            key=key,
            timestamp=_as_utc(candle.timestamp),
            open=float(candle.open), high=float(candle.high),
            low=float(candle.low), close=float(candle.close),
            volume=candle.volume, open_interest=candle.open_interest,
            iv=candle.iv, bid=candle.bid, ask=candle.ask,
            underlying_close=candle.underlying_close,
            bar_kind=candle.bar_kind, source=candle.source,
            samples=candle.samples, session_date=candle.session_date,
            available_at=_as_utc(candle.ingested_at) if candle.ingested_at else None,
            exchange_time=(_as_utc(candle.exchange_time)
                           if has_clocks and candle.exchange_time else None),
            capture_time=(_as_utc(candle.capture_time)
                          if has_clocks and candle.capture_time else None),
            first_seen=(_as_utc(candle.first_seen)
                        if has_clocks and candle.first_seen else None),
        ))

    log.info("loaded %d option bars over %d contracts for %s",
             sum(len(v) for v in bars.values()), len(bars), underlying)
    return ChainStore(bars, contracts, underlying=underlying,
                      timeframe=timeframe, staleness_minutes=staleness_minutes)


def _window_bound(value: datetime | date, end_of_day: bool = False) -> datetime:
    if isinstance(value, datetime):
        return value
    moment = datetime(value.year, value.month, value.day, tzinfo=UTC)
    return moment + timedelta(days=1) - timedelta(seconds=1) if end_of_day else moment


def empty_store(underlying: str = "NIFTY", timeframe: str = "5m") -> ChainStore:
    """A store holding nothing. What a modelled-only run reads from."""
    return ChainStore({}, {}, underlying=underlying, timeframe=timeframe)
