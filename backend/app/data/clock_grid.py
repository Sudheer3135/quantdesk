"""The exchange clock grid for NIFTY intraday candles (TC-1).

The one authoritative answer to "is this bar where the exchange says a bar
should be?". Row counts could not answer it. A session missing its 09:20 bar
and carrying an extra 15:30 bar has exactly the 75 rows a full session
should, and every check that counted rows passed it — while a backtest over
it traded a bar that never opened and skipped one that did.

So each session is compared bar by bar against the grid the declared
session implies, and five different faults are reported separately because
they have different causes and different remedies:

  missing         an expected bar is absent. The feed dropped it.
  off_grid        a bar whose timestamp is not on a bar boundary at all —
                  09:17, or 09:20:30. A clock or aggregation fault.
  duplicate       two rows claim the same bar. Which one is true is not
                  knowable from the rows.
  out_of_session  on a boundary but outside the session — the 15:30 bar
                  that opens after the close, a pre-open bar, a bar on a
                  weekend or an exchange holiday.
  incomplete      the session is missing at least one expected bar.

Malformed rows are **quarantined**, never snapped. Moving 09:17 onto 09:15
would manufacture a bar the exchange never printed, at a price it never
traded at that time, and after the move nothing would show it had been
done. A research series either has the exchange's bar or it has a
recorded hole.

This is the research path. `analytics.indicators.drop_outside_session` is
the separate, older live/UI normalisation — it keeps a 15:30 bar because
the chart renders one — and it is deliberately left alone. The two are not
interchangeable, which is the point of naming this one.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

import pandas as pd

from .. import market_hours

IST = "Asia/Kolkata"

# Bar lengths the grid is defined for. A timeframe outside this table has no
# declared grid and is refused rather than approximated.
GRID_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30}

MISSING = "missing"
OFF_GRID = "off_grid"
DUPLICATE = "duplicate"
OUT_OF_SESSION = "out_of_session"
INCOMPLETE = "incomplete"
FAULTS = (MISSING, OFF_GRID, DUPLICATE, OUT_OF_SESSION, INCOMPLETE)


def _minutes(timeframe: str) -> int:
    if timeframe not in GRID_MINUTES:
        raise ValueError(f"no declared exchange grid for timeframe {timeframe!r}")
    return GRID_MINUTES[timeframe]


def session_grid(day: date, timeframe: str = "5m") -> list[pd.Timestamp]:
    """Every bar open the exchange's session implies for `day`, in UTC.

    09:15 up to the last bar that *closes* by 15:30. For 5m that is 09:15
    through 15:25 — 75 bars. A bar opening at 15:30 would close at 15:35,
    after the market, so it is not on the grid. Empty on a non-trading day.
    """
    minutes = _minutes(timeframe)
    if not market_hours.is_trading_date(day):
        return []
    open_ = pd.Timestamp(datetime.combine(day, market_hours.MARKET_OPEN), tz=IST)
    close = pd.Timestamp(datetime.combine(day, market_hours.MARKET_CLOSE), tz=IST)
    step = pd.Timedelta(minutes=minutes)
    stamps = []
    moment = open_
    while moment + step <= close:
        stamps.append(moment.tz_convert("UTC"))
        moment += step
    return stamps


def _on_boundary(stamp: pd.Timestamp, minutes: int) -> bool:
    """Is this a bar boundary at all, independent of the session?"""
    local = stamp.tz_convert(IST)
    if local.second or local.microsecond or local.nanosecond:
        return False
    # Boundaries are measured from the session open, not from the hour, so
    # a 15m grid is 09:15, 09:30 … rather than 09:00, 09:15 ….
    since_open = (local.hour * 60 + local.minute) - (
        market_hours.MARKET_OPEN.hour * 60 + market_hours.MARKET_OPEN.minute)
    return since_open % minutes == 0


@dataclass
class SessionGrid:
    """One session measured against its grid."""
    session: str
    expected: int
    received: int
    missing: list[str] = field(default_factory=list)
    off_grid: list[str] = field(default_factory=list)
    duplicate: list[str] = field(default_factory=list)
    out_of_session: list[str] = field(default_factory=list)
    # True when the session is still being traded. Its unfinished tail is
    # left out of `missing` — a bar that has not closed yet is the future,
    # not a gap — but a bar that should already exist and does not is
    # missing whether or not the session is over.
    in_progress: bool = False

    @property
    def incomplete(self) -> bool:
        return bool(self.missing)

    @property
    def ok(self) -> bool:
        return not (self.incomplete or self.off_grid or self.duplicate
                    or self.out_of_session)

    def faults(self) -> list[str]:
        found = [name for name in (MISSING, OFF_GRID, DUPLICATE, OUT_OF_SESSION)
                 if getattr(self, name)]
        if self.incomplete:
            found.append(INCOMPLETE)
        return found

    def to_dict(self) -> dict:
        out = asdict(self)
        out["incomplete"] = self.incomplete
        out["ok"] = self.ok
        out["faults"] = self.faults()
        return out


@dataclass
class GridReport:
    """Every session, and the totals a reader checks first."""
    timeframe: str
    sessions: list[SessionGrid] = field(default_factory=list)

    def count(self, fault: str) -> int:
        if fault == INCOMPLETE:
            return sum(1 for s in self.sessions if s.incomplete)
        return sum(len(getattr(s, fault)) for s in self.sessions)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.sessions)

    def to_dict(self) -> dict:
        return {
            "timeframe": self.timeframe,
            "ok": self.ok,
            "sessions": len(self.sessions),
            "totals": {fault: self.count(fault) for fault in FAULTS},
            "faulty_sessions": [s.to_dict() for s in self.sessions if not s.ok],
        }


def _stamps(frame: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(frame["timestamp"], utc=True)


def validate(frame: pd.DataFrame, timeframe: str = "5m", *,
             as_of: datetime | None = None,
             sessions: Iterable[date] | None = None) -> GridReport:
    """Measure every session in `frame` against the exchange grid.

    `sessions` adds days that must be checked even if the frame holds no
    row for them — a wholly absent trading day is 75 missing bars, not
    silence. `as_of` marks a session still in progress so its unfinished
    tail is not reported as missing.
    """
    minutes = _minutes(timeframe)
    report = GridReport(timeframe=timeframe)
    stamps = _stamps(frame) if len(frame) else pd.Series([], dtype="datetime64[ns, UTC]")
    local_days = stamps.dt.tz_convert(IST).dt.date if len(stamps) else pd.Series([], dtype=object)

    days = set(local_days) | set(sessions or [])
    cutoff = pd.Timestamp(as_of) if as_of is not None else None

    for day in sorted(days):
        grid = session_grid(day, timeframe)
        grid_set = set(grid)
        todays = stamps[local_days == day] if len(stamps) else stamps

        counts = todays.value_counts()
        entry = SessionGrid(session=day.isoformat(), expected=len(grid),
                            received=int(len(todays)))

        for stamp, n in sorted(counts.items()):
            label = stamp.tz_convert(IST).isoformat()
            if n > 1:
                entry.duplicate.append(label)
            if not _on_boundary(stamp, minutes):
                entry.off_grid.append(label)
            elif stamp not in grid_set:
                entry.out_of_session.append(label)

        present = set(counts.index)
        for stamp in grid:
            if stamp in present:
                continue
            # A bar that has not closed yet is not missing; it is the future.
            if cutoff is not None and stamp + pd.Timedelta(minutes=minutes) > cutoff:
                entry.in_progress = True
                continue
            entry.missing.append(stamp.tz_convert(IST).isoformat())

        report.sessions.append(entry)
    return report


def quarantine(frame: pd.DataFrame, timeframe: str = "5m", *,
               as_of: datetime | None = None
               ) -> tuple[pd.DataFrame, pd.DataFrame, GridReport]:
    """Split a frame into research-usable rows and quarantined ones.

    Kept: exactly the rows on the grid, once each. Quarantined, whole and
    unmodified: off-grid rows, out-of-session rows, and *every* copy of a
    duplicated bar — two rows that disagree about one bar cannot be
    resolved by keeping whichever arrived first.

    Missing bars stay missing. Nothing is interpolated, forward-filled or
    snapped; the report says where the holes are.
    """
    minutes = _minutes(timeframe)
    report = validate(frame, timeframe, as_of=as_of)
    if frame.empty:
        return frame.copy(), frame.copy(), report

    stamps = _stamps(frame)
    days = stamps.dt.tz_convert(IST).dt.date
    duplicated = stamps.duplicated(keep=False)
    grids: dict[date, set] = {}
    for day in set(days):
        grids[day] = set(session_grid(day, timeframe))
    on_grid = pd.Series(
        [s in grids[d] for s, d in zip(stamps, days, strict=True)],
        index=frame.index)

    keep = on_grid & ~duplicated
    clean = frame[keep].reset_index(drop=True)
    held = frame[~keep].reset_index(drop=True)
    clean.attrs = dict(frame.attrs)
    clean.attrs["clock_grid"] = report.to_dict() | {
        "quarantined_rows": int((~keep).sum()),
        "policy": "quarantine_never_snap",
    }
    return clean, held, report
