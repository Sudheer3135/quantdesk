"""Is the data fit for research? One report, per session (item 11).

Diagnostic infrastructure, not strategy logic. Every number here is a count
of what the archive holds against what the exchange implies it should hold,
and the verdicts at the bottom are about the *data* — whether a study that
needs a given field has it — never about whether a strategy is any good.

Per session:

  index    expected bars, received, missing, off-grid, duplicate,
           out-of-session (all from `clock_grid`), and bars restated by a
           later import with and without their earlier values archived
  options  option bars and distinct snapshot buckets, quote coverage
           against the session's bar grid, and the share of option bars
           carrying a bid/ask pair, open interest and a capture clock
  metadata the share of contracts traded that session whose lot size is
           verified (see `contract_specs`)

A column this database does not have — an archive from before migration
0009 — is reported as unavailable, never as zero coverage: zero would claim
the field was looked for and not found, which is a different statement.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from types import SimpleNamespace

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CandleRecord, CandleRevision, OptionCandle, OptionContract
from . import clock_grid, contract_specs, schema

READY = "READY"
FAULTY = "FAULTY"
BLOCKED = "BLOCKED_BY_DATA"
UNAVAILABLE = "unavailable"


def _session(stamp) -> date:
    """The IST session of a stored timestamp. SQLite returns them naive UTC."""
    moment = pd.Timestamp(stamp)
    if moment.tzinfo is None:
        moment = moment.tz_localize("UTC")
    return moment.tz_convert(clock_grid.IST).date()


def _pct(part: int, whole: int):
    return round(part / whole * 100, 1) if whole else None


def report(db: Session, symbol: str = "NIFTY", timeframe: str = "5m",
           underlying: str = "NIFTY") -> dict:
    candles = db.execute(
        select(CandleRecord.timestamp, CandleRecord.revision)
        .where(CandleRecord.symbol == symbol, CandleRecord.timeframe == timeframe)
    ).all()
    frame = pd.DataFrame(candles, columns=["timestamp", "revision"])
    grid = clock_grid.validate(frame, timeframe) if len(frame) else \
        clock_grid.GridReport(timeframe=timeframe)

    revised: dict[date, int] = defaultdict(int)
    if len(frame):
        stamps = pd.to_datetime(frame["timestamp"], utc=True)
        for day, rev in zip(stamps.dt.tz_convert(clock_grid.IST).dt.date,
                            frame["revision"], strict=True):
            if rev:
                revised[day] += 1

    archived: dict[date, int] = defaultdict(int)
    has_revisions = schema.has_table(db, CandleRevision.__tablename__)
    if has_revisions:
        for (stamp,) in db.execute(select(CandleRevision.timestamp).where(
                CandleRevision.symbol == symbol,
                CandleRevision.timeframe == timeframe)).all():
            archived[_session(stamp)] += 1

    option_cols = schema.columns(db, OptionCandle.__tablename__) or frozenset()
    has_capture = "capture_time" in option_cols
    fields = [OptionCandle.session_date, OptionCandle.timestamp,
              OptionCandle.contract_id, OptionCandle.bid, OptionCandle.ask,
              OptionCandle.open_interest]
    if has_capture:
        fields.append(OptionCandle.capture_time)
    options: dict[date, list] = defaultdict(list)
    for row in db.execute(select(*fields).join(
            OptionContract, OptionCandle.contract_id == OptionContract.id).where(
            OptionContract.underlying == underlying,
            OptionCandle.timeframe == timeframe)).all():
        if row.session_date is not None:
            options[row.session_date].append(row)

    contracts = {c.id: c for c in db.scalars(select(OptionContract).where(
        OptionContract.underlying == underlying))}

    sessions = []
    grid_by_day = {s.session: s for s in grid.sessions}
    for day in sorted(set(grid_by_day) | {d.isoformat() for d in options}):
        g = grid_by_day.get(day)
        d = date.fromisoformat(day)
        rows = options.get(d, [])
        buckets = {r.timestamp for r in rows}
        expected = len(clock_grid.session_grid(d, timeframe))
        traded = {r.contract_id for r in rows}
        verified = 0
        for cid in traded:
            meta = contracts.get(cid)
            if meta is None:
                continue
            spec = contract_specs.resolve(
                underlying=underlying,
                key=SimpleNamespace(strike=meta.strike, option_type=meta.option_type,
                                    expiry=meta.expiry_date),
                meta=SimpleNamespace(lot_size=meta.lot_size, source=meta.source,
                                     contract_id=meta.id,
                                     tradingsymbol=meta.tradingsymbol),
                on=d, expiry_basis=contract_specs.ARCHIVE_LISTED)
            verified += spec.lot_size_verified
        sessions.append({
            "session": day,
            "expected_bars": expected,
            "received_bars": g.received if g else 0,
            "missing_bars": len(g.missing) if g else expected,
            "off_grid_bars": len(g.off_grid) if g else 0,
            "duplicate_bars": len(g.duplicate) if g else 0,
            "out_of_session_bars": len(g.out_of_session) if g else 0,
            "revised_bars": revised.get(d, 0),
            "revisions_archived": archived.get(d, 0) if has_revisions else UNAVAILABLE,
            "option_bars": len(rows),
            "option_snapshots": len(buckets),
            "quote_coverage_pct": _pct(len(buckets), expected),
            "bid_ask_coverage_pct": _pct(
                sum(1 for r in rows if r.bid is not None and r.ask is not None), len(rows)),
            "oi_coverage_pct": _pct(
                sum(1 for r in rows if r.open_interest is not None), len(rows)),
            "capture_clock_coverage_pct": (
                _pct(sum(1 for r in rows if r.capture_time is not None), len(rows))
                if has_capture else UNAVAILABLE),
            "contract_metadata_coverage_pct": _pct(verified, len(traded)),
            "index_status": READY if (g is not None and g.ok) else FAULTY,
        })

    bid_ask_rows = sum(1 for rows in options.values() for r in rows
                       if r.bid is not None and r.ask is not None)
    option_rows = sum(len(rows) for rows in options.values())
    return {
        "symbol": symbol, "timeframe": timeframe, "underlying": underlying,
        "sessions": sessions,
        "totals": {
            "sessions": len(sessions),
            "index_sessions_faulty": sum(1 for s in sessions if s["index_status"] == FAULTY),
            **{f"{k}_bars": grid.count(v) for k, v in (
                ("missing", clock_grid.MISSING), ("off_grid", clock_grid.OFF_GRID),
                ("duplicate", clock_grid.DUPLICATE),
                ("out_of_session", clock_grid.OUT_OF_SESSION))},
            "revised_bars": sum(revised.values()),
            "revisions_archived": (sum(archived.values()) if has_revisions else UNAVAILABLE),
            "option_bars": option_rows,
            "bid_ask_rows": bid_ask_rows,
        },
        "verdicts": {
            # Index research can proceed on grid-clean sessions; the loader
            # quarantines the rest (`data.research`).
            "index_research": READY if not any(
                s["index_status"] == FAULTY for s in sessions) else
            "READY_WITH_QUARANTINE",
            # Executing against quoted prices needs quotes. With none, option
            # execution research is blocked, not approximated.
            "option_execution": BLOCKED if bid_ask_rows == 0 else READY,
            "option_capture_clocks": (UNAVAILABLE if not has_capture else
                                      READY if all(
                                          s["capture_clock_coverage_pct"] == 100.0
                                          for s in sessions if s["option_bars"])
                                      else "PARTIAL"),
        },
        "schema": {"candle_revisions": has_revisions,
                   "option_capture_clocks": has_capture},
    }
