"""Candle archiver.

Free sources cap intraday lookback at about 60 days. This is how you get
around that without paying: store every candle you fetch, and after a few
months you own a history that no provider can revoke.

Writes are idempotent — a unique constraint on (symbol, timeframe,
timestamp) means re-fetching an overlapping window updates rows instead of
duplicating them.
"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..analytics.indicators import (
    drop_future,
    drop_outside_session,
    drop_unclosed,
)
from ..models import CandleRecord

log = logging.getLogger(__name__)

# Rows per INSERT. Nine columns each, so 500 rows is ~4,500 bind parameters —
# comfortably inside Postgres's 65535 limit with room for wider tables later.
CHUNK_SIZE = 500


def archive(db: Session, df: pd.DataFrame, symbol: str, timeframe: str,
            source: str, skip_unclosed: bool = True) -> int:
    # `source` is deliberately required. It used to default to "free", so a
    # caller that forgot to pass it labelled mock candles as real data — and
    # the one column that distinguished good rows from bad became useless
    # exactly when it was needed. A missing argument is now a TypeError at
    # import time rather than corruption discovered weeks later.
    """Store candles, updating any that already exist. Returns rows written.

    Two things this has to survive that a naive bulk insert does not:

    1. Postgres caps a statement at 65535 bind parameters. At nine columns
       per row a 59-day backfill of 5-minute candles is close enough to that
       ceiling to be worth chunking, and a single enormous VALUES clause is
       slow to compile and heavy on memory besides.

    2. ON CONFLICT DO UPDATE raises "cannot affect row a second time" if the
       same key appears twice inside one statement. Yahoo occasionally
       returns a repeated timestamp across a session boundary, so the batch
       must be de-duplicated before it is sent, not just relied on to
       conflict cleanly against what is already stored.
    """
    if df.empty:
        return 0

    if skip_unclosed:
        df = drop_unclosed(df, timeframe)
        df = drop_outside_session(df)
        df = drop_future(df)
        if df.empty:
            return 0

    rows: dict[datetime, dict] = {}
    for r in df.itertuples():
        ts = r.timestamp.to_pydatetime()
        # Last value wins — a later fetch of the same bar is the fresher one.
        rows[ts] = {
            "symbol": symbol, "timeframe": timeframe, "source": source,
            "timestamp": ts,
            "open": float(r.open), "high": float(r.high),
            "low": float(r.low), "close": float(r.close),
            "volume": float(r.volume),
        }

    batch = list(rows.values())
    dropped = len(df) - len(batch)
    if dropped:
        log.info("dropped %s duplicate timestamps before insert", dropped)

    written = 0
    for start in range(0, len(batch), CHUNK_SIZE):
        chunk = batch[start : start + CHUNK_SIZE]
        stmt = pg_insert(CandleRecord).values(chunk)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_candle",
            set_={
                "open": stmt.excluded.open, "high": stmt.excluded.high,
                "low": stmt.excluded.low, "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
            },
        )
        try:
            db.execute(stmt)
            db.commit()
            written += len(chunk)
        except Exception:
            # Commit per chunk so one bad batch does not discard the rest.
            db.rollback()
            log.exception("chunk starting at %s failed; continuing", start)

    log.info("archived %s of %s %s %s candles", written, len(batch), symbol, timeframe)
    return written


def load(db: Session, symbol: str, timeframe: str,
         start: datetime | None = None, end: datetime | None = None,
         limit: int | None = None) -> pd.DataFrame:
    """Read archived candles back out in the platform's candle shape.

    Backtests should read from here, not from a live API — it is faster,
    it is reproducible, and it reaches further back than the free tier.
    """
    stmt = (
        select(CandleRecord)
        .where(CandleRecord.symbol == symbol, CandleRecord.timeframe == timeframe)
        .order_by(CandleRecord.timestamp)
    )
    if start:
        stmt = stmt.where(CandleRecord.timestamp >= start)
    if end:
        stmt = stmt.where(CandleRecord.timestamp <= end)
    if limit:
        stmt = stmt.limit(limit)

    rows = db.scalars(stmt).all()
    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame([
        {"timestamp": r.timestamp, "open": r.open, "high": r.high,
         "low": r.low, "close": r.close, "volume": r.volume}
        for r in rows
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def coverage(db: Session, symbol: str, timeframe: str) -> dict:
    """How much history do you actually own? Check this before backtesting."""
    rows = db.scalars(
        select(CandleRecord)
        .where(CandleRecord.symbol == symbol, CandleRecord.timeframe == timeframe)
        .order_by(CandleRecord.timestamp)
    ).all()
    if not rows:
        return {"symbol": symbol, "timeframe": timeframe, "candles": 0,
                "note": "Nothing archived yet. Run the agent during market hours."}

    first, last = rows[0].timestamp, rows[-1].timestamp
    sessions = len({r.timestamp.date() for r in rows})
    return {
        "symbol": symbol, "timeframe": timeframe,
        "candles": len(rows),
        "sessions": sessions,
        "first": first.isoformat(),
        "last": last.isoformat(),
        "span_days": (last - first).days,
    }