"""Session-based folds with an embargo (OS-2, OS-3).

Cross-validation here splits by whole trading sessions, never by rows. A
row split puts 10:05 in training and 10:10 in validation, and every
feature, label and open position that spans the two leaks the answer. So:

  * sessions are ordered chronologically and never shuffled
  * a session is in exactly one of train / embargo / validation per fold
  * quarantined (data-quality excluded) sessions are in none of them
  * the windows expand: train_1 → val_1, train_1+2 → val_2, … — a fold never
    trains on a session later than the one it validates

**Embargo.** `embargo_sessions` sessions between the last training session
and the first validation session are neither trained nor scored. The
policy is 1: a methodological rule, not a tuned value. A strategy whose
labels or positions span `label_horizon_sessions` sessions needs at least
that many, and the required embargo expands to it automatically.

**Warmup is context, not score.** Bars before a validation window may be
fed to indicators as causal history (`validation_frame`); they are marked
unscored, and only validation-session bars are scored.

**Labelling.** Folds built on the current archive are *cross-validation on
previously seen data*. They are not, and are never labelled as, an
untouched out-of-sample test.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from ..data import clock_grid
from . import events, registry

EMBARGO_SESSIONS = 1
SEEN_DATA_LABEL = "cross-validation on previously seen data"
UNSEEN_LABEL = "cross-validation on sessions with no recorded prior use"


@dataclass(frozen=True)
class EmbargoPolicy:
    embargo_sessions: int = EMBARGO_SESSIONS
    label_horizon_sessions: int = 1       # an intraday strategy closes same session

    def __post_init__(self) -> None:
        if self.embargo_sessions < 0 or self.label_horizon_sessions < 1:
            raise ValueError("embargo must be >= 0 and the horizon >= 1 session")

    @property
    def required(self) -> int:
        """The embargo actually applied: never shorter than the horizon."""
        return max(self.embargo_sessions, self.label_horizon_sessions)

    def to_dict(self) -> dict:
        return asdict(self) | {"required_embargo_sessions": self.required}


@dataclass(frozen=True)
class Fold:
    index: int
    train: tuple[str, ...]
    embargo: tuple[str, ...]
    validation: tuple[str, ...]

    def to_dict(self, fingerprints: dict[str, str]) -> dict:
        return {"index": self.index, "train": list(self.train),
                "embargo": list(self.embargo), "validation": list(self.validation),
                "fingerprints": {
                    part: events.digest([fingerprints.get(s) for s in sessions])
                    for part, sessions in (("train", self.train),
                                           ("embargo", self.embargo),
                                           ("validation", self.validation))}}


@dataclass(frozen=True)
class FoldManifest:
    folds: tuple[Fold, ...]
    excluded: tuple[str, ...]
    policy: EmbargoPolicy
    label: str
    fingerprints: dict
    manifest_hash: str

    def to_dict(self) -> dict:
        return {"label": self.label, "policy": self.policy.to_dict(),
                "excluded_sessions": list(self.excluded),
                "folds": [f.to_dict(self.fingerprints) for f in self.folds],
                "manifest_hash": self.manifest_hash}


def build(db, sessions: dict[str, dict], *, n_folds: int, min_train_sessions: int,
          validation_sessions: int, policy: EmbargoPolicy = EmbargoPolicy(),
          categories: dict[str, str] | None = None) -> FoldManifest:
    """Expanding-window folds over whole sessions.

    `sessions` maps each IST session date to {"quality": "clean" | "faulty"
    | "unknown", "dataset_fingerprint": str} — use `session_table`, which
    reads the durable raw verdicts. Only "clean" is trained or scored; a
    faulty or QUALITY_UNKNOWN session is excluded. `db` is required: the registry is consulted
    here, not trusted to the caller, and a protected holdout session
    anywhere in the input is refused. `categories` may only add protection:
    a session the caller marks protected is refused too, but a caller can
    never mark a registry-protected session usable.
    """
    if db is None:
        raise ValueError("folds need the research registry: pass the database session")
    if n_folds < 1 or min_train_sessions < 1 or validation_sessions < 1:
        raise ValueError("folds, training and validation sizes must be positive")
    current = registry.state(db)
    categories = {s: current.category(s) for s in sessions} | {
        s: c for s, c in (categories or {}).items() if c in registry.PROTECTED}
    protected = sorted(s for s in sessions if categories.get(s) in registry.PROTECTED)
    if protected:
        raise registry.HoldoutAccessDenied(
            f"folds cannot include protected holdout sessions: {protected[:5]}")

    ordered = sorted(sessions)                         # chronological, no shuffle
    clean = [s for s in ordered if sessions[s]["quality"] == "clean"]
    excluded = tuple(s for s in ordered if sessions[s]["quality"] != "clean")
    embargo = policy.required
    needed = min_train_sessions + n_folds * (embargo + validation_sessions)
    if needed > len(clean):
        raise ValueError(f"{n_folds} folds need {needed} clean sessions; "
                         f"{len(clean)} available")

    folds = []
    cut = min_train_sessions
    for k in range(n_folds):
        train = tuple(clean[:cut])
        gap = tuple(clean[cut:cut + embargo])
        val = tuple(clean[cut + embargo:cut + embargo + validation_sessions])
        # The invariants, checked rather than assumed.
        assert not set(train) & set(val) and not set(gap) & set(val)
        assert not train or not val or max(train) < min(val)
        folds.append(Fold(index=k + 1, train=train, embargo=gap, validation=val))
        cut += embargo + validation_sessions

    seen = any(categories.get(s, registry.SEEN_PRE_REGISTRY) in registry.SEEN
               for s in clean)
    label = SEEN_DATA_LABEL if seen else UNSEEN_LABEL
    fingerprints = {s: sessions[s].get("dataset_fingerprint") for s in ordered}
    body = {"label": label, "policy": policy.to_dict(), "excluded": list(excluded),
            "folds": [f.to_dict(fingerprints) for f in folds]}
    return FoldManifest(folds=tuple(folds), excluded=excluded, policy=policy,
                        label=label, fingerprints=fingerprints,
                        manifest_hash=events.digest(body))


def session_table(candles: pd.DataFrame, timeframe: str = "5m") -> dict[str, dict]:
    """Quality and a content fingerprint for every session in a frame.

    Quality comes from `clock_grid.session_quality`, the one source folds,
    holdout observation, the MTM ledger and readiness share: the durable
    per-row record stamped at the research boundary, so a session whose raw
    bars failed the grid stays faulty after quarantine and concatenation,
    and a session with no record is "unknown", never clean.
    """
    from ..data.dataset import fingerprint

    quality = clock_grid.session_quality(candles, timeframe)
    days = pd.to_datetime(candles["timestamp"], utc=True).dt.tz_convert(
        clock_grid.IST).dt.date.map(lambda d: d.isoformat())
    out = {}
    for day, verdict in quality.items():
        subset = candles[(days == day).to_numpy()]
        out[day] = {"quality": verdict["quality"], "faults": verdict["faults"],
                    "quality_basis": verdict["basis"],
                    "dataset_fingerprint": fingerprint(subset, "NIFTY", timeframe).hash}
    return out


def validation_frame(db, candles: pd.DataFrame, fold: Fold,
                     excluded: tuple[str, ...] = ()) -> pd.DataFrame:
    """Past-only warmup context followed by the validation sessions.

    Context is every bar from a non-excluded, non-protected session strictly
    before the first validation session; nothing after the last validation
    session is included. The `scored` column is True only for
    validation-session bars. Protected holdout sessions are withheld here
    against the registry, so warmup cannot carry one in either.
    """
    if db is None:
        raise ValueError("validation frames need the research registry")
    candles = registry.withhold(db, candles)
    days = pd.to_datetime(candles["timestamp"], utc=True).dt.tz_convert(
        clock_grid.IST).dt.date.map(lambda d: d.isoformat())
    first = min(fold.validation)
    last = max(fold.validation)
    keep = ((days < first) | days.isin(fold.validation)) & ~days.isin(excluded) \
        & (days <= last)
    frame = candles[keep.to_numpy()].copy().reset_index(drop=True)
    frame["scored"] = days[keep].isin(fold.validation).to_numpy()
    context = frame.loc[~frame["scored"], "timestamp"]
    scored = frame.loc[frame["scored"], "timestamp"]
    if len(context) and len(scored) and \
            pd.to_datetime(context, utc=True).max() >= pd.to_datetime(scored, utc=True).min():
        raise AssertionError("warmup context is not strictly before validation")
    frame.attrs = dict(candles.attrs) | {"fold": fold.index,
                                         "scored_sessions": list(fold.validation)}
    return frame


def scored_trades(trades: list, fold: Fold) -> list:
    """Only trades that open *and* close inside the fold's validation sessions."""
    def day(stamp) -> str:
        return registry.session_key(pd.Timestamp(stamp).to_pydatetime())

    allowed = set(fold.validation)
    return [t for t in trades if day(t.entry_time) in allowed
            and day(t.exit_time) in allowed]
