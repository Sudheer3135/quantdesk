"""Preregistered trials and multiple-testing awareness (OS-4).

Every research attempt whose result is looked at is a trial, and it is
registered *before* it runs. The alternative — run, inspect, register the
winners — is how a search of fifty variants is reported as one good idea.

Three kinds of event, never an edit:

  trial_preregistered  the full specification, before any result exists
  trial_result         the result, its fingerprint, and whether it was
                       inspected. One per trial: running the same thing
                       again is another attempt and needs its own trial
  trial_invalidated    a later correction, naming what it invalidates

Studies are registered the same way; a study's primary metric may be
changed only while the study has no results.

**Before this registry.** Quant Desk inspected many results before any of
this existed — the audit, the outcome reports, the slippage and execution
studies. How many is not known, so it is reported as unknown, never as 0,
and every family count below is a count of *registered* trials only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from . import events

EXPLORATORY = "exploratory"
CONFIRMATORY = "confirmatory"
MODES = (EXPLORATORY, CONFIRMATORY)

PRE_REGISTRY_EXPLORATION = True
PRE_REGISTRY_TRIAL_COUNT = None        # unknown — not zero
PRE_REGISTRY_NOTE = ("results were inspected before the registry existed "
                     "(audit probes, outcome reports, slippage and execution "
                     "studies); their number is unknown and is not counted below")

REQUIRED = ("trial_id", "study_id", "family_id", "mode", "hypothesis", "rationale",
            "strategy_version", "code_id", "parameter_hash", "changed_parameters",
            "dataset_ids", "fold_manifest_hash", "embargo_policy",
            "execution_policy_hash", "cost_model_hash", "primary_metric",
            "secondary_metrics", "benchmark_ids")


class TrialError(RuntimeError):
    pass


@dataclass
class Trial:
    spec: dict
    preregistered_seq: int
    preregistered_at: str
    results: list[dict] = field(default_factory=list)
    invalidations: list[dict] = field(default_factory=list)

    @property
    def inspected(self) -> bool:
        return any(r["inspected"] for r in self.results)


@dataclass
class TrialState:
    studies: dict[str, list[dict]] = field(default_factory=dict)
    trials: dict[str, Trial] = field(default_factory=dict)

    def study(self, study_id: str) -> dict | None:
        history = self.studies.get(study_id)
        return history[-1] if history else None

    def study_has_results(self, study_id: str) -> bool:
        return any(t.results for t in self.trials.values()
                   if t.spec["study_id"] == study_id)


def fold(log: list[events.Event]) -> TrialState:
    state = TrialState()
    for e in log:
        p = e.payload
        if e.event_type == "study_registered":
            state.studies.setdefault(p["study_id"], []).append(p)
        elif e.event_type == "trial_preregistered":
            state.trials[p["trial_id"]] = Trial(
                spec=p, preregistered_seq=e.seq, preregistered_at=e.created_at.isoformat())
        elif e.event_type == "trial_result":
            state.trials[p["trial_id"]].results.append(p | {"seq": e.seq})
        elif e.event_type == "trial_invalidated":
            state.trials[p["trial_id"]].invalidations.append(p | {"seq": e.seq})
    return state


def state(db: Session) -> TrialState:
    if not events.available(db):
        return TrialState()
    return fold(events.read(db, events.TRIALS))


def register_study(db: Session, manifest: dict, *, now: datetime | None = None) -> dict:
    """Record a study manifest. An amendment is allowed only before results."""
    study_id = manifest.get("study_id")
    if not study_id or not manifest.get("primary_metric"):
        raise TrialError("a study needs a study_id and a primary_metric")
    current = state(db)
    if current.study(study_id) is not None and current.study_has_results(study_id):
        raise TrialError(f"study {study_id} already has results; its manifest, "
                         "primary metric included, is frozen")
    events.append(db, stream=events.TRIALS, event_type="study_registered",
                  subject=study_id, payload=manifest, now=now)
    return manifest


def preregister(db: Session, spec: dict, *, now: datetime | None = None) -> dict:
    missing = [f for f in REQUIRED if f not in spec]
    if missing:
        raise TrialError(f"preregistration incomplete: missing {missing}")
    if spec["mode"] not in MODES:
        raise TrialError(f"mode must be one of {MODES}")
    current = state(db)
    if spec["trial_id"] in current.trials:
        raise TrialError(f"trial {spec['trial_id']} already exists; a repeat is a "
                         "new trial")
    study = current.study(spec["study_id"])
    if study is None:
        raise TrialError(f"study {spec['study_id']} is not registered")
    if spec["primary_metric"] != study["primary_metric"]:
        raise TrialError("a trial's primary metric is its study's")
    # What is being tested, independent of when or by whom. Two trials with
    # the same fingerprint are repeats of one experiment, and both count.
    spec = spec | {"spec_fingerprint": events.digest(
        {k: spec[k] for k in REQUIRED
         if k not in ("trial_id", "mode", "hypothesis", "rationale")})}
    events.append(db, stream=events.TRIALS, event_type="trial_preregistered",
                  subject=spec["trial_id"], payload=spec, now=now)
    return spec


def result_fingerprint(result: dict) -> str:
    return events.digest(result)


def record_result(db: Session, *, trial_id: str, result: dict, inspected: bool,
                  partition: str, now: datetime | None = None) -> dict:
    current = state(db)
    trial = current.trials.get(trial_id)
    if trial is None:
        raise TrialError(f"trial {trial_id} was not preregistered; register before "
                         "evaluating, not after")
    if trial.results:
        raise TrialError(f"trial {trial_id} already has a result; running it again "
                         "is another attempt and needs its own preregistration")
    payload = {"trial_id": trial_id, "result": result, "inspected": bool(inspected),
               "partition": partition, "result_fingerprint": result_fingerprint(result)}
    events.append(db, stream=events.TRIALS, event_type="trial_result",
                  subject=trial_id, payload=payload, now=now)
    return payload


def invalidate(db: Session, *, trial_id: str, reason: str,
               now: datetime | None = None) -> dict:
    if trial_id not in state(db).trials:
        raise TrialError(f"trial {trial_id} does not exist")
    payload = {"trial_id": trial_id, "reason": reason}
    events.append(db, stream=events.TRIALS, event_type="trial_invalidated",
                  subject=trial_id, payload=payload, now=now)
    return payload


def summary(current: TrialState) -> dict:
    families: dict[str, dict] = {}
    for trial in current.trials.values():
        f = families.setdefault(trial.spec["family_id"], {
            "registered_attempts": 0, "results_inspected": 0, "completed": 0,
            "invalidated": 0, "exploratory": 0, "confirmatory": 0,
            "distinct_specifications": set()})
        f["registered_attempts"] += 1
        f["results_inspected"] += int(trial.inspected)
        f["completed"] += int(bool(trial.results))
        f["invalidated"] += int(bool(trial.invalidations))
        f[trial.spec["mode"]] += 1
        f["distinct_specifications"].add(trial.spec["spec_fingerprint"])
    for f in families.values():
        f["distinct_specifications"] = len(f["distinct_specifications"])
    return {
        "known_registered_trials": len(current.trials),
        "pre_registry_exploration": PRE_REGISTRY_EXPLORATION,
        "pre_registry_trial_count": PRE_REGISTRY_TRIAL_COUNT,
        "pre_registry_trial_count_status": "unknown",
        "pre_registry_note": PRE_REGISTRY_NOTE,
        "families": families,
        "note": ("counts are registered trials from the registry onward; a "
                 "selected result is one of `registered_attempts`, not the only "
                 "experiment"),
    }
