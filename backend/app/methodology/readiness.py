"""Methodology readiness and the study manifest (Pass 2D).

One status per question, never a single green light. "Can I cross-validate
on the archive?" and "do I have an untouched holdout?" have different
answers today — yes, and not yet — and folding them into one verdict is
how the second answer gets lost.
"""
from __future__ import annotations

from collections import Counter

import pandas as pd
from sqlalchemy.orm import Session

from ..data import clock_grid
from . import benchmarks, events, folds, registry, sample, trials

READY = "READY"
NOT_READY = "NOT_READY"
AVAILABLE = "AVAILABLE"
BLOCKED = "BLOCKED_BY_DATA"

OPTION_BLOCKERS = [
    "historical observed option execution: the archive has no bid/ask pairs",
    "verified historical lot sizes: unavailable (no auditable evidence captured)",
    "legacy option capture clocks: unavailable",
    "legacy zero-filled open-interest provenance: unrecoverable",
    "historical candle revisions overwritten before Pass 2C: unrecoverable",
]

MTM_LIMITATIONS = [
    "consumes sequential execution streams only; the signal-outcome evaluator "
    "cannot feed it",
    "index positions are marked from the last completed index bar of the session",
    "historical option positions have no permissible mark and are MTM_UNAVAILABLE",
    "executions on a data-quality-excluded session are booked for equity "
    "continuity but excluded from every performance statistic",
]


def study_manifest(*, study_id: str, fold_manifest: folds.FoldManifest | None,
                   dataset_fingerprint: str, primary_metric: str = benchmarks.PRIMARY_METRIC,
                   bootstrap_seed: int = 20260925, bootstrap_resamples: int = 2000) -> dict:
    """What a study commits to before it has a single result."""
    body = {
        "study_id": study_id,
        "dataset_fingerprint": dataset_fingerprint,
        "primary_metric": primary_metric,
        "secondary_metrics": list(benchmarks.SECONDARY_METRICS),
        "descriptive_only": list(benchmarks.DESCRIPTIVE_ONLY),
        "benchmark_ids": benchmarks.ids(),
        "benchmark_definition_hashes": {k: b.definition_hash
                                        for k, b in sorted(benchmarks.BENCHMARKS.items())},
        "fold_manifest_hash": fold_manifest.manifest_hash if fold_manifest else None,
        "fold_label": fold_manifest.label if fold_manifest else None,
        "embargo_policy": (fold_manifest.policy.to_dict() if fold_manifest
                           else folds.EmbargoPolicy().to_dict()),
        "sample_policy": {"min_independent_sessions": sample.MIN_INDEPENDENT_SESSIONS,
                          "min_closed_trades": sample.MIN_CLOSED_TRADES,
                          "target_holdout_sessions": registry.TARGET_HOLDOUT_SESSIONS},
        "uncertainty_policy": {"method": sample.METHOD,
                               "cluster_basis": sample.CLUSTER_BASIS,
                               "seed": bootstrap_seed, "resamples": bootstrap_resamples},
    }
    benchmarks.verify_frozen()
    return body | {"manifest_hash": events.digest(body)}


def summary(db: Session | None, archive: pd.DataFrame | None = None,
            option_execution: str = BLOCKED) -> dict:
    log_ready = db is not None and events.available(db)
    current = registry.state(db) if log_ready else registry.State()
    sessions = registry.archive_sessions(archive) if archive is not None else []
    categories = Counter(current.category(s) for s in sessions)
    lock = current.lock
    sealed = current.locked_sessions()
    eligible = [s for s in current.observed
                if current.category(s) in (registry.HOLDOUT_CANDIDATE,
                                           registry.HOLDOUT_LOCKED)]

    if lock is None:
        holdout = "NOT YET AVAILABLE — no prospective lock"
    elif lock["lock_id"] in current.consumptions:
        holdout = "CONSUMED — a new generation needs a new lock"
    elif sealed:
        holdout = f"SEALED — {len(sealed)} of {lock['target_sessions']} sessions"
    else:
        holdout = (f"NOT YET AVAILABLE — collecting, {len(eligible)} of "
                   f"{lock['target_sessions']} clean sessions")

    trial_state = trials.state(db) if log_ready else trials.TrialState()
    # The same session-quality record folds, holdout observation and the MTM
    # ledger read: only "clean" is research-usable; "unknown" is excluded.
    quality = Counter(v["quality"] for v in clock_grid.session_quality(archive).values()) \
        if archive is not None else Counter()
    return {
        "current_historical_data": {
            "status": "SEEN / DEVELOPMENT ONLY",
            "sessions": len(sessions),
            "classification": dict(sorted(categories.items())),
            "note": "no part of the existing archive is or can become an unseen holdout",
        },
        "session_quality": {
            "clean": quality.get(clock_grid.CLEAN_SESSION, 0),
            "excluded_data_quality": quality.get(clock_grid.FAULTY_SESSION, 0),
            "quality_unknown": quality.get(clock_grid.QUALITY_UNKNOWN, 0),
            "basis": "durable per-session raw grid verdicts; missing provenance is "
                     "unknown, never clean"},
        "seen_data_cross_validation": {"status": AVAILABLE,
                                       "label": folds.SEEN_DATA_LABEL},
        "genuine_untouched_holdout": {"status": holdout},
        "prospective_holdout_mechanism": {
            "status": READY if log_ready else
            f"{NOT_READY} — migration 0010 (research_events) not applied",
            "target_sessions": registry.TARGET_HOLDOUT_SESSIONS,
            "lock": lock},
        # Blocked until *every* requirement holds, not just quotes. Lot-size
        # evidence is not captured anywhere yet, so this cannot clear today
        # whatever the quote coverage says.
        "option_historical_executable_research": {
            "status": BLOCKED,
            "quote_verdict": option_execution,
            "blockers": OPTION_BLOCKERS,
            "note": "not certified as an executable historical backtest until all "
                    "blockers are resolved; lot-size evidence capture is a tracked "
                    "future-data limitation"},
        "trial_registry": {
            "status": READY if log_ready else
            f"{NOT_READY} — migration 0010 (research_events) not applied",
            **trials.summary(trial_state)},
        "daily_mtm_ledger": {"status": "READY — with limitations",
                             "limitations": MTM_LIMITATIONS},
        "benchmarks": {"status": "FROZEN", "ids": benchmarks.ids()},
        "primary_metric": benchmarks.METRIC_POLICY,
    }
