"""What strategy v2 actually saw when it decided (Phase 3B).

An envelope around a decision, not a second decision. Everything in it is
read from values the paper trader had already obtained for the decision it
was making — the signal it consumed, the spot and VIX it read, the chain
quotes it handed the selector, the selector's own trace. Nothing here fetches
a market observation of its own, so nothing here can describe a later quote
as the evidence for an earlier decision, and nothing here can change what was
decided: the envelope is assembled after the decision is fixed.

Three separations are the point of the record:

  Contemporaneous inputs (identity, causality, market state, setup, evidence,
  data status, versions) versus the decision context (the gates, the account,
  the risk verdict). The second is what the desk concluded and the state of
  its books; neither is a market feature.

  Observed versus derived or modelled. A quote's bid is observed; its mid,
  implied volatility and delta are derived from it; premium levels are
  modelled. Each carries its basis.

  Present versus future. Predictor categories are built from declared scalar
  fields only — a nested structure in the signal is never copied in whole —
  and a timestamped predictor dated after the signal's own clocks is set
  aside under `diagnostics`, outside the vector.

The envelope's `status` says how far it can be trusted, worst first:
`capture_failed` (nothing could be assembled), `capture_incomplete` (a
capture stage failed or the decision could not be reconciled with a gate;
what was captured is kept), `inconsistent` (clocks out of order or in the
future, or the selector trace disagreeing with the decision), `partial`
(observation linkage missing), `complete`. Every problem behind a status is
listed under `issues`.

What is unknown stays unknown. Opportunity identity and strategy family are
deliberately unset: nothing in the desk yet defines them.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from . import rules

log = logging.getLogger(__name__)

EVIDENCE_SCHEMA = "v2_decision_evidence/1"

COMPLETE = "complete"
PARTIAL = "partial"
INCONSISTENT = "inconsistent"
CAPTURE_INCOMPLETE = "capture_incomplete"
CAPTURE_FAILED = "capture_failed"
_SEVERITY = (COMPLETE, PARTIAL, INCONSISTENT, CAPTURE_INCOMPLETE, CAPTURE_FAILED)

# Issue kinds. A capture error or an unreconciled gate is a failure of the
# recording itself and is counted and logged at ERROR; the rest describe the
# evidence and are recorded without being counted as capture failures.
CAPTURE_ERROR = "capture_error"
GATE_UNRECONCILED = "gate_unreconciled"
TRACE_DISAGREEMENT = "selector_trace_disagreement"
CLOCK = "clock_inconsistent"
FUTURE_PREDICTOR = "future_predictor_excluded"
_ISSUE_STATUS = {CAPTURE_ERROR: CAPTURE_INCOMPLETE, GATE_UNRECONCILED: CAPTURE_INCOMPLETE,
                 TRACE_DISAGREEMENT: INCONSISTENT, CLOCK: INCONSISTENT,
                 FUTURE_PREDICTOR: INCONSISTENT}

# The order `PaperTrader.consider` applies its gates, and the rejection codes
# each one can produce. A decision refused at one gate passed every gate
# before it and never reached any gate after it.
GATE_CODES = {
    "signal.direction": {rules.HOLD},
    "signal.levels": {rules.NO_LEVELS},
    "signal.age": {rules.STALE_SIGNAL},
    "signal.entry_state": {rules.ENTRY_STATE},
    "signal.bias": {rules.BIAS},
    "position": {rules.POSITION_OPEN},
    "kill_switch": {rules.KILL_SWITCH},
    "entry_window": {rules.OUTSIDE_WINDOW},
    "instrument_master": {rules.CHAIN_NOT_READY},
    "expiry_day": {rules.EXPIRY_DAY},
    "feed": {rules.FEED_DOWN},
    "spot": {rules.NO_SPOT},
    "levels_vs_spot": {rules.PAST_STOP, rules.PAST_TARGET},
    "vix": {rules.VIX_NO_HISTORY, rules.VIX_NO_LIVE, rules.VIX_HIGH, rules.VIX_SPIKE},
    "expiry_choice": {rules.NO_EXPIRY},
    "chain": {rules.CHAIN_NOT_READY},
    "contract_selection": {rules.CHAIN_NOT_READY, rules.NO_IV, rules.NO_DELTA,
                           rules.STALE_QUOTE, rules.NO_DEPTH, rules.WIDE_SPREAD,
                           rules.CHEAP},
    "lot_size": {rules.CHAIN_NOT_READY},
    "premium_risk": {rules.NO_DEFINED_RISK},
    "risk_manager": {rules.RISK_VETO},
}
GATES = tuple(GATE_CODES)
PASSED, FAILED, NOT_REACHED, DISABLED, UNKNOWN = (
    "passed", "failed", "not_reached", "disabled", "unknown")

# Keys that name a future or realised quantity. A second line of defence:
# predictors are built from declared fields, and this refuses one of these
# names should a declared field ever carry it.
FUTURE_KEYS = frozenset({
    "mfe", "mae", "mfe_r", "mae_r", "mfe_points", "mae_points", "outcome",
    "r_multiple", "r_multiple_net", "exit_price", "exit_time", "exit_reason",
    "pnl", "net_pnl", "gross_pnl", "won", "resolved", "label", "labels",
    "realised", "realized", "final_regime", "revision",
})
CONTEMPORANEOUS = ("identity", "causality", "market_state", "setup", "evidence",
                   "data_status", "versions")

# The only fields copied from the signal into predictor categories. Each must
# be a scalar; anything else is set aside, never copied.
STRUCTURE_EVENT_FIELDS = ("kind", "direction", "timestamp", "broken_level", "close", "index")
CHECK_FIELDS = ("name", "score", "weight", "contribution", "disabled", "reason")
VIX_FIELDS = ("ok", "code", "detail", "live", "previous_close", "percentile", "spike_pct",
              "history_sessions")

# Signal clocks that must not be later than the moment v2 decided.
NOT_AFTER_DECISION = ("bar_open_time", "bar_close_time", "received_at", "available_at",
                      "signal_decision_at", "published_at", "signal_timestamp",
                      "generated_at")
# Pairs that must be in order when both are present.
CLOCK_ORDER = (("bar_open_time", "bar_close_time"), ("bar_close_time", "available_at"),
               ("available_at", "signal_decision_at"), ("received_at", "signal_decision_at"),
               ("signal_decision_at", "published_at"),
               ("signal_decision_at", "earliest_execution_time"))

DUPLICATE_CAPACITY = 512

# How each kind of value came to be, stated once and referenced by name.
BASES = {
    "signal": "observed: the agent's published signal payload, as consumed",
    "spot": "observed: the paper trader's spot source at decision time",
    "vix_live": "observed: the live India VIX reading at decision time",
    "quote_fields": "observed: LiveChain quote fields as supplied to the selector",
    "open_interest_volume": "last known: the chain keeps a contract's last OI and "
                            "volume when a tick omits them, so these may predate "
                            "the quote's own source_time",
    "mid_iv_delta_spread": "derived: computed by the selector from the observed quote",
    "premium_levels": "modelled: Black-Scholes projection at the contract's IV, "
                      "capped by the configured percentages",
    "paper_fill": "simulated: paper entry at the live ask; no order is sent",
    "execution_friction": "not recorded here: no execution occurs at decision time",
    "score": "uncalibrated: the signal's confidence is a score, not a probability",
}

MISSING_SCOPE = ("Lists absent clocks, absent observation linkage, and values dropped "
                 "as non-finite. Other absent fields are null where they stand and "
                 "are not enumerated here.")


def assert_no_future_fields(vector: dict) -> None:
    """Refuse a vector carrying an outcome, an excursion or a revision."""
    found: list[str] = []

    def walk(value, path):
        if isinstance(value, dict):
            for key, inner in value.items():
                if str(key).lower() in FUTURE_KEYS:
                    found.append(f"{path}.{key}")
                walk(inner, f"{path}.{key}")
        elif isinstance(value, list):
            for i, inner in enumerate(value):
                walk(inner, f"{path}[{i}]")

    for name in CONTEMPORANEOUS:
        walk(vector.get(name), name)
    if found:
        raise ValueError(f"future or realised fields in contemporaneous evidence: {found}")


def _finite(value, path: str, dropped: list[str]):
    """JSON the database will take: a non-finite float becomes None, and says so."""
    if isinstance(value, float) and not math.isfinite(value):
        dropped.append(path)
        return None
    if isinstance(value, dict):
        return {k: _finite(v, f"{path}.{k}", dropped) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v, f"{path}[{i}]", dropped) for i, v in enumerate(value)]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


# ---- declared-field copying ---------------------------------------------------

_SCALAR = (str, int, float, bool, type(None))


def _scalar(value, path: str, rejected: list[dict]):
    if isinstance(value, _SCALAR):
        return value
    rejected.append({"path": path, "type": type(value).__name__,
                     "reason": "not a scalar; nested values are never copied into "
                               "predictor evidence"})
    return None


def _declared(src, keys, path: str, rejected: list[dict]) -> dict | None:
    if src is None:
        return None
    if not isinstance(src, dict):
        rejected.append({"path": path, "type": type(src).__name__,
                         "reason": "expected an object"})
        return None
    extra = sorted(set(src) - set(keys))
    if extra:
        rejected.append({"path": path, "undeclared_fields": extra,
                         "reason": "undeclared fields are not copied"})
    return {k: _scalar(src.get(k), f"{path}.{k}", rejected) for k in keys}


def _checks(checks, rejected: list[dict]) -> list | None:
    if checks is None:
        return None
    if not isinstance(checks, list):
        rejected.append({"path": "evidence.checks", "type": type(checks).__name__,
                         "reason": "expected a list"})
        return None
    return [_declared(c, CHECK_FIELDS, f"evidence.checks[{i}]", rejected)
            for i, c in enumerate(checks)]


def _strings(values, path: str, rejected: list[dict]) -> list | None:
    if values is None:
        return None
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        rejected.append({"path": path, "type": type(values).__name__,
                         "reason": "expected a list of names"})
        return None
    return list(values)


# ---- clocks -----------------------------------------------------------------------

def _parse(stamp) -> datetime | None | str:
    """A timezone-aware datetime, None for absent, or 'unparseable'."""
    if stamp is None:
        return None
    if isinstance(stamp, datetime):
        return stamp if stamp.tzinfo else "unparseable"
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return "unparseable"
    return moment if moment.tzinfo else "unparseable"


def clock_issues(clocks: dict, decided_at: datetime | None) -> list[dict]:
    """Evidence-only checks on the recorded clocks. They grade the record;
    they have no say in the decision, which was made on the trading clocks."""
    parsed = {k: _parse(clocks.get(k)) for k in set(NOT_AFTER_DECISION) | {
        k for pair in CLOCK_ORDER for k in pair}}
    issues = []
    for key, moment in parsed.items():
        if moment == "unparseable":
            issues.append({"kind": CLOCK, "clock": key,
                           "detail": "not a timezone-aware timestamp"})
    if decided_at is not None:
        for key in NOT_AFTER_DECISION:
            moment = parsed.get(key)
            if isinstance(moment, datetime) and moment > decided_at:
                issues.append({"kind": CLOCK, "clock": key,
                               "detail": f"{(moment - decided_at).total_seconds():.3f}s "
                                         "after v2 decided"})
    for earlier, later in CLOCK_ORDER:
        a, b = parsed.get(earlier), parsed.get(later)
        if isinstance(a, datetime) and isinstance(b, datetime) and a > b:
            issues.append({"kind": CLOCK, "clock": f"{earlier}<={later}",
                           "detail": f"{earlier} is {(a - b).total_seconds():.3f}s "
                                     f"after {later}"})
    return issues


# ---- quotes -----------------------------------------------------------------------

def _value_status(value, *, zero_is_value: bool) -> str:
    """observed | missing | invalid_non_finite | invalid_type | not_positive."""
    if value is None:
        return "missing"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "invalid_type"
    if not math.isfinite(value):
        return "invalid_non_finite"
    if value < 0 or (value == 0 and not zero_is_value):
        return "not_positive"
    return "observed"


def quote_snapshot(quotes, now: datetime, *, max_quote_age_seconds: float) -> list[dict]:
    """The quotes the selector was given, copied the moment they were read.

    LiveChain replaces a Quote object on every tick rather than mutating it,
    and these values are copied out immediately, so a later tick cannot
    rewrite what this decision saw.

    `age_seconds` is the age the selector used (`Quote.age_seconds`, which
    floors at zero). `raw_age_seconds` is the unfloored difference: negative
    means the quote is stamped after the decision, and such a quote is
    marked `future_timestamp`, never `fresh`.
    """
    rows = []
    for q in quotes:
        c = q.contract
        age = q.age_seconds(now)
        raw = (now - q.source_time).total_seconds() if q.source_time else None
        received_after = bool(q.received_at and q.received_at > now)
        if raw is None:
            freshness = "unknown"
        elif raw < 0 or received_after:
            freshness = "future_timestamp"
        else:
            freshness = "stale" if age > max_quote_age_seconds else "fresh"
        oi = _value_status(q.open_interest, zero_is_value=True)
        vol = _value_status(q.volume, zero_is_value=True)
        rows.append({
            "token": c.token, "symbol": c.symbol, "strike": c.strike,
            "option_type": c.option_type,
            "expiry": c.expiry.isoformat() if getattr(c, "expiry", None) else None,
            "lot_size": c.lot_size,
            "lot_size_status": "instrument_master" if c.lot_size else "missing",
            "ltp": q.price, "bid": q.bid, "ask": q.ask,
            "ltp_status": _value_status(q.price, zero_is_value=False),
            "bid_status": _value_status(q.bid, zero_is_value=False),
            "ask_status": _value_status(q.ask, zero_is_value=False),
            "open_interest": q.open_interest, "volume": q.volume,
            "open_interest_status": "last_known" if oi == "observed" else oi,
            "volume_status": "last_known" if vol == "observed" else vol,
            "source_time": q.source_time.isoformat() if q.source_time else None,
            "received_at": q.received_at.isoformat() if q.received_at else None,
            "age_seconds": round(age, 3),
            "age_basis": "Quote.age_seconds as used by the selector (floored at zero)",
            "raw_age_seconds": round(raw, 3) if raw is not None else None,
            "freshness": freshness,
            "updates": q.updates,
        })
    return rows


@dataclass
class Capture:
    """What `consider` obtained on the way to its decision, as it obtained it.

    Filled in only at the moment each value is read for the decision. A field
    still None was never read, which is different from read-and-missing; the
    gate trace says which gates were reached.
    """
    consumed_at: datetime | None = None
    generated_at_basis: str | None = None
    killed: bool | None = None
    listed: list | None = None
    feed_ok: bool | None = None
    spot: float | None = None
    vix: dict | None = None
    expiry: str | None = None
    chain_max_age_seconds: float | None = None
    selection_reached: bool = False
    quotes: list[dict] | None = None
    selection: dict | None = None
    pick: dict | None = None
    levels: dict | None = None
    risk: dict | None = None
    equity: float | None = None
    position_id: int | None = None
    failed_gate: str | None = None
    disabled: set = field(default_factory=set)
    capture_errors: list[dict] = field(default_factory=list)


class Recorder:
    """Builds, validates and serialises the envelope; never raises."""

    def __init__(self, cfg, *, strategy: str, version: str,
                 remember: int = DUPLICATE_CAPACITY) -> None:
        self.cfg = cfg
        self.strategy = strategy
        self.version = version
        self.failures = 0                 # envelopes whose capture failed in whole or part
        self.trace_disagreements = 0
        self.capacity = remember
        self._consumed: deque[str] = deque(maxlen=remember)
        self._consumed_set: set[str] = set()
        self.config_hash = hashlib.sha256(
            json.dumps(cfg.to_dict(), sort_keys=True, default=str).encode()).hexdigest()

    # ---- the one entry point -------------------------------------------------

    def record(self, sig: dict, cap: Capture, *, outcome: str, code: str) -> dict:
        try:
            envelope = self.build(sig, cap, outcome=outcome, code=code)
            assert_no_future_fields(envelope["vector"])
            dropped: list[str] = []
            envelope = _finite(envelope, "evidence", dropped)
            if dropped:
                envelope["non_finite_fields"] = dropped
                envelope["missing"] = sorted(set(envelope["missing"]) | set(dropped))
            # Proves it serialises exactly as the database column will store it.
            envelope = json.loads(json.dumps(envelope, allow_nan=False, default=str))
        except Exception as exc:                                  # noqa: BLE001
            self.failures += 1
            log.error("v2 decision evidence could not be captured (%s): %s",
                      type(exc).__name__, exc)
            return {"schema": EVIDENCE_SCHEMA, "status": CAPTURE_FAILED,
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "observation_id": _observation(sig).get("observation_id"),
                    "outcome": outcome, "code": code}

        kinds = {i["kind"] for i in envelope["issues"]}
        if kinds & {CAPTURE_ERROR, GATE_UNRECONCILED}:
            self.failures += 1
            log.error("v2 decision evidence is incomplete for %s/%s: %s", outcome, code,
                      [i for i in envelope["issues"]
                       if i["kind"] in (CAPTURE_ERROR, GATE_UNRECONCILED)])
        if TRACE_DISAGREEMENT in kinds:
            self.trace_disagreements += 1
            log.error("v2 selector trace disagrees with the decision it describes (%s/%s)",
                      outcome, code)
        if kinds & {CLOCK, FUTURE_PREDICTOR}:
            log.warning("v2 decision evidence has inconsistent clocks or future "
                        "predictors (%s/%s)", outcome, code)
        return envelope

    # ---- assembly --------------------------------------------------------------

    def _seen(self, observation_id: str | None) -> bool | None:
        if not observation_id:
            return None
        seen = observation_id in self._consumed_set
        if not seen:
            if len(self._consumed) == self._consumed.maxlen:
                self._consumed_set.discard(self._consumed[0])
            self._consumed.append(observation_id)
            self._consumed_set.add(observation_id)
        return seen

    def build(self, sig: dict, cap: Capture, *, outcome: str, code: str) -> dict:
        obs = _observation(sig)
        context = sig.get("context") or {}
        timing = context.get("timing") or {}
        prov = context.get("provenance") or {}
        plan = sig.get("plan") or {}
        entry = plan.get("entry") or {}
        bias = plan.get("bias") or {}
        regime = plan.get("regime") or {}
        issues: list[dict] = list(cap.capture_errors)
        rejected: list[dict] = []

        observation_id = obs.get("observation_id")
        seen_before = self._seen(observation_id)
        linkage = ("linked" if observation_id and obs.get("signal_id") is not None
                   else "partial" if observation_id or obs.get("signal_id") is not None
                   else "missing")

        clocks = {
            "bar_open_time": timing.get("bar_open_time"),
            "bar_close_time": timing.get("bar_close_time"),
            "received_at": timing.get("received_at"),
            "available_at": timing.get("available_at"),
            "available_basis": timing.get("available_basis"),
            "signal_decision_at": timing.get("decision_at"),
            "earliest_execution_time": timing.get("earliest_execution_time"),
            "execution_latency_seconds": timing.get("execution_latency_seconds"),
            "signal_timestamp": sig.get("timestamp"),
            "signal_timestamp_basis": "bar timestamp as published by the agent",
            "published_at": obs.get("published_at"),
            "generated_at": sig.get("generated_at"),
            "generated_at_basis": cap.generated_at_basis,
            "v2_decided_at": cap.consumed_at.isoformat() if cap.consumed_at else None,
            "v2_decided_at_basis": "paper trader clock",
        }
        clocks = {k: _scalar(v, f"causality.{k}", rejected) for k, v in clocks.items()}
        if cap.generated_at_basis and cap.generated_at_basis.startswith("stamped by v2"):
            clocks["signal_age_note"] = (
                "the payload carried no generated_at, so v2 measured the signal's age "
                "from its own receipt stamp; the signal-age gate cannot refuse it on age")
        issues += clock_issues(clocks, cap.consumed_at)

        structure = _declared(context.get("last_structure_event"), STRUCTURE_EVENT_FIELDS,
                              "market_state.last_structure_event", rejected)
        structure = self._not_after(structure, clocks, rejected, issues)

        vector = {
            "identity": {
                "observation_id": _scalar(observation_id, "identity.observation_id",
                                          rejected),
                "signal_id": _scalar(obs.get("signal_id"), "identity.signal_id", rejected),
                "linkage": linkage,
                "observation_schema": _scalar(obs.get("schema"), "identity.schema",
                                              rejected),
                "seen_in_recent_history": seen_before,
                "duplicate_check": {
                    "kind": "bounded_recent_history", "capacity": self.capacity,
                    "scope": "this process only; an id evicted from the window, or "
                             "consumed before a restart, reports false"},
                "symbol": _scalar(sig.get("symbol"), "identity.symbol", rejected),
                "timeframe": _scalar(sig.get("timeframe"), "identity.timeframe", rejected),
                "strategy": self.strategy,
                "opportunity_id": None, "opportunity_id_status": "unset",
                "strategy_family": None, "strategy_family_status": "unknown",
            },
            "causality": clocks,
            "market_state": {
                "signal_price": _scalar(sig.get("price"), "market_state.signal_price",
                                        rejected),
                "atr14": _scalar(context.get("atr14"), "market_state.atr14", rejected),
                "vwap": _scalar(context.get("vwap"), "market_state.vwap", rejected),
                "trend": _scalar(context.get("trend"), "market_state.trend", rejected),
                "india_vix_in_signal": _scalar(context.get("india_vix"),
                                               "market_state.india_vix", rejected),
                "last_structure_event": structure,
                "regime_day": _scalar(entry.get("regime_day"), "market_state.regime_day",
                                      rejected),
                "regime_hour": _scalar(entry.get("regime_hour"),
                                       "market_state.regime_hour", rejected),
                "regime_day_provisional": _scalar(
                    (regime.get("day") or {}).get("provisional"),
                    "market_state.regime_day_provisional", rejected),
                "regime_hour_provisional": _scalar(
                    (regime.get("hour") or {}).get("provisional"),
                    "market_state.regime_hour_provisional", rejected),
                "regime_basis": "classified as of the signal's decision time",
                "v2_spot": cap.spot,
                "v2_vix": _declared(cap.vix, VIX_FIELDS, "market_state.v2_vix", rejected),
            },
            "setup": {
                key: _scalar(value, f"setup.{key}", rejected) for key, value in {
                    "action": sig.get("action"),
                    "score": sig.get("confidence"), "score_calibrated": False,
                    "entry": sig.get("entry"), "stop_loss": sig.get("stop_loss"),
                    "target": sig.get("target"), "risk_reward": sig.get("risk_reward"),
                    "bias": bias.get("label"), "bias_score": bias.get("score"),
                    "entry_state": entry.get("state"),
                    "trigger_level": entry.get("trigger_level"),
                }.items()},
            "evidence": {
                "checks": _checks(sig.get("checks"), rejected),
                "threshold_used": _scalar(context.get("threshold_used"),
                                          "evidence.threshold_used", rejected),
            },
            "data_status": {
                "disabled_checks": _strings(context.get("disabled_checks"),
                                            "data_status.disabled_checks", rejected),
                # Present or not; its contents are not copied (undeclared shape).
                "signal_option_chain_present": context.get("option_chain") is not None,
                "feed_healthy_at_decision": cap.feed_ok,
                "chain_quotes_supplied": (len(cap.quotes) if cap.quotes is not None
                                          else None),
                "chain_max_age_seconds": cap.chain_max_age_seconds,
            },
            "versions": {
                key: _scalar(value, f"versions.{key}", rejected) for key, value in {
                    "evidence_schema": EVIDENCE_SCHEMA,
                    "signal_strategy_version": prov.get("strategy_version"),
                    "parameter_hash": prov.get("parameter_hash"),
                    "input_fingerprint": prov.get("input_fingerprint"),
                    "input_fingerprint_note": "identifies the input; it is not the input",
                    "code_id": obs.get("code_id"),
                    "code_id_basis": obs.get("code_id_basis"),
                    "data_source": obs.get("data_source"),
                    "v2_strategy": self.strategy, "v2_version": self.version,
                    "v2_config_hash": self.config_hash,
                }.items()},
        }
        trace, gate_problem = gate_trace(cap, code=code, outcome=outcome)
        if gate_problem:
            issues.append({"kind": GATE_UNRECONCILED, "detail": gate_problem})
        vector["decision_context"] = {
            "role": "the desk's conclusions and account state; not market features",
            "outcome": outcome, "code": code,
            "gate_trace": trace,
            "kill_switch": cap.killed,
            "listed_expiries": cap.listed,
            "chosen_expiry": cap.expiry,
            "equity": cap.equity,
            "risk": cap.risk,
            "position_id": cap.position_id,
        }

        contract = contract_evidence(cap, observation_id=observation_id)
        if contract.get("trace_consistent_with_decision") is False:
            issues.append({"kind": TRACE_DISAGREEMENT,
                           "detail": "the traced selection does not match the decision"})
        for q in contract.get("universe", {}).get("quotes") or []:
            if q["freshness"] == "future_timestamp":
                issues.append({"kind": CLOCK, "clock": f"quote {q['token']}",
                               "detail": "quote stamped after the decision clock"})

        missing = sorted([f"causality.{k}" for k in NOT_AFTER_DECISION + (
                              "earliest_execution_time", "v2_decided_at")
                          if clocks.get(k) is None]
                         + ([] if observation_id else ["identity.observation_id"])
                         + ([] if obs.get("signal_id") is not None
                            else ["identity.signal_id"]))
        status = status_for(issues, linkage)
        return {
            "schema": EVIDENCE_SCHEMA, "status": status, "issues": issues,
            "vector": vector, "contract_evidence": contract,
            "outcome_label": {"status": "not_implemented",
                              "note": "design only; executable option labels are "
                                      "blocked by data and kept out of this record"},
            "diagnostics": {"rejected_predictors": rejected,
                            "note": "material not admitted to the vector; kept for "
                                    "diagnosis only, never a predictor"},
            "bases": BASES, "missing": missing, "missing_scope": MISSING_SCOPE,
        }

    @staticmethod
    def _not_after(structure, clocks, rejected, issues):
        """A timestamped predictor dated after the signal is not a predictor."""
        if not structure or structure.get("timestamp") is None:
            return structure
        stamp = _parse(structure["timestamp"])
        bound_key = next((k for k in ("bar_open_time", "signal_decision_at",
                                      "v2_decided_at") if clocks.get(k)), None)
        bound = _parse(clocks.get(bound_key)) if bound_key else None
        reason = None
        if stamp == "unparseable":
            reason = "timestamp is not timezone-aware"
        elif isinstance(bound, datetime) and stamp > bound:
            reason = f"dated after the signal's {bound_key}"
        if reason is None:
            return structure
        rejected.append({"path": "market_state.last_structure_event",
                         "reason": reason, "value": structure})
        issues.append({"kind": FUTURE_PREDICTOR, "path": "market_state.last_structure_event",
                       "detail": reason})
        return None


def status_for(issues: list[dict], linkage: str) -> str:
    worst = COMPLETE if linkage == "linked" else PARTIAL
    for issue in issues:
        candidate = _ISSUE_STATUS.get(issue["kind"], CAPTURE_INCOMPLETE)
        if _SEVERITY.index(candidate) > _SEVERITY.index(worst):
            worst = candidate
    return worst


def _observation(sig: dict) -> dict:
    obs = sig.get("observation")
    return obs if isinstance(obs, dict) else {}


def gate_trace(cap: Capture, *, code: str, outcome: str) -> tuple[list[dict], str | None]:
    """Every gate with what happened at it, and any reason the trace cannot be
    reconciled with the decision.

    `enabled` is configuration; `status` is execution. A gate switched off by
    configuration that execution reached is `disabled`; one it never reached
    is `not_reached` whatever its configuration. A rejection must name a known
    gate whose codes include the rejection's code, and an entry must name none;
    otherwise every status is `unknown` and the problem is returned.
    """
    failed = cap.failed_gate
    problem = None
    if outcome == "entered":
        if failed is not None or code != rules.ENTERED:
            problem = f"an entry recorded with failed gate {failed!r} and code {code!r}"
    elif failed not in GATE_CODES:
        problem = f"rejection {code!r} names no known gate ({failed!r})"
    elif code not in GATE_CODES[failed]:
        problem = f"code {code!r} is not one gate {failed!r} produces"
    elif failed in cap.disabled:
        problem = f"gate {failed!r} failed although configuration disables it"
    if problem:
        return [{"gate": g, "enabled": g not in cap.disabled, "status": UNKNOWN}
                for g in GATES], problem

    out, after = [], False
    for gate in GATES:
        enabled = gate not in cap.disabled
        if after:
            status = NOT_REACHED
        elif gate == failed:
            status, after = FAILED, True
        else:
            status = PASSED if enabled else DISABLED
        row = {"gate": gate, "enabled": enabled, "status": status}
        if status == FAILED:
            row["code"] = code
        out.append(row)
    return out, None


def contract_evidence(cap: Capture, *, observation_id: str | None) -> dict:
    """The selector's inputs and its trace — or why there are none."""
    if not cap.selection_reached:
        return {"status": "selection_not_reached", "observation_id": observation_id,
                "note": "the decision ended before contract selection; no contract "
                        "evidence was read for it"}
    selection = cap.selection or {}
    return {
        "status": "selection_reached",
        "observation_id": observation_id,
        "expiry": cap.expiry,
        "universe": {
            "definition": "LiveChain.quotes(now) for the chosen expiry: contracts "
                          "quoted within the chain's max age; older contracts were "
                          "not supplied to the selector",
            # None here means the snapshot itself failed; see `issues`.
            "quotes": cap.quotes,
        },
        "alternatives": selection.get("alternatives"),
        "result": selection.get("result"),
        # The trace comes from re-running the pure selector on the same
        # inputs. None means the trace was not captured; False means it was
        # and disagrees with the decision — two different problems.
        "trace_consistent_with_decision": selection.get("consistent_with_decision"),
        "pick": cap.pick,
        "levels": cap.levels,
    }
