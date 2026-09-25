"""Index candles as research may read them.

Two things the plain loader (`repository.load_index_candles`) does not do,
and which research needs:

  **The exchange grid (TC-1).** Every row is checked against the session
  grid and anything off it — a 15:30 bar, an off-boundary stamp, a
  duplicated bar — is quarantined, never snapped. Each kept row carries its
  session's raw verdict in the `session_quality` column; the report also
  travels on `frame.attrs["clock_grid"]`, as diagnostics. The plain loader
  stays as it is for the chart and the live path, which have their own
  normalisation; this is the one research uses, and the two are named
  apart so that nobody mistakes one for the other.

  **As-known reads (TC-6).** `as_known_at` rebuilds each bar as it was
  known at that instant, from `candle_revisions`, instead of the latest
  correction. A replay of a 10:02 decision sees the 100 the source said at
  10:00, not the 101 it said at 10:07.

Without `as_known_at` the series is the latest known one, and every bar
that has been restated is named in `frame.attrs["revisions"]` so a latest
value is never mistaken for the original.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CandleRecord, CandleRevision
from . import repository, schema

LATEST = "latest_known"
AS_KNOWN = "as_known_at"

# Strategy access is the default everywhere below. Seeing protected
# holdout sessions takes a `registry.trusted_access` grant passed as
# `access`; a purpose string is refused (Pass 2D.2).


def _utc(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _bar(record) -> dict:
    return {"timestamp": _utc(record.timestamp), "open": record.open,
            "high": record.high, "low": record.low, "close": record.close,
            "volume": record.volume}


def as_known(db: Session, symbol: str, timeframe: str, as_known_at: datetime,
             start: datetime | date | None = None,
             end: datetime | date | None = None, *,
             access=None) -> tuple[pd.DataFrame, dict]:
    """Every bar exactly as it was known at `as_known_at`.

    For each stored bar, one of three things is true at that instant:

      the current values were already known   → they are used
      an earlier version was the known one    → that version is used
      no version of the bar was known yet     → the bar is absent

    The third case is not an error. A bar first stored after the decision
    did not exist as far as the decision was concerned, and including it
    would be exactly the look-ahead this read exists to rule out.

    A bar restated before revisions were archived has lost its earlier
    values for good. Those are counted under `unrecoverable` and left out
    rather than served with the later values, which they would otherwise be
    silently.
    """
    moment = _utc(as_known_at)
    stmt = select(CandleRecord).where(CandleRecord.symbol == symbol,
                                      CandleRecord.timeframe == timeframe)
    if start is not None:
        stmt = stmt.where(CandleRecord.timestamp >= repository._to_datetime(start))  # noqa: SLF001
    if end is not None:
        stmt = stmt.where(CandleRecord.timestamp
                          <= repository._to_datetime(end, end_of_day=True))  # noqa: SLF001
    current = db.scalars(stmt.order_by(CandleRecord.timestamp)).all()

    history: dict[datetime, list[CandleRevision]] = {}
    archived = schema.has_table(db, CandleRevision.__tablename__)
    if archived:
        for rev in db.scalars(select(CandleRevision).where(
                CandleRevision.symbol == symbol,
                CandleRevision.timeframe == timeframe)):
            history.setdefault(_utc(rev.timestamp), []).append(rev)

    rows: list[dict] = []
    counts = {"current": 0, "earlier_revision": 0, "not_yet_known": 0,
              "unrecoverable": 0}
    for record in current:
        known = _utc(record.ingested_at)
        if known is not None and known <= moment:
            rows.append(_bar(record))
            counts["current"] += 1
            continue
        earlier = [r for r in history.get(_utc(record.timestamp), [])
                   if r.known_from is not None and _utc(r.known_from) <= moment]
        if earlier:
            rows.append(_bar(max(earlier, key=lambda r: r.revision)))
            counts["earlier_revision"] += 1
        elif record.revision and not history.get(_utc(record.timestamp)):
            counts["unrecoverable"] += 1
        else:
            counts["not_yet_known"] += 1

    frame = (pd.DataFrame(rows, columns=repository.CANDLE_COLUMNS) if rows
             else pd.DataFrame(columns=repository.CANDLE_COLUMNS))
    if len(frame):
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    # This reads CandleRecord directly, below the repository, so it applies
    # the holdout boundary itself (Pass 2D.1).
    from ..methodology import registry
    frame = registry.withhold(db, frame, access=access)
    return frame, {"basis": AS_KNOWN, "as_known_at": moment.isoformat(),
                   "revision_history": "archived" if archived else "table_absent",
                   **counts}


def load_research_candles(db: Session, symbol: str = "NIFTY",
                          timeframe: str = "5m", *,
                          start: datetime | date | None = None,
                          end: datetime | date | None = None,
                          as_known_at: datetime | None = None,
                          access=None) -> pd.DataFrame:
    """Grid-validated research candles, latest-known or as-known.

    The database source of `methodology.protection.protect_research_frame`,
    which every research source passes through: sessions the registry
    protects as prospective holdout data are withheld — their bars never
    reach the caller — and named in `attrs["holdout"]`, and every kept row
    carries its session's raw grid verdict. Only a trusted grant as
    `access` sees protected sessions.
    """
    latest = repository.load_index_candles(db, symbol, timeframe,
                                           start=start, end=end, access=access)
    attrs = dict(latest.attrs)

    if as_known_at is not None:
        frame, basis = as_known(db, symbol, timeframe, as_known_at, start, end,
                                access=access)
        if "holdout" in frame.attrs:
            attrs["holdout"] = frame.attrs["holdout"]
    else:
        frame = latest
        restated = db.scalars(
            select(CandleRecord.timestamp).where(
                CandleRecord.symbol == symbol,
                CandleRecord.timeframe == timeframe,
                CandleRecord.revision > 0)).all()
        # A latest value is served, and every bar whose latest value is not
        # its first is named. `archived` says how many of those still have
        # their earlier values to be read back.
        # An archive older than migration 0009 has no revision table at all.
        # Its restated bars then have no recoverable history, which is said
        # rather than implied by an empty set.
        has_history = schema.has_table(db, CandleRevision.__tablename__)
        archived = set(db.scalars(
            select(CandleRevision.timestamp).where(
                CandleRevision.symbol == symbol,
                CandleRevision.timeframe == timeframe)).all()) if has_history else set()
        basis = {"basis": LATEST,
                 "revision_history": "archived" if has_history else "table_absent",
                 "revised_bars": len(restated),
                 "revised_with_history": len({_utc(t) for t in archived}),
                 "revised_without_history": len(
                     {_utc(t) for t in restated} - {_utc(t) for t in archived})}

    # This loader read these bars from the database itself, so it is a raw
    # loader: it certifies their session quality (the only kind of code
    # that may), then passes the result through the shared research
    # protection every source goes through.
    from ..methodology import protection, registry
    certified = protection.certify_raw_frame(
        frame, source=protection.DATABASE, timeframe=timeframe,
        certification=registry.trusted_access(registry.RAW_CERTIFICATION))
    clean = protection.protect_research_frame(
        db, certified, source=protection.DATABASE, timeframe=timeframe, access=access)
    clean.attrs = attrs | clean.attrs
    clean.attrs["revisions"] = basis
    return clean
