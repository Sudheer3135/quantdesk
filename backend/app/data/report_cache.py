"""Keep an expensive report until the data under it changes.

The dashboard asks for the data-quality report and the signal outcome study
once a minute, and both were recomputed from scratch on every request: 1.36s
and 0.47s of CPU, measured on 14-Sep-2026, together the only sustained spike
in the backend. Neither answer can move faster than the archive does, and
the archive gains a bar every five minutes.

So each report is kept against a *data version* — row counts, highest ids
and newest timestamps across the tables they read — which costs about 13ms
to read and changes the moment anything is inserted.

What the version cannot see is an in-place update: both archives are written
by upsert, and the option collector widens the forming bar every sixty
seconds without adding a row or touching an id. So nothing is kept longer
than MAX_AGE_SECONDS either. Five minutes is one bar, the finest grain either
report is expressed in; a report can be at most one bar behind an upsert,
and is never behind an insert.

Anything that reads the clock must not go through here. `quality.report`
keeps its one such check, collector liveness, outside the cache for exactly
that reason — a cached "the collector is fine" would be a silenced alarm.
"""
from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable, Hashable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import CandleRecord, OptionCandle, OptionContract, SignalRecord

MAX_AGE_SECONDS = 300.0

_lock = threading.Lock()
_entries: dict[Hashable, tuple[tuple, float, Any]] = {}


def data_version(db: Session) -> tuple:
    """A value that changes whenever a row is added to what the reports read."""
    def shape(model, stamped: bool) -> tuple:
        columns = [func.count(model.id), func.max(model.id)]
        if stamped:
            columns.append(func.max(model.timestamp))
        return tuple(db.execute(select(*columns)).one())

    return (
        # Which database, so two engines with coincidentally equal counts —
        # as test databases routinely have — can never share an entry.
        str(db.get_bind().url),
        shape(CandleRecord, True),
        shape(OptionCandle, True),
        shape(OptionContract, False),
        shape(SignalRecord, False),
    )


def memoise(key: Hashable, db: Session, compute: Callable[[], Any], *,
            clock: Callable[[], float] = time.monotonic) -> Any:
    """`compute()`, or the kept answer if the data has not moved.

    Hands back a copy either way. A caller that edits what it receives —
    `quality.report` sorts and extends its findings — must not be able to
    reach into the kept value and change the next caller's answer.
    """
    version = data_version(db)
    with _lock:
        kept = _entries.get(key)
        if kept and kept[0] == version and clock() - kept[1] < MAX_AGE_SECONDS:
            return copy.deepcopy(kept[2])

    # Computed outside the lock: a slow report must not block a fast one.
    # Two requests racing on a cold entry both compute, which costs one
    # duplicate report and never a wrong one.
    value = compute()
    with _lock:
        _entries[key] = (version, clock(), value)
    return copy.deepcopy(value)


def clear() -> None:
    with _lock:
        _entries.clear()
