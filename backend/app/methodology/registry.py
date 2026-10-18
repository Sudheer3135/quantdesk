"""Which data research has seen, and the prospective holdout (OS-1).

**The rule.** Data research has already looked at can never be an unseen
holdout. The archive Quant Desk holds today fed the audit probes, the
outcome reports, the slippage studies and every execution repair; carving a
slice of it off now and calling it out-of-sample would be a false claim, so
nothing here can do that. Every session without a registration is
classified SEEN_PRE_REGISTRY — the conservative reading of an archive that
was inspected before any registry existed — and a seen session has no path
back to unseen.

**The prospective holdout.** A lock records a boundary, `seen_through`,
fixed by data availability alone: the latest session that is not already
protected. It is never chosen, and never chosen by looking at results.
Complete sessions after it are observed as they arrive and checked against
the Pass 2C exchange grid:

  clean      HOLDOUT_CANDIDATE, and once `target_sessions` clean sessions
             exist the first `target_sessions` of them are HOLDOUT_LOCKED
  faulty     EXCLUDED_DATA_QUALITY — never counted toward the target
  pending    HOLDOUT_PENDING_QUALITY — not yet complete or not yet checked

Candidate, locked and pending sessions are *protected*: strategy research
reads withhold them (`data.research.load_research_candles`) and an explicit
request for one is refused. Raw collection and data-quality validation stay
allowed.

**Exposure.** If a protected session is shown to strategy logic anyway — a
signal, a P&L, an outcome label, a manual backtest — `record_exposure`
records it and the session becomes SEEN_CONTAMINATED for good. It is never
put back. The pool simply continues with later clean sessions.

**Consumption.** `final_evaluation` spends a sealed pool exactly once, for
one frozen, preregistered candidate. The sessions become HOLDOUT_CONSUMED
permanently; a changed strategy needs a new lock and new sessions.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import pandas as pd
from sqlalchemy.orm import Session

from .. import market_hours
from ..data import clock_grid
from . import events, trials

SEEN_PRE_REGISTRY = "SEEN_PRE_REGISTRY"
SEEN_DEVELOPMENT = "SEEN_DEVELOPMENT"
SEEN_VALIDATION = "SEEN_VALIDATION"
SEEN_CONTAMINATED = "SEEN_CONTAMINATED"
HOLDOUT_PENDING_QUALITY = "HOLDOUT_PENDING_QUALITY"
HOLDOUT_CANDIDATE = "HOLDOUT_CANDIDATE"
HOLDOUT_LOCKED = "HOLDOUT_LOCKED"
HOLDOUT_CONSUMED = "HOLDOUT_CONSUMED"
EXCLUDED_DATA_QUALITY = "EXCLUDED_DATA_QUALITY"

SEEN = frozenset({SEEN_PRE_REGISTRY, SEEN_DEVELOPMENT, SEEN_VALIDATION, SEEN_CONTAMINATED})
PROTECTED = frozenset({HOLDOUT_PENDING_QUALITY, HOLDOUT_CANDIDATE, HOLDOUT_LOCKED})
REGISTRABLE_USAGE = frozenset({SEEN_PRE_REGISTRY, SEEN_DEVELOPMENT, SEEN_VALIDATION})

# The initial research policy. An operational target, not a claim that 30
# sessions is statistically sufficient for anything.
TARGET_HOLDOUT_SESSIONS = 30
QUALITY_REQUIREMENTS = {
    "session_complete": "the session's last bar has closed",
    "clock_grid": "Pass 2C grid: no missing, off-grid, duplicate or "
                  "out-of-session bar (data.clock_grid)",
}
PRE_REGISTRY_REASON = ("pre-registry default: the archive was inspected by the "
                       "audit, outcome reports and execution repairs before any "
                       "registry existed")

SESSION_CLOSE = market_hours.MARKET_CLOSE


class HoldoutAccessDenied(PermissionError):
    """Strategy research asked for a protected holdout session."""


class HoldoutError(RuntimeError):
    """A holdout operation the registry's rules do not permit."""


def session_key(value) -> str:
    """Sessions are named by their IST trading date."""
    if isinstance(value, str):
        return date.fromisoformat(value).isoformat()
    if isinstance(value, datetime):
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        return stamp.tz_convert(clock_grid.IST).date().isoformat()
    return value.isoformat()


# ---------------------------------------------------------------------------
# state — a pure fold over the event log
# ---------------------------------------------------------------------------

@dataclass
class Classification:
    session: str
    category: str
    dataset_fingerprint: str | None = None
    first_registered_usage: str | None = None
    study_id: str | None = None
    reason: str | None = None
    code_id: str | None = None
    history: list[dict] = field(default_factory=list)

    @property
    def protected(self) -> bool:
        return self.category in PROTECTED

    def to_dict(self) -> dict:
        return {"session": self.session, "category": self.category,
                "dataset_fingerprint": self.dataset_fingerprint,
                "first_registered_usage": self.first_registered_usage,
                "study_id": self.study_id, "reason": self.reason,
                "code_id": self.code_id, "history": self.history}


@dataclass
class State:
    usage: dict[str, list[dict]] = field(default_factory=dict)
    observed: dict[str, dict] = field(default_factory=dict)
    exposed: dict[str, list[dict]] = field(default_factory=dict)
    consumed: dict[str, str] = field(default_factory=dict)
    locks: list[dict] = field(default_factory=list)
    consumptions: dict[str, dict] = field(default_factory=dict)
    results: dict[str, dict] = field(default_factory=dict)

    @property
    def lock(self) -> dict | None:
        return self.locks[-1] if self.locks else None

    # ---- classification ------------------------------------------------

    def ever_seen(self, session: str) -> bool:
        """Has research ever had this session? If so, it is never holdout.

        Answered from the whole event history, never from whichever archive
        a caller happens to pass: a usage registration of any kind, an
        exposure, a consumption, or falling at or before the boundary of
        *any* lock generation (each lock also records every session known
        when it was made). A session missing from today's archive — deleted,
        truncated, not yet re-imported — is exactly as seen as it was.
        """
        return (session in self.usage or session in self.exposed
                or session in self.consumed
                or any(session <= lock["seen_through_session"]
                       or session in lock.get("known_sessions", ())
                       for lock in self.locks))

    def holdout_eligible(self, session: str) -> bool:
        """The one eligibility rule. Everything that counts, locks or reads the
        pool goes through this."""
        lock = self.lock
        seen = self.observed.get(session)
        return (lock is not None and session > lock["seen_through_session"]
                and seen is not None and seen["quality"] == "clean"
                and not self.ever_seen(session))

    def _pool(self) -> list[str]:
        """Eligible sessions after the active boundary, oldest first."""
        return sorted(s for s in self.observed if self.holdout_eligible(s))

    def locked_sessions(self) -> list[str]:
        lock = self.lock
        pool = self._pool()
        if lock is None or len(pool) < lock["target_sessions"]:
            return []
        return pool[:lock["target_sessions"]]

    def category(self, session: str) -> str:
        if session in self.consumed:
            return HOLDOUT_CONSUMED
        if session in self.exposed:
            return SEEN_CONTAMINATED
        observed = self.observed.get(session)
        if observed is not None and observed["quality"] != "clean":
            # Excluded stays excluded when a later lock moves the boundary
            # past it; it never becomes a "seen" session that was usable.
            return EXCLUDED_DATA_QUALITY
        if self.lock is None or self.ever_seen(session):
            recorded = self.usage.get(session)
            return recorded[-1]["category"] if recorded else SEEN_PRE_REGISTRY
        if self.observed.get(session) is None:
            return HOLDOUT_PENDING_QUALITY
        if not self.holdout_eligible(session):     # cannot happen; fail closed
            return SEEN_PRE_REGISTRY
        return HOLDOUT_LOCKED if session in self.locked_sessions() else HOLDOUT_CANDIDATE

    def classify(self, session) -> Classification:
        key = session_key(session)
        category = self.category(key)
        history = (self.usage.get(key, []) + ([self.observed[key]] if key in self.observed
                                              else []) + self.exposed.get(key, []))
        history = sorted(history, key=lambda h: h["seq"])
        first = history[0] if history else None
        latest_usage = (self.usage.get(key) or [None])[-1]
        source = latest_usage or first or {}
        return Classification(
            session=key, category=category,
            dataset_fingerprint=source.get("dataset_fingerprint"),
            first_registered_usage=first["at"] if first else None,
            study_id=source.get("study_id"),
            reason=(source.get("reason") if source else PRE_REGISTRY_REASON)
            or PRE_REGISTRY_REASON,
            code_id=source.get("code_id"),
            history=history)

    def protected_sessions(self, known: Iterable[str] = ()) -> set[str]:
        candidates = set(self.observed) | {session_key(s) for s in known}
        return {s for s in candidates if self.category(s) in PROTECTED}


def fold(log: list[events.Event]) -> State:
    state = State()
    for e in log:
        p = e.payload
        at = e.created_at.isoformat()
        if e.event_type == "usage_registered":
            for s in p["sessions"]:
                state.usage.setdefault(s, []).append({
                    "seq": e.seq, "at": at, "event": e.event_type,
                    "category": p["category"], "study_id": p.get("study_id"),
                    "reason": p.get("reason"), "code_id": p.get("code_id"),
                    "dataset_fingerprint": p.get("dataset_fingerprint")})
        elif e.event_type == "sessions_observed":
            for row in p["sessions"]:
                # The first observation stands. A later re-check cannot turn
                # an excluded session into a clean candidate after the fact.
                state.observed.setdefault(row["session"], {
                    "seq": e.seq, "at": at, "event": e.event_type,
                    "quality": row["quality"], "faults": row.get("faults"),
                    "dataset_fingerprint": row.get("dataset_fingerprint"),
                    "lock_id": p.get("lock_id")})
        elif e.event_type == "exposure_recorded":
            for s in p["sessions"]:
                state.exposed.setdefault(s, []).append({
                    "seq": e.seq, "at": at, "event": e.event_type,
                    "channel": p["channel"], "study_id": p.get("study_id"),
                    "reason": p.get("detail"), "was": p["was"].get(s)})
        elif e.event_type == "holdout_lock_created":
            state.locks.append(p)
        elif e.event_type == events.CONSUMED:
            # The generation is the row's subject — the value the schema
            # constrains unique — never a payload field a forged row could
            # point elsewhere.
            if p.get("lock_id") != e.subject:
                raise events.TamperedLog(
                    f"event {e.seq}: consumption payload names {p.get('lock_id')!r} "
                    f"but spends {e.subject!r}")
            state.consumptions[e.subject] = p
            for s in p["sessions"]:
                state.consumed[s] = e.subject
        elif e.event_type in ("final_evaluation_result", "final_evaluation_failed"):
            state.results[p["lock_id"]] = p
    return state


def state(db: Session) -> State:
    """The registry as of now. Empty — everything pre-registry — without 0010."""
    if not events.available(db):
        return State()
    return fold(events.read(db))


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------

def classify_sessions(db: Session | None, sessions: Iterable) -> list[Classification]:
    current = state(db) if db is not None else State()
    return [current.classify(s) for s in sorted({session_key(s) for s in sessions})]


def register_usage(db: Session, *, sessions: Iterable, category: str, study_id: str,
                   reason: str, dataset_fingerprint: str | None,
                   code_id: str | None, now: datetime | None = None) -> events.Event:
    """Record that a study used these sessions. Refused for protected sessions:
    looking at one is an exposure, and is recorded as such instead."""
    if category not in REGISTRABLE_USAGE:
        raise ValueError(f"usage category must be one of {sorted(REGISTRABLE_USAGE)}")
    keys = sorted({session_key(s) for s in sessions})
    current = state(db)
    blocked = [s for s in keys if current.category(s) in PROTECTED]
    if blocked:
        raise HoldoutAccessDenied(
            f"{len(blocked)} protected holdout session(s) cannot be registered as "
            f"{category}: {blocked[:5]}. If they were looked at, record_exposure.")
    return events.append(db, stream=events.REGISTRY, event_type="usage_registered",
                         subject=study_id, now=now, payload={
                             "sessions": keys, "category": category,
                             "study_id": study_id, "reason": reason,
                             "dataset_fingerprint": dataset_fingerprint,
                             "code_id": code_id})


def archive_sessions(candles: pd.DataFrame) -> list[str]:
    if candles is None or candles.empty:
        return []
    stamps = pd.to_datetime(candles["timestamp"], utc=True)
    return sorted({d.isoformat() for d in stamps.dt.tz_convert(clock_grid.IST).dt.date})


def authoritative_sessions(db: Session) -> set[str]:
    """Every session the database itself holds any record of.

    Index candles of every symbol and timeframe, archived candle revisions,
    option bars and stored signals. A caller's archive list can add to this,
    never subtract from it.
    """
    from sqlalchemy import distinct, select

    from ..data import schema
    from ..models import CandleRecord, CandleRevision, OptionCandle, SignalRecord

    found: set[str] = set()
    if schema.has_table(db, CandleRecord.__tablename__):
        for (stamp,) in db.execute(select(distinct(CandleRecord.timestamp))):
            found.add(session_key(stamp))
    if schema.has_table(db, CandleRevision.__tablename__):
        for (stamp,) in db.execute(select(distinct(CandleRevision.timestamp))):
            found.add(session_key(stamp))
    if schema.has_table(db, OptionCandle.__tablename__):
        for (day,) in db.execute(select(distinct(OptionCandle.session_date))):
            if day is not None:
                found.add(session_key(day))
    if schema.has_table(db, SignalRecord.__tablename__):
        for (stamp,) in db.execute(select(SignalRecord.created_at)):
            if stamp is not None:
                found.add(session_key(stamp))
    return found


def create_lock(db: Session, *, study_id: str, archive: Iterable = (),
                dataset_fingerprint: str, created_at: datetime | None = None,
                target_sessions: int = TARGET_HOLDOUT_SESSIONS) -> dict:
    """Open a prospective holdout generation.

    The boundary is computed, not chosen: the latest session, across the
    database's own records, the registry's whole history and anything the
    caller adds, that is not currently protected. Every session up to it is
    seen (or consumed, or excluded); a clean session after it is unseen only
    if nothing in the registry says otherwise (`State.ever_seen`). Nothing
    about a strategy's results enters the calculation, and a truncated or
    partial `archive` argument cannot pull the boundary earlier.
    """
    if target_sessions <= 0:
        raise ValueError("target_sessions must be positive")
    current = state(db)
    if current.lock is not None and current.lock["lock_id"] not in current.consumptions \
            and current.locked_sessions():
        raise HoldoutError("the active holdout is sealed and unconsumed; it must be "
                           "evaluated or abandoned before a new generation opens")
    history = (set(current.usage) | set(current.exposed) | set(current.consumed)
               | {lock["seen_through_session"] for lock in current.locks}
               | {s for lock in current.locks for s in lock.get("known_sessions", ())})
    known = sorted({session_key(s) for s in archive} | authoritative_sessions(db) | history)
    unprotected = [s for s in known if current.category(s) not in PROTECTED]
    if not unprotected:
        raise HoldoutError("no sessions on record: nothing establishes a boundary")
    boundary = max(unprotected)
    generation = len(current.locks) + 1
    lock_id = f"{study_id}:holdout:{generation}"
    created = (created_at or datetime.now(UTC)).astimezone(UTC)
    payload = {
        "lock_id": lock_id, "study_id": study_id, "generation": generation,
        "holdout_lock_created_at": created.isoformat(),
        "seen_through_session": boundary,
        # Recorded so the lock itself remembers what existed: a session
        # later deleted from the archive is still known to have been seen.
        "known_sessions": [s for s in known if s <= boundary],
        "dataset_fingerprint": dataset_fingerprint,
        "target_sessions": target_sessions,
        "quality_requirements": QUALITY_REQUIREMENTS,
        "boundary_basis": "data availability: latest session on record (database, "
                          "registry history, caller) that is not currently protected",
    }
    events.append(db, stream=events.HOLDOUT, event_type="holdout_lock_created",
                  subject=lock_id, payload=payload, now=created)
    return payload


def observe_sessions(db: Session, candles: pd.DataFrame, *, timeframe: str = "5m",
                     as_of: datetime, now: datetime | None = None) -> list[dict]:
    """Quality-check complete sessions after the boundary as they arrive.

    Collection and validation only: bars are counted against the exchange
    grid and fingerprinted, never passed to strategy code. A session still
    trading at `as_of` is left pending.
    """
    from ..data.dataset import fingerprint

    current = state(db)
    lock = current.lock
    if lock is None:
        return []
    as_of = pd.Timestamp(as_of).tz_convert("UTC")
    # The shared session-quality record, stamped by raw certification
    # (`protection.certify_raw_frame`): a session whose raw bars failed
    # the grid stays faulty after quarantine. A session with no record is
    # QUALITY_UNKNOWN and is not observed at all — it stays pending, never
    # counted, until its quality is established from the raw archive. The
    # first observation is permanent, so a guess is never written.
    quality = clock_grid.session_quality(candles, timeframe)
    days = pd.to_datetime(candles["timestamp"], utc=True).dt.tz_convert(
        clock_grid.IST).dt.date.map(lambda d: d.isoformat()) if len(candles) else pd.Series([])
    rows = []
    for day, verdict in quality.items():
        if current.ever_seen(day) or day <= lock["seen_through_session"] \
                or day in current.observed:
            continue
        close = pd.Timestamp(datetime.combine(date.fromisoformat(day), SESSION_CLOSE),
                             tz=clock_grid.IST).tz_convert("UTC")
        if as_of < close or verdict["quality"] == clock_grid.QUALITY_UNKNOWN:
            continue
        subset = candles[(days == day).to_numpy()] if len(candles) else candles
        rows.append({"session": day,
                     "quality": verdict["quality"],
                     "faults": verdict["faults"],
                     "quality_basis": verdict["basis"],
                     "dataset_fingerprint": fingerprint(subset, "NIFTY", timeframe).hash})
    if rows:
        events.append(db, stream=events.REGISTRY, event_type="sessions_observed",
                      subject=lock["lock_id"], now=now,
                      payload={"sessions": rows, "lock_id": lock["lock_id"],
                               "observed_as_of": as_of.isoformat()})
    return rows


def record_exposure(db: Session, *, sessions: Iterable, channel: str, detail: str,
                    study_id: str | None = None, now: datetime | None = None) -> list[str]:
    """A protected session was shown to strategy logic. It is seen from now on.

    Returns the sessions whose status changed. Sessions already seen are
    left alone — there is nothing further to lose.
    """
    if not events.available(db):
        return []
    current = state(db)
    keys = sorted({session_key(s) for s in sessions})
    hit = [s for s in keys if current.category(s) in PROTECTED]
    if hit:
        events.append(db, stream=events.REGISTRY, event_type="exposure_recorded",
                      subject=study_id or channel, now=now, payload={
                          "sessions": hit, "channel": channel, "detail": detail,
                          "study_id": study_id,
                          "was": {s: current.category(s) for s in hit}})
    return hit


def note_strategy_output(db: Session, *, moment, channel: str, detail: str) -> list[str]:
    """Strategy output was produced for the session containing `moment`.

    Called wherever the live strategy's signals are served or stored. If
    that session is protected holdout data it is now seen, and it is
    recorded as contaminated and committed at once — an exposure that was
    rolled back with some unrelated failure would be an exposure forgotten.
    A database without migration 0010 has no holdout to protect: no-op.
    """
    if not events.available(db):
        return []
    hit = record_exposure(db, sessions=[moment], channel=channel, detail=detail)
    if hit:
        db.commit()
    return hit


# Why something is reading market data. Only the last two may see a
# protected session; everything else — research, features, regimes,
# signals, backtests, reports, charts, streams — is strategy access.
STRATEGY = "strategy_research"
DATA_QUALITY = "data_quality"
COLLECTION = "collection"
# Grading raw observations into CLEAN/FAULTY session verdicts (Pass 2D.3).
# It sees nothing protected: a raw-certification grant is strategy access
# for every other purpose.
RAW_CERTIFICATION = "raw_certification"
PURPOSES = (STRATEGY, DATA_QUALITY, COLLECTION, RAW_CERTIFICATION)
MAY_SEE_PROTECTED = frozenset({DATA_QUALITY, COLLECTION})

# The internal modules that may read protected sessions, and why. Nothing
# else can obtain a grant: not a strategy, not an API caller, not a script.
# A purpose *string* never unlocks anything — a flag anyone can type is not
# an authorisation — so a public caller has nothing to spoof.
TRUSTED_HOLDERS: dict[str, frozenset[str]] = {
    "app.data.angel_history": frozenset({COLLECTION}),   # backfill gap checks
    "app.api.data": frozenset({DATA_QUALITY}),           # readiness counts only
    # The raw loaders: each certifies only bars it has itself just read
    # from the database or pulled from the broker.
    "app.data.research": frozenset({RAW_CERTIFICATION}),
    "app.api.backtest": frozenset({RAW_CERTIFICATION}),
}
_GRANT = object()


class AccessGrant:
    """A capability to see protected sessions, issued only to trusted code."""
    __slots__ = ("holder", "purpose")

    def __init__(self, purpose: str, holder: str, token: object) -> None:
        if token is not _GRANT:
            raise HoldoutAccessDenied("access grants are issued by "
                                      "registry.trusted_access, not constructed")
        self.purpose, self.holder = purpose, holder

    def __repr__(self) -> str:
        return f"AccessGrant({self.purpose!r}, holder={self.holder!r})"


def trusted_access(purpose: str) -> AccessGrant:
    """A grant for the calling module, if it is a trusted internal path."""
    import sys

    check_purpose(purpose)
    holder = sys._getframe(1).f_globals.get("__name__", "")  # noqa: SLF001
    if purpose not in TRUSTED_HOLDERS.get(holder, frozenset()):
        raise HoldoutAccessDenied(f"{holder or 'caller'} is not a trusted "
                                  f"{purpose} path and cannot see protected sessions")
    return AccessGrant(purpose, holder, _GRANT)


def check_purpose(purpose: str) -> None:
    if purpose not in PURPOSES:
        raise ValueError(f"unknown data-access purpose {purpose!r}; "
                         f"expected one of {PURPOSES}")


def access_purpose(access) -> str:
    """What an `access` argument actually permits. Fail closed.

    None is strategy access. Only a genuine grant widens it; a purpose
    string asking for more is refused rather than silently narrowed, so a
    caller that thought it could see everything finds out.
    """
    if access is None or access == STRATEGY:
        return STRATEGY
    if isinstance(access, AccessGrant) and type(access) is AccessGrant:
        return access.purpose
    if isinstance(access, str):
        check_purpose(access)
        raise HoldoutAccessDenied(
            f"the purpose string {access!r} cannot reveal protected sessions; trusted "
            "internal code obtains a grant with registry.trusted_access")
    raise HoldoutAccessDenied(f"{type(access).__name__} is not an access grant")


def withhold(db: Session, frame: pd.DataFrame, *, access=None,
             time_column: str = "timestamp", session_column: str | None = None
             ) -> pd.DataFrame:
    """Remove every row of a protected session unless `access` may see it.

    The registry half of the research boundary (`methodology.protection`):
    candle, as-known, regime and option loaders call this themselves, and
    every other source reaches it through `protect_research_frame`. Withheld
    sessions are named in `attrs["holdout"]`; their contents never leave.
    """
    purpose = access_purpose(access)
    if purpose in MAY_SEE_PROTECTED or frame is None or frame.empty \
            or not events.available(db):
        return frame
    if session_column is not None:
        days = frame[session_column].map(session_key)
    else:
        days = pd.to_datetime(frame[time_column], utc=True).dt.tz_convert(
            clock_grid.IST).dt.date.map(lambda d: d.isoformat())
    protected = state(db).protected_sessions(set(days)) & set(days)
    if not protected:
        return frame
    kept = frame[~days.isin(protected).to_numpy()].reset_index(drop=True)
    kept.attrs = dict(frame.attrs) | {"holdout": {
        "withheld_sessions": sorted(protected),
        "policy": "protected prospective holdout sessions are withheld from "
                  "strategy access"}}
    return kept


def protected_sessions(db: Session, known: Iterable = ()) -> set[str]:
    if not events.available(db):
        return set()
    return state(db).protected_sessions(known)


def assert_accessible(db: Session, sessions: Iterable) -> None:
    """Refuse strategy research on any protected session. Fail closed."""
    blocked = sorted(protected_sessions(db, sessions) & {session_key(s) for s in sessions})
    if blocked:
        raise HoldoutAccessDenied(
            f"{len(blocked)} session(s) are protected holdout data and cannot be "
            f"used for strategy research: {blocked[:5]}")


# ---------------------------------------------------------------------------
# one-time final evaluation
# ---------------------------------------------------------------------------

FROZEN_FIELDS = ("study_id", "trial_id", "strategy_version", "parameter_hash",
                 "code_id", "execution_policy_hash", "cost_model_hash",
                 "primary_metric", "benchmark_ids")


def plan_consumption(db: Session, candidate: dict) -> dict:
    """Validate a final evaluation against one read of the log. Writes nothing.

    Returns the plan, including the log head it was decided on; the write
    that follows is refused if the log has moved since.
    """
    missing = [f for f in FROZEN_FIELDS if candidate.get(f) in (None, "", [])]
    if missing:
        raise HoldoutError(f"candidate is not frozen: missing {missing}")
    if "+dirty" in str(candidate["code_id"]):
        raise HoldoutError("a dirty worktree is not a frozen candidate")

    log = events.read(db)
    head = log[-1].seq if log else 0
    current = fold(log)
    lock = current.lock
    if lock is None:
        raise HoldoutError("no prospective holdout exists")
    if lock["lock_id"] in current.consumptions:
        raise HoldoutError(f"holdout {lock['lock_id']} is already consumed; a new "
                           "generation needs a new lock and new sessions")
    if candidate["study_id"] != lock["study_id"]:
        raise HoldoutError("candidate study does not own this holdout")
    sealed = current.locked_sessions()
    if len(sealed) < lock["target_sessions"]:
        raise HoldoutError(f"holdout not sealed: {len(sealed)} of "
                           f"{lock['target_sessions']} eligible sessions")

    registered = trials.fold([e for e in log if e.stream == events.TRIALS]
                             ).trials.get(candidate["trial_id"])
    if registered is None:
        raise HoldoutError("candidate trial is not preregistered")
    if registered.spec["mode"] != trials.CONFIRMATORY:
        raise HoldoutError("only a confirmatory trial may consume the holdout")
    if registered.results:
        raise HoldoutError("candidate trial already has a result")
    differs = [f for f in FROZEN_FIELDS if f != "trial_id"
               and registered.spec.get(f) != candidate[f]]
    if differs:
        raise HoldoutError(f"candidate differs from its preregistration: {differs}")
    return {"lock_id": lock["lock_id"], "sessions": sealed, "head": head,
            "candidate": {f: candidate[f] for f in FROZEN_FIELDS}}


def commit_consumption(db: Session, plan: dict, *, now: datetime | None = None) -> None:
    """Write the consumption and COMMIT it, or refuse.

    Compare-and-set on the log head, plus the schema's own rule — one
    `consumed_lock_id` per holdout generation, mandatory on every
    consumption row: of any number of concurrent contenders, one commits
    and every other fails here — before anything is evaluated.
    """
    from sqlalchemy.exc import IntegrityError, OperationalError

    try:
        events.append(db, stream=events.HOLDOUT, event_type=events.CONSUMED,
                      subject=plan["lock_id"], now=now, expect_head=plan["head"],
                      payload={"lock_id": plan["lock_id"], "sessions": plan["sessions"],
                               "candidate": plan["candidate"]})
        db.commit()
    except (events.Conflict, IntegrityError, OperationalError) as refused:
        db.rollback()
        raise HoldoutError(f"consumption of {plan['lock_id']} refused: the log changed "
                           f"or it was already consumed ({type(refused).__name__})"
                           ) from refused


def final_evaluation(sessions: Callable[[], Session], candidate: dict,
                     evaluate: Callable[[list[str]], dict], *,
                     now: datetime | None = None) -> dict:
    """Spend the sealed holdout once, on one frozen, preregistered candidate.

    `sessions` is a session factory, not a session: the consumption is
    committed in a transaction of its own *before* `evaluate` runs, so no
    later rollback, crash or failed evaluation in anyone's transaction can
    un-spend it. A failed evaluation leaves the holdout consumed with no
    result — which is correct; it is never refunded.
    """
    with sessions() as tx:
        plan = plan_consumption(tx, candidate)
        tx.rollback()                      # the read is over; the write re-checks
        commit_consumption(tx, plan, now=now)

    try:
        result = evaluate(list(plan["sessions"]))
    except Exception as failure:
        with sessions() as tx:
            events.append(tx, stream=events.HOLDOUT, event_type="final_evaluation_failed",
                          subject=plan["lock_id"], now=now, payload={
                              "lock_id": plan["lock_id"], "error": repr(failure)})
            tx.commit()
        raise
    fingerprint = events.digest(result)
    with sessions() as tx:
        events.append(tx, stream=events.HOLDOUT, event_type="final_evaluation_result",
                      subject=plan["lock_id"], now=now, payload={
                          "lock_id": plan["lock_id"], "trial_id": candidate["trial_id"],
                          "result": result, "result_fingerprint": fingerprint})
        trials.record_result(tx, trial_id=candidate["trial_id"], result=result,
                             inspected=True, partition=f"holdout:{plan['lock_id']}",
                             now=now)
        tx.commit()
    return {"lock_id": plan["lock_id"], "sessions": plan["sessions"], "result": result,
            "result_fingerprint": fingerprint}
