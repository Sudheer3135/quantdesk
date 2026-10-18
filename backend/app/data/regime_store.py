"""Reading and writing the regime history.

`analytics.regime` knows how to classify a candle frame and nothing about a
database; this is the other half. It exists so that splitting an outcome
study by regime is a join rather than a recomputation of the whole archive
on every request.

The one thing worth understanding here is why an incremental update is
allowed at all. Classification is causal but not memoryless: ATR is a
Wilder average, VWAP is anchored to the session open, the ATR baseline looks
back a hundred bars, and the gap needs the previous session's close. So
classifying "just the newest bar" in isolation would produce a different
answer from classifying it inside the full archive, and the table would end
up holding two subtly different definitions of the same label.

`INCREMENTAL_LOOKBACK_BARS` is the fix, and it is chosen rather than guessed:
ATR(14) is an EWM with alpha 1/14, so after 750 bars the influence of the
starting value is (13/14)^750, which is about 1e-24 — below floating-point
resolution. Ten sessions also comfortably contains the current session's
VWAP anchor, its opening range, and the previous session's close. Within
that window an incremental classification and a full backfill agree, and
`test_regime_store` asserts exactly that rather than taking my word for it.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..analytics import regime as regime_engine
from ..models import MarketRegime, utc_now
from . import repository
from .upsert import upsert

log = logging.getLogger(__name__)

CONFLICT = ("symbol", "timeframe", "timestamp")
UPDATABLE = ("session_date", "day_regime", "day_confidence", "day_reasons",
             "hour_regime", "hour_confidence", "hour_reasons", "features",
             "engine_version", "computed_at")

# See the module docstring. Long enough that an incremental pass and a full
# backfill produce the same numbers.
INCREMENTAL_LOOKBACK_BARS = 750


@dataclass
class BackfillReport:
    """What a classification pass did. `engine_version` is in here because a
    backfill that rewrote every row with a new classifier and a backfill that
    added forty new bars are otherwise indistinguishable in a log."""
    symbol: str
    timeframe: str
    engine_version: str = regime_engine.ENGINE_VERSION
    candles: int = 0
    classified: int = 0
    inserted: int = 0
    updated: int = 0
    failed: int = 0
    sessions: int = 0
    first: str | None = None
    last: str | None = None
    day_labels: dict[str, int] = field(default_factory=dict)
    hour_labels: dict[str, int] = field(default_factory=dict)
    note: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _rows(symbol: str, timeframe: str, verdicts: pd.DataFrame) -> list[dict]:
    """Turn classified bars into rows the upsert can take."""
    stamped = utc_now()
    out = []
    for _, row in verdicts.iterrows():
        day, hour = row["day"], row["hour"]
        out.append({
            "symbol": symbol,
            "timeframe": timeframe,
            "timestamp": pd.Timestamp(row["timestamp"]).to_pydatetime(),
            "session_date": row["session_date"],
            "day_regime": day.label,
            "day_confidence": day.confidence,
            "day_reasons": day.reasons,
            "hour_regime": hour.label,
            "hour_confidence": hour.confidence,
            "hour_reasons": hour.reasons,
            # Both feature sets on one row, namespaced. A single flat dict
            # would silently overwrite `efficiency` with whichever level was
            # written last, and the stored justification would then contradict
            # the stored reasons.
            "features": {"day": day.features, "hour": hour.features,
                         "day_scores": day.scores, "hour_scores": hour.scores,
                         "day_provisional": day.provisional,
                         "hour_provisional": hour.provisional},
            "engine_version": regime_engine.ENGINE_VERSION,
            "computed_at": stamped,
        })
    return out


def classify_and_store(db: Session, candles: pd.DataFrame, symbol: str,
                       timeframe: str, keep_last: int | None = None) -> BackfillReport:
    """Classify a candle frame and write the verdicts.

    `keep_last` stores only the final N bars while still classifying the
    whole frame. That is how an incremental pass gets full-history context
    without rewriting rows that have not changed.
    """
    report = BackfillReport(symbol=symbol, timeframe=timeframe,
                            candles=int(len(candles)))
    if candles.empty:
        report.note = ("No candles stored for this symbol and timeframe — "
                       "nothing to classify.")
        return report

    verdicts = regime_engine.classify_frame(candles)
    if keep_last is not None:
        verdicts = verdicts.tail(keep_last)
    report.classified = int(len(verdicts))
    if verdicts.empty:
        return report

    result = upsert(db, MarketRegime, _rows(symbol, timeframe, verdicts),
                    conflict_columns=CONFLICT, update_columns=UPDATABLE)
    report.inserted, report.updated = result.inserted, result.updated
    report.failed = result.failed

    report.sessions = int(verdicts["session_date"].nunique())
    report.first = pd.Timestamp(verdicts["timestamp"].iloc[0]).isoformat()
    report.last = pd.Timestamp(verdicts["timestamp"].iloc[-1]).isoformat()
    report.day_labels = (verdicts["day"].map(lambda v: v.label)
                         .value_counts().to_dict())
    report.hour_labels = (verdicts["hour"].map(lambda v: v.label)
                          .value_counts().to_dict())
    return report


def backfill(db: Session, symbol: str = "NIFTY",
             timeframe: str = "5m") -> BackfillReport:
    """Classify every stored candle. Safe to re-run; it upserts."""
    candles = repository.load_index_candles(db, symbol, timeframe)
    log.info("classifying %s stored %s %s candles", len(candles), symbol, timeframe)
    return classify_and_store(db, candles, symbol, timeframe)


def refresh_recent(db: Session, symbol: str = "NIFTY", timeframe: str = "5m",
                   lookback: int = INCREMENTAL_LOOKBACK_BARS,
                   keep_last: int = 24) -> BackfillReport:
    """Keep the table current without re-classifying the whole archive.

    Called from the agent tick after new candles are archived. Classifies a
    lookback window so the indicators carry their history, then stores only
    the tail — the older bars in the window are already on record with
    identical values.
    """
    candles = repository.load_index_candles(db, symbol, timeframe)
    if candles.empty:
        return BackfillReport(symbol=symbol, timeframe=timeframe,
                              note="No candles stored yet.")
    return classify_and_store(db, candles.tail(lookback).reset_index(drop=True),
                              symbol, timeframe, keep_last=keep_last)


# --------------------------------------------------------------------------
# reading it back
# --------------------------------------------------------------------------

def coverage(db: Session, symbol: str = "NIFTY", timeframe: str = "5m") -> dict:
    """How much of the archive has been classified, and by which version."""
    where = (MarketRegime.symbol == symbol, MarketRegime.timeframe == timeframe)
    rows, first, last, sessions = db.execute(
        select(func.count(MarketRegime.id), func.min(MarketRegime.timestamp),
               func.max(MarketRegime.timestamp),
               func.count(func.distinct(MarketRegime.session_date)))
        .where(*where)).one()

    versions = {v: n for v, n in db.execute(
        select(MarketRegime.engine_version, func.count(MarketRegime.id))
        .where(*where).group_by(MarketRegime.engine_version)).all()}

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "rows": rows or 0,
        "sessions": sessions or 0,
        "first": first.isoformat() if first else None,
        "last": last.isoformat() if last else None,
        "engine_versions": versions,
        "current_engine_version": regime_engine.ENGINE_VERSION,
        # A table classified by two generations of the engine produces splits
        # that look like findings and are artefacts of the mix. Say so here
        # rather than leaving it to be noticed.
        "mixed_versions": len(versions) > 1,
        "note": ("Nothing classified yet. POST /data/regimes/backfill to build it."
                 if not rows else None),
    }


def load(db: Session, symbol: str = "NIFTY", timeframe: str = "5m") -> pd.DataFrame:
    """Stored regimes as a frame, oldest first, timestamps normalised to UTC.

    The normalisation is not cosmetic. SQLite hands these back naive while
    Postgres returns them aware, and a join against signal timestamps would
    then match on one backend and raise on the other — the same divergence
    that silently emptied the outcome study on one backend and not the other.
    """
    rows = db.scalars(
        select(MarketRegime)
        .where(MarketRegime.symbol == symbol, MarketRegime.timeframe == timeframe)
        .order_by(MarketRegime.timestamp)).all()
    columns = ["timestamp", "session_date", "day_regime", "day_confidence",
               "hour_regime", "hour_confidence", "engine_version"]
    if not rows:
        return pd.DataFrame(columns=columns)

    frame = pd.DataFrame([
        {"timestamp": repository.as_utc(r.timestamp),
         "session_date": r.session_date,
         "day_regime": r.day_regime, "day_confidence": r.day_confidence,
         "hour_regime": r.hour_regime, "hour_confidence": r.hour_confidence,
         "engine_version": r.engine_version}
        for r in rows])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    # Regime labels are derived features. A protected holdout session's are
    # withheld here, below every caller (Pass 2D.1).
    from ..methodology import registry
    return registry.withhold(db, frame)


def latest(db: Session, symbol: str = "NIFTY", timeframe: str = "5m") -> dict | None:
    """The most recently classified bar, or None if nothing is stored."""
    row = db.scalars(
        select(MarketRegime)
        .where(MarketRegime.symbol == symbol, MarketRegime.timeframe == timeframe)
        .order_by(MarketRegime.timestamp.desc()).limit(1)).first()
    if row is None:
        return None
    # The API route and the stream both read the regime through here. A bar
    # from a protected holdout session is refused: no label, confidence or
    # feature leaves, only the fact that it was withheld (Pass 2D.1).
    from ..methodology import registry
    stamp = repository.as_utc(row.timestamp)
    if registry.session_key(stamp) in registry.protected_sessions(db, [stamp]):
        return {"withheld": True, "timestamp": None, "engine_version": None,
                "day": None, "hour": None,
                "session_date": registry.session_key(stamp),
                "reason": "protected prospective holdout session; regime features "
                          "are not served for it"}
    features = row.features or {}
    return {
        "timestamp": repository.as_utc(row.timestamp).isoformat(),
        "session_date": str(row.session_date) if row.session_date else None,
        "engine_version": row.engine_version,
        "day": {"level": "day", "label": row.day_regime,
                "confidence": row.day_confidence, "reasons": row.day_reasons or [],
                "scores": features.get("day_scores", {}),
                "features": features.get("day", {}),
                "provisional": bool(features.get("day_provisional", False))},
        "hour": {"level": "hour", "label": row.hour_regime,
                 "confidence": row.hour_confidence, "reasons": row.hour_reasons or [],
                 "scores": features.get("hour_scores", {}),
                 "features": features.get("hour", {}),
                 "provisional": bool(features.get("hour_provisional", False))},
    }


def clear(db: Session, symbol: str = "NIFTY", timeframe: str = "5m") -> int:
    """Drop every stored regime for one series. Returns rows removed.

    For the case the `engine_version` field exists to catch: after changing
    the classifier, wiping and re-classifying is the only way to be sure the
    table holds one definition throughout.
    """
    removed = db.execute(
        delete(MarketRegime).where(MarketRegime.symbol == symbol,
                                   MarketRegime.timeframe == timeframe)).rowcount
    db.commit()
    return int(removed or 0)
