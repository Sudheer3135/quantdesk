"""Reading history back out of the database.

This is the only read path a backtest is allowed to use. The point of Phase
2 is that a statistic can be traced to specific stored rows, and that stops
being true the moment something calls a broker mid-run: the window silently
becomes "the last 59 days as of whenever you pressed the button", which is a
different dataset every day and reproducible on none of them.

`archiver.load` did most of this already and was called by nothing. The
substance added here is provenance on the way out — the frame knows which
sources it came from and whether its volume is real — and an honest answer
to "do you actually have the window I asked for?".
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..market_hours import trading_date
from ..models import CandleRecord, TradeRecord

log = logging.getLogger(__name__)

CANDLE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]

# The columns a backtest needs but must never be able to confuse with price
# data. Kept on the frame as attributes rather than columns so that
# `indicators.enrich` and every downstream check see exactly the six columns
# they have always seen.
PROVENANCE = "quantdesk_provenance"


@dataclass
class Coverage:
    """What the database actually holds for one symbol and timeframe."""
    symbol: str
    timeframe: str
    rows: int = 0
    sessions: int = 0
    first: datetime | None = None
    last: datetime | None = None
    sources: dict[str, int] = field(default_factory=dict)
    synthetic_volume_rows: int = 0

    @property
    def empty(self) -> bool:
        return self.rows == 0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "rows": self.rows,
            "sessions": self.sessions,
            "first": self.first.isoformat() if self.first else None,
            "last": self.last.isoformat() if self.last else None,
            "sources": self.sources,
            "synthetic_volume_rows": self.synthetic_volume_rows,
            "note": (
                "Nothing archived yet. Run POST /data/import/index to seed it."
                if self.empty else None
            ),
        }


def coverage(db: Session, symbol: str, timeframe: str) -> Coverage:
    """How much history you own. Check this before trusting a backtest."""
    out = Coverage(symbol=symbol, timeframe=timeframe)

    where = (CandleRecord.symbol == symbol, CandleRecord.timeframe == timeframe)
    row = db.execute(
        select(
            func.count(CandleRecord.id),
            func.min(CandleRecord.timestamp),
            func.max(CandleRecord.timestamp),
            func.count(func.distinct(CandleRecord.session_date)),
        ).where(*where)
    ).one()

    out.rows, out.first, out.last, out.sessions = row[0] or 0, row[1], row[2], row[3] or 0
    if out.empty:
        return out

    out.sources = {
        src: count for src, count in db.execute(
            select(CandleRecord.source, func.count(CandleRecord.id))
            .where(*where).group_by(CandleRecord.source)
        ).all()
    }
    out.synthetic_volume_rows = db.scalar(
        select(func.count(CandleRecord.id))
        .where(*where, CandleRecord.volume_is_synthetic.is_(True))
    ) or 0
    return out


@dataclass
class CoverageGap:
    """Why a requested window cannot be served, and what to do about it."""
    symbol: str
    timeframe: str
    requested_start: str | None
    requested_end: str | None
    available: dict
    reason: str

    def to_dict(self) -> dict:
        return {
            "error": "insufficient coverage",
            "reason": self.reason,
            "requested": {"start": self.requested_start, "end": self.requested_end},
            "available": self.available,
            "fix": (
                f"POST /data/import/index?symbol={self.symbol}"
                f"&timeframe={self.timeframe}&days=59"
                "  — then wait for the agent to accumulate more. Free sources "
                "cap intraday history at about 60 days; anything deeper has to "
                "be collected forward."
            ),
        }


def check_coverage(
    db: Session,
    symbol: str,
    timeframe: str,
    start: datetime | date | None = None,
    end: datetime | date | None = None,
    min_sessions: int = 5,
) -> CoverageGap | None:
    """None when the window can be served, a gap description when it cannot.

    Deliberately refuses rather than quietly serving a shorter window. A
    backtest that silently ran on six weeks when you asked for two years is
    the exact failure this phase exists to remove, and it is invisible in
    the statistics — they look like perfectly good statistics.
    """
    # The API hands these in as plain dates, which is how people ask for a
    # window. Comparing a date against a stored timestamp raises, so widen
    # both to the whole day here rather than at every comparison below.
    if start is not None:
        start = _to_datetime(start)
    if end is not None:
        end = _to_datetime(end, end_of_day=True)

    have = coverage(db, symbol, timeframe)
    if have.empty:
        return CoverageGap(symbol, timeframe,
                           start.isoformat() if start else None,
                           end.isoformat() if end else None,
                           have.to_dict(),
                           "no candles stored for this symbol and timeframe")

    if start is not None and have.first is not None:
        first = _as_utc(have.first)
        if first > _as_utc(start):
            return CoverageGap(
                symbol, timeframe, start.isoformat(),
                end.isoformat() if end else None, have.to_dict(),
                f"history begins at {first.isoformat()}, after the requested start")

    if end is not None and have.last is not None:
        last = _as_utc(have.last)
        if last < _as_utc(end):
            return CoverageGap(
                symbol, timeframe,
                start.isoformat() if start else None, end.isoformat(),
                have.to_dict(),
                f"history ends at {last.isoformat()}, before the requested end")

    if have.sessions < min_sessions:
        return CoverageGap(
            symbol, timeframe,
            start.isoformat() if start else None,
            end.isoformat() if end else None, have.to_dict(),
            f"only {have.sessions} session(s) stored; at least {min_sessions} "
            "are needed before any statistic means anything")

    return None


def as_utc(moment: datetime) -> datetime:
    """SQLite hands back naive datetimes. Everything here is stored in UTC.

    Public because the same normalisation is needed anywhere a stored
    timestamp is compared against a real instant, not only inside this
    module. Reading one backend's naive value as though it carried a zone is
    how a query answers differently on Postgres and SQLite.
    """
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


# The previous private name, kept so nothing in-module has to change.
_as_utc = as_utc


def load_index_candles(
    db: Session,
    symbol: str = "NIFTY",
    timeframe: str = "5m",
    start: datetime | date | None = None,
    end: datetime | date | None = None,
    limit: int | None = None,
    sources: Sequence[str] | None = None,
    newest: bool = False,
) -> pd.DataFrame:
    """Candles in the platform's standard shape, oldest first.

    The returned frame carries a `.attrs` entry describing where the rows
    came from. It is deliberately not a column: every indicator and check in
    this codebase expects exactly six columns, and adding a seventh would
    put provenance one careless `df.iloc` away from being treated as price
    data.

    `newest` changes which end `limit` takes from. By default the limit is
    applied to the oldest rows, which is what a backtest wants — the window
    starts where the history starts. A chart being panned backwards wants
    the opposite: the newest rows *below* a cursor. Without this the only
    way to get them is to load the whole archive and discard the front of
    it, which is O(all history) per request and becomes the dominant cost
    the moment a real backfill lands. The frame is still returned
    oldest-first either way.

    `sources` restricts the read to particular vendors. It defaults to None,
    meaning all of them, which is correct while the archive holds one row
    per bar: `uq_candle` is unique on (symbol, timeframe, timestamp), so no
    two sources can currently describe the same bar and an unfiltered read
    cannot double-count. The parameter exists for the day that changes —
    `HistoricalFeed.__init__` raises on duplicate timestamps, so if the
    unique key ever gains `source`, every caller here needs a way to pick
    one vendor before a backtest can run at all.
    """
    stmt = (
        select(CandleRecord)
        .where(CandleRecord.symbol == symbol, CandleRecord.timeframe == timeframe)
        .order_by(CandleRecord.timestamp.desc() if newest
                  else CandleRecord.timestamp)
    )
    if sources:
        stmt = stmt.where(CandleRecord.source.in_(list(sources)))
    if start is not None:
        stmt = stmt.where(CandleRecord.timestamp >= _to_datetime(start))
    if end is not None:
        stmt = stmt.where(CandleRecord.timestamp <= _to_datetime(end, end_of_day=True))
    if limit:
        stmt = stmt.limit(limit)

    rows = db.scalars(stmt).all()
    if newest:
        # Selected newest-first so the database could apply the limit; the
        # contract is oldest-first, so it is restored here.
        rows = list(reversed(rows))
    if not rows:
        empty = pd.DataFrame(columns=CANDLE_COLUMNS)
        empty.attrs[PROVENANCE] = {"rows": 0, "sources": {}, "volume_is_synthetic": False}
        return empty

    df = pd.DataFrame([
        {"timestamp": r.timestamp, "open": r.open, "high": r.high,
         "low": r.low, "close": r.close, "volume": r.volume}
        for r in rows
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    sources: dict[str, int] = {}
    for r in rows:
        sources[r.source] = sources.get(r.source, 0) + 1

    df.attrs[PROVENANCE] = {
        "rows": len(rows),
        "sources": sources,
        # Any synthetic row makes the whole frame's volume untrustworthy:
        # relative volume is a ratio against a rolling window, so one
        # placeholder bar contaminates the twenty around it.
        "volume_is_synthetic": any(r.volume_is_synthetic for r in rows),
        "sessions": len({r.session_date for r in rows if r.session_date}),
    }
    return df


def provenance_of(df: pd.DataFrame) -> dict:
    """The provenance block, or a safe default for a frame from elsewhere."""
    return df.attrs.get(PROVENANCE, {"rows": len(df), "sources": {},
                                     "volume_is_synthetic": False})


def _to_datetime(value: datetime | date, end_of_day: bool = False) -> datetime:
    """Accept a plain date as the whole day, which is how people ask for one."""
    if isinstance(value, datetime):
        return value
    stamp = pd.Timestamp(value, tz="UTC")
    if end_of_day:
        stamp = stamp + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    return stamp.to_pydatetime()


# --------------------------------------------------------------------------
# the trade journal, read back for risk
# --------------------------------------------------------------------------

# How far back to scan for today's trades. The journal records a handful of
# rows a day, so this covers well over a year while keeping the query bounded.
TRADE_SCAN_LIMIT = 500


def todays_trades(db: Session, day: date | None = None,
                  limit: int = TRADE_SCAN_LIMIT) -> list[TradeRecord]:
    """Every trade opened on this *trading* day, newest scan first.

    The day is an IST trading date, not a UTC calendar date, because that is
    what a daily trade cap means to the person the cap protects.

    Filtering happens in Python rather than SQL on purpose. `created_at` is
    stored as UTC but SQLite hands it back naive, so a SQL comparison against
    a timezone-aware bound is correct on Postgres and quietly wrong on the
    backend the tests run against. Normalising on read is dialect-proof, and
    at a few rows a day the scan is free.
    """
    day = day or trading_date()
    rows = db.scalars(
        select(TradeRecord).order_by(TradeRecord.created_at.desc()).limit(limit)
    ).all()
    return [r for r in rows
            if r.created_at is not None and trading_date(_as_utc(r.created_at)) == day]


def open_trades(db: Session) -> list[TradeRecord]:
    """Every trade still open, whatever day it was opened on.

    Not filtered by date: a position carried overnight still occupies a slot
    this morning, and counting only today's would let it be ignored on the
    one day it matters most.
    """
    return list(db.scalars(select(TradeRecord).where(TradeRecord.status == "open")).all())
