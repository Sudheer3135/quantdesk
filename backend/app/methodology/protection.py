"""Raw certification and research protection — two boundaries, never one.

**Raw certification** (`certify_raw_frame`) is the only step that creates a
CLEAN or FAULTY session verdict. It grades *raw observations* against the
Pass 2C exchange grid, quarantines off-grid rows and stamps each surviving
row with its session's raw verdict and source. It demands a raw-
certification grant, which `registry.trusted_access` issues only to the
raw loaders (the database research loader and the broker backtest pull),
and it refuses any frame that already carries processing marks.

**Research protection** (`protect_research_frame`) is what every source —
database, broker, import, in-memory, cache, anything later — passes before
research or strategy code receives it:

    canonical session identification   IST trading date per bar
    registry protection                protected holdout sessions withheld
    session-quality protection         recorded verdicts kept exactly; a row
                                       without one becomes QUALITY_UNKNOWN

It never grades. Rows that look valid prove nothing about a session whose
bad bar was quarantined upstream, so a processed frame whose provenance
was lost stays QUALITY_UNKNOWN however many times it passes through here
(Pass 2D.3). Folds, holdout counting, the MTM ledger and readiness score
only CLEAN.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
from sqlalchemy.orm import Session

from ..data import clock_grid
from . import registry

DATABASE = "database"
BROKER = "broker"
MEMORY = "in_memory"

# Signs a frame has been through research processing already. Raw
# certification refuses such a frame rather than re-grading what survived.
PROCESSED_MARKS = ("clock_grid", "research_boundary", "quarantined")


def _canonical(frame: pd.DataFrame, source: str) -> pd.DataFrame:
    candles = frame.copy()
    candles.attrs = dict(frame.attrs)
    if len(candles):
        # Every bar is a UTC instant and its session is its IST trading
        # date. A naive stamp is ambiguous and refused rather than guessed.
        stamps = pd.to_datetime(candles["timestamp"])
        if stamps.dt.tz is None:
            raise ValueError(f"source {source!r} produced timestamps without a timezone")
        candles["timestamp"] = stamps.dt.tz_convert("UTC")
    return candles


def certify_raw_frame(raw: pd.DataFrame, *, source: str, certification,
                      timeframe: str = "5m", as_of: datetime | None = None
                      ) -> pd.DataFrame:
    """Grade a raw loader's own observations. The only source of CLEAN/FAULTY.

    `certification` must be a raw-certification grant — held only by the
    raw loaders, never obtainable by a caller with a string or a flag. A
    frame carrying a quality record or processing marks is not raw and is
    refused: re-grading it could turn a laundered session clean.
    """
    if registry.access_purpose(certification) != registry.RAW_CERTIFICATION:
        raise registry.HoldoutAccessDenied(
            "raw certification needs a raw-certification grant held by a raw loader")
    if raw is None or not source:
        raise ValueError("raw certification needs a frame and its source")
    marked = [m for m in PROCESSED_MARKS if m in raw.attrs]
    if clock_grid.QUALITY_COLUMN in raw.columns or marked:
        raise ValueError(f"source {source!r} handed a processed frame to raw "
                         f"certification ({marked or [clock_grid.QUALITY_COLUMN]}); "
                         "only untouched raw observations can be certified")
    candles = _canonical(raw, source)
    clean, held, _report = clock_grid._certify_raw_observations(  # noqa: SLF001
        candles, timeframe, source=source, as_of=as_of)
    clean.attrs["quarantined"] = [
        str(t) for t in pd.to_datetime(held["timestamp"], utc=True)][:50] if len(held) else []
    clean.attrs["raw_certification"] = {"source": source, "quarantined_rows": len(held)}
    return clean


def protect_research_frame(db: Session, frame: pd.DataFrame, *, source: str,
                           timeframe: str = "5m", access=None) -> pd.DataFrame:
    """Candles from any source, made safe for research. Never grades.

    Holdout sessions are withheld unless `access` is a trusted grant that
    may see them. Quality records are carried exactly as certified; a row
    without one is marked QUALITY_UNKNOWN, and unknown is never promoted.
    """
    if frame is None:
        raise ValueError(f"source {source!r} produced no frame")
    if not source:
        raise ValueError("every research frame names its source")
    registry.access_purpose(access)                 # a spoofed grant fails here
    candles = _canonical(frame, source)
    withheld = registry.withhold(db, candles, access=access)
    protected = clock_grid.mark_unknown(withheld)
    protected.attrs["research_boundary"] = {
        "source": source,
        "access": registry.access_purpose(access),
        "timeframe": timeframe,
        "withheld_sessions": (withheld.attrs.get("holdout") or {}).get(
            "withheld_sessions", []),
    }
    return protected
