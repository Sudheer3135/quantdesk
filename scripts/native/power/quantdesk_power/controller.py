"""The shutdown transaction: one serialized sequence, journaled at every step.

Order (each step is journaled after it is done; any failure or doubt before
the request is sent restores the recorded prior state and leaves the VM up):

  ACCEPTED_REQUEST  prior state of every unit and timer recorded, before any
                    change; pre-checks (window, clock, holds, special
                    sessions, package activity, API identity, IMDS
                    isolation, open positions). A failed pre-check: ABORTED,
                    nothing changed.
  FROZEN            the observer timers that were active are stopped (no new
                    scheduled run), then the observer's own lock is taken and
                    held (no manual run either) - waiting a bounded time for a
                    run already in progress. The automatic-update timers that
                    were active are stopped (never disabled), and package
                    activity is checked again after the pause. From here the
                    admission gate (gate.py) refuses every start of the
                    workload units in this boot except this transaction's own
                    restoration: the admission barrier for the API and the
                    units it needs, set before the drain.
  EVIDENCE_CLEAR    the observer's shutdown evidence, read under that lock
                    and bound to this transaction, says CLEAR.
  SYSTEM_CLEAR      the controller's own facts, checked again.
  WEB_STOPPED       the dashboard is stopped.
  API_STOPPED       the API is stopped with PostgreSQL and Redis still up, so
                    its scheduler, imports and paper engine stop and its
                    writers drain; it must exit cleanly (status 0, not 70),
                    and its absence must be CONFIRMED (facts.api_presence:
                    PRESENT or UNKNOWN both stop the shutdown).
  WRITERS_EXCLUDED  the controller takes QuantDesk's writer key EXCLUSIVE:
                    proof no writer remains, and a bar on any new write.
  POSITIONS_CLEAR   open paper positions and open journal trades: zero, read
                    under that key, after the drain.
  DRAINED_EVIDENCE_CLEAR  the observer's evidence again, after the drain.
  REDIS_STOPPED, POSTGRES_STOPPED  stopped cleanly. The key's session is
                    verified (same server process, key held) after each of
                    the last three stages; the WAL insert position is read
                    under the key; the session is kept in one statement
                    through PostgreSQL's stop. Then the cluster must report
                    "shut down" AND its shutdown checkpoint must lie exactly
                    at that position: no WAL record, so no commit by anyone,
                    after the last proof the key was held. The session's
                    57P01 ending is required but is NOT taken as proof that
                    the cluster stopped admitting work (an individual
                    termination gives the same code). A lost session or any
                    other WAL abandons the shutdown. WRITER EXCLUSION IS NOT
                    PROVEN (README, "Limits"): the WAL argument is unverified
                    on a real cluster, and standalone writers are not barred
                    before the drain.
  SUBMITTING        recorded (and a status record published) before the
                    deallocation request leaves.
  then NOT_SENT | REJECTED | ACCEPTED | COMPLETED | UNKNOWN (see azure.py).

Restoration — starting what was running before and is not now: PostgreSQL,
Redis, the API, the dashboard, then (after the observer lock is released) the
observer timers and the automatic-update timers — happens only from a state
where Azure's outcome permits it: before the request was sent, NOT_SENT,
REJECTED, an operation whose own status says Failed or Canceled, or an
operator's recorded finding that the deallocation was not performed. Never
after an UNKNOWN or ACCEPTED outcome. RESTORING must be written and read back
intact first, or nothing is started (RestorationWithheld); readiness is then
verified by condition within a bound (await_ready), never by a fixed delay.
"""
from __future__ import annotations

import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import VERSION, evidence, facts, fsafe, journal, locks, requests, status
from .azure import (
    ACCEPTED,
    COMPLETED,
    NOT_SENT,
    POLL_FAILED,
    POLL_SUCCEEDED,
    REJECTED,
)
from .db import TERMINATED_BY_SHUTDOWN, DatabaseError, lsn_value
from .journal import (
    ABORTED,
    API_STOPPED,
    DRAINED_EVIDENCE_CLEAR,
    EVIDENCE_CLEAR,
    FROZEN,
    POSITIONS_CLEAR,
    POSTGRES_STOPPED,
    REDIS_STOPPED,
    RESTORE_FAILED,
    RESTORED,
    RESTORING,
    SUBMITTING,
    SYSTEM_CLEAR,
    UNKNOWN_RETAINED,
    WEB_STOPPED,
    WRITERS_EXCLUDED,
)
from .system import CommandFailed, active, cleanly_stopped

DATA_ORDER = ("postgres", "redis", "api", "web")      # start order on restoration
CLUSTER_SHUT_DOWN = re.compile(r"^Database cluster state:\s+shut down\s*$", re.M)
CHECKPOINT_AT = re.compile(r"^Latest checkpoint location:\s+([0-9A-Fa-f]+/[0-9A-Fa-f]+)\s*$",
                           re.M)
RESTORABLE = {journal.ACCEPTED_REQUEST, *journal.PRE_SUBMIT, journal.NOT_SENT,
              journal.REJECTED, journal.POLL_FAILED, journal.OPERATOR_CONFIRMED_NOT_PERFORMED,
              journal.RESTORE_FAILED, journal.RESTORING}
SHUTDOWN_GRACE = 60          # seconds the holding statement outlives the cluster's stop timeout


class Stop(Exception):
    """This step cannot proceed; restore."""

    def __init__(self, reason: str, data: dict | None = None):
        super().__init__(reason)
        self.reason, self.data = reason, data or {}


@dataclass
class Env:
    """Everything the controller touches, so tests can substitute each part."""
    cfg: dict
    runner: object
    systemd: object
    clock: object                       # () -> aware datetime
    boot_id: object                     # () -> str
    uid_of: object                      # (user) -> uid
    open_db: object                     # (cfg) -> PsqlSession-like
    arm: object                         # Arm-like
    observer_lock: object               # (state_root, uid) -> ObserverLock-like
    obtain_evidence: object = evidence.obtain
    sync: object = os.sync
    sleep: object = time.sleep
    monotonic: object = time.monotonic
    notes: list = field(default_factory=list)

    def now(self) -> datetime:
        return self.clock().astimezone(UTC)


def _now_text(env: Env) -> str:
    return env.now().isoformat()


def snapshot(env: Env) -> dict:
    """ActiveState and UnitFileState of every unit and timer the controller may change."""
    cfg = env.cfg
    found = {"units": {}, "timers": {}}
    for key, unit in cfg["units"].items():
        props = env.systemd.show(unit)
        found["units"][key] = {"unit": unit, "active_state": props["ActiveState"],
                               "unit_file_state": props.get("UnitFileState")}
    for timer in cfg["observer"]["timers"]:
        props = env.systemd.show(timer)
        found["timers"][timer] = {"active_state": props["ActiveState"],
                                  "unit_file_state": props.get("UnitFileState")}
    found["package_timers"] = {}
    for timer in cfg["package_timers"]:
        props = env.systemd.show(timer)
        found["package_timers"][timer] = {"active_state": props["ActiveState"],
                                          "unit_file_state": props.get("UnitFileState")}
    return found


def pre_checks(env: Env, db=None) -> list[facts.Fact]:
    cfg, now = env.cfg, env.now()
    _, hold_fact = facts.read_holds(cfg, now)
    found = [facts.in_window(now, cfg), facts.clock_synchronized(env.runner, cfg), hold_fact,
             facts.special_sessions(cfg, now),
             facts.package_activity(cfg, env.systemd),
             facts.api_identity(cfg, env.runner, env.systemd),
             facts.imds_isolation(cfg, env.runner)]
    if db is not None:
        try:
            paper, open_trades = db.open_positions()
            ok = paper == 0 and open_trades == 0
            found.append(facts.Fact("open_positions", facts.PASS if ok else facts.BLOCKED,
                                    f"{paper} open paper position(s), {open_trades} open "
                                    "journal trade(s)",
                                    {"paper": paper, "journal": open_trades}))
        except DatabaseError as exc:
            found.append(facts.Fact("open_positions", facts.UNKNOWN,
                                    f"positions could not be read: {exc}"))
    return found


class Transaction:
    """One shutdown attempt, from an accepted request to its last journal record."""

    def __init__(self, env: Env, jrnl: journal.Journal, txn: journal.Transaction):
        self.env, self.journal, self.txn = env, jrnl, txn
        self.lock = None
        self.db = None
        self.boot = env.boot_id()

    def record(self, state: str, data: dict | None = None) -> None:
        self.journal.append(self.txn, state, self.boot, data or {}, clock=lambda: _now_text(
            self.env))

    # -- the forward path ------------------------------------------------------------------
    def run(self) -> str:
        cfg, env = self.env.cfg, self.env
        prior = self.txn.first["data"]["prior"]
        try:
            try:
                self.db = env.open_db(cfg)
            except (OSError, DatabaseError) as exc:
                self.record(ABORTED, {"reason": f"the database session could not be opened: "
                                                f"{exc}"})
                return ABORTED
            checks = pre_checks(env, self.db)
            if facts.worst(checks) != facts.PASS:
                self.record(ABORTED, {"reason": "a pre-check did not pass",
                                      "facts": [f.as_dict() for f in checks]})
                return ABORTED
            api = next(f for f in checks if f.name == "api_identity").data

            stopped = []
            for timer, state in prior["timers"].items():
                if state["active_state"] in ("active", "activating"):
                    env.systemd.stop(timer, cfg["timeouts"]["command"])
                    stopped.append(timer)
            self.lock = env.observer_lock(cfg["observer"]["state_root"],
                                          env.uid_of(cfg["observer"]["user"]))
            try:
                self.lock.acquire(cfg["observer"]["lock_wait_seconds"])
            except (locks.Busy, OSError) as exc:
                raise Stop(f"the observer lock could not be held: {exc}") from exc
            paused = []
            for timer, state in prior["package_timers"].items():
                if state["active_state"] in ("active", "activating"):
                    env.systemd.stop(timer, cfg["timeouts"]["command"])
                    paused.append(timer)
            packages = facts.package_activity(cfg, env.systemd)
            if packages.status != facts.PASS:
                raise Stop("package management is active after automatic updates were paused",
                           {"fact": packages.as_dict()})
            frozen_at = env.now()
            self.record(FROZEN, {"timers_stopped": stopped, "frozen_at_utc": frozen_at.isoformat(),
                                 "observer_lock": list(self.lock.identity),
                                 "package_timers_paused": paused})

            first = self._evidence(api, False, frozen_at)
            self.record(EVIDENCE_CLEAR, {"evidence_sha256": first["integrity"]["content_sha256"],
                                         "evidence": first})

            again = pre_checks(env)
            again_api = next(f for f in again if f.name == "api_identity").data
            if facts.worst(again) != facts.PASS or again_api != api:
                raise Stop("a fact changed or could not be re-established",
                           {"facts": [f.as_dict() for f in again]})
            self.record(SYSTEM_CLEAR, {"facts": [f.as_dict() for f in again]})

            self._stop("web", require_clean=False)
            self.record(WEB_STOPPED, {})
            self._stop("api", require_clean=True)
            presence = facts.api_presence(cfg, env.runner, env.systemd)
            if presence.data.get("presence") != facts.ABSENT:
                raise Stop(f"the API's absence is not confirmed: {presence.detail}",
                           {"api_presence": presence.as_dict()})
            self.record(API_STOPPED, {"api_presence": presence.as_dict()})

            try:
                if not self.db.take_writer_key():
                    raise Stop("a QuantDesk writer still holds the writer key")
                others = self.db.role_sessions(cfg["postgres"]["app_role"])
            except DatabaseError as exc:
                raise Stop(f"writer exclusion could not be established: {exc}") from exc
            self.record(WRITERS_EXCLUDED, {"writer_key": "exclusive",
                                           "key_session_pid": self.db.key_pid,
                                           "application_role_sessions": others})
            try:
                paper, open_trades = self.db.open_positions()
            except DatabaseError as exc:
                raise Stop(f"positions could not be re-read after the drain: {exc}") from exc
            if paper or open_trades:
                raise Stop("positions are open after the drain",
                           {"paper": paper, "journal": open_trades})
            self._key_still_held("the position recheck")
            self.record(POSITIONS_CLEAR, {"paper": 0, "journal": 0})

            drained = self._evidence(api, True, frozen_at)
            self._key_still_held("the drained evidence")
            self.record(DRAINED_EVIDENCE_CLEAR,
                        {"evidence_sha256": drained["integrity"]["content_sha256"],
                         "evidence": drained})

            self._stop("redis", require_clean=True)
            self._key_still_held("Redis's stop")
            self.record(REDIS_STOPPED, {})
            self._stop_postgres_holding_the_key()
            self._cluster_shut_down()
            self.record(POSTGRES_STOPPED, {"session_ending": "57P01 (this session only)",
                                           "wal_at_last_proof": self.db.wal_lsn,
                                           "shutdown_checkpoint": "at the same position"})
        except Stop as stop:
            return self.restore(stop.reason, stop.data)
        except Exception as exc:  # noqa: BLE001 - any failure before the request restores
            outcome = self.restore(f"{type(exc).__name__}: {exc}")
            if isinstance(exc, journal.CorruptJournal):
                raise
            return outcome
        finally:
            if self.db is not None and self.txn.state == ABORTED:
                self.db.close()
        return self.submit()

    def _evidence(self, api: dict, api_stopped: bool, frozen_at: datetime) -> dict:
        try:
            record = self.env.obtain_evidence(
                self.env.cfg, self.env.runner, txn_id=self.txn.txn_id, boot_id=self.boot,
                lock_fd=self.lock.fd, api=api, api_stopped=api_stopped, frozen_at=frozen_at,
                clock=self.env.clock)
        except evidence.EvidenceRejected as exc:
            raise Stop(f"observer evidence rejected: {exc}") from exc
        if record["verdict"] != "CLEAR":
            raise Stop(f"the observer's evidence says {record['verdict']}",
                       {"reasons": record.get("reasons")})
        return record

    def _stop(self, key: str, require_clean: bool) -> None:
        cfg = self.env.cfg
        unit = cfg["units"][key]
        self.env.systemd.stop(unit, cfg["timeouts"][f"stop_{key}"])
        props = self.env.systemd.show(unit)
        ok = cleanly_stopped(props) if require_clean else props["ActiveState"] == "inactive"
        if not ok:
            raise Stop(f"{unit} did not stop cleanly",
                       {k: props.get(k) for k in ("ActiveState", "Result", "ExecMainStatus")})

    def _key_still_held(self, after: str) -> None:
        try:
            self.db.verify_writer_key()
        except DatabaseError as exc:
            raise Stop(f"the writer key was lost after {after}: {exc}") from exc

    def _stop_postgres_holding_the_key(self) -> None:
        """Stop the cluster while the key's session waits in one statement; it must be the
        cluster's own shutdown that ends that session."""
        cfg = self.env.cfg
        try:
            self.db.hold_through_shutdown(int(cfg["timeouts"]["stop_postgres"]) + SHUTDOWN_GRACE)
        except DatabaseError as exc:
            raise Stop(f"the writer key could not be held into the cluster's stop: {exc}") \
                from exc
        if not self.db.guard_intact():
            raise Stop("the writer key's session ended before the cluster's stop was issued")
        self._stop("postgres", require_clean=True)
        outcome = self.db.shutdown_outcome(cfg["timeouts"]["command"])
        self.db.close()
        if outcome != TERMINATED_BY_SHUTDOWN:
            raise Stop("the writer key's session did not last until the cluster shut down",
                       {"session_outcome": outcome})

    def _cluster_shut_down(self) -> None:
        pg, cfg = self.env.cfg["postgres"], self.env.cfg
        result = self.env.runner.run([cfg["programs"]["pg_controldata"], "-D", pg["data_dir"]],
                                     cfg["timeouts"]["command"], user=pg["cluster_user"],
                                     env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C"})
        if result.returncode != 0 or not CLUSTER_SHUT_DOWN.search(result.stdout):
            raise Stop("the database cluster does not report a clean shutdown")
        found = CHECKPOINT_AT.search(result.stdout)
        proved = getattr(self.db, "wal_lsn", None)
        try:
            same = found is not None and proved is not None and \
                lsn_value(found.group(1)) == lsn_value(proved)
        except ValueError:
            same = False
        if not same:
            raise Stop("WAL was written after the writer key was last proved held, or the "
                       "shutdown checkpoint cannot be located",
                       {"wal_at_last_proof": proved,
                        "shutdown_checkpoint": found.group(1) if found else None})

    # -- the request ------------------------------------------------------------------------
    def submit(self) -> str:
        env = self.env
        env.sync()
        client_id = str(uuid.UUID(self.txn.txn_id))
        self.record(SUBMITTING, {"client_request_id": client_id})
        publish_status(env, self.journal, self.boot)
        sub = env.arm.deallocate(client_id)
        self.record(sub.outcome, sub.as_dict())
        if sub.outcome in (NOT_SENT, REJECTED):
            return self.restore(f"the deallocation was {sub.outcome.lower().replace('_', ' ')}")
        if sub.outcome == COMPLETED:
            return COMPLETED
        if sub.outcome != ACCEPTED:
            return journal.UNKNOWN
        state, detail, resume = env.arm.follow(sub.as_dict())
        if state == POLL_SUCCEEDED:
            self.record(journal.POLL_SUCCEEDED, {"detail": detail})
            return journal.POLL_SUCCEEDED
        if state == POLL_FAILED:
            self.record(journal.POLL_FAILED, {"detail": detail})
            return self.restore("the operation's own status says it did not complete")
        self.record(UNKNOWN_RETAINED, {"detail": detail, "poll_not_before_utc": resume})
        return UNKNOWN_RETAINED

    # -- restoration --------------------------------------------------------------------------
    def restore(self, reason: str, data: dict | None = None) -> str:
        return restore(self.env, self.journal, self.txn, reason, data, lock=self.lock,
                       db=self.db, boot=self.boot)


READY_POLL_SECONDS = 2.0       # between readings of the started units
READY_STABLE_SECONDS = 15.0    # every unit ready, identity unchanged, for at least this long


class RestorationWithheld(journal.CorruptJournal):
    """The record that must precede any start could not be written and read back.

    Nothing was started; the journal is left exactly as it is, and recovery
    retries the restoration (or resumption) from the state it shows.
    """


PROCESS_NUMBER = re.compile(r"^[1-9][0-9]{0,18}$")   # decimal, no sign, no padding, nonzero
PID_LIMIT = 4194304                                # Linux's largest possible pid_max


def _number(props: dict, name: str, limit: int | None = None) -> int | None:
    """A systemd property that must be a positive decimal integer, or None if it is not."""
    value = props.get(name)
    if not isinstance(value, str) or not PROCESS_NUMBER.match(value):
        return None
    number = int(value)
    return number if limit is None or number <= limit else None


def _reading(env: Env, unit: str, deadline: float | None = None) -> tuple[bool, str, tuple | None]:
    """(ready, why, identity): one reading of one unit.

    A service (PostgreSQL, Redis, the API, the dashboard) is ready only when
    systemd reports it active AND running, with a MainPID and a main-process
    start time that are both present, decimal, nonzero and in range - for the
    Type=notify database and Redis units, active means the server itself
    reported ready. For the API, additionally exactly one API process that is
    that MainPID, with the same start time as systemd reports in the same
    reading. The identity (MainPID, start time) is a pair of integers; it must
    not change while readiness is held (a change is a restart). Missing,
    malformed, zero, inconsistent or unreadable identity is not ready, so a
    unit that never shows an identity can never be counted as stable.

    A timer has no process: it is ready when systemd reports it active, and
    its identity is the constant ("timer",).

    With `deadline` (an env.monotonic() instant), every inspection is refused
    when no time is left and otherwise waits at most until the deadline, and a
    reading that finishes at or after it is not ready.
    """
    if deadline is None:
        budget = None
    else:
        def budget() -> float:
            return deadline - env.monotonic()
        if budget() <= 0:
            return False, "no time was left before the readiness deadline to read it", None
    try:
        props = env.systemd.show(unit) if budget is None \
            else env.systemd.show(unit, timeout=budget())
    except CommandFailed as exc:
        return False, f"its state cannot be read: {exc}", None
    ready, why, identity = _judge(env, unit, props, budget)
    if budget is not None and budget() <= 0:
        return False, "its reading finished at or after the readiness deadline", None
    return ready, why, identity


def _judge(env: Env, unit: str, props: dict, budget) -> tuple[bool, str, tuple | None]:
    cfg = env.cfg
    state, sub = props.get("ActiveState"), props.get("SubState")
    if unit.endswith(".timer"):
        if state != "active":
            return False, f"systemd reports the timer {state} ({sub or '?'})", None
        return True, "the timer is active (a timer has no process)", ("timer",)
    if state != "active" or sub != "running":
        return False, f"systemd reports it {state} ({sub or '?'})", None
    pid = _number(props, "MainPID", PID_LIMIT)
    if pid is None:
        return False, f"systemd gives no valid MainPID ({props.get('MainPID')!r})", None
    started = _number(props, "ExecMainStartTimestampMonotonic")
    if started is None:
        return False, (f"systemd gives no valid main-process start time "
                       f"({props.get('ExecMainStartTimestampMonotonic')!r})"), None
    identity = (pid, started)
    if unit == cfg["units"]["api"]:
        fact = facts.api_identity(cfg, env.runner, env.systemd) if budget is None \
            else facts.api_identity(cfg, env.runner, env.systemd, budget=budget)
        if fact.status != facts.PASS:
            return False, fact.detail, None
        seen = (fact.data.get("main_pid"), fact.data.get("main_started_monotonic_usec"))
        if seen != identity:
            return False, (f"the API's identity is inconsistent within one reading "
                           f"({identity} then {seen})"), None
        return True, fact.detail, identity
    return True, f"active and running as MainPID {pid}", identity


def await_ready(env: Env, units: list[str]) -> dict[str, tuple[str, str]]:
    """Wait, bounded, until every unit is ready and stays so: {unit: (outcome, detail)}.

    ONE absolute monotonic deadline covers the whole assessment: the configured
    start timeout plus the stable period, fixed when the assessment begins and
    never extended. Before each inspection the time left is checked and the
    inspection's own timeout is capped to it; after each reading the clock is
    read again, and a reading that finishes at or after the deadline counts as
    not ready. Stability is timed from the completion of the first complete,
    all-ready reading, and restarts whenever an identity changes. The clock is
    read once more immediately before `verified` is returned.

    At the deadline a unit whose last reading was not ready is `not_running`;
    one that was ready while the whole set never held steady long enough
    before the deadline is `unconfirmed`. Both are failures.
    """
    if not units:
        return {}
    limit = float(env.cfg["timeouts"]["start_unit"]) + READY_STABLE_SECONDS
    deadline = env.monotonic() + limit
    steady_since, previous, restarted = None, None, set()
    last = {unit: (False, "it was never read before the readiness deadline", None)
            for unit in units}
    while True:
        readings = {}
        for unit in units:
            if env.monotonic() >= deadline:
                break
            readings[unit] = _reading(env, unit, deadline)
        finished = env.monotonic()
        last.update(readings)
        complete = len(readings) == len(units) and finished < deadline
        if complete and all(r[0] for r in readings.values()):
            identities = {unit: r[2] for unit, r in readings.items()}
            if previous is not None and identities != previous:
                restarted |= {u for u in units if identities[u] != previous[u]}
            if steady_since is None or identities != previous:
                steady_since = finished
            previous = identities
            held = finished - steady_since
            if held >= READY_STABLE_SECONDS and env.monotonic() < deadline:
                return {unit: ("verified", f"{why}; ready and unchanged for {held:.0f} s")
                        for unit, (_, why, _) in readings.items()}
        else:
            steady_since, previous = None, None
        remaining = deadline - env.monotonic()
        if remaining <= 0:
            result = {}
            for unit, (ok, why, _) in last.items():
                if not ok:
                    result[unit] = ("not_running", f"not ready within {limit:.0f} s: {why}")
                else:
                    note = " (it restarted while being verified)" if unit in restarted else ""
                    result[unit] = ("unconfirmed", f"ready, but the workload did not stay ready "
                                                   f"and unchanged for {READY_STABLE_SECONDS:.0f}"
                                                   f" s within {limit:.0f} s{note}")
            return result
        env.sleep(min(READY_POLL_SECONDS, remaining))


def bring_back(env: Env, prior: dict, note, lock=None) -> tuple[list[str], list[str]]:
    """Start what ran before and does not now, then verify it all runs: (started, problems).

    `note(unit, outcome, detail)` persists each unit's progress before the next
    is touched. Workload units first (PostgreSQL, Redis, the API, the dashboard),
    then - after the observer lock, if held, is released - the observer and
    automatic-update timers. Verification is bounded and condition-based
    (await_ready): every required unit ready, identities unchanged for a
    stable period, the API exactly one process that is systemd's MainPID.
    Nothing here sends anything to Azure.
    """
    cfg, started, problems = env.cfg, [], []
    units = [prior["units"][key]["unit"] for key in DATA_ORDER
             if prior["units"][key]["active_state"] in ("active", "activating", "reloading")]
    timer_states = {**prior["timers"], **prior.get("package_timers", {})}
    timers = [t for t, state in timer_states.items()
              if state["active_state"] in ("active", "activating")]

    def start(unit: str, timeout: float) -> None:
        try:
            if active(env.systemd.show(unit)):
                note(unit, "already_active", "")
                return
            env.systemd.start(unit, timeout)
            started.append(unit)
            note(unit, "started", "")
        except CommandFailed as exc:
            problems.append(f"{unit}: {exc}")
            note(unit, "failed", str(exc))
    for unit in units:
        start(unit, cfg["timeouts"]["start_unit"])
    if lock is not None:
        lock.release()
    for timer in timers:
        start(timer, cfg["timeouts"]["command"])
    for unit, (outcome, why) in await_ready(env, units + timers).items():
        note(unit, outcome, why)
        if outcome != "verified":
            problems.append(f"{unit} is not verified running after the start: {why}")
    return started, problems


def _noter(env: Env, jrnl: journal.Journal, txn: journal.Transaction, boot: str, failures: list):
    progress = None

    def note(unit: str, outcome: str, detail: str) -> None:
        nonlocal progress
        try:
            if progress is None:
                progress = journal.ProgressLog(jrnl.state_dir, owner=jrnl.owner)
            progress.append(txn, unit, outcome, detail, boot, clock=lambda: _now_text(env))
        except (OSError, journal.CorruptJournal) as exc:
            failures.append(exc)       # keep going; reported once the units are handled
    return note


def restore(env: Env, jrnl: journal.Journal, txn: journal.Transaction, reason: str,
            data: dict | None = None, lock=None, db=None, boot: str | None = None) -> str:
    """Bring back what was running before, if the transaction's state permits it.

    RESTORING is journaled, and read back intact, before anything is started; if
    that fails nothing is started (RestorationWithheld). Each unit's progress is
    kept in the progress log; RESTORED only when every required unit is verified
    ready, otherwise RESTORE_FAILED, which recovery retries.
    """
    if txn.state not in RESTORABLE:
        raise journal.CorruptJournal(f"restoration is not permitted from {txn.state}")
    boot = boot or env.boot_id()

    def record(state, payload):
        jrnl.append(txn, state, boot, payload, clock=lambda: _now_text(env))

    if db is not None:
        try:
            db.close()
        except Exception:  # noqa: BLE001 - the session may already be gone
            pass
    if txn.state != RESTORING:
        try:
            jrnl.append_durably(txn, RESTORING, boot, {"reason": reason, "from_state": txn.state,
                                                       **(data or {})},
                                clock=lambda: _now_text(env))
        except (OSError, journal.CorruptJournal) as exc:
            # Never start anything that the journal does not show is being restored.
            raise RestorationWithheld(
                f"restoration withheld, nothing started: RESTORING could not be durably "
                f"journaled from {txn.state} ({type(exc).__name__}: {exc}); nothing further "
                "is written and recovery retries from what the journal shows") from exc
    journal_errors: list = []
    started, problems = bring_back(env, txn.first["data"]["prior"],
                                   _noter(env, jrnl, txn, boot, journal_errors), lock=lock)
    if journal_errors:
        result = "failed: " + "; ".join(problems) if problems else "complete"
        raise journal.CorruptJournal(f"restoration ran ({result}) but its progress could not "
                                     f"be kept: {journal_errors[0]}")
    if problems:
        record(RESTORE_FAILED, {"started": started, "problems": problems})
        return RESTORE_FAILED
    record(RESTORED, {"started": started})
    return RESTORED


def resume(env: Env, jrnl: journal.Journal, txn: journal.Transaction, reason: str) -> str:
    """Bring the workload back after a deallocation Azure settled, in a LATER boot.

    Separate from Azure's outcome: RESUMING is journaled first (one record per
    attempt; an interrupted attempt is followed by a new one), each unit's
    progress is kept, and only verified running units give RESUMED. Never sends
    anything to Azure; a failure is RESUME_FAILED, retried by recovery.
    """
    if txn.state not in journal.RESUMABLE:
        raise journal.CorruptJournal(f"resumption is not permitted from {txn.state}")
    boot = env.boot_id()
    submitted = next((r["boot_id"] for r in txn.records if r["state"] == SUBMITTING), None)
    if submitted is None or submitted == boot:
        raise journal.CorruptJournal("resumption needs a later boot than the one that asked "
                                     "for the deallocation")

    def record(state, payload):
        jrnl.append(txn, state, boot, payload, clock=lambda: _now_text(env))
    try:
        jrnl.append_durably(txn, journal.RESUMING, boot, {"reason": reason,
                                                          "from_state": txn.state},
                            clock=lambda: _now_text(env))
    except (OSError, journal.CorruptJournal) as exc:
        raise RestorationWithheld(
            f"resumption withheld, nothing started: RESUMING could not be durably journaled "
            f"from {txn.state} ({type(exc).__name__}: {exc}); recovery retries") from exc
    failures: list = []
    started, problems = bring_back(env, txn.first["data"]["prior"],
                                   _noter(env, jrnl, txn, boot, failures))
    if failures:
        raise journal.CorruptJournal(f"resumption ran but its progress could not be kept: "
                                     f"{failures[0]}")
    if problems:
        record(journal.RESUME_FAILED, {"started": started, "problems": problems})
        return journal.RESUME_FAILED
    record(journal.RESUMED, {"started": started})
    return journal.RESUMED


# -- status --------------------------------------------------------------------------------
def publish_status(env: Env, jrnl: journal.Journal, boot: str | None = None) -> str | None:
    """Keep one signed status record. Never affects the transaction: a failure is reported."""
    try:
        try:
            txns = jrnl.all()
        except journal.CorruptJournal:
            txns = None
        return status.publish(env.cfg, jrnl.state_dir, txns, boot or env.boot_id(), env.now())
    except Exception as exc:  # noqa: BLE001 - a status problem must not change the outcome
        print(f"power: the status record could not be kept: {type(exc).__name__}: {exc}")
        return None


# -- entry points -----------------------------------------------------------------------------
def open_state(cfg: dict) -> fsafe.HeldDir:
    return fsafe.open_dir(cfg["state_root"], owner=os.geteuid(), private=True, create=True,
                          mode=0o700)


def handle_requests(env: Env, reconcile) -> int:
    """The path unit's action: settle earlier transactions, then act on at most one request."""
    cfg = env.cfg
    state_dir = open_state(cfg)
    try:
        try:
            coordination = locks.coordination_lock(state_dir)
        except locks.Busy:
            print("power: another controller run is in progress; nothing done")
            return 0
        try:
            jrnl = journal.Journal(state_dir, owner=os.geteuid())
            unresolved = reconcile(env, jrnl)
            request_dir = fsafe.open_dir(cfg["request_dir"], owner=facts.ROOT_UID,
                                         private=False)
            try:
                nonce, refusals = requests.consume(
                    request_dir, state_dir, env.uid_of(cfg["requester_user"]), env.boot_id(),
                    env.now())
            finally:
                request_dir.close()
            for refusal in refusals:
                print(f"power: request refused: {refusal['reason']}")
            if nonce is None:
                publish_status(env, jrnl)
                return 1 if unresolved else 0
            if unresolved:
                print("power: an earlier transaction is unresolved "
                      f"({', '.join(t.txn_id for t in unresolved)}); the request is not acted on")
                return 1
            try:
                prior = snapshot(env)
            except CommandFailed as exc:
                print(f"power: the prior state could not be recorded ({exc}); nothing done")
                return 1
            txn = jrnl.begin(secrets.token_hex(16), env.boot_id(),
                             {"nonce": nonce, "prior": prior, "controller_version": VERSION})
            try:
                outcome = Transaction(env, jrnl, txn).run()
            finally:
                publish_status(env, jrnl)
            print(f"power: transaction {txn.txn_id}: {outcome}")
            return 0 if outcome in (ABORTED, RESTORED, journal.COMPLETED,
                                    journal.POLL_SUCCEEDED) else 1
        finally:
            os.close(coordination)
    finally:
        state_dir.close()


def file_request(cfg: dict, now: datetime, boot) -> int:
    """The unprivileged requester's action: inside the window, file one request.

    `cfg` is the requester's own configuration (config.load_request): the
    request directory and the window, nothing else. `boot` is the boot id or
    a callable giving it, read only when a request is filed.
    """
    if facts.in_window(now, cfg).status != facts.PASS:
        print("power-request: outside the attempt window; nothing filed")
        return 0
    boot = boot() if callable(boot) else boot
    # Search-only: the requester may create a name in the directory, never list or read it.
    request_dir = fsafe.open_dir(cfg["request_dir"], owner=facts.ROOT_UID, private=False,
                                 search_only=True)
    try:
        nonce = requests.make_request(request_dir, boot, now)
    finally:
        request_dir.close()
    print(f"power-request: filed {nonce[:8]}…")
    return 0
