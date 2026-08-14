"""Compatibility shim for the old archiver.

The write path moved to `app.data.importer` and the read path to
`app.data.repository`, where they gained rejection accounting, provenance
columns, a dialect-portable upsert and a holiday-aware validation gate.

This module stays because `scripts/doctor.py` and any local scripts still
import it, and because deleting a working entry point to make a diagram
tidier is a poor trade. It delegates — it does not reimplement. Two ways of
writing one table means two sets of guards to keep in step, and the one that
gets forgotten is the one that corrupts the archive.

New code should import from `app.data` directly.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
from sqlalchemy.orm import Session

from ..data.importer import import_index_candles
from ..data.repository import coverage as _coverage
from ..data.repository import load_index_candles


def archive(db: Session, df: pd.DataFrame, symbol: str, timeframe: str,
            source: str, skip_unclosed: bool = True) -> int:
    """Store candles, updating any that already exist. Returns rows written.

    `source` remains a required positional argument. It defaulted to "free"
    once, and a caller that forgot it labelled thousands of mock candles as
    real data — the one column separating trustworthy rows from junk became
    useless exactly when it mattered.

    `skip_unclosed` is accepted and ignored. Validation is no longer
    optional: that flag was a single switch that turned off every guard at
    once, and there was never a good reason to reach for it.
    """
    return import_index_candles(db, df, symbol, timeframe, source).write.written


def load(db: Session, symbol: str, timeframe: str,
         start: datetime | None = None, end: datetime | None = None,
         limit: int | None = None) -> pd.DataFrame:
    """Read archived candles back out in the platform's candle shape."""
    return load_index_candles(db, symbol, timeframe, start=start, end=end, limit=limit)


def coverage(db: Session, symbol: str, timeframe: str) -> dict:
    """How much history do you actually own? Check this before backtesting."""
    return _coverage(db, symbol, timeframe).to_dict()
