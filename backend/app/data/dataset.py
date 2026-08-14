"""Dataset fingerprinting — naming the exact data a backtest read.

A backtest result without one of these is an anecdote. Three weeks later the
same command returns a different Sharpe and there is no way to tell whether
you improved the strategy, whether the archive grew by four sessions,
whether a source restated a handful of bars, or whether you simply ran it on
a different day.

The hash makes that question answerable: identical data collides by design,
and one changed tick produces a different hash.

**What is hashed, and what is not.** The hash covers the symbol, the
timeframe, and the price content of every row. It deliberately excludes
`source`, `ingested_at` and `revision`. The question it answers is "were the
numbers the same?", which is the question you have when a result moves —
relabelling a row's source from `unknown` to `free` changes provenance
without changing a single price, and should not invalidate the comparison.
Provenance is recorded alongside the hash instead, so nothing is lost.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import DatasetVersion
from .repository import provenance_of

log = logging.getLogger(__name__)

# Prices are rounded before hashing. Without this, a value that survives a
# float round trip as 24000.000000000004 in one run and 24000.0 in another —
# which happens across pandas and driver versions — would produce two
# different hashes for identical data, and the mechanism would be worse than
# useless because it would cry wolf.
PRICE_DECIMALS = 4
VOLUME_DECIMALS = 2

HASH_VERSION = "1"


@dataclass
class Fingerprint:
    hash: str
    symbol: str
    timeframe: str
    first_ts: str | None
    last_ts: str | None
    row_count: int
    session_count: int
    sources: dict[str, int] = field(default_factory=dict)
    volume_is_synthetic: bool = False
    caveats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _canonical_rows(df: pd.DataFrame) -> list[str]:
    ts = pd.to_datetime(df["timestamp"], utc=True)
    return [
        "|".join((
            t.isoformat(),
            f"{o:.{PRICE_DECIMALS}f}", f"{h:.{PRICE_DECIMALS}f}",
            f"{low:.{PRICE_DECIMALS}f}", f"{c:.{PRICE_DECIMALS}f}",
            f"{v:.{VOLUME_DECIMALS}f}",
        ))
        for t, o, h, low, c, v in zip(
            ts, df["open"], df["high"], df["low"], df["close"], df["volume"],
            strict=True)
    ]


def fingerprint(df: pd.DataFrame, symbol: str, timeframe: str) -> Fingerprint:
    """Content-address a candle frame.

    Rows are sorted by timestamp first, so a frame that arrives in a
    different order but holds the same bars hashes the same. That is the
    correct behaviour: row order is an artefact of the query, not a property
    of the data.
    """
    provenance = provenance_of(df)
    caveats: list[str] = []

    if df is None or len(df) == 0:
        return Fingerprint(
            hash=hashlib.sha256(f"{HASH_VERSION}|{symbol}|{timeframe}|empty"
                                .encode()).hexdigest(),
            symbol=symbol, timeframe=timeframe, first_ts=None, last_ts=None,
            row_count=0, session_count=0,
            caveats=["The dataset is empty."],
        )

    ordered = df.sort_values("timestamp").reset_index(drop=True)
    digest = hashlib.sha256()
    digest.update(f"{HASH_VERSION}|{symbol}|{timeframe}\n".encode())
    for line in _canonical_rows(ordered):
        digest.update(line.encode())
        digest.update(b"\n")

    ts = pd.to_datetime(ordered["timestamp"], utc=True)
    sessions = provenance.get("sessions") or len(
        ts.dt.tz_convert("Asia/Kolkata").dt.date.unique())

    if provenance.get("volume_is_synthetic"):
        caveats.append(
            "Volume is a constant placeholder, not traded volume. The volume "
            "check contributes nothing to any signal in this run."
        )
    if "mock" in provenance.get("sources", {}):
        caveats.append(
            "Some rows came from the mock broker — a random walk. Any "
            "statistic computed from this dataset describes noise."
        )
    if sessions < 20:
        caveats.append(
            f"Only {sessions} sessions. Statistics over a sample this small "
            "are not evidence of an edge."
        )

    return Fingerprint(
        hash=digest.hexdigest(),
        symbol=symbol,
        timeframe=timeframe,
        first_ts=ts.iloc[0].isoformat(),
        last_ts=ts.iloc[-1].isoformat(),
        row_count=len(ordered),
        session_count=int(sessions),
        sources=provenance.get("sources", {}),
        volume_is_synthetic=bool(provenance.get("volume_is_synthetic")),
        caveats=caveats,
    )


def register(db: Session, print_: Fingerprint) -> DatasetVersion:
    """Record the fingerprint, or return the existing row for that hash.

    Registering is idempotent for the same reason the importer is: a hash
    that already exists describes the same data by construction, so there is
    nothing to update and re-running a backtest should not grow a table.
    """
    existing = db.scalars(
        select(DatasetVersion).where(DatasetVersion.hash == print_.hash)
    ).first()
    if existing:
        return existing

    version = DatasetVersion(
        hash=print_.hash,
        symbol=print_.symbol,
        timeframe=print_.timeframe,
        first_ts=datetime.fromisoformat(print_.first_ts) if print_.first_ts
        else datetime.now(UTC),
        last_ts=datetime.fromisoformat(print_.last_ts) if print_.last_ts
        else datetime.now(UTC),
        row_count=print_.row_count,
        session_count=print_.session_count,
        source_mix=print_.sources,
        volume_is_synthetic=print_.volume_is_synthetic,
        created_at=datetime.now(UTC),
    )
    db.add(version)
    db.commit()
    log.info("registered dataset %s (%s rows)", print_.hash[:12], print_.row_count)
    return version


def recent(db: Session, limit: int = 25) -> list[dict]:
    rows = db.scalars(
        select(DatasetVersion).order_by(DatasetVersion.created_at.desc()).limit(limit)
    ).all()
    return [
        {
            "hash": r.hash,
            "symbol": r.symbol,
            "timeframe": r.timeframe,
            "first_ts": r.first_ts.isoformat() if r.first_ts else None,
            "last_ts": r.last_ts.isoformat() if r.last_ts else None,
            "rows": r.row_count,
            "sessions": r.session_count,
            "sources": r.source_mix,
            "volume_is_synthetic": r.volume_is_synthetic,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
