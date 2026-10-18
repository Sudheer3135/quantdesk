"""The power controller (scripts/native/power), on synthetic state only.

Nothing here reaches Azure, systemd, PostgreSQL or the network. systemd, the
database session, the observer's evidence and Azure Resource Manager are
replaced by recording fakes; files, locks and journal records are real, in a
temporary directory. Tests that need a competing process start a real one.
"""
import json
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

POWER = Path(__file__).resolve().parents[1] / "scripts/native/power"
OBSERVER = Path(__file__).resolve().parents[1] / "scripts/native/observer"
sys.path.insert(0, str(POWER))

from quantdesk_power import (  # noqa: E402
    azure,
    config,
    controller,
    evidence,
    facts,
    fsafe,
    gate,
    journal,
    locks,
    recovery,
    requests,
    special,
    status,
)
from quantdesk_power import db as db_module  # noqa: E402
from quantdesk_power import system as system_module  # noqa: E402
from quantdesk_power.azure import Submission  # noqa: E402
from quantdesk_power.db import DatabaseError  # noqa: E402
from quantdesk_power.system import CommandFailed, Result  # noqa: E402

IST = facts.IST
BOOT = "0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e"
BOOT2 = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
MONO = 9184000000
TIMERS = ["quantdesk-observer-liveness.timer", "quantdesk-observer-premarket.timer",
          "quantdesk-observer-midsession.timer", "quantdesk-observer-afterclose.timer"]
UNITS = {"web": "quantdesk-web.service", "api": "quantdesk-api.service",
         "redis": "quantdesk-redis.service", "postgres": "quantdesk-postgres.service"}
NFT_LISTING = textwrap.dedent("""\
    table inet quantdesk_imds {
        chain output {
            type filter hook output priority filter; policy accept;
            ip daddr 169.254.169.254 meta skuid != 0 counter packets 0 bytes 0 drop \
comment "qd-imds-nonroot-drop"
        }
    }
""")
PACKAGE_TIMERS = ["apt-daily.timer", "apt-daily-upgrade.timer"]
# MainPIDs of the running services in the fake systemd (the API's is the one ps lists).
SERVICE_PIDS = {UNITS["postgres"]: "901", UNITS["redis"]: "902", UNITS["api"]: "943",
                UNITS["web"]: "944"}
WAL_AT_PROOF = "0/16B3A28"


def nft_json(rules=None, chain=None, extra=()):
    """`nft -j list table inet quantdesk_imds` for the shipped rule (libnftables JSON)."""
    base = {"family": "inet", "table": "quantdesk_imds"}
    chain = {**base, "name": "output", "handle": 1, "type": "filter", "hook": "output",
             "prio": 0, "policy": "accept", **(chain or {})}
    chain.pop("table_override", None)
    drop = [{"match": {"op": "==", "left": {"payload": {"protocol": "ip", "field": "daddr"}},
                       "right": "169.254.169.254"}},
            {"match": {"op": "!=", "left": {"meta": {"key": "skuid"}}, "right": 0}},
            {"counter": {"packets": 0, "bytes": 0}}, {"drop": None}]
    rules = [{"expr": drop}] if rules is None else rules
    objects = [{"metainfo": {"version": "1.0.9", "json_schema_version": 1}},
               {"table": {"family": "inet", "name": "quantdesk_imds", "handle": 1}},
               {"chain": chain}]
    objects += [{"rule": {**base, "chain": chain["name"], "handle": 2 + i,
                          "comment": "qd-imds-nonroot-drop", **r}} for i, r in enumerate(rules)]
    objects += list(extra)
    return json.dumps({"nftables": objects})


NFT_JSON = nft_json()


def ist(text: str, day: str = "2026-10-27") -> datetime:       # a Tuesday
    return datetime.fromisoformat(f"{day}T{text}+05:30")


@pytest.fixture(autouse=True)
def unprivileged_root(monkeypatch):
    """Operator-written files would be root's; here they are the test user's."""
    monkeypatch.setattr(facts, "ROOT_UID", os.geteuid())


def write_calendar(path: Path, special=(), closed=(), reviewed=True) -> None:
    dates = {}
    day = datetime(2026, 10, 20).date()
    for _ in range(30):
        kind = ("SPECIAL" if day.isoformat() in special else
                "CLOSED" if day.weekday() >= 5 or day.isoformat() in closed else "NORMAL")
        entry = {"session_type": kind, "review_status": "REVIEWED" if reviewed else "UNREVIEWED"}
        if reviewed:
            entry.update(reviewer="Astra", reviewed_at="2026-10-16T18:00:00+05:30",
                         official_reference="NSE/FAOP/71777")
        dates[day.isoformat()] = entry
        day += timedelta(days=1)
    path.write_text(json.dumps({"schema": "observer_session_calendar/2", "dates": dates}))


def make_cfg(tmp_path: Path, **over) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    etc = tmp_path / "etc"
    etc.mkdir(mode=0o700, exist_ok=True)
    (etc / "holds").mkdir(mode=0o700, exist_ok=True)
    calendar = etc / "session_calendar.json"
    if not calendar.exists():
        write_calendar(calendar)
    observer_cfg = etc / "observer.json"
    observer_cfg.write_text('{"observer": "configuration"}')
    run = tmp_path / "run"
    run.mkdir(mode=0o700, exist_ok=True)
    (run / "requests").mkdir(mode=0o700, exist_ok=True)
    cfg = json.loads((POWER / "config/power.json.example").read_text())
    cfg.update(state_root=str(tmp_path / "state"), request_dir=str(run / "requests"),
               calendar_file=str(calendar), holds_dir=str(etc / "holds"))
    cfg["observer"].update(state_root=str(tmp_path / "observer-state"), config=str(observer_cfg))
    cfg["package_locks"] = [str(tmp_path / "no-such-lock")]
    for key, value in over.items():
        cfg[key] = value
    return config.validate(cfg)


# -- fakes ---------------------------------------------------------------------------------
class FakeSystemd:
    """Units and timers with ActiveState; stop/start change them. Failures are scripted."""

    def __init__(self, inactive_timers=(), stop_result=None, start_fail=(), stop_fail=()):
        self.state = {u: "active" for u in UNITS.values()}
        self.state.update({t: ("inactive" if t in inactive_timers else "active") for t in TIMERS})
        self.state.update({"apt-daily.service": "inactive", "apt-daily-upgrade.service":
                           "inactive", "unattended-upgrades.service": "inactive"})
        self.state.update({t: "active" for t in PACKAGE_TIMERS})
        self.stop_result = stop_result or {}          # unit -> (Result, ExecMainStatus, state)
        self.start_fail, self.stop_fail = set(start_fail), set(stop_fail)
        self.calls = []
        self.show_timeouts = []

    def show(self, unit, timeout=None):
        # Pass 24: an optional per-call timeout (the readiness deadline's budget) is
        # accepted and recorded, exactly as the real Systemd.show now accepts it.
        self.show_timeouts.append((unit, timeout))
        if unit not in self.state:
            raise CommandFailed(f"{unit} is not loaded")
        result, status = "success", "0"
        if unit in self.stop_result and self.state[unit] != "active":
            result, status, _ = self.stop_result[unit]
        running = self.state[unit] == "active"
        # Pass 22 -> 23: every running service has its own MainPID and a running SubState,
        # as on systemd; timers have no process (MainPID 0) and wait.
        service = unit.endswith(".service")
        return {"ActiveState": self.state[unit], "LoadState": "loaded",
                "UnitFileState": "enabled", "Result": result, "ExecMainStatus": status,
                "SubState": ("running" if service else "waiting") if running else "dead",
                "MainPID": SERVICE_PIDS.get(unit, "0") if running and service else "0",
                "ExecMainStartTimestampMonotonic": str(MONO)}

    def stop(self, unit, timeout):
        self.calls.append(("stop", unit))
        if unit in self.stop_fail:
            raise CommandFailed(f"systemctl stop {unit} failed")
        self.state[unit] = self.stop_result.get(unit, (None, None, "inactive"))[2]

    def start(self, unit, timeout):
        self.calls.append(("start", unit))
        if unit in self.start_fail:
            raise CommandFailed(f"systemctl start {unit} failed")
        self.state[unit] = "active"

    def active(self, unit):
        return self.state[unit] == "active"


class FakeRunner:
    def __init__(self, systemd, nft=NFT_JSON, ntp="yes", controldata=None, extra_api=False):
        self.systemd, self.nft, self.ntp = systemd, nft, ntp
        self.controldata, self.extra_api = controldata, extra_api
        self.checkpoint = lambda: WAL_AT_PROOF     # where the shutdown checkpoint landed
        self.calls = []

    def run(self, argv, timeout, *, user=None, env=None, pass_fds=(), input_text=None):
        self.calls.append((list(argv), user))
        name = os.path.basename(argv[0])
        if name == "timedatectl":
            return Result(argv, 0, self.ntp + "\n", "")
        if name == "ps":
            lines = ["1 /sbin/init"]
            if self.systemd.state[UNITS["api"]] == "active":
                lines.append("943 /venv/bin/python3 /venv/bin/uvicorn app.main:app --port 8000")
            if self.extra_api:
                lines.append("944 python uvicorn app.main:app --port 8001")
            return Result(argv, 0, "\n".join(lines) + "\n", "")
        if name == "nft":
            return Result(argv, 0 if self.nft else 1, self.nft or "", "")
        if name == "pg_controldata":
            state = self.controldata or ("shut down" if self.systemd.state[UNITS["postgres"]]
                                         != "active" else "in production")
            return Result(argv, 0, f"pg_control version number:            1300\n"
                                   f"Database cluster state:               {state}\n"
                                   f"Latest checkpoint location:           "
                                   f"{self.checkpoint()}\n", "")
        raise AssertionError(f"unexpected program {argv[0]}")


class FakeDB:
    def __init__(self, positions=(0, 0), after_drain=None, key=True, error_on=None,
                 lose_key_after=None, guard_broken=False,
                 outcome=db_module.TERMINATED_BY_SHUTDOWN):
        self.positions, self.after_drain, self.key = positions, after_drain, key
        self.error_on = error_on
        self.closed, self.key_held, self.calls = False, False, []
        self.key_pid, self.verified = None, 0
        self.wal_lsn = None
        self.lose_key_after, self.guard_broken, self.outcome = (lose_key_after, guard_broken,
                                                                outcome)
        self.guarding = False

    def verify_writer_key(self):
        self._maybe_fail("verify")
        if self.lose_key_after is not None and self.verified >= self.lose_key_after:
            self.key_held = False
        if not self.key_held:
            raise DatabaseError("the writer key is no longer held by the controller's session")
        self.verified += 1

    def hold_through_shutdown(self, seconds):
        self.verify_writer_key()
        self._maybe_fail("guard")
        self.wal_lsn = WAL_AT_PROOF
        self.guarding = True

    def guard_intact(self):
        return self.guarding and not self.guard_broken

    def shutdown_outcome(self, wait):
        self.calls.append("outcome")
        self.key_held = False
        return self.outcome

    def _maybe_fail(self, what):
        self.calls.append(what)
        if self.error_on == what:
            raise DatabaseError(f"{what} failed")

    def open_positions(self):
        self._maybe_fail("positions")
        if self.key_held and self.after_drain is not None:
            return self.after_drain
        return self.positions

    def take_writer_key(self):
        self._maybe_fail("key")
        self.key_held = self.key
        self.key_pid = 4242 if self.key else None
        return self.key

    def role_sessions(self, role):
        self._maybe_fail("sessions")
        return 0

    def close(self, kill=False):
        self.closed = True
        self.key_held = False


class FakeArm:
    def __init__(self, submission=None, follow=("Succeeded", "the operation succeeded"),
                 recovery_follow=None):
        self.submission = submission or Submission(azure.ACCEPTED, 202, "accepted",
                                                   async_url="https://management.azure.com/"
                                                   "subscriptions/x/op", retry_after=5)
        self.follow_result, self.recovery_follow = follow, recovery_follow
        self.deallocations, self.follows = [], []

    def deallocate(self, client_request_id):
        self.deallocations.append(client_request_id)
        return self.submission

    def follow(self, operation, budget=None, not_before=None):
        self.follows.append((operation, budget))
        if budget is not None and self.recovery_follow is not None:
            return (*self.recovery_follow, None)[:3]
        return (*self.follow_result, None)[:3]


class FakeObserverLock:
    def __init__(self, state_root, uid, busy=False, path=None):
        self.busy, self.path = busy, path
        self.fd, self.identity, self.released = None, None, False

    def acquire(self, wait):
        if self.busy:
            raise locks.Busy("an observer run held its lock for the whole wait")
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        st = os.fstat(self.fd)
        self.identity = (st.st_dev, st.st_ino)

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.released = True


class Evidence:
    """Scripted observer evidence: one verdict per request."""

    def __init__(self, *verdicts, reject=None):
        self.verdicts, self.reject, self.calls = list(verdicts or ["CLEAR", "CLEAR"]), reject, []

    def __call__(self, cfg, runner, *, txn_id, boot_id, lock_fd, api, api_stopped, frozen_at,
                 clock):
        self.calls.append({"txn_id": txn_id, "api_stopped": api_stopped, "lock_fd": lock_fd,
                           "api": api, "boot_id": boot_id})
        if self.reject and len(self.calls) == self.reject:
            raise evidence.EvidenceRejected("the evidence was made for another transaction")
        verdict = self.verdicts[min(len(self.calls), len(self.verdicts)) - 1]
        return {"verdict": verdict, "integrity": {"content_sha256": f"{len(self.calls):064x}"},
                "reasons": {"blocked": [] if verdict == "CLEAR" else ["fault_open: f1"],
                            "unknown": []}}


class World:
    """Everything one controller run touches, wired together."""

    def __init__(self, tmp_path, now="17:30:00", day="2026-10-27", systemd=None, db=None,
                 arm=None, ev=None, lock_busy=False, boots=None, runner=None, **cfg_over):
        self.tmp_path = tmp_path
        self.cfg = make_cfg(tmp_path, **cfg_over)
        self.systemd = systemd or FakeSystemd()
        self.runner = runner or FakeRunner(self.systemd)
        self.db = db or FakeDB()
        self.arm = arm or FakeArm()
        self.ev = ev or Evidence()
        self.boots = list(boots or [BOOT])
        self.now = ist(now, day)
        self.locks = []
        self.lock_busy = lock_busy
        # Pass 22: readiness is waited for (bounded); a fake monotonic clock that the fake
        # sleep advances keeps every wait instant and deterministic.
        self.mono, self.slept = 0.0, []

        def sleep(seconds):
            self.slept.append(seconds)
            self.mono += seconds

        def observer_lock(state_root, uid):
            lock = FakeObserverLock(state_root, uid, self.lock_busy,
                                    str(tmp_path / "observer.lock"))
            self.locks.append(lock)
            return lock

        self.env = controller.Env(
            cfg=self.cfg, runner=self.runner, systemd=self.systemd, clock=lambda: self.now,
            boot_id=lambda: self.boots[0], uid_of=lambda user: os.geteuid(),
            open_db=lambda cfg: self.db, arm=self.arm, observer_lock=observer_lock,
            obtain_evidence=self.ev, sync=lambda: None, sleep=sleep,
            monotonic=lambda: self.mono)

    def request(self, boot=None, when=None):
        held = fsafe.open_dir(self.cfg["request_dir"], private=False)
        try:
            return requests.make_request(held, boot or self.boots[0], when or self.now)
        finally:
            held.close()

    def control(self):
        return controller.handle_requests(self.env, recovery.reconcile)

    def journal(self):
        state = controller.open_state(self.cfg)
        return journal.Journal(state, owner=os.geteuid())

    def txns(self):
        return self.journal().all()

    def states(self):
        [txn] = self.txns()
        return [r["state"] for r in txn.records]

    def reconcile(self):
        return recovery.reconcile(self.env, self.journal())

    def all_active(self):
        return all(self.systemd.state[u] == "active" for u in UNITS.values())


FORWARD = ["ACCEPTED_REQUEST", "FROZEN", "EVIDENCE_CLEAR", "SYSTEM_CLEAR", "WEB_STOPPED",
           "API_STOPPED", "WRITERS_EXCLUDED", "POSITIONS_CLEAR", "DRAINED_EVIDENCE_CLEAR",
           "REDIS_STOPPED", "POSTGRES_STOPPED", "SUBMITTING"]


# -- the whole transaction ------------------------------------------------------------------
def test_a_clear_shutdown_runs_every_stage_in_order_and_submits_once(tmp_path):
    w = World(tmp_path)
    w.request()
    assert w.control() == 0
    assert w.states() == FORWARD + ["ACCEPTED", "POLL_SUCCEEDED"]
    assert len(w.arm.deallocations) == 1
    every_stop = [u for op, u in w.systemd.calls if op == "stop"]
    # Correction (pass 20): automatic updates are now paused after the observer is frozen
    # and before the workload is touched; the workload's own order is unchanged.
    assert set(every_stop[4:6]) == set(PACKAGE_TIMERS)
    stops = [u for u in every_stop if u not in PACKAGE_TIMERS]
    assert set(stops[:4]) == set(TIMERS)          # no new scheduled observer run first
    assert stops[4:] == [UNITS["web"], UNITS["api"], UNITS["redis"], UNITS["postgres"]]
    assert [c["api_stopped"] for c in w.ev.calls] == [False, True]
    # The observer lock stays held to the end: the VM is going down with it.
    assert w.locks[0].fd is not None and not w.locks[0].released
    assert w.db.closed


def test_the_api_drains_while_the_database_and_redis_are_still_running(tmp_path):
    w = World(tmp_path)
    seen = {}
    real_stop = w.systemd.stop

    def stop(unit, timeout):
        if unit == UNITS["api"]:
            seen["db"] = w.systemd.state[UNITS["postgres"]]
            seen["redis"] = w.systemd.state[UNITS["redis"]]
        real_stop(unit, timeout)
    w.systemd.stop = stop
    w.request()
    w.control()
    assert seen == {"db": "active", "redis": "active"}
    assert w.db.calls.index("key") > 0 and "positions" in w.db.calls


def test_no_request_means_nothing_happens(tmp_path):
    w = World(tmp_path)
    assert w.control() == 0
    assert w.txns() == [] and w.systemd.calls == [] and w.arm.deallocations == []


@pytest.mark.parametrize("change, fact", [
    (dict(now="16:00:00"), "attempt_window"),
    (dict(day="2026-10-31"), "attempt_window"),                      # a Saturday
    (dict(runner_ntp="no"), "clock"),
    (dict(runner_extra_api=True), "api_identity"),
    (dict(runner_nft=""), "imds_isolation"),
    (dict(db=FakeDB(positions=(1, 0))), "open_positions"),
    (dict(db=FakeDB(error_on="positions")), "open_positions"),
])
def test_a_failed_pre_check_aborts_before_anything_is_changed(tmp_path, change, fact):
    runner_opts = {k.split("_", 1)[1]: change.pop(k) for k in list(change) if
                   k.startswith("runner_")}
    systemd = FakeSystemd()
    w = World(tmp_path, systemd=systemd, runner=FakeRunner(systemd, **runner_opts), **change)
    w.request(when=w.now)
    assert w.control() == 0
    [txn] = w.txns()
    assert txn.state == "ABORTED"
    failing = [f["name"] for f in txn.data("ABORTED")["facts"] if f["status"] != "PASS"]
    assert fact in failing
    assert w.systemd.calls == [] and w.arm.deallocations == []


def test_a_keep_running_hold_and_a_reviewed_special_session_abort(tmp_path):
    w = World(tmp_path)
    hold = json.loads((POWER / "config/power_hold.example.json").read_text())
    hold.update(from_ist="2026-10-27T17:00:00+05:30", to_ist="2026-10-28T09:00:00+05:30")
    (Path(w.cfg["holds_dir"]) / "h.json").write_text(json.dumps(hold))
    w.request()
    w.control()
    assert w.txns()[0].state == "ABORTED" and w.systemd.calls == []
    w2 = World(tmp_path / "special")
    write_calendar(Path(w2.cfg["calendar_file"]), special=("2026-10-28",))
    w2.request()
    w2.control()
    [txn] = w2.txns()
    assert txn.state == "ABORTED"
    assert any(f["name"] == "special_sessions" and f["status"] == "BLOCKED"
               for f in txn.data("ABORTED")["facts"])


def test_a_malformed_hold_is_unknown_and_aborts(tmp_path):
    w = World(tmp_path)
    (Path(w.cfg["holds_dir"]) / "h.json").write_text('{"kind": "KEEP_RUNNING"}')
    w.request()
    w.control()
    assert w.txns()[0].state == "ABORTED" and w.systemd.calls == []


def restored(w, timers_inactive=()):
    return w.all_active() and all(
        w.systemd.state[t] == ("inactive" if t in timers_inactive else "active") for t in TIMERS)


@pytest.mark.parametrize("make, stage", [
    (lambda tp: World(tp, ev=Evidence("BLOCKED")), "FROZEN"),
    (lambda tp: World(tp, ev=Evidence("UNKNOWN")), "FROZEN"),
    (lambda tp: World(tp, ev=Evidence(reject=1)), "FROZEN"),
    (lambda tp: World(tp, lock_busy=True), "ACCEPTED_REQUEST"),
    (lambda tp: World(tp, systemd=FakeSystemd(stop_result={UNITS["api"]: ("exit-code", "70",
                                                                          "failed")})),
     "WEB_STOPPED"),
    (lambda tp: World(tp, systemd=FakeSystemd(stop_fail={UNITS["api"]})), "WEB_STOPPED"),
    (lambda tp: World(tp, db=FakeDB(key=False)), "API_STOPPED"),
    (lambda tp: World(tp, db=FakeDB(after_drain=(1, 0))), "WRITERS_EXCLUDED"),
    (lambda tp: World(tp, db=FakeDB(after_drain=(0, 1))), "WRITERS_EXCLUDED"),
    (lambda tp: World(tp, ev=Evidence("CLEAR", "BLOCKED")), "POSITIONS_CLEAR"),
    (lambda tp: World(tp, systemd=FakeSystemd(stop_fail={UNITS["redis"]})),
     "DRAINED_EVIDENCE_CLEAR"),
    (lambda tp: World(tp, systemd=FakeSystemd(stop_result={UNITS["postgres"]: (
        "timeout", "1", "failed")})), "REDIS_STOPPED"),
])
def test_any_doubt_before_the_request_restores_exactly_the_prior_state(tmp_path, make, stage):
    w = make(tmp_path)
    w.request()
    assert w.control() == 0
    states = w.states()
    assert states[-3:] == [stage, "RESTORING", "RESTORED"]
    assert restored(w) and w.arm.deallocations == []
    if w.locks:
        assert w.locks[0].released


def test_a_cluster_that_does_not_report_a_clean_shutdown_restores(tmp_path):
    systemd = FakeSystemd()
    w = World(tmp_path, systemd=systemd, runner=FakeRunner(systemd, controldata="in production"))
    w.request()
    w.control()
    assert w.states()[-3:] == ["REDIS_STOPPED", "RESTORING", "RESTORED"]
    assert restored(w) and w.arm.deallocations == []


def test_restoration_starts_only_what_was_running_before(tmp_path):
    w = World(tmp_path, systemd=FakeSystemd(inactive_timers={TIMERS[2]}),
              ev=Evidence("BLOCKED"))
    w.request()
    w.control()
    assert restored(w, timers_inactive={TIMERS[2]})
    assert ("stop", TIMERS[2]) not in w.systemd.calls
    assert ("start", TIMERS[2]) not in w.systemd.calls


def test_a_writer_that_appears_during_the_drain_stops_the_shutdown(tmp_path):
    # A position opened between the API's stop and the exclusive key is found by the
    # recheck made under the key, and everything is brought back.
    w = World(tmp_path, db=FakeDB(after_drain=(1, 0)))
    w.request()
    w.control()
    [txn] = w.txns()
    assert txn.data("RESTORING")["reason"] == "positions are open after the drain"
    assert restored(w) and w.arm.deallocations == []


def test_restoration_failure_is_recorded_and_retried(tmp_path):
    w = World(tmp_path, systemd=FakeSystemd(start_fail={UNITS["api"]}), ev=Evidence("CLEAR",
                                                                                   "BLOCKED"))
    w.request()
    assert w.control() == 1
    assert w.txns()[0].state == "RESTORE_FAILED"
    # The observer timers come back even though the API would not.
    assert all(w.systemd.state[t] == "active" for t in TIMERS)
    w.systemd.start_fail.clear()
    assert w.reconcile() == []
    assert w.txns()[0].state == "RESTORED" and restored(w)


# -- Azure outcomes ---------------------------------------------------------------------
@pytest.mark.parametrize("outcome", [azure.NOT_SENT, azure.REJECTED])
def test_a_definitive_not_sent_or_rejection_restores(tmp_path, outcome):
    w = World(tmp_path, arm=FakeArm(Submission(outcome, 409 if outcome == azure.REJECTED
                                               else None, "no")))
    w.request()
    assert w.control() == 0
    assert w.states()[-4:] == ["SUBMITTING", outcome, "RESTORING", "RESTORED"]
    assert restored(w) and len(w.arm.deallocations) == 1


def test_completed_is_terminal_and_nothing_is_restored(tmp_path):
    w = World(tmp_path, arm=FakeArm(Submission(azure.COMPLETED, 200, "done")))
    w.request()
    assert w.control() == 0
    assert w.states()[-1] == "COMPLETED" and not w.all_active()


def test_an_unknown_outcome_is_never_resubmitted_and_nothing_is_restarted(tmp_path):
    w = World(tmp_path, arm=FakeArm(Submission(azure.UNKNOWN, None, "no readable response")))
    w.request()
    assert w.control() == 1
    assert w.states()[-1] == "UNKNOWN"
    assert len(w.arm.deallocations) == 1
    assert not any(w.systemd.state[u] == "active" for u in UNITS.values())
    assert not any(w.systemd.state[t] == "active" for t in TIMERS)


def test_accepted_but_its_status_unreadable_stays_unknown(tmp_path):
    w = World(tmp_path, arm=FakeArm(follow=("Unavailable", "this identity may not read it")))
    w.request()
    assert w.control() == 1
    assert w.states()[-2:] == ["ACCEPTED", "UNKNOWN_RETAINED"]
    assert not w.all_active() and len(w.arm.deallocations) == 1


def test_accepted_then_the_operation_says_failed_restores(tmp_path):
    w = World(tmp_path, arm=FakeArm(follow=("Failed", "the operation ended Failed")))
    w.request()
    assert w.control() == 0
    assert w.states()[-4:] == ["ACCEPTED", "POLL_FAILED", "RESTORING", "RESTORED"]
    assert restored(w)


def test_a_prolonged_unknown_is_never_resolved_by_time_a_running_vm_or_a_new_boot(tmp_path):
    w = World(tmp_path, arm=FakeArm(Submission(azure.UNKNOWN, 504, "gateway timeout")))
    w.request()
    w.control()
    for days in (1, 2, 7):
        w.now = ist("07:40:00") + timedelta(days=days)
        unresolved = w.reconcile()
        assert [t.state for t in unresolved] in (["UNKNOWN_RETAINED"], ["OBSERVED_NEW_BOOT"])
    w.boots.insert(0, BOOT2)                                # the VM was started again
    [txn] = w.reconcile()
    assert txn.state == "OBSERVED_NEW_BOOT"
    assert [r["state"] for r in txn.records].count("OBSERVED_NEW_BOOT") == 1
    w.reconcile()                                           # the same boot is noted once
    assert [r["state"] for r in w.txns()[0].records].count("OBSERVED_NEW_BOOT") == 1
    assert len(w.arm.deallocations) == 1
    assert ("start", UNITS["api"]) not in w.systemd.calls


def test_an_unresolved_transaction_blocks_every_new_shutdown(tmp_path):
    w = World(tmp_path, arm=FakeArm(Submission(azure.UNKNOWN, None, "lost")))
    w.request()
    w.control()
    w.systemd = FakeSystemd()                               # say an operator restarted all
    w.env.systemd = w.systemd
    w.runner.systemd = w.systemd
    w.request()
    assert w.control() == 1
    assert len(w.txns()) == 1 and w.systemd.calls == []
    assert os.listdir(w.cfg["request_dir"]) == []           # the request was consumed


def test_operation_specific_evidence_settles_an_accepted_transaction_later(tmp_path):
    w = World(tmp_path, arm=FakeArm(follow=("InProgress", "budget"),
                                    recovery_follow=("Succeeded", "the operation succeeded")))
    w.request()
    w.control()
    assert w.txns()[0].state == "UNKNOWN_RETAINED"
    w.boots.insert(0, BOOT2)
    assert w.reconcile() == []
    states = [r["state"] for r in w.txns()[0].records]
    # Correction (pass 21): Azure's success no longer closes the transaction; the
    # workload's verified return in this later boot does.
    assert states[-4:] == ["OBSERVED_NEW_BOOT", "POLL_SUCCEEDED", "RESUMING", "RESUMED"]


@pytest.mark.parametrize("finding, final", [
    ("deallocation-completed", "OPERATOR_CONFIRMED_DEALLOCATED"),
    ("deallocation-not-performed", "RESTORED"),
])
def test_an_operator_finding_settles_an_unknown_outcome(tmp_path, finding, final):
    w = World(tmp_path, arm=FakeArm(Submission(azure.UNKNOWN, None, "lost")))
    w.request()
    w.control()
    [txn] = w.txns()
    # Correction (pass 20): a completed deallocation ends the boot that asked for it, so
    # that finding is accepted only in a later boot - where the workload the gate held
    # down is then resumed. "Not performed" restores in any boot.
    w.boots.insert(0, BOOT2)
    state = recovery.resolve(w.env, w.journal(), txn.txn_id, finding,
                             "activity log entry 1234", "operator")
    assert state == final and w.txns()[0].terminal
    assert w.all_active()
    with pytest.raises(ValueError):
        recovery.resolve(w.env, w.journal(), txn.txn_id, finding, "x", "operator")


def test_operator_findings_apply_only_where_they_mean_something(tmp_path):
    w = World(tmp_path, ev=Evidence("BLOCKED"))
    w.request()
    w.control()
    [txn] = w.txns()
    for finding in recovery.FINDINGS:
        with pytest.raises(ValueError):
            recovery.resolve(w.env, w.journal(), txn.txn_id, finding, "ref", "operator")
    with pytest.raises(ValueError):
        recovery.resolve(w.env, w.journal(), txn.txn_id, "deallocation-completed", " ",
                         "operator")


# -- crashes at every stage ------------------------------------------------------------------
class Crash(BaseException):
    """A process killed outright: not an Exception, so nothing catches it."""


@pytest.mark.parametrize("after", FORWARD + ["ACCEPTED"])
def test_a_crash_after_any_journal_stage_is_settled_safely(tmp_path, monkeypatch, after):
    w = World(tmp_path, arm=FakeArm(follow=("InProgress", "budget"),
                                    recovery_follow=("InProgress", "still")))
    real = journal.Journal.append

    def append(self, txn, state, boot_id, data, clock=journal.now_utc):
        record = real(self, txn, state, boot_id, data, clock)
        if state == after:
            raise Crash(state)
        return record
    monkeypatch.setattr(journal.Journal, "append", append)
    w.request()
    with pytest.raises(Crash):
        w.control()
    monkeypatch.setattr(journal.Journal, "append", real)
    unresolved = w.reconcile()
    [txn] = w.txns()
    if after in FORWARD[:-1]:
        # Before the request was sent: everything that was running runs again.
        assert txn.state == "RESTORED" and unresolved == [] and restored(w)
    else:
        # The request may have left: nothing is restarted and it stays unresolved.
        assert txn.state == "UNKNOWN_RETAINED" and [t.txn_id for t in unresolved] == [txn.txn_id]
        assert not w.all_active()
    assert len(w.arm.deallocations) == (1 if after == "ACCEPTED" else 0)


def test_a_crash_during_restoration_is_completed_by_recovery(tmp_path, monkeypatch):
    w = World(tmp_path, ev=Evidence("CLEAR", "BLOCKED"))
    real = FakeSystemd.start
    calls = {"n": 0}

    def start(self, unit, timeout):
        calls["n"] += 1
        if calls["n"] == 2:
            raise Crash("killed while restoring")
        return real(self, unit, timeout)
    monkeypatch.setattr(FakeSystemd, "start", start)
    w.request()
    with pytest.raises(Crash):
        w.control()
    assert w.txns()[0].state == "RESTORING"
    monkeypatch.setattr(FakeSystemd, "start", real)
    assert w.reconcile() == []
    assert w.txns()[0].state == "RESTORED" and restored(w)


def test_journal_failure_still_restores_and_then_refuses_further_work(tmp_path, monkeypatch):
    w = World(tmp_path, ev=Evidence("BLOCKED"))
    real = journal.Journal.append

    def append(self, txn, state, boot_id, data, clock=journal.now_utc):
        if state == "RESTORING":
            raise OSError(28, "No space left on device")
        return real(self, txn, state, boot_id, data, clock)
    monkeypatch.setattr(journal.Journal, "append", append)
    w.request()
    # Correction (pass 22): Astra's decision reverses this test's former expectation (its
    # name is kept so its identity is preserved). Restoration is WITHHELD when RESTORING
    # cannot be durably journaled: nothing is started, the journal keeps what it had, the
    # failure is raised (exit 2, "JOURNAL UNUSABLE"), and recovery retries later.
    before = list(w.systemd.calls)
    with pytest.raises(journal.CorruptJournal, match="restoration withheld, nothing started"):
        w.control()
    assert [c for c in w.systemd.calls[len(before):] if c[0] == "start"] == []
    assert w.states()[-1] == "FROZEN"
    monkeypatch.setattr(journal.Journal, "append", real)
    assert w.reconcile() == [] and restored(w)


# -- concurrency with real processes ---------------------------------------------------------
def hold_flock(path: Path, mode="flock"):
    code = ("import fcntl,sys,time\n"
            "f=open(sys.argv[1],'a+')\n"
            + ("fcntl.flock(f, fcntl.LOCK_EX)\n" if mode == "flock"
               else "fcntl.lockf(f, fcntl.LOCK_EX)\n")
            + "print('held', flush=True)\ntime.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE,
                            text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_a_second_controller_waits_its_turn_and_consumes_nothing(tmp_path):
    w = World(tmp_path)
    state = controller.open_state(w.cfg)
    state.close()
    lock = Path(w.cfg["state_root"]) / "coordination.lock"
    lock.touch(mode=0o600)
    holder = hold_flock(lock)
    try:
        w.request()
        assert w.control() == 0
        assert w.txns() == [] and len(os.listdir(w.cfg["request_dir"])) == 1
    finally:
        holder.kill()
        holder.wait()
    assert w.control() == 0 and len(w.txns()) == 1


def observer_state(tmp_path: Path) -> Path:
    root = tmp_path / "observer-state"
    root.mkdir(mode=0o750)
    (root / "lock").touch(mode=0o644)
    return root


def test_the_real_observer_lock_excludes_a_running_observer(tmp_path):
    root = observer_state(tmp_path)
    holder = hold_flock(root / "lock")
    try:
        lock = locks.ObserverLock(str(root), os.geteuid())
        clock = iter([0, 1, 2, 999])
        with pytest.raises(locks.Busy):
            lock.acquire(10, sleep=lambda s: None, monotonic=lambda: next(clock))
    finally:
        holder.kill()
        holder.wait()
    lock.acquire(0)
    try:
        # Now no observer process can take it (assessments find it held).
        probe = subprocess.run(
            [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1],'a');\n"
             "try:\n fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB); print('got')\n"
             "except BlockingIOError: print('busy')", str(root / "lock")],
            capture_output=True, text=True, timeout=30)
        assert probe.stdout.strip() == "busy"
    finally:
        lock.release()


def test_the_observer_lock_is_never_created_and_never_followed_through_a_link(tmp_path):
    root = tmp_path / "observer-state"
    root.mkdir(mode=0o750)
    with pytest.raises(FileNotFoundError):
        locks.ObserverLock(str(root), os.geteuid()).acquire(0)
    assert not (root / "lock").exists()
    (tmp_path / "elsewhere").write_text("")
    (root / "lock").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OSError):
        locks.ObserverLock(str(root), os.geteuid()).acquire(0)
    real = tmp_path / "real-state"
    real.mkdir(mode=0o750)
    (real / "lock").touch()
    linked = tmp_path / "linked-state"
    linked.symlink_to(real)
    with pytest.raises(fsafe.UnsafePath):
        locks.ObserverLock(str(linked), os.geteuid()).acquire(0)
    open_root = tmp_path / "group-writable"
    open_root.mkdir()
    open_root.chmod(0o770)
    (open_root / "lock").touch()
    with pytest.raises(fsafe.UnsafePath):
        locks.ObserverLock(str(open_root), os.geteuid()).acquire(0)


def test_package_locks_are_queried_without_being_taken(tmp_path):
    lock = tmp_path / "lock-frontend"
    lock.touch()
    assert facts.lock_holder(str(lock)) is None
    holder = hold_flock(lock, mode="lockf")
    try:
        assert facts.lock_holder(str(lock)) == holder.pid
    finally:
        holder.kill()
        holder.wait()
    assert facts.lock_holder(str(lock)) is None
    w = World(tmp_path / "w", package_locks=[str(lock)])
    holder = hold_flock(lock, mode="lockf")
    try:
        found = facts.package_activity(w.cfg, w.systemd)
        assert found.status == "BLOCKED" and holder.pid in found.data["lock_holders"].values()
    finally:
        holder.kill()
        holder.wait()
    w.systemd.state["apt-daily-upgrade.service"] = "activating"
    assert facts.package_activity(w.cfg, w.systemd).status == "BLOCKED"


# -- requests ------------------------------------------------------------------------------
def consume(w, now=None, boot=None):
    state = controller.open_state(w.cfg)
    req = fsafe.open_dir(w.cfg["request_dir"], private=False)
    try:
        return requests.consume(req, state, os.geteuid(), boot or w.boots[0], now or w.now)
    finally:
        req.close()
        state.close()


def test_a_valid_request_is_accepted_once_and_a_copy_is_refused_forever(tmp_path):
    w = World(tmp_path)
    nonce = w.request()
    saved = (Path(w.cfg["request_dir"]) / f"req-{nonce}.json").read_bytes()
    assert consume(w)[0] == nonce
    copy = Path(w.cfg["request_dir"]) / f"req-{nonce}.json"
    copy.write_bytes(saved)
    copy.chmod(0o600)
    accepted, refusals = consume(w, now=w.now + timedelta(seconds=10))
    assert accepted is None and "replayed" in refusals[0]["reason"]
    assert os.listdir(w.cfg["request_dir"]) == []


@pytest.mark.parametrize("tamper, why", [
    (lambda p, b: p.write_text(json.dumps({**b, "extra": 1})), "unexpected or missing fields"),
    (lambda p, b: p.write_text(json.dumps({**b, "nonce": "0" * 32})), "does not match"),
    (lambda p, b: p.write_text(json.dumps({**b, "boot_id": BOOT2})), "another boot"),
    (lambda p, b: p.write_text(json.dumps({**b, "created_at_utc":
                                           "2026-10-27T11:00:00+00:00"})), "stale"),
    (lambda p, b: p.write_text(json.dumps({**b, "created_at_utc":
                                           "2026-10-27T13:00:00+00:00"})), "future"),
    (lambda p, b: p.write_text("x" * 2000), "larger"),
    (lambda p, b: p.chmod(0o644), "group or others"),
])
def test_forged_or_altered_requests_are_refused(tmp_path, tamper, why):
    w = World(tmp_path)
    nonce = w.request()
    path = Path(w.cfg["request_dir"]) / f"req-{nonce}.json"
    body = json.loads(path.read_text())
    path.chmod(0o600)
    tamper(path, body)
    accepted, refusals = consume(w)
    assert accepted is None and why in refusals[0]["reason"]
    assert os.listdir(w.cfg["request_dir"]) == []


def test_links_fifos_directories_and_strange_names_are_refused(tmp_path):
    w = World(tmp_path)
    req = Path(w.cfg["request_dir"])
    target = tmp_path / "target.json"
    target.write_text("{}")
    (req / f"req-{'a' * 32}.json").symlink_to(target)
    os.mkfifo(req / f"req-{'b' * 32}.json", 0o600)
    (req / f"req-{'c' * 32}.json").mkdir()
    (req / "please-shut-down.json").write_text("{}")
    # A well-formed request kept elsewhere and hard-linked in: it has two links.
    outside = tmp_path / "kept.json"
    outside.write_text(json.dumps({"schema": requests.SCHEMA, "nonce": "d" * 32, "boot_id": BOOT,
                                   "created_at_utc": w.now.astimezone(UTC).isoformat()}))
    outside.chmod(0o600)
    os.link(outside, req / f"req-{'d' * 32}.json")
    nonce = w.request()                                # the one genuine request
    accepted, refusals = consume(w)
    assert accepted == nonce
    reasons = sorted(r["reason"] for r in refusals)
    assert len(refusals) == 5 and any("2 links" in r for r in reasons)
    assert target.read_text() == "{}" and outside.exists()
    assert os.listdir(req) == []


def test_two_valid_requests_give_one_transaction(tmp_path):
    w = World(tmp_path)
    w.request()
    w.request()
    accepted, refusals = consume(w)
    assert accepted and len(refusals) == 1 and "already accepted" in refusals[0]["reason"]


def test_the_requester_files_only_inside_the_window(tmp_path):
    w = World(tmp_path)
    assert controller.file_request(w.cfg, ist("12:00:00"), BOOT) == 0
    assert os.listdir(w.cfg["request_dir"]) == []
    assert controller.file_request(w.cfg, ist("17:30:00"), BOOT) == 0
    [name] = os.listdir(w.cfg["request_dir"])
    assert (os.stat(Path(w.cfg["request_dir"]) / name).st_mode & 0o777) == 0o600


# -- evidence binding --------------------------------------------------------------------------
def sealed(record):
    record = dict(record)
    record["integrity"] = {"algorithm": "sha256",
                           "content_sha256": evidence.content_hash(record)}
    return record


def good_evidence(w, lock_identity, api_stopped=False, **over):
    record = {"schema": evidence.SCHEMA, "txn_id": "a" * 32, "observer_version": "0.4.14",
              "generated_at_utc": w.now.astimezone(UTC).isoformat(), "verdict": "CLEAR",
              "lock": {"mode": "inherited", "device": lock_identity[0],
                       "inode": lock_identity[1]},
              "boot_id": BOOT,
              "api": ({"state": "stopped", "main_started_monotonic_usec": MONO} if api_stopped
                      else {"state": "running", "main_pid": 943, "pids": [943],
                            "main_started_monotonic_usec": MONO}),
              "config_sha256": evidence.file_digest(w.cfg["observer"]["config"]),
              "calendar_file": w.cfg["calendar_file"],
              "calendar_sha256": evidence.file_digest(w.cfg["calendar_file"])}
    record.update(over)
    return sealed(record)


def check(w, record, api_stopped=False, frozen_at=None):
    return evidence.validate(
        record, cfg=w.cfg, txn_id="a" * 32, boot_id=BOOT, lock_identity=(1, 2),
        api={"main_pid": 943, "main_started_monotonic_usec": MONO}, api_stopped=api_stopped,
        frozen_at=frozen_at or w.now - timedelta(seconds=30), now=w.now)


def test_good_evidence_is_accepted_before_and_after_the_drain(tmp_path):
    w = World(tmp_path)
    assert check(w, good_evidence(w, (1, 2)))["verdict"] == "CLEAR"
    assert check(w, good_evidence(w, (1, 2), api_stopped=True), api_stopped=True)


@pytest.mark.parametrize("over, api_stopped", [
    ({"txn_id": "b" * 32}, False),
    ({"boot_id": BOOT2}, False),
    ({"observer_version": "0.4.13"}, False),
    ({"lock": {"mode": "own", "device": 1, "inode": 2}}, False),
    ({"lock": {"mode": "inherited", "device": 1, "inode": 3}}, False),
    ({"api": {"state": "running", "main_pid": 944, "pids": [944],
              "main_started_monotonic_usec": MONO}}, False),
    ({"api": {"state": "running", "main_pid": 943, "pids": [943],
              "main_started_monotonic_usec": MONO + 1}}, False),
    ({"api": {"state": "running", "main_pid": 943, "pids": [943],
              "main_started_monotonic_usec": MONO}}, True),
    ({"config_sha256": "0" * 64}, False),
    ({"calendar_sha256": "0" * 64}, False),
    ({"calendar_file": "/etc/other.json"}, False),
    ({"generated_at_utc": "2026-10-27T11:00:00+00:00"}, False),   # before the freeze
    ({"generated_at_utc": "2026-10-27T13:00:00+00:00"}, False),   # in the future
    ({"verdict": "MAYBE"}, False),
])
def test_stale_forged_or_misbound_evidence_is_rejected(tmp_path, over, api_stopped):
    w = World(tmp_path)
    with pytest.raises(evidence.EvidenceRejected):
        check(w, good_evidence(w, (1, 2), **over), api_stopped=api_stopped)


def test_evidence_with_a_broken_seal_or_older_than_allowed_is_rejected(tmp_path):
    w = World(tmp_path)
    record = good_evidence(w, (1, 2))
    record["verdict"] = "CLEAR "
    with pytest.raises(evidence.EvidenceRejected, match="hash"):
        check(w, record)
    old = good_evidence(w, (1, 2), generated_at_utc=(w.now - timedelta(minutes=5))
                        .astimezone(UTC).isoformat())
    with pytest.raises(evidence.EvidenceRejected, match="too old"):
        check(w, old, frozen_at=w.now - timedelta(minutes=10))


def test_the_evidence_is_requested_as_the_observer_with_only_the_lock_descriptor(tmp_path):
    w = World(tmp_path)
    seen = {}

    class Recorder:
        def run(self, argv, timeout, *, user=None, env=None, pass_fds=(), input_text=None):
            seen.update(argv=argv, user=user, env=env, pass_fds=pass_fds)
            return Result(argv, 0, json.dumps(good_evidence(w, (1, 2))), "")
    evidence.request(w.cfg, Recorder(), "a" * 32, 7, api_stopped=True)
    assert seen["user"] == "quantdesk-observer" and seen["pass_fds"] == (7,)
    assert seen["argv"][-5:] == ["--txn", "a" * 32, "--lock-fd", "7", "--api-stopped"]
    assert seen["argv"][:4] == ["/usr/bin/python3", "-I", "/opt/quantdesk-observer/observer.py",
                                "shutdown-evidence"]
    assert set(seen["env"]) == {"PATH", "TZ"}


# -- the journal ---------------------------------------------------------------------------------
def test_journal_records_are_sealed_chained_and_never_replaced(tmp_path):
    w = World(tmp_path, ev=Evidence("BLOCKED"))
    w.request()
    w.control()
    [txn] = w.txns()
    directory = Path(w.cfg["state_root"]) / "txn" / txn.txn_id
    names = sorted(os.listdir(directory))
    assert all(os.stat(directory / n).st_mode & 0o777 == 0o400 for n in names)
    assert txn.records[1]["prev_sha256"] == txn.records[0]["integrity"]["content_sha256"]
    with pytest.raises(journal.CorruptJournal):
        w.journal().append(txn, "FROZEN", BOOT, {})        # RESTORED is terminal


@pytest.mark.parametrize("damage", ["edit", "delete_middle", "rename", "extra", "swap"])
def test_a_damaged_journal_refuses_all_new_work(tmp_path, damage):
    w = World(tmp_path, ev=Evidence("BLOCKED"))
    w.request()
    w.control()
    [txn] = w.txns()
    directory = Path(w.cfg["state_root"]) / "txn" / txn.txn_id
    names = sorted(os.listdir(directory))
    if damage == "edit":
        target = directory / names[1]
        target.chmod(0o600)
        target.write_text(target.read_text().replace(BOOT, BOOT2))
    elif damage == "delete_middle":
        (directory / names[1]).unlink()
    elif damage == "rename":
        (directory / names[1]).rename(directory / names[1].replace("FROZEN", "ABORTED"))
    elif damage == "extra":
        (directory / "9999-RESTORED.json").write_text("{}")
    else:
        a, b = directory / names[1], directory / names[2]
        data_a, data_b = a.read_bytes(), b.read_bytes()
        a.chmod(0o600)
        b.chmod(0o600)
        a.write_bytes(data_b)
        b.write_bytes(data_a)
    with pytest.raises(journal.CorruptJournal):
        w.journal().all()
    w.request()
    with pytest.raises(journal.CorruptJournal):
        w.control()


def test_the_state_tree_is_reached_without_following_links(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(fsafe.UnsafePath):
        fsafe.open_dir(str(link / "state"), create=True)
    with pytest.raises(fsafe.UnsafePath):
        fsafe.open_dir("relative/path")
    with pytest.raises(fsafe.UnsafePath):
        fsafe.open_dir(str(tmp_path / ".." / "x"))
    held = fsafe.open_dir(str(real))
    try:
        assert fsafe.publish(held, "r.json", b"1") is True
        assert fsafe.publish(held, "r.json", b"2") is False
        assert (real / "r.json").read_bytes() == b"1"
        os.link(real / "r.json", real / "twin.json")
        with pytest.raises(fsafe.UnsafePath, match="links"):
            fsafe.read_file(held, "r.json", max_bytes=10)
        (real / "big").write_bytes(b"x" * 11)
        with pytest.raises(fsafe.UnsafePath, match="larger"):
            fsafe.read_file(held, "big", max_bytes=10)
    finally:
        held.close()
    moved = fsafe.open_dir(str(real))
    try:
        (tmp_path / "real").rename(tmp_path / "gone")
        (tmp_path / "real").mkdir(mode=0o700)
        with pytest.raises(fsafe.UnsafePath, match="replaced"):
            fsafe.verify_dir(moved)
    finally:
        moved.close()


# -- isolation -----------------------------------------------------------------------------------
def source_files(root: Path):
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_trading_runtime_import_and_no_observer_azure_access():
    for path in source_files(POWER):
        text = path.read_text()
        assert "from app" not in text and "import app" not in text, path
        assert "backend" not in text or path.name == "db.py", path
    for path in source_files(OBSERVER):
        text = path.read_text()
        assert "169.254.169.254" not in text and "management.azure.com" not in text, path
        assert "quantdesk_power" not in text, path
    example = json.loads((OBSERVER / "observer.json.example").read_text())
    assert not any("azure" in key.lower() for key in example)


def test_only_root_can_reach_the_metadata_service():
    rules = (POWER / "nftables/quantdesk-imds.nft").read_text()
    assert "ip daddr 169.254.169.254 meta skuid != 0 counter drop" in rules
    units = {p.name: p.read_text() for p in (POWER / "systemd").iterdir()}
    assert "IPAddressDeny=any" in units["quantdesk-power-request.service"]
    assert "User=quantdesk-power" in units["quantdesk-power-request.service"]
    assert "IPAddressDeny=169.254.169.254/32" in \
        units["quantdesk-observer-20-power-isolation.conf"]
    assert "Requires=quantdesk-power-imds.service" in units["quantdesk-power-controller.service"]
    assert "User=" not in units["quantdesk-power-controller.service"]


def test_the_controller_refuses_when_the_isolation_table_is_absent_or_incomplete(tmp_path):
    w = World(tmp_path)
    assert facts.imds_isolation(w.cfg, w.runner).status == "PASS"
    w.runner.nft = NFT_LISTING.replace("qd-imds-nonroot-drop", "something-else")
    assert facts.imds_isolation(w.cfg, w.runner).status == "UNKNOWN"
    w.runner.nft = ""
    assert facts.imds_isolation(w.cfg, w.runner).status == "UNKNOWN"


def test_every_unit_is_a_disabled_template_and_the_path_unit_exists():
    units = {p.name: p.read_text() for p in (POWER / "systemd").iterdir()}
    assert {"quantdesk-power-controller.path", "quantdesk-power-controller.service",
            "quantdesk-power-request.service", "quantdesk-power-request.timer",
            "quantdesk-power-recover.service", "quantdesk-power-recover.timer",
            "quantdesk-power-imds.service"} <= set(units)
    assert "DirectoryNotEmpty=/run/quantdesk-power/requests" in \
        units["quantdesk-power-controller.path"]
    for name, text in units.items():
        if name.endswith((".service", ".timer", ".path")):
            assert "NOT INSTALLED" in text, name
    timer = units["quantdesk-power-request.timer"]
    assert "17:30,45:00" in timer and "Persistent=false" in timer


# -- the Azure client ------------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status, body=b"", headers=None):
        self.status, self.body, self.headers = status, body, headers or {}

    def read(self, n=-1):
        return self.body

    def getheader(self, name):
        return self.headers.get(name)


class FakeConn:
    def __init__(self, script):
        self.script, self.requests = script, []

    def connect(self):
        if self.script.get("connect"):
            raise self.script["connect"]

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, headers))
        if self.script.get("send"):
            raise self.script["send"]

    def getresponse(self):
        if self.script.get("receive"):
            raise self.script["receive"]
        return self.script["response"]

    def close(self):
        pass


def arm_with(cfg, arm_script, token=b'{"access_token": "t"}', polls=None):
    made = []

    def connect(host):
        if host == azure.IMDS_HOST:
            conn = FakeConn({"response": FakeResponse(200, token)})
        elif polls is not None and made and any(r for r in made if r.requests):
            conn = FakeConn({"response": polls.pop(0)})
        else:
            conn = FakeConn(arm_script)
        made.append(conn)
        return conn
    return azure.Arm(cfg, connect=connect, sleep=lambda s: None,
                     monotonic=iter(range(0, 100000, 5)).__next__), made


@pytest.mark.parametrize("script, outcome", [
    ({"connect": ConnectionRefusedError()}, azure.NOT_SENT),
    ({"connect": OSError("tls handshake failed")}, azure.NOT_SENT),
    ({"send": BrokenPipeError()}, azure.UNKNOWN),
    ({"receive": TimeoutError()}, azure.UNKNOWN),
    ({"response": FakeResponse(200)}, azure.COMPLETED),
    ({"response": FakeResponse(202, headers={"Azure-AsyncOperation": "https://x",
                                             "Retry-After": "1000"})}, azure.ACCEPTED),
    ({"response": FakeResponse(409, b'{"error": {"code": "OperationNotAllowed"}}')},
     azure.REJECTED),
    ({"response": FakeResponse(429)}, azure.REJECTED),
    ({"response": FakeResponse(500)}, azure.UNKNOWN),
    ({"response": FakeResponse(504)}, azure.UNKNOWN),
])
def test_each_stage_of_the_request_is_classified_by_what_is_known(tmp_path, script, outcome):
    cfg = make_cfg(tmp_path)
    arm, made = arm_with(cfg, script)
    sub = arm.deallocate("client-1")
    assert sub.outcome == outcome
    arm_requests = [r for c in made for r in c.requests if r[0] == "POST"]
    assert len(arm_requests) <= 1
    if outcome == azure.ACCEPTED:
        # Correction (pass 20): Retry-After is honoured as given, never cut to a ceiling.
        assert sub.async_url == "https://x" and sub.retry_after == 1000
    if outcome == azure.REJECTED and sub.status == 409:
        assert sub.error_code == "OperationNotAllowed"


def test_without_a_token_nothing_is_sent(tmp_path):
    cfg = make_cfg(tmp_path)
    arm, made = arm_with(cfg, {"response": FakeResponse(202)}, token=b'{"nope": 1}')
    assert arm.deallocate("c").outcome == azure.NOT_SENT
    assert not any(r[0] == "POST" for c in made for r in c.requests)


def op_url(cfg):
    return (f"https://management.azure.com/subscriptions/{cfg['azure']['subscription_id']}"
            "/providers/Microsoft.Compute/locations/centralindia/operations/abc?api-version=x")


@pytest.mark.parametrize("responses, expected", [
    ([FakeResponse(200, b'{"status": "InProgress"}'), FakeResponse(200, b'{"status":'
                                                                       b' "Succeeded"}')],
     azure.POLL_SUCCEEDED),
    ([FakeResponse(200, b'{"status": "Failed"}')], azure.POLL_FAILED),
    ([FakeResponse(200, b'{"status": "Canceled"}')], azure.POLL_FAILED),
    ([FakeResponse(403)], azure.POLL_UNAVAILABLE),
])
def test_an_accepted_operation_is_followed_through_its_own_status(tmp_path, responses,
                                                                   expected):
    cfg = make_cfg(tmp_path)
    queue = list(responses)

    def connect(host):
        if host == azure.IMDS_HOST:
            return FakeConn({"response": FakeResponse(200, b'{"access_token": "t"}')})
        return FakeConn({"response": queue.pop(0)})
    waits = []
    arm = azure.Arm(cfg, connect=connect, sleep=waits.append,
                    monotonic=iter(range(0, 10 ** 6, 1)).__next__)
    state, _, _ = arm.follow({"async_url": op_url(cfg), "retry_after": 7})
    assert state == expected and waits[0] == 7


def test_location_polling_and_foreign_urls(tmp_path):
    cfg = make_cfg(tmp_path)
    queue = [FakeResponse(202, headers={"Retry-After": "9"}), FakeResponse(204)]

    def connect(host):
        if host == azure.IMDS_HOST:
            return FakeConn({"response": FakeResponse(200, b'{"access_token": "t"}')})
        return FakeConn({"response": queue.pop(0)})
    arm = azure.Arm(cfg, connect=connect, sleep=lambda s: None,
                    monotonic=iter(range(0, 10 ** 6, 1)).__next__)
    assert arm.follow({"location_url": op_url(cfg)})[0] == azure.POLL_SUCCEEDED
    for url in ("http://management.azure.com/subscriptions/x", "https://evil.example/subscriptions/"
                f"{cfg['azure']['subscription_id']}/x",
                "https://management.azure.com/subscriptions/other/x",
                f"https://user@management.azure.com/subscriptions/"
                f"{cfg['azure']['subscription_id']}/x"):
        assert arm.follow({"async_url": url})[0] == azure.POLL_UNAVAILABLE
    assert arm.follow({})[0] == azure.POLL_UNAVAILABLE


def test_polling_ends_within_its_budget_without_a_terminal_answer(tmp_path):
    cfg = make_cfg(tmp_path)

    def connect(host):
        if host == azure.IMDS_HOST:
            return FakeConn({"response": FakeResponse(200, b'{"access_token": "t"}')})
        return FakeConn({"response": FakeResponse(200, b'{"status": "InProgress"}')})
    clock = {"t": 0.0}
    arm = azure.Arm(cfg, connect=connect, sleep=lambda s: clock.update(t=clock["t"] + s),
                    monotonic=lambda: clock["t"])
    state, detail, resume = arm.follow({"async_url": op_url(cfg)}, budget=60)
    assert state == azure.POLL_IN_PROGRESS and clock["t"] <= 60 and resume


# -- configuration ----------------------------------------------------------------------------
@pytest.mark.parametrize("path, value", [
    (("azure", "vm_name"), "vm/../../other"),
    (("azure", "subscription_id"), "not-a-guid"),
    (("azure", "resource_group"), "rg/x"),
    (("state_root",), "relative/state"),
    (("observer", "accepted_versions"), []),
    (("window", "first_attempt_ist"), "25:00"),
    (("units", "api"), "quantdesk-api"),
    (("timeouts", "stop_api"), 0),
])
def test_an_unsafe_configuration_is_refused(tmp_path, path, value):
    cfg = json.loads((POWER / "config/power.json.example").read_text())
    target = cfg
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(config.ConfigError):
        config.validate(cfg)


# -- special sessions ------------------------------------------------------------------------------
def test_a_special_session_gets_explicit_monitoring_deadlines():
    record = json.loads((POWER / "config/special_session.example.json").read_text())
    plan = special.plan(record)
    assert plan["deadlines"]["first_heartbeat_by"] == "2026-11-08T17:20:00+05:30"
    assert plan["deadlines"]["first_heartbeat_alert_by"] == "2026-11-08T17:30:00+05:30"
    assert plan["power_hold"]["kind"] == "KEEP_RUNNING"
    assert plan["power_hold"]["to_ist"] == record["keep_running_until_ist"]
    assert plan["azure_start"]["frequency"] == "OneTime"
    assert plan["monitor"]["variable_entry"]["expect_running_until"] == \
        record["keep_running_until_ist"]
    assert plan["healthchecks_manual"]["add_oncalendar_lines"][0].startswith("2026-11-08 17:")


@pytest.mark.parametrize("change", [
    {"first_heartbeat_by_ist": "2026-11-08T17:05:00+05:30"},     # less than 10 min after start
    {"session_close_ist": "2026-11-08T17:59:00+05:30"},          # closes before it opens
    {"vm_start_ist": "2026-11-07T17:00:00+05:30"},               # another day
    {"label": ""},
])
def test_an_inconsistent_special_session_is_refused(change):
    record = json.loads((POWER / "config/special_session.example.json").read_text())
    record.update(change)
    with pytest.raises(special.PlanError):
        special.plan(record)


def test_a_special_session_hold_blocks_shutdown_until_it_ends(tmp_path):
    record = json.loads((POWER / "config/special_session.example.json").read_text())
    hold = special.plan(record)["power_hold"]
    w = World(tmp_path, now="18:30:00", day="2026-11-08",
              window={"weekdays": [0, 1, 2, 3, 4, 5, 6], "first_attempt_ist": "17:30",
                      "last_attempt_ist": "21:00"})
    (Path(w.cfg["holds_dir"]) / "special.json").write_text(json.dumps(hold))
    w.request()
    w.control()
    assert w.txns()[0].state == "ABORTED" and w.systemd.calls == []


# -- the command line ----------------------------------------------------------------------------
def power_cli(*args):
    return subprocess.run([sys.executable, "-I", str(POWER / "power.py"), *args],
                          capture_output=True, text=True, timeout=60)


def test_the_command_line_refuses_an_unsafe_configuration(tmp_path):
    bad = json.loads((POWER / "config/power.json.example").read_text())
    bad["azure"]["vm_name"] = "../../x"
    path = tmp_path / "power.json"
    path.write_text(json.dumps(bad))
    out = power_cli("status", "--config", str(path))
    assert out.returncode == 2 and "configuration refused" in out.stderr


def test_status_reports_unresolved_transactions_as_text_and_json(tmp_path):
    w = World(tmp_path, arm=FakeArm(Submission(azure.UNKNOWN, None, "lost")))
    w.request()
    w.control()
    path = tmp_path / "power.json"
    path.write_text(json.dumps(w.cfg))
    out = power_cli("status", "--config", str(path), "--json")
    found = json.loads(out.stdout)
    assert out.returncode == 1 and found["unresolved"] == [w.txns()[0].txn_id]
    text = power_cli("status", "--config", str(path))
    assert "UNKNOWN" in text.stdout and text.returncode == 1


def test_plan_special_prints_the_plan_and_refuses_a_bad_record(tmp_path):
    out = power_cli("plan-special", "--record", str(POWER / "config/special_session.example.json"))
    assert out.returncode == 0 and json.loads(out.stdout)["power_hold"]["kind"] == "KEEP_RUNNING"
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert power_cli("plan-special", "--record", str(bad)).returncode == 2


def forge(directory: Path, seq: int, state: str, prev, txn_id: str, data=None) -> dict:
    record = {"schema": journal.SCHEMA, "txn_id": txn_id, "seq": seq, "state": state,
              "at_utc": "2026-10-27T12:00:00+00:00", "boot_id": BOOT, "data": data or {},
              "prev_sha256": prev}
    record["integrity"] = {"algorithm": "sha256", "content_sha256": journal.record_hash(record)}
    path = directory / f"{seq:04d}-{state}.json"
    if path.exists():
        path.chmod(0o600)
    path.write_text(json.dumps(record))
    path.chmod(0o400)
    return record


def test_a_resealed_record_that_breaks_the_chain_is_refused(tmp_path):
    w = World(tmp_path, ev=Evidence("BLOCKED"))
    w.request()
    w.control()
    [txn] = w.txns()
    directory = Path(w.cfg["state_root"]) / "txn" / txn.txn_id
    # Record 2 replaced by a properly sealed one that does not follow record 1.
    forge(directory, 2, "FROZEN", "f" * 64, txn.txn_id)
    with pytest.raises(journal.CorruptJournal, match="chain"):
        w.journal().all()


def test_a_sealed_and_chained_but_illegal_transition_is_refused(tmp_path):
    w = World(tmp_path)
    state = controller.open_state(w.cfg)
    jrnl = journal.Journal(state, owner=os.geteuid())
    txn_id = "e" * 32
    directory = Path(w.cfg["state_root"]) / "txn" / txn_id
    directory.mkdir(mode=0o700)
    first = forge(directory, 1, "ACCEPTED_REQUEST", None, txn_id, {"prior": {}})
    forge(directory, 2, "SUBMITTING", first["integrity"]["content_sha256"], txn_id)
    with pytest.raises(journal.CorruptJournal, match="not an allowed transition"):
        jrnl.load(txn_id)


# == pass 20 corrections ===========================================================================
# -- 1. requester and sandbox permissions ----------------------------------------------------------
def unit_settings(name: str) -> dict:
    found = {}
    for line in (POWER / "systemd" / name).read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            found.setdefault(key.strip(), []).append(value.strip())
    return found


def test_the_requester_reads_only_its_own_non_secret_configuration(tmp_path):
    example = json.loads((POWER / "config/request.json.example").read_text())
    assert set(example) == config.REQUEST_KEYS
    assert config.validate_request(example) == example
    text = json.dumps(example).lower()
    for secret in ("postgres", "pgpass", "pass_file", "azure", "observer", "status", "key"):
        assert secret not in text
    for extra in ({"postgres": {}}, {"observer": {}}, {"state_root": "/var/lib/x"},
                  {"azure": {}}):
        with pytest.raises(config.ConfigError):
            config.validate_request({**example, **extra})
    full = json.loads((POWER / "config/power.json.example").read_text())
    with pytest.raises(config.ConfigError):
        config.validate_request(full)
    with pytest.raises(config.ConfigError):
        config.validate(example)
    path = tmp_path / "power.json"
    path.write_text(json.dumps(full))
    out = power_cli("request", "--config", str(path))
    assert out.returncode == 2 and "configuration refused" in out.stderr
    # The boot id is read only when a request is actually filed.
    w = World(tmp_path / "w")
    cfg = {**example, "request_dir": w.cfg["request_dir"]}

    def no_boot():
        raise AssertionError("the boot id was read outside the window")
    assert controller.file_request(cfg, ist("12:00:00"), no_boot) == 0
    assert controller.file_request(cfg, ist("17:30:00"), lambda: BOOT) == 0
    assert len(os.listdir(w.cfg["request_dir"])) == 1


def test_the_requester_unit_cannot_reach_credentials_controller_or_observer_state():
    unit = unit_settings("quantdesk-power-request.service")
    hidden = set(unit["InaccessiblePaths"][0].replace("-/", "/").split())
    assert hidden == {"/etc/quantdesk-power", "/var/lib/quantdesk-power",
                      "/etc/quantdesk-observer", "/var/lib/quantdesk-observer"}
    assert unit["ExecStart"][0].endswith("--config /etc/quantdesk-power-request/request.json")
    own = Path(unit["ExecStart"][0].split("--config ")[1])
    assert not any(own.is_relative_to(h) for h in hidden)
    assert unit["ReadWritePaths"] == ["/run/quantdesk-power/requests"]
    assert unit["User"] == ["quantdesk-power"] and unit["IPAddressDeny"] == ["any"]
    assert unit["CapabilityBoundingSet"] == [""] and unit["ProtectSystem"] == ["strict"]
    assert unit["ProtectHome"] == ["yes"] and unit["NoNewPrivileges"] == ["yes"]


def test_the_controller_sandbox_keeps_the_observer_state_read_only():
    unit = unit_settings("quantdesk-power-controller.service")
    writable = " ".join(unit["ReadWritePaths"]).split()
    assert writable == ["/var/lib/quantdesk-power", "/run/quantdesk-power"]
    assert "/var/lib/quantdesk-observer" in unit["ReadOnlyPaths"][0].split()
    assert unit["ProtectSystem"] == ["strict"]
    source = (POWER / "quantdesk_power/locks.py").read_text()
    lock_open = source[source.index('os.open("lock"'):].split("\n")[0]
    assert "O_RDONLY" in lock_open and "O_RDWR" not in lock_open and "O_CREAT" not in lock_open


def test_the_observer_lock_needs_no_write_access_and_still_excludes_observer_runs(tmp_path):
    root = observer_state(tmp_path)
    (root / "lock").chmod(0o444)                  # nobody may write it, not even its owner
    if os.geteuid() != 0:
        with pytest.raises(PermissionError):      # what opening it for writing would meet
            os.open(root / "lock", os.O_RDWR)
    lock = locks.ObserverLock(str(root), os.geteuid())
    lock.acquire(0)
    try:
        probe = subprocess.run(
            [sys.executable, "-c", "import fcntl,os,sys\nfd=os.open(sys.argv[1], os.O_RDONLY)\n"
             "try:\n fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB); print('got')\n"
             "except BlockingIOError: print('busy')", str(root / "lock")],
            capture_output=True, text=True, timeout=30)
        assert probe.stdout.strip() == "busy"
        assert os.fstat(lock.fd).st_mode & 0o777 == 0o444
    finally:
        lock.release()


LINUX_ROOT = sys.platform.startswith("linux") and os.geteuid() == 0


@pytest.mark.skipif(not LINUX_ROOT, reason="UNVERIFIED here: needs Linux and root to switch to "
                                            "a second real account")
def test_a_real_second_account_cannot_read_private_power_files(tmp_path):
    import pwd
    nobody = pwd.getpwnam("nobody")
    private = tmp_path / "etc-quantdesk-power"
    private.mkdir(mode=0o700)
    (private / "pgpass").write_text("secret")
    (private / "pgpass").chmod(0o600)
    requests_dir = tmp_path / "requests"
    requests_dir.mkdir()
    os.chown(requests_dir, 0, nobody.pw_gid)
    requests_dir.chmod(0o1730)
    code = (f"import os,sys\nos.setgroups([]);os.setgid({nobody.pw_gid});"
            f"os.setuid({nobody.pw_uid})\nout=[]\n"
            "for p in sys.argv[1:3]:\n try:\n  open(p).read(); out.append('read')\n"
            " except PermissionError: out.append('denied')\n"
            "try:\n os.listdir(sys.argv[3]); out.append('listed')\n"
            "except PermissionError: out.append('unlisted')\n"
            "open(os.path.join(sys.argv[3],'req-x.json'),'w').close(); out.append('filed')\n"
            "print(','.join(out))")
    found = subprocess.run([sys.executable, "-c", code, str(private / "pgpass"),
                            str(private), str(requests_dir)], capture_output=True, text=True,
                           timeout=30)
    assert found.stdout.strip() == "denied,denied,unlisted,filed"


@pytest.mark.skipif(not (LINUX_ROOT and os.path.exists("/usr/bin/systemd-run")),
                    reason="UNVERIFIED here: needs systemd and root")
def test_the_systemd_sandbox_lets_the_controller_lock_but_not_write_observer_state(tmp_path):
    root = observer_state(tmp_path)
    code = ("import fcntl,os,sys\nfd=os.open(sys.argv[1]+'/lock', os.O_RDONLY)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
            "try:\n open(sys.argv[1]+'/new','w'); print('wrote')\n"
            "except OSError as e: print('locked-readonly', e.errno)")
    out = subprocess.run(["/usr/bin/systemd-run", "--wait", "--pipe", "--quiet",
                          "-p", "ProtectSystem=strict", "-p", f"ReadOnlyPaths={root}",
                          sys.executable, "-c", code, str(root)],
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.startswith("locked-readonly")


# -- 2. continuous writer exclusion ----------------------------------------------------------------
class Cluster:
    """The writer key as PostgreSQL holds it: one EXCLUSIVE holder, or SHARED writers."""

    def __init__(self):
        self.exclusive, self.shared, self.writes = None, set(), []
        self.controller_alive, self.running = True, True
        self.stage = "running"
        self.wal = 0x16B3A28                      # the WAL insert position
        self.individually_terminated = False

    def writer_attempt(self, who):
        """A supported writer: try SHARED (lease or transaction); write only if granted."""
        if not self.running or self.exclusive is not None:
            return False
        self.shared.add(who)
        self.writes.append((who, self.stage))
        self.wal += 0x48                  # its commit record
        self.shared.discard(who)          # a short transaction: committed and released
        return True

    def lsn(self):
        return f"0/{self.wal:X}"

    def writer_holds(self, who):
        if self.exclusive is None and self.running:
            self.shared.add(who)

    def lose_controller_session(self):
        self.controller_alive = False
        if self.exclusive == "controller":
            self.exclusive = None


class ClusterDB(FakeDB):
    """The controller's session against the Cluster model."""

    def __init__(self, cluster, **kw):
        super().__init__(**kw)
        self.cluster = cluster

    def take_writer_key(self):
        self.calls.append("key")
        c = self.cluster
        if not c.controller_alive:
            raise DatabaseError("the database session has ended")
        if c.exclusive is None and not c.shared:
            c.exclusive, self.key_held, self.key_pid = "controller", True, 4242
            c.stage = "key-held"
            return True
        return False

    def verify_writer_key(self):
        self.calls.append("verify")
        if not (self.cluster.controller_alive and self.cluster.exclusive == "controller"):
            raise DatabaseError("the writer key is no longer held by the controller's session")

    def hold_through_shutdown(self, seconds):
        self.verify_writer_key()
        self.wal_lsn = self.cluster.lsn()
        self.guarding = True

    def guard_intact(self):
        return self.guarding and self.cluster.controller_alive

    def shutdown_outcome(self, wait):
        if self.cluster.individually_terminated:
            return db_module.TERMINATED_BY_SHUTDOWN     # the same 57P01 an admin kill gives
        if self.cluster.controller_alive and not self.cluster.running:
            return db_module.TERMINATED_BY_SHUTDOWN
        return "the session ended otherwise: server closed the connection unexpectedly"


BOUNDARIES = ["after_api_stop", "after_key", "after_positions", "after_drained_evidence",
              "after_redis_stop", "before_postgres_stop", "during_postgres_stop"]


@pytest.mark.parametrize("boundary", BOUNDARIES)
@pytest.mark.parametrize("event", ["session_death", "competing_writer", "both"])
def test_no_writer_can_write_after_the_key_while_the_shutdown_proceeds(tmp_path, boundary,
                                                                        event):
    cluster = Cluster()
    w = World(tmp_path, db=ClusterDB(cluster))
    w.runner.checkpoint = cluster.lsn          # the shutdown checkpoint lands at the WAL end

    def fire(here):
        if here != boundary:
            return
        if event in ("session_death", "both"):
            cluster.lose_controller_session()
        if event in ("competing_writer", "both"):
            if not cluster.writer_attempt("angel_backfill") and boundary == "after_api_stop":
                pass
            if boundary == "after_api_stop":
                cluster.writer_holds("doctor")          # a lease taken in the gap and kept

    real_stop, real_take = w.systemd.stop, w.db.take_writer_key
    real_positions, real_hold = w.db.open_positions, w.db.hold_through_shutdown

    def stop(unit, timeout):
        if unit == UNITS["postgres"]:
            fire("during_postgres_stop")
            cluster.running = False
        real_stop(unit, timeout)
        if unit == UNITS["api"]:
            fire("after_api_stop")
        if unit == UNITS["redis"]:
            fire("after_redis_stop")

    def take():
        got = real_take()
        fire("after_key")
        return got

    def positions():
        found = real_positions()
        fire("after_positions")
        return found

    def hold(seconds):
        real_hold(seconds)
        fire("before_postgres_stop")
    w.systemd.stop, w.db.take_writer_key = stop, take
    w.db.open_positions, w.db.hold_through_shutdown = positions, hold
    real_ev = w.ev

    def evidence_then_fire(*args, **kw):
        record = real_ev(*args, **kw)
        if kw["api_stopped"]:
            fire("after_drained_evidence")
        return record
    w.env.obtain_evidence = evidence_then_fire
    w.request()
    w.control()
    states = w.states()
    after_key = [x for x in cluster.writes if x[1] == "key-held"]
    if event == "competing_writer" and boundary != "after_api_stop":
        assert "SUBMITTING" in states            # the refused writer changed nothing (pass 21)
    if "SUBMITTING" in states:
        # The shutdown went ahead only because nothing could write after the key was taken.
        assert after_key == [] and event == "competing_writer" and boundary != "after_api_stop"
    else:
        assert states[-2:] == ["RESTORING", "RESTORED"] and w.arm.deallocations == []
        assert restored(w)


def test_the_key_is_proved_held_after_every_stage_that_follows_it(tmp_path):
    w = World(tmp_path)
    w.request()
    w.control()
    assert w.db.calls.count("verify") == 4              # positions, evidence, Redis, the stop
    for lose_after in range(4):
        tp = tmp_path / f"lose{lose_after}"
        w = World(tp, db=FakeDB(lose_key_after=lose_after))
        w.request()
        w.control()
        [txn] = w.txns()
        assert txn.data("RESTORING")["reason"].startswith(("the writer key was lost",
                                                          "the writer key could not be held"))
        assert restored(w) and w.arm.deallocations == []


@pytest.mark.parametrize("db, reason", [
    (dict(guard_broken=True), "ended before the cluster's stop was issued"),
    (dict(outcome="the session ended otherwise: FATAL:  57P02"), "did not last until"),
    (dict(outcome="the holding statement was still running after the cluster's stop"),
     "did not last until"),
    (dict(error_on="guard"), "could not be held into the cluster's stop"),
])
def test_the_key_must_last_until_the_clusters_own_shutdown(tmp_path, db, reason):
    w = World(tmp_path, db=FakeDB(**db))
    w.request()
    w.control()
    [txn] = w.txns()
    assert reason in txn.data("RESTORING")["reason"]
    assert "SUBMITTING" not in w.states() and restored(w)
    if db.get("guard_broken") or db.get("error_on"):
        assert ("stop", UNITS["postgres"]) not in w.systemd.calls


PSQL_STUB = textwrap.dedent('''\
    import json, os, sys, time
    plan = json.load(open(os.path.splitext(sys.argv[0])[0] + ".json"))
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if line == "\\\\q":
            sys.exit(0)
        if line.startswith("\\\\echo "):
            out.write(line[6:] + "\\n"); out.flush(); continue
        if "pg_try_advisory_lock" in line:
            out.write(plan["take"] + "\\n")
        elif "pg_locks" in line:
            answers = plan.get("verify_sequence")
            out.write((answers.pop(0) if answers else plan["verify"]) + "\\n")
        elif "pg_current_wal_insert_lsn" in line:
            out.write(plan.get("lsn", "0/16B3A28") + "\\n")
        elif "pg_sleep" in line:
            if plan.get("die_before_stop"):
                sys.exit(2)
            while not os.path.exists(plan["trigger"]):
                time.sleep(0.02)
            sys.stderr.write(plan["ending"]); sys.stderr.flush(); sys.exit(2)
        out.flush()
''')


def psql_session(tmp_path, **plan):
    (tmp_path / "psql.py").write_text(PSQL_STUB)
    stub = tmp_path / "psql"          # a wrapper: the interpreter's path may hold a space
    stub.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{tmp_path / "psql.py"}" "$@"\n')
    stub.chmod(0o755)
    plan = {"take": "true,4242", "verify": "4242,1", "trigger": str(tmp_path / "stopped"),
            "ending": "FATAL:  57P01\nserver closed the connection unexpectedly\n", **plan}
    (tmp_path / "psql.json").write_text(json.dumps(plan))
    cfg = make_cfg(tmp_path / "cfg")
    cfg["programs"]["psql"] = str(stub)
    return db_module.PsqlSession(cfg), tmp_path / "stopped"


def test_a_real_session_process_holds_the_key_until_its_shutdown_ending(tmp_path):
    session, stopped = psql_session(tmp_path)
    assert session.take_writer_key() is True and session.key_pid == 4242
    session.verify_writer_key()
    session.hold_through_shutdown(180)
    assert session.guard_intact()
    with pytest.raises(DatabaseError):
        session.query("SELECT 1")                 # nothing else may use the holding session
    stopped.touch()                               # the cluster's fast shutdown
    assert session.shutdown_outcome(10) == db_module.TERMINATED_BY_SHUTDOWN
    assert not session.guard_intact()
    session.close()


@pytest.mark.parametrize("plan, problem", [
    ({"verify": "4243,1"}, "verify"),             # another server process: the session was replaced
    ({"verify": "4242,0"}, "verify"),             # the key is gone
    ({"ending": "FATAL:  57P02\n"}, "outcome"),   # a crash restart, not the shutdown
    ({"ending": "ERROR:  57014\n"}, "outcome"),   # cancelled by a timeout
    ({"ending": ""}, "outcome"),                  # the connection simply vanished
    ({"die_before_stop": True}, "guard"),
])
def test_a_real_session_process_that_loses_the_key_is_detected(tmp_path, plan, problem):
    session, stopped = psql_session(tmp_path, **plan)
    try:
        assert session.take_writer_key()
        if problem == "verify":
            with pytest.raises(DatabaseError):
                session.verify_writer_key()
            return
        session.hold_through_shutdown(180)
        if problem == "guard":
            deadline = __import__("time").monotonic() + 10
            while session.guard_intact() and __import__("time").monotonic() < deadline:
                __import__("time").sleep(0.02)
            assert not session.guard_intact()
            return
        assert session.guard_intact()
        stopped.touch()
        assert session.shutdown_outcome(10) != db_module.TERMINATED_BY_SHUTDOWN
    finally:
        stopped.touch()
        session.close(kill=True)


def test_a_writer_holding_the_key_when_the_drain_ends_stops_the_shutdown(tmp_path):
    w = World(tmp_path, db=FakeDB(key=False))
    w.request()
    w.control()
    [txn] = w.txns()
    assert txn.data("RESTORING")["reason"] == "a QuantDesk writer still holds the writer key"
    assert restored(w) and w.arm.deallocations == []


# -- the admission gate: the barrier before the drain, and boot ------------------------------------
class GatedSystemd(FakeSystemd):
    """systemd with the gate drop-in on the workload units."""
    world = None

    def start(self, unit, timeout):
        if unit in UNITS.values():
            allowed, why = gate.judge(self.world.journal().all(), self.world.boots[0])
            if not allowed:
                self.calls.append(("refused", unit))
                raise CommandFailed(f"{unit}: ExecStartPre gate refused: {why}")
        return super().start(unit, timeout)


def gated_world(tmp_path, **kw):
    systemd = GatedSystemd(**{k: kw.pop(k) for k in ("start_fail", "stop_result")
                              if k in kw})
    w = World(tmp_path, systemd=systemd, **kw)
    systemd.world = w
    return w


def test_from_the_freeze_nothing_but_the_restoration_can_start_the_workload(tmp_path):
    attempts = []
    w = gated_world(tmp_path, ev=Evidence("CLEAR", "BLOCKED"))
    real_ev = w.ev

    def evidence_with_a_restart(*args, **kw):
        # Mid-transaction: the API was just drained; an operator or Restart= tries again.
        if kw["api_stopped"]:
            with pytest.raises(CommandFailed, match="gate refused"):
                w.systemd.start(UNITS["api"], 5)
            attempts.append("refused")
        return real_ev(*args, **kw)
    w.env.obtain_evidence = evidence_with_a_restart
    w.request()
    w.control()
    assert attempts == ["refused"]
    assert w.states()[-2:] == ["RESTORING", "RESTORED"] and restored(w)


GATE_SAME_BOOT = {   # journal state -> may the workload start in the boot that recorded it?
    "ACCEPTED_REQUEST": True, "FROZEN": False, "EVIDENCE_CLEAR": False, "SYSTEM_CLEAR": False,
    "WEB_STOPPED": False, "API_STOPPED": False, "WRITERS_EXCLUDED": False,
    "POSITIONS_CLEAR": False, "DRAINED_EVIDENCE_CLEAR": False, "REDIS_STOPPED": False,
    "POSTGRES_STOPPED": False, "SUBMITTING": False, "NOT_SENT": True, "REJECTED": True,
    "ACCEPTED": False, "COMPLETED": False, "UNKNOWN": False, "POLL_SUCCEEDED": False,
    "POLL_FAILED": True, "UNKNOWN_RETAINED": False, "OBSERVED_NEW_BOOT": False,
    "ABORTED": True, "RESTORING": True, "RESTORED": True, "RESTORE_FAILED": True,
    "OPERATOR_CONFIRMED_DEALLOCATED": False, "OPERATOR_CONFIRMED_NOT_PERFORMED": True,
    "OPERATOR_CLOSED": True, "RESUMING": True, "RESUMED": True, "RESUME_FAILED": True}
GATE_LATER_BOOT = {**GATE_SAME_BOOT, **{s: True for s in (
    "FROZEN", "EVIDENCE_CLEAR", "SYSTEM_CLEAR", "WEB_STOPPED", "API_STOPPED", "WRITERS_EXCLUDED",
    "POSITIONS_CLEAR", "DRAINED_EVIDENCE_CLEAR", "REDIS_STOPPED", "POSTGRES_STOPPED",
    "COMPLETED", "POLL_SUCCEEDED", "OPERATOR_CONFIRMED_DEALLOCATED")}}


def synthetic(state, boot, submitted=None):
    records = [{"state": "ACCEPTED_REQUEST", "boot_id": boot, "at_utc": "2026-10-27T12:00:00"}]
    if submitted:
        records.append({"state": "SUBMITTING", "boot_id": submitted, "at_utc": "x"})
    records.append({"state": state, "boot_id": boot, "at_utc": "2026-10-27T12:01:00"})
    return journal.Transaction("f" * 32, records)


def test_the_gate_table_covers_every_journal_state():
    assert set(GATE_SAME_BOOT) == journal.STATES


@pytest.mark.parametrize("state", sorted(journal.STATES))
def test_the_gate_decides_every_journal_state_in_this_boot_and_a_later_one(state):
    submitted = BOOT if state in journal.DEALLOCATED else None
    assert gate.judge([synthetic(state, BOOT, submitted)], BOOT)[0] is GATE_SAME_BOOT[state]
    assert gate.judge([synthetic(state, BOOT, submitted)], BOOT2)[0] is GATE_LATER_BOOT[state]


def test_the_gate_fails_closed_on_missing_unreadable_or_contradictory_records(tmp_path):
    w = World(tmp_path)
    assert gate.decide(w.cfg, lambda: BOOT) == (False, "the power controller's state "
                                                       "directory is missing")
    assert not (Path(w.cfg["state_root"])).exists()            # the gate created nothing
    Path(w.cfg["state_root"]).mkdir(mode=0o700)
    allowed, why = gate.decide(w.cfg, lambda: BOOT)
    assert not allowed and "journal is missing" in why
    assert not (Path(w.cfg["state_root"]) / "txn").exists()

    def no_boot():
        raise OSError(2, "No such file or directory")
    assert gate.decide(w.cfg, no_boot)[0] is False
    w.journal()                                                 # initialized, as by install
    assert gate.decide(w.cfg, lambda: BOOT) == (True, "no unresolved or in-progress shutdown")
    Path(w.cfg["state_root"]).chmod(0o770)
    assert gate.decide(w.cfg, lambda: BOOT)[0] is False         # unsafe directory
    Path(w.cfg["state_root"]).chmod(0o700)
    w.ev = Evidence("BLOCKED")
    w.env.obtain_evidence = w.ev
    w.request()
    w.control()
    [txn] = w.txns()
    target = Path(w.cfg["state_root"]) / "txn" / txn.txn_id / sorted(
        os.listdir(Path(w.cfg["state_root"]) / "txn" / txn.txn_id))[1]
    target.chmod(0o600)
    target.write_text(target.read_text().replace("FROZEN", "FROZEN "))
    allowed, why = gate.decide(w.cfg, lambda: BOOT)
    assert not allowed and "unusable" in why
    two = [synthetic("RESTORING", BOOT), journal.Transaction("e" * 32, synthetic(
        "RESTORE_FAILED", BOOT).records)]
    assert gate.judge(two, BOOT)[0] is False


@pytest.mark.parametrize("after", FORWARD + ["ACCEPTED"])
def test_after_a_crash_and_a_reboot_the_gate_holds_exactly_what_may_have_been_sent(
        tmp_path, monkeypatch, after):
    w = World(tmp_path, arm=FakeArm(follow=("InProgress", "budget"),
                                    recovery_follow=("Unavailable", "403")))
    real = journal.Journal.append

    def append(self, txn, state, boot_id, data, clock=journal.now_utc):
        record = real(self, txn, state, boot_id, data, clock)
        if state == after:
            raise Crash(state)
        return record
    monkeypatch.setattr(journal.Journal, "append", append)
    w.request()
    with pytest.raises(Crash):
        w.control()
    monkeypatch.setattr(journal.Journal, "append", real)
    sent = after in ("SUBMITTING", "ACCEPTED")
    # Same boot, before recovery: every stopping stage holds the workload down.
    assert gate.decide(w.cfg, lambda: BOOT)[0] is (after == "ACCEPTED_REQUEST")
    w.boots.insert(0, BOOT2)                                    # the machine restarted
    assert gate.decide(w.cfg, lambda: BOOT2)[0] is (not sent)
    w.reconcile()
    assert gate.decide(w.cfg, lambda: BOOT2)[0] is (not sent)
    if sent:
        for days in (1, 2, 7):                                  # time passing changes nothing
            w.now += timedelta(days=days)
            w.reconcile()
            assert gate.decide(w.cfg, lambda: BOOT2)[0] is False
        assert ("start", UNITS["api"]) not in w.systemd.calls


def test_a_deallocation_settled_on_a_later_boot_resumes_the_held_workload(tmp_path):
    w = gated_world(tmp_path, arm=FakeArm(follow=("InProgress", "budget"),
                                          recovery_follow=("Succeeded", "done")))
    w.request()
    w.control()
    assert not w.all_active()
    w.boots.insert(0, BOOT2)                     # started by qd-start; the gate held it down
    with pytest.raises(CommandFailed, match="gate refused"):
        w.systemd.start(UNITS["postgres"], 5)
    assert w.reconcile() == []
    # Correction (pass 21): RESUMED (workload verified back) follows POLL_SUCCEEDED.
    assert [r["state"] for r in w.txns()[0].records][-3:] == ["POLL_SUCCEEDED", "RESUMING",
                                                              "RESUMED"]
    assert w.all_active()
    # Correction (pass 21): resumption now also brings back (and verifies) the timers that
    # ran before; the workload units still come back first and in dependency order.
    starts = [u for op, u in w.systemd.calls if op == "start" and u in UNITS.values()]
    assert starts[-4:] == [UNITS["postgres"], UNITS["redis"], UNITS["api"], UNITS["web"]]
    assert all(w.systemd.state[t] == "active" for t in TIMERS + PACKAGE_TIMERS)


def test_a_completed_finding_in_the_boot_that_asked_is_refused(tmp_path):
    w = World(tmp_path, arm=FakeArm(Submission(azure.UNKNOWN, None, "lost")))
    w.request()
    w.control()
    [txn] = w.txns()
    with pytest.raises(ValueError, match="still the boot that sent the request"):
        recovery.resolve(w.env, w.journal(), txn.txn_id, "deallocation-completed", "ref", "op")
    assert w.txns()[0].state == "UNKNOWN"


def test_the_gate_drop_in_covers_every_workload_unit_and_status_shows_the_gate(tmp_path):
    text = (POWER / "systemd/quantdesk-workload-10-power-gate.conf").read_text()
    assert ("ExecStartPre=+/usr/bin/python3 -I /opt/quantdesk-power/power.py gate --config "
            "/etc/quantdesk-power/power.json --unit %n") in text
    for unit in UNITS.values():
        assert f"/etc/systemd/system/{unit}.d/10-power-gate.conf" in text
    assert "NOT INSTALLED" in text
    w = World(tmp_path)
    path = tmp_path / "power.json"
    path.write_text(json.dumps(w.cfg))
    out = power_cli("gate", "--config", str(path), "--unit", UNITS["api"])
    assert out.returncode != 0 and "REFUSED" in out.stderr     # no state yet: fail closed
    state = Path(w.cfg["state_root"])
    assert not state.exists()
    shown = json.loads(power_cli("status", "--config", str(path), "--json").stdout)
    assert shown["workload_start"]["allowed"] is False
    # Reading the status creates nothing: a missing state directory stays
    # missing, so the gate keeps refusing for that reason on every platform.
    assert not state.exists()
    assert shown["transactions"] == [] and shown["unresolved"] == []


# -- 3. package updates ----------------------------------------------------------------------------
def test_automatic_updates_are_paused_for_the_protected_interval_and_restored_exactly(tmp_path):
    systemd = FakeSystemd()
    systemd.state["apt-daily-upgrade.timer"] = "inactive"         # the operator had it off
    w = World(tmp_path, systemd=systemd, ev=Evidence("BLOCKED"))
    w.request()
    w.control()
    [txn] = w.txns()
    assert txn.data("FROZEN")["package_timers_paused"] == ["apt-daily.timer"]
    assert ("stop", "apt-daily-upgrade.timer") not in w.systemd.calls
    assert w.systemd.state["apt-daily.timer"] == "active"
    assert w.systemd.state["apt-daily-upgrade.timer"] == "inactive"
    assert ("start", "apt-daily-upgrade.timer") not in w.systemd.calls
    verbs = {op for op, _ in w.systemd.calls}
    assert verbs <= {"stop", "start"}


def test_an_update_already_starting_when_paused_stops_the_shutdown(tmp_path):
    systemd = FakeSystemd()
    real = systemd.stop

    def stop(unit, timeout):
        real(unit, timeout)
        if unit == "apt-daily-upgrade.timer":      # it had just fired
            systemd.state["apt-daily-upgrade.service"] = "activating"
    systemd.stop = stop
    w = World(tmp_path, systemd=systemd)
    w.request()
    w.control()
    [txn] = w.txns()
    assert w.states() == ["ACCEPTED_REQUEST", "RESTORING", "RESTORED"]
    assert "package management is active" in txn.data("RESTORING")["reason"]
    assert all(w.systemd.state[t] == "active" for t in PACKAGE_TIMERS + TIMERS)
    assert w.arm.deallocations == [] and w.all_active()


def test_updates_are_never_disabled_or_masked_and_return_after_an_unresolved_case(tmp_path):
    for path in source_files(POWER / "quantdesk_power"):
        text = path.read_text()
        for verb in ('"disable"', '"mask"', "'disable'", "'mask'"):
            assert verb not in text, path
    w = World(tmp_path, arm=FakeArm(Submission(azure.UNKNOWN, None, "lost")))
    w.request()
    w.control()
    assert all(w.systemd.state[t] == "inactive" for t in PACKAGE_TIMERS)   # still protected
    [txn] = w.txns()
    recovery.resolve(w.env, w.journal(), txn.txn_id, "deallocation-not-performed", "ref", "op")
    assert all(w.systemd.state[t] == "active" for t in PACKAGE_TIMERS)


def test_the_unattended_upgrades_shutdown_helper_is_not_update_activity(tmp_path):
    with pytest.raises(config.ConfigError, match="not an update in progress"):
        make_cfg(tmp_path, apt_units=["apt-daily.service", "unattended-upgrades.service"])
    with pytest.raises(config.ConfigError):
        make_cfg(tmp_path / "b", package_timers=[])


def test_a_crash_while_pausing_updates_is_restored_by_recovery(tmp_path, monkeypatch):
    w = World(tmp_path)
    real = FakeSystemd.stop

    def stop(self, unit, timeout):
        if unit == "apt-daily-upgrade.timer":
            raise Crash("killed while pausing updates")
        return real(self, unit, timeout)
    monkeypatch.setattr(FakeSystemd, "stop", stop)
    w.request()
    with pytest.raises(Crash):
        w.control()
    monkeypatch.setattr(FakeSystemd, "stop", real)
    assert w.reconcile() == []
    assert all(w.systemd.state[t] == "active" for t in PACKAGE_TIMERS + TIMERS)


def test_a_crash_during_the_clusters_stop_is_restored_by_recovery(tmp_path, monkeypatch):
    w = World(tmp_path)
    real = FakeSystemd.stop

    def stop(self, unit, timeout):
        if unit == UNITS["postgres"]:
            raise Crash("killed while the cluster stopped")
        return real(self, unit, timeout)
    monkeypatch.setattr(FakeSystemd, "stop", stop)
    w.request()
    with pytest.raises(Crash):
        w.control()
    monkeypatch.setattr(FakeSystemd, "stop", real)
    assert w.states()[-1] == "REDIS_STOPPED"
    assert w.reconcile() == [] and restored(w) and w.arm.deallocations == []


# -- 6. IMDS isolation -----------------------------------------------------------------------------
DROP = [{"match": {"op": "==", "left": {"payload": {"protocol": "ip", "field": "daddr"}},
                   "right": "169.254.169.254"}},
        {"match": {"op": "!=", "left": {"meta": {"key": "skuid"}}, "right": 0}},
        {"drop": None}]


def with_match(index, **change):
    expr = json.loads(json.dumps(DROP))
    expr[index]["match"].update(change)
    return [{"expr": expr}]


@pytest.mark.parametrize("listing", [
    nft_json(rules=[{"expr": DROP, "comment": "anything at all"}]),
    nft_json(rules=[{"expr": DROP, "comment": None}]),
    nft_json(rules=[{"expr": DROP}], chain={"prio": -100}),
])
def test_an_effective_isolation_rule_passes_whatever_its_comment(tmp_path, listing):
    w = World(tmp_path)
    w.runner.nft = listing
    assert facts.imds_isolation(w.cfg, w.runner).status == "PASS"


@pytest.mark.parametrize("listing, why", [
    (NFT_LISTING, "not nftables JSON"),
    (nft_json(chain={"hook": "input"}), "output hook"),
    (nft_json(chain={"hook": "postrouting"}), "output hook"),
    (nft_json(chain={"type": "nat"}), "output hook"),
    (json.dumps({"nftables": [o for o in json.loads(NFT_JSON)["nftables"] if "chain" not in o
                              or True] and [
        {"table": {"family": "inet", "name": "quantdesk_imds"}},
        {"chain": {"family": "inet", "table": "quantdesk_imds", "name": "output"}},
        {"rule": {"family": "inet", "table": "quantdesk_imds", "chain": "output",
                  "comment": "qd-imds-nonroot-drop", "expr": DROP}}]}), "output hook"),
    (nft_json(rules=[{"expr": [{"accept": None}]}, {"expr": DROP}]), "2 rules"),
    (nft_json(rules=[{"expr": [{"jump": {"target": "elsewhere"}}]}, {"expr": DROP}]),
     "2 rules"),
    (nft_json(rules=[{"expr": DROP}, {"expr": [{"accept": None}]}]), "2 rules"),
    (nft_json(rules=with_match(1, right=1000)), "is not 'ip daddr"),
    (nft_json(rules=with_match(1, op=">")), "is not 'ip daddr"),
    (nft_json(rules=with_match(1, right="nobody")), "is not 'ip daddr"),
    (nft_json(rules=with_match(0, right="169.254.169.253")), "is not 'ip daddr"),
    (nft_json(rules=[{"expr": DROP[:1] + DROP[2:]}]), "is not 'ip daddr"),
    (nft_json(rules=[{"expr": DROP[:2] + [{"accept": None}]}]), "is not 'ip daddr"),
    (nft_json(rules=[]), "0 rules"),
    (nft_json(extra=[{"chain": {"family": "inet", "table": "quantdesk_imds",
                                "name": "spare"}}]), "2 chains"),
    (nft_json(extra=[{"set": {"family": "inet", "table": "quantdesk_imds",
                              "name": "allowed"}}]), "other objects"),
    (json.dumps({"nftables": [{"table": {"family": "ip", "name": "quantdesk_imds"}}]}),
     "not exactly the isolation table"),
])
def test_an_ineffective_isolation_rule_is_refused_whatever_its_comment(tmp_path, listing, why):
    w = World(tmp_path)
    w.runner.nft = listing
    found = facts.imds_isolation(w.cfg, w.runner)
    assert found.status == "UNKNOWN" and why in found.detail


def test_the_shipped_rule_file_is_the_structure_the_controller_accepts():
    lines = [x.strip() for x in (POWER / "nftables/quantdesk-imds.nft").read_text().splitlines()
             if x.strip() and not x.strip().startswith("#")]
    assert lines[0] == "table inet quantdesk_imds {" and lines[1] == "chain output {"
    assert lines[2] == "type filter hook output priority filter; policy accept;"
    rules = lines[3:-2]
    assert rules == ['ip daddr 169.254.169.254 meta skuid != 0 counter drop comment '
                     '"qd-imds-nonroot-drop"']
    assert facts.imds_ruleset_problem(json.loads(NFT_JSON), "inet", "quantdesk_imds") is None


@pytest.mark.skipif(not (LINUX_ROOT and os.path.exists("/usr/sbin/nft")),
                    reason="UNVERIFIED here: needs Linux, root and nftables")
def test_real_nftables_lists_the_shipped_table_as_the_accepted_structure():
    subprocess.run(["/usr/sbin/nft", "-f", str(POWER / "nftables/quantdesk-imds.nft")],
                   check=True, timeout=30)
    try:
        out = subprocess.run(["/usr/sbin/nft", "-j", "list", "table", "inet", "quantdesk_imds"],
                             capture_output=True, text=True, check=True, timeout=30)
        assert facts.imds_ruleset_problem(json.loads(out.stdout), "inet",
                                          "quantdesk_imds") is None
        probe = ("import os,socket\nos.setgid(65534);os.setuid(65534)\n"
                 "s=socket.socket();s.settimeout(3)\n"
                 "try:\n s.connect(('169.254.169.254',80)); print('reached')\n"
                 "except OSError: print('refused')")
        assert subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                              timeout=30).stdout.strip() == "refused"
    finally:
        subprocess.run(["/usr/sbin/nft", "delete", "table", "inet", "quantdesk_imds"],
                       timeout=30)


# -- 7. Retry-After --------------------------------------------------------------------------------
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("value, seconds", [
    (None, azure.RETRY_DEFAULT), ("120", 120.0), ("1000", 1000.0), ("0", azure.RETRY_MIN),
    ("3", azure.RETRY_MIN), ("-5", azure.RETRY_MALFORMED), ("1.5", azure.RETRY_MALFORMED),
    ("nan", azure.RETRY_MALFORMED), ("inf", azure.RETRY_MALFORMED), ("", azure.RETRY_MALFORMED),
    ("soon", azure.RETRY_MALFORMED), ("12x", azure.RETRY_MALFORMED),
    ("Tue, 27 Oct 2026 12:01:30 GMT", 90.0), ("Tue, 27 Oct 2026 11:00:00 GMT", azure.RETRY_MIN),
])
def test_retry_after_is_honoured_as_given_and_never_guessed(value, seconds):
    assert azure.retry_after(value, NOW) == seconds


def timed_arm(cfg, responses, clock):
    reads = []

    def connect(host):
        if host == azure.IMDS_HOST:
            return FakeConn({"response": FakeResponse(200, b'{"access_token": "t"}')})
        reads.append(clock["t"])
        item = responses.pop(0)
        return FakeConn(item if isinstance(item, dict) else {"response": item})
    return azure.Arm(cfg, connect=connect, sleep=lambda s: clock.update(t=clock["t"] + s),
                     monotonic=lambda: clock["t"],
                     wall=lambda: NOW + timedelta(seconds=clock["t"])), reads


def test_status_reads_never_come_earlier_than_azure_asked(tmp_path):
    cfg = make_cfg(tmp_path)
    clock = {"t": 0.0}
    arm, reads = timed_arm(cfg, [FakeResponse(200, b'{"status": "InProgress"}',
                                              {"Retry-After": "120"}),
                                 FakeResponse(200, b'{"status": "InProgress"}',
                                              {"Retry-After": "nonsense"}),
                                 FakeResponse(200, b'{"status": "Succeeded"}')], clock)
    state, _, _ = arm.follow({"async_url": op_url(cfg), "retry_after": 30.0}, budget=900)
    assert state == azure.POLL_SUCCEEDED
    assert reads == [30.0, 150.0, 150.0 + azure.RETRY_MALFORMED]


def test_a_delay_beyond_the_budget_leaves_the_outcome_unknown_without_reading_early(tmp_path):
    cfg = make_cfg(tmp_path)
    clock = {"t": 0.0}
    arm, reads = timed_arm(cfg, [], clock)
    state, detail, resume = arm.follow({"async_url": op_url(cfg), "retry_after": 1200.0},
                                       budget=900)
    assert state == azure.POLL_IN_PROGRESS and reads == [] and clock["t"] == 0
    assert datetime.fromisoformat(resume) == NOW + timedelta(seconds=1200)


def test_a_lost_or_timed_out_status_read_is_only_a_lost_read(tmp_path):
    cfg = make_cfg(tmp_path)
    clock = {"t": 0.0}
    arm, reads = timed_arm(cfg, [{"receive": TimeoutError()}, {"receive": ConnectionResetError()},
                                 FakeResponse(200, b'{"status": "Succeeded"}')], clock)
    state, _, _ = arm.follow({"async_url": op_url(cfg), "retry_after": 10.0}, budget=900)
    assert state == azure.POLL_SUCCEEDED
    assert reads == [10.0, 10.0 + azure.RETRY_DEFAULT, 10.0 + 2 * azure.RETRY_DEFAULT]


def test_recovery_honours_the_journaled_earliest_read_across_runs(tmp_path):
    w = World(tmp_path)
    clock = {"t": 0.0}
    gets, posts = [], []

    def connect(host):
        if host == azure.IMDS_HOST:
            return FakeConn({"response": FakeResponse(200, b'{"access_token": "t"}')})
        if not posts:
            posts.append(clock["t"])
            return FakeConn({"response": FakeResponse(202, headers={
                "Azure-AsyncOperation": op_url(w.cfg), "Retry-After": "1200"})})
        gets.append(w.now)
        return FakeConn({"response": FakeResponse(200, b'{"status": "Succeeded"}')})
    w.env.arm = w.arm = azure.Arm(w.cfg, connect=connect,
                                  sleep=lambda s: clock.update(t=clock["t"] + s),
                                  monotonic=lambda: clock["t"], wall=lambda: w.now)
    w.request()
    assert w.control() == 1
    [txn] = w.txns()
    earliest = datetime.fromisoformat(txn.data("UNKNOWN_RETAINED")["poll_not_before_utc"])
    assert earliest == w.now + timedelta(seconds=1200) and gets == []
    w.now += timedelta(seconds=300)
    w.reconcile()
    assert gets == [] and w.txns()[0].state == "UNKNOWN_RETAINED"
    assert len(w.txns()[0].records) == len(txn.records)          # nothing new to record
    w.now += timedelta(seconds=1000)
    # Correction (pass 21): in the boot that asked, a succeeded deallocation stays open
    # (the workload is resumed only in a later boot); nothing is resent.
    assert [t.state for t in w.reconcile()] == ["POLL_SUCCEEDED"]
    assert len(gets) == 1 and gets[0] >= earliest and len(posts) == 1
    assert w.txns()[0].state == "POLL_SUCCEEDED"


@pytest.mark.parametrize("response", [FakeResponse(202), FakeResponse(201), FakeResponse(204)])
def test_an_ambiguous_acceptance_is_never_treated_as_done(tmp_path, response):
    cfg = make_cfg(tmp_path)
    arm, made = arm_with(cfg, {"response": response})
    sub = arm.deallocate("c")
    assert sub.outcome in (azure.ACCEPTED, azure.UNKNOWN)
    if sub.outcome == azure.ACCEPTED:
        assert arm.follow(sub.as_dict())[0] == azure.POLL_UNAVAILABLE
    w = World(tmp_path / "w", arm=FakeArm(sub, follow=("Unavailable", "no operation URL")))
    w.request()
    assert w.control() == 1
    assert w.states()[-1] in ("UNKNOWN", "UNKNOWN_RETAINED")
    assert gate.decide(w.cfg, lambda: BOOT)[0] is False and len(w.arm.deallocations) == 1


# -- 8. API process inspection ---------------------------------------------------------------------
class AfterStopRunner(FakeRunner):
    def __init__(self, systemd, ps_after):
        super().__init__(systemd)
        self.ps_after = ps_after

    def run(self, argv, timeout, **kw):
        if os.path.basename(argv[0]) == "ps" and self.systemd.state[UNITS["api"]] != "active":
            outcome = self.ps_after
            if isinstance(outcome, Exception):
                raise outcome
            return Result(argv, outcome[0], outcome[1], "")
        return super().run(argv, timeout, **kw)


class AfterStopSystemd(FakeSystemd):
    def __init__(self, api_props=None, show_fails=False):
        super().__init__()
        self.api_props, self.show_fails = api_props or {}, show_fails

    def show(self, unit, timeout=None):
        props = super().show(unit, timeout)
        if unit == UNITS["api"] and self.state[unit] != "active" and \
                ("stop", unit) in self.calls and ("start", unit) not in self.calls:
            if self.show_fails and self.calls.count(("show-api",)) == 1 and \
                    ("show-api-failed",) not in self.calls:
                self.calls.append(("show-api-failed",))
                raise CommandFailed("systemctl show timed out")
            self.calls.append(("show-api",))
            props.update(self.api_props)
        return props


@pytest.mark.parametrize("systemd_opts, ps_after, presence", [
    ({}, (0, "1 /sbin/init\n"), "ABSENT"),
    ({}, (1, ""), "UNKNOWN"),
    ({}, CommandFailed("ps did not finish within 30 s"), "UNKNOWN"),
    ({}, (0, "944 bash\n"), "UNKNOWN"),                            # no process 1: implausible
    ({}, (0, "1 /sbin/init\n943 /venv/bin/uvicorn app.main:app\n"), "PRESENT"),
    ({"api_props": {"MainPID": "943"}}, (0, "1 /sbin/init\n"), "PRESENT"),
    ({"api_props": {"MainPID": "?"}}, (0, "1 /sbin/init\n"), "UNKNOWN"),
    ({"show_fails": 1}, (0, "1 /sbin/init\n"), "UNKNOWN"),
])
def test_only_a_confirmed_absent_api_lets_its_dependencies_stop(tmp_path, systemd_opts,
                                                                 ps_after, presence):
    systemd = AfterStopSystemd(**systemd_opts)
    w = World(tmp_path, systemd=systemd, runner=AfterStopRunner(systemd, ps_after))
    w.request()
    w.control()
    states = w.states()
    if presence == "ABSENT":
        assert "API_STOPPED" in states and "SUBMITTING" in states
        return
    [txn] = w.txns()
    assert states[-3:] == ["WEB_STOPPED", "RESTORING", "RESTORED"]
    assert txn.data("RESTORING")["api_presence"]["data"]["presence"] == presence
    assert ("stop", UNITS["redis"]) not in w.systemd.calls
    assert ("stop", UNITS["postgres"]) not in w.systemd.calls


@pytest.mark.parametrize("props, rows, presence", [
    ({"ActiveState": "deactivating", "MainPID": "0"}, "1 /sbin/init\n", "PRESENT"),
    ({"ActiveState": "inactive", "MainPID": "0"}, "1 /sbin/init\n", "ABSENT"),
    ({"ActiveState": "failed", "MainPID": "0"}, "1 /sbin/init\n", "ABSENT"),
    ({"ActiveState": "inactive", "MainPID": ""}, "1 /sbin/init\n", "UNKNOWN"),
    ({"ActiveState": "maintenance", "MainPID": "0"}, "1 /sbin/init\n", "UNKNOWN"),
    ({"ActiveState": "inactive", "MainPID": "0"}, "", "UNKNOWN"),
])
def test_api_presence_is_three_valued(tmp_path, props, rows, presence):
    w = World(tmp_path)

    class Shown:
        def show(self, unit):
            return {"LoadState": "loaded", **props}

    class Ps:
        def run(self, argv, timeout, **kw):
            return Result(argv, 0, rows, "")
    found = facts.api_presence(w.cfg, Ps(), Shown())
    assert found.data["presence"] == presence
    assert found.status == {"ABSENT": "PASS", "PRESENT": "BLOCKED"}.get(presence, "UNKNOWN")


# -- 9. special sessions ---------------------------------------------------------------------------
def allow_deallocation_hold(w):
    hold = json.loads((POWER / "config/power_hold.example.json").read_text())
    hold.update(kind="ALLOW_DEALLOCATION", reason_code="SPECIAL_SESSION",
                from_ist="2026-10-27T17:00:00+05:30", to_ist="2026-10-29T09:00:00+05:30")
    (Path(w.cfg["holds_dir"]) / "allow.json").write_text(json.dumps(hold))


def test_no_hold_or_setting_can_permit_a_shutdown_past_a_special_session(tmp_path):
    w = World(tmp_path)
    write_calendar(Path(w.cfg["calendar_file"]), special=("2026-10-28",))
    allow_deallocation_hold(w)
    before = Path(w.cfg["calendar_file"]).read_bytes()
    w.request()
    w.control()
    [txn] = w.txns()
    found = {f["name"]: f["status"] for f in txn.data("ABORTED")["facts"]}
    assert txn.state == "ABORTED" and found["special_sessions"] == "BLOCKED"
    assert w.systemd.calls == [] and Path(w.cfg["calendar_file"]).read_bytes() == before


def edit_calendar(path: Path, day: str, entry):
    calendar = json.loads(path.read_text())
    if entry is None:
        calendar["dates"].pop(day)
    else:
        calendar["dates"][day] = entry
    path.write_text(json.dumps(calendar))


REVIEW = {"review_status": "REVIEWED", "reviewer": "Astra",
          "reviewed_at": "2026-10-16T18:00:00+05:30", "official_reference": "NSE/FAOP/71777"}


@pytest.mark.parametrize("entry, status", [
    ({"session_type": "SPECIAL", "review_status": "UNREVIEWED"}, "UNKNOWN"),
    (None, "UNKNOWN"),                                          # no entry: not ruled out
    ("SPECIAL", "UNKNOWN"),                                     # malformed
    ({"session_type": "MUHURAT", **REVIEW}, "UNKNOWN"),         # an unknown kind
    ({"session_type": "NORMAL", "review_status": "UNREVIEWED"}, "UNKNOWN"),
    ({"session_type": "SPECIAL", **REVIEW}, "BLOCKED"),
    ({"session_type": "NORMAL", **REVIEW}, "PASS"),
    ({"session_type": "CLOSED", **REVIEW}, "PASS"),
])
def test_an_uncertain_special_session_tomorrow_blocks_the_shutdown(tmp_path, entry, status):
    w = World(tmp_path)
    edit_calendar(Path(w.cfg["calendar_file"]), "2026-10-28", entry)
    allow_deallocation_hold(w)
    assert facts.special_sessions(w.cfg, w.now).status == status
    w.request()
    w.control()
    [txn] = w.txns()
    if status == "PASS":
        assert "SUBMITTING" in w.states()
    else:
        assert txn.state == "ABORTED" and w.systemd.calls == []


# -- 5. the status record, kept by the controller --------------------------------------------------
def provision_key(w, key=bytes(range(32))):
    path = Path(w.cfg["holds_dir"]).parent / "status.key"
    path.write_bytes(key)
    path.chmod(0o600)
    w.cfg["status"]["key_file"] = str(path)
    return key


def status_records(w):
    directory = Path(w.cfg["state_root"]) / "status"
    return [(directory / n).read_text().strip() for n in sorted(os.listdir(directory))]


def test_the_controller_keeps_a_signed_status_including_one_before_the_request_leaves(tmp_path):
    w = World(tmp_path, arm=FakeArm(Submission(azure.UNKNOWN, None, "lost")))
    key = provision_key(w)
    seen = {}
    real = w.arm.deallocate

    def deallocate(client_id):
        seen["before"] = status_records(w)
        return real(client_id)
    w.arm.deallocate = deallocate
    w.request()
    w.control()
    [before] = seen["before"]
    fields = status.decode(before, key)
    assert fields["state"] == "SUBMITTING" and fields["flag"] == "UNRESOLVED"
    assert fields["sub"] == BOOT[:8] and fields["vm"] == status.vm_digest(
        status.vm_resource_id(w.cfg))
    records = status_records(w)
    assert [status.decode(r, key)["seq"] for r in records] == [1, 2]
    assert status.decode(records[-1], key)["state"] == "UNKNOWN"
    w.reconcile()


def test_no_key_means_no_status_and_no_transport_is_authorized(tmp_path):
    w = World(tmp_path)
    w.request()
    w.control()
    assert not (Path(w.cfg["state_root"]) / "status").exists()
    with pytest.raises(config.ConfigError, match="not authorized"):
        make_cfg(tmp_path / "b", status={"key_file": "/etc/k", "transport": "vm-tag"})
    short = World(tmp_path / "c")
    provision_key(short, key=b"short")
    assert status.read_key(short.cfg) is None


@pytest.mark.parametrize("journal_state, flag", [
    ("RESTORED", "OK"), ("RESTORE_FAILED", "RESTORE_FAILED"), ("UNKNOWN", "UNRESOLVED"),
    ("FROZEN", "IN_PROGRESS"), ("COMPLETED", "OK")])
def test_the_status_flag_summarizes_the_journal(journal_state, flag):
    txn = synthetic(journal_state, BOOT, BOOT if journal_state in ("UNKNOWN", "COMPLETED")
                    else None)
    assert status.summarize([txn], BOOT)[3] == flag
    assert status.summarize(None, BOOT) == ("-", "NONE", "-", "CORRUPT")


# -- 10. notification, qualification and checkpoint isolation --------------------------------------
def tree_state(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None,
                                       p.stat().st_mode, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}


@pytest.mark.parametrize("ev", [("CLEAR", "CLEAR"), ("CLEAR", "BLOCKED")])
def test_observer_notifications_qualifications_and_checkpoints_are_never_touched(tmp_path, ev):
    root = observer_state(tmp_path)
    for name, body in (("notifications/n-1.json", b'{"state": "UNRESOLVED"}'),
                       ("qualification/q.json", b'{"level": "PROVISIONAL"}'),
                       ("checkpoints/c-1.json", b'{"checkpoint": 1}'),
                       ("state.json", b"{}")):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(body)
    before = tree_state(root)
    w = World(tmp_path / "w", ev=Evidence(*ev))
    w.env.observer_lock = locks.ObserverLock
    w.cfg["observer"]["state_root"] = str(root)
    w.request()
    w.control()
    assert tree_state(root) == before
    # Correction (pass 21): PostgreSQL's own "shutdown checkpoint" / "Latest checkpoint
    # location" (the WAL evidence) is not the observer's checkpoint; only those exact
    # PostgreSQL phrases are set aside, every other mention is still refused.
    postgres_terms = ("latest checkpoint location", "shutdown checkpoint",
                      "shutdown_checkpoint", "checkpoint_at")
    for path in source_files(POWER / "quantdesk_power"):
        text = path.read_text().lower()
        for term in postgres_terms:
            text = text.replace(term, "")
        for word in ("notification", "qualif", "checkpoint"):
            assert word not in text, (path, word)


# == pass 21 corrections ===========================================================================
# -- A1. requester directory access ------------------------------------------------------------
def requester_tree(tmp_path: Path) -> tuple[Path, dict]:
    """/run/quantdesk-power (0711) and requests/ with the requester's real rights: write and
    search, no read (the group bits of 1730), modelled on the owner bits here."""
    run = tmp_path / "run-quantdesk-power"
    run.mkdir()
    requests_dir = run / "requests"
    requests_dir.mkdir()
    requests_dir.chmod(0o330)
    run.chmod(0o711)
    cfg = json.loads((POWER / "config/request.json.example").read_text())
    cfg["request_dir"] = str(requests_dir)
    return requests_dir, config.validate_request(cfg)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_the_requester_files_a_request_with_search_only_access(tmp_path):
    requests_dir, cfg = requester_tree(tmp_path)
    try:
        with pytest.raises(PermissionError):
            os.listdir(requests_dir)              # it may not read the directory
        with pytest.raises(fsafe.UnsafePath):     # the read-mode walk the defect used
            fsafe.open_dir(str(requests_dir), private=False)
        assert controller.file_request(cfg, ist("17:30:00"), lambda: BOOT) == 0
    finally:
        requests_dir.chmod(0o700)
    [name] = os.listdir(requests_dir)
    assert name.startswith("req-") and (requests_dir / name).stat().st_mode & 0o777 == 0o600


def test_a_search_only_walk_still_refuses_links_and_creates_nothing(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(fsafe.UnsafePath):
        fsafe.open_dir(str(tmp_path / "link"), private=False, search_only=True)
    with pytest.raises(fsafe.UnsafePath, match="never creates"):
        fsafe.open_dir(str(tmp_path / "new"), create=True, search_only=True)
    with pytest.raises(FileNotFoundError):
        fsafe.open_dir(str(tmp_path / "absent"), private=False, search_only=True)
    held = fsafe.open_dir(str(real), owner=os.geteuid(), private=False, search_only=True)
    held.close()


def test_the_request_directories_grant_no_read_access():
    lines = [x.split() for x in (POWER / "systemd/quantdesk-power.tmpfiles.conf").read_text()
             .splitlines() if x.startswith("d ")]
    assert lines == [["d", "/run/quantdesk-power", "0711", "root", "root", "-"],
                     ["d", "/run/quantdesk-power/requests", "1730", "root", "quantdesk-power",
                      "-"]]


@pytest.mark.skipif(not LINUX_ROOT, reason="UNVERIFIED here: needs Linux and root to run the "
                                            "requester as a second real account")
def test_the_real_requester_entry_point_runs_as_an_unprivileged_account(tmp_path):
    import pwd
    nobody = pwd.getpwnam("nobody")
    run = tmp_path / "run"
    (run / "requests").mkdir(parents=True)
    os.chown(run / "requests", 0, nobody.pw_gid)
    (run / "requests").chmod(0o1730)
    run.chmod(0o711)
    tmp_path.chmod(0o711)
    cfg = {"schema": config.REQUEST_SCHEMA, "request_dir": str(run / "requests"),
           "window": {"weekdays": [0, 1, 2, 3, 4, 5, 6], "first_attempt_ist": "00:00",
                      "last_attempt_ist": "23:59"}}
    path = tmp_path / "request.json"
    path.write_text(json.dumps(cfg))
    path.chmod(0o644)
    secret = tmp_path / "pgpass"
    secret.write_text("x")
    secret.chmod(0o600)

    def demote():
        os.setgroups([])
        os.setgid(nobody.pw_gid)
        os.setuid(nobody.pw_uid)
    out = subprocess.run([sys.executable, "-I", str(POWER / "power.py"), "request", "--config",
                          str(path)], capture_output=True, text=True, timeout=60,
                         preexec_fn=demote)
    assert out.returncode == 0 and "filed" in out.stdout, out.stderr
    assert len(os.listdir(run / "requests")) == 1
    peek = subprocess.run([sys.executable, "-c", "import os,sys\ntry:\n os.listdir(sys.argv[1]);"
                           "print('listed')\nexcept PermissionError: print('denied')\n"
                           "try:\n open(sys.argv[2]).read(); print('read')\n"
                           "except PermissionError: print('denied')",
                           str(run / "requests"), str(secret)], capture_output=True, text=True,
                          timeout=60, preexec_fn=demote)
    assert peek.stdout.split() == ["denied", "denied"]


# -- A2. durable workload recovery -----------------------------------------------------------------
def progress(w):
    state = controller.open_state(w.cfg)
    try:
        log = journal.ProgressLog(state, owner=os.geteuid())
        return log.load(w.txns()[0].txn_id)
    finally:
        state.close()


def settled_then_new_boot(tmp_path, **kw):
    w = World(tmp_path, arm=FakeArm(follow=("InProgress", "budget"),
                                    recovery_follow=("Succeeded", "done")), **kw)
    w.request()
    w.control()
    assert not w.all_active()
    w.boots.insert(0, BOOT2)
    return w


def test_azure_success_and_workload_recovery_are_separate_and_journaled_first(tmp_path,
                                                                              monkeypatch):
    w = settled_then_new_boot(tmp_path)
    real = FakeSystemd.start
    calls = {"n": 0}

    def start(self, unit, timeout):
        calls["n"] += 1
        if calls["n"] == 2:
            raise Crash("killed while resuming")
        return real(self, unit, timeout)
    monkeypatch.setattr(FakeSystemd, "start", start)
    with pytest.raises(Crash):
        w.reconcile()
    monkeypatch.setattr(FakeSystemd, "start", real)
    states = [r["state"] for r in w.txns()[0].records]
    assert states[-2:] == ["POLL_SUCCEEDED", "RESUMING"]      # recorded before any start
    assert [(r["unit"], r["outcome"]) for r in progress(w)] == [(UNITS["postgres"], "started")]
    w.boots.insert(0, "2b3c4d5e-6f70-4a8b-9c0d-1e2f3a4b5c6d")   # and a reboot on top
    assert w.reconcile() == []
    states = [r["state"] for r in w.txns()[0].records]
    assert states[-3:] == ["RESUMING", "RESUMING", "RESUMED"]
    attempts = {r["attempt"] for r in progress(w)}
    assert len(attempts) == 2
    verified = [r["unit"] for r in progress(w) if r["outcome"] == "verified"]
    assert set(UNITS.values()) <= set(verified) and w.all_active()
    assert len(w.arm.deallocations) == 1                      # never resent


def test_a_failed_resumption_is_recorded_retried_and_never_resends(tmp_path):
    w = settled_then_new_boot(tmp_path, systemd=FakeSystemd(start_fail={UNITS["api"]}))
    assert [t.state for t in w.reconcile()] == ["RESUME_FAILED"]
    [txn] = w.txns()
    assert any(UNITS["api"] in p for p in txn.data("RESUME_FAILED")["problems"])
    assert (UNITS["api"], "failed") in [(r["unit"], r["outcome"]) for r in progress(w)]
    assert gate.decide(w.cfg, lambda: BOOT2)[0] is True        # the retry may start units
    assert status.summarize(w.txns(), BOOT2)[3] == "RESUME_FAILED"
    w.reconcile()
    assert w.txns()[0].state == "RESUME_FAILED"                # still failing, retried
    w.systemd.start_fail.clear()
    assert w.reconcile() == [] and w.txns()[0].state == "RESUMED" and w.all_active()
    assert len(w.arm.deallocations) == 1


def test_a_unit_that_starts_but_does_not_run_is_not_recovered(tmp_path):
    systemd = FakeSystemd()
    real = systemd.start

    def start(unit, timeout):
        real(unit, timeout)
        if unit == UNITS["api"]:
            systemd.state[unit] = "failed"                     # it exited straight away
    systemd.start = start
    w = settled_then_new_boot(tmp_path, systemd=systemd)
    w.reconcile()
    assert w.txns()[0].state == "RESUME_FAILED"
    assert (UNITS["api"], "not_running") in [(r["unit"], r["outcome"]) for r in progress(w)]


def test_the_api_must_be_its_own_single_process_to_count_as_recovered(tmp_path):
    w = settled_then_new_boot(tmp_path)
    w.runner.extra_api = True                    # a second API process appears in this boot
    w.reconcile()
    assert w.txns()[0].state == "RESUME_FAILED"
    [api] = [r for r in progress(w) if r["unit"] == UNITS["api"] and r["outcome"] ==
             "not_running"]
    assert "not exactly" in api["detail"]


def test_a_completed_deallocation_waits_for_the_next_boot_to_resume(tmp_path):
    w = World(tmp_path, arm=FakeArm(Submission(azure.COMPLETED, 200, "done")))
    w.request()
    w.control()
    before = list(w.systemd.calls)
    assert [t.state for t in w.reconcile()] == ["COMPLETED"]    # same boot: going down
    assert w.systemd.calls == before
    assert status.summarize(w.txns(), BOOT)[3] == "OK"
    w.boots.insert(0, BOOT2)
    for unit in UNITS.values():                                # the boot started them
        w.systemd.state[unit] = "active"
    assert w.reconcile() == []
    assert w.txns()[0].state == "RESUMED"
    assert {r["outcome"] for r in progress(w) if r["unit"] in UNITS.values()} == \
        {"already_active", "verified"}


def test_restoration_keeps_per_unit_progress_and_a_crash_resumes_from_real_state(tmp_path,
                                                                                monkeypatch):
    w = World(tmp_path, ev=Evidence("CLEAR", "BLOCKED"))
    real = FakeSystemd.start
    calls = {"n": 0}

    def start(self, unit, timeout):
        calls["n"] += 1
        if calls["n"] == 2:
            raise Crash("killed while restoring")
        return real(self, unit, timeout)
    monkeypatch.setattr(FakeSystemd, "start", start)
    w.request()
    with pytest.raises(Crash):
        w.control()
    monkeypatch.setattr(FakeSystemd, "start", real)
    assert w.txns()[0].state == "RESTORING"
    assert [(r["unit"], r["outcome"], r["phase"]) for r in progress(w)] == [
        (UNITS["postgres"], "already_active", "RESTORING"),
        (UNITS["redis"], "already_active", "RESTORING"),
        (UNITS["api"], "started", "RESTORING")]         # the dashboard's start was cut off
    assert w.reconcile() == [] and restored(w)
    assert {r["unit"] for r in progress(w) if r["outcome"] == "verified"} >= set(UNITS.values())


@pytest.mark.parametrize("damage", ["edit", "delete", "extra"])
def test_damaged_progress_is_refused(tmp_path, damage):
    w = World(tmp_path, ev=Evidence("BLOCKED"))
    w.request()
    w.control()
    directory = Path(w.cfg["state_root"]) / "progress" / w.txns()[0].txn_id
    names = sorted(os.listdir(directory))
    if damage == "edit":
        (directory / names[0]).chmod(0o600)
        (directory / names[0]).write_text((directory / names[0]).read_text().replace(
            BOOT, BOOT2))
    elif damage == "delete":
        (directory / names[0]).unlink()
    else:
        (directory / "99999.json").write_text("{}")
    with pytest.raises(journal.CorruptJournal):
        progress(w)


def test_progress_is_only_recorded_inside_a_restoration_or_resumption(tmp_path):
    w = World(tmp_path, ev=Evidence("BLOCKED"))
    w.request()
    w.control()
    [txn] = w.txns()
    state = controller.open_state(w.cfg)
    try:
        with pytest.raises(journal.CorruptJournal):
            journal.ProgressLog(state, os.geteuid()).append(txn, UNITS["api"], "started", "",
                                                            BOOT)
        txn.records[-1] = {**txn.records[-1], "state": "RESTORING"}
        with pytest.raises(journal.CorruptJournal):
            journal.ProgressLog(state, os.geteuid()).append(txn, UNITS["api"], "rebooted", "",
                                                            BOOT)
    finally:
        state.close()


def test_resumption_is_refused_in_the_boot_that_asked(tmp_path):
    w = World(tmp_path, arm=FakeArm(Submission(azure.COMPLETED, 200, "done")))
    w.request()
    w.control()
    with pytest.raises(journal.CorruptJournal, match="later boot"):
        controller.resume(w.env, w.journal(), w.txns()[0], "test")


def test_a_failed_resumption_can_be_closed_by_an_operator(tmp_path):
    w = settled_then_new_boot(tmp_path, systemd=FakeSystemd(start_fail={UNITS["web"]}))
    w.reconcile()
    [txn] = w.txns()
    assert recovery.resolve(w.env, w.journal(), txn.txn_id, "restoration-repaired", "ticket 7",
                            "operator") == "OPERATOR_CLOSED"
    assert w.txns()[0].terminal


# -- A3. status bound to its operating cycle (controller side) -------------------------------
def test_the_controller_signs_the_cycle_it_publishes_in(tmp_path):
    w = World(tmp_path, now="17:30:00")
    key = provision_key(w)
    w.request()
    w.control()
    fields = status.decode(status_records(w)[-1], key)
    assert fields["cycle"] == "202610270730"
    record = json.loads((POWER / "config/special_session.example.json").read_text())
    (Path(w.cfg["holds_dir"]) / "special.json").write_text(json.dumps(
        special.plan(record)["power_hold"]))
    w.now = ist("17:40:00", "2026-11-08")
    controller.publish_status(w.env, w.journal())
    assert status.decode(status_records(w)[-1], key)["cycle"] == "202611081700"
    (Path(w.cfg["holds_dir"]) / "broken.json").write_text("{")
    controller.publish_status(w.env, w.journal())
    assert status.decode(status_records(w)[-1], key)["cycle"] == "-"   # no exemption assumed
    directory = Path(w.cfg["state_root"]) / "status"
    first = sorted(os.listdir(directory))[0]
    assert (directory / first).stat().st_mode & 0o777 == 0o400          # history is kept
    with directory.joinpath("x").open("w"):
        pass
    assert not fsafe.publish(fsafe.open_dir(str(directory)), first, b"rewritten")


# -- B. writer exclusion: what the session's ending does and does not show -----------------------
def test_an_individual_57p01_with_a_commit_behind_it_is_caught_by_the_wal(tmp_path):
    cluster = Cluster()
    w = World(tmp_path, db=ClusterDB(cluster))
    w.runner.checkpoint = cluster.lsn
    real_stop = w.systemd.stop

    def stop(unit, timeout):
        if unit == UNITS["postgres"]:
            # Our session alone is terminated (pg_terminate_backend: 57P01 as well); the key
            # is gone, a writer commits, and only then does the shutdown begin.
            cluster.individually_terminated = True
            cluster.lose_controller_session()
            assert cluster.writer_attempt("angel_backfill")
            cluster.running = False
        real_stop(unit, timeout)
    w.systemd.stop = stop
    w.request()
    w.control()
    [txn] = w.txns()
    assert txn.data("RESTORING")["reason"].startswith("WAL was written after the writer key")
    assert "SUBMITTING" not in w.states() and restored(w)


@pytest.mark.parametrize("checkpoint", ["0/16B3A70", None, "garbage"])
def test_any_other_wal_or_an_unlocatable_checkpoint_abandons_the_shutdown(tmp_path, checkpoint):
    w = World(tmp_path)
    if checkpoint is None:
        w.runner.checkpoint = lambda: ""
    else:
        w.runner.checkpoint = lambda: checkpoint
    w.request()
    w.control()
    assert "SUBMITTING" not in w.states() and restored(w)


def test_the_competing_writer_matrix_still_reaches_the_request_when_nothing_wrote(tmp_path):
    cluster = Cluster()
    w = World(tmp_path, db=ClusterDB(cluster))
    w.runner.checkpoint = cluster.lsn
    real_stop = w.systemd.stop

    def stop(unit, timeout):
        if unit == UNITS["postgres"]:
            cluster.running = False
        real_stop(unit, timeout)
    w.systemd.stop = stop
    w.request()
    w.control()
    assert "SUBMITTING" in w.states() and cluster.writes == []


def test_no_claim_that_57p01_closes_the_cluster(tmp_path):
    w = World(tmp_path)
    w.request()
    w.control()
    [txn] = w.txns()
    data = txn.data("POSTGRES_STOPPED")
    assert data["session_ending"] == "57P01 (this session only)"
    assert data["wal_at_last_proof"] == WAL_AT_PROOF
    for path in [*source_files(POWER / "quantdesk_power"), POWER / "README.md"]:
        assert "held until the cluster's own shutdown" not in path.read_text(), path
    readme = (POWER / "README.md").read_text()
    assert "WRITER EXCLUSION: BLOCKED" in readme


@pytest.mark.parametrize("text, value", [("0/16B3A28", 0x16B3A28), ("1/0", 1 << 32),
                                         ("a/ff", (0xA << 32) | 0xFF)])
def test_wal_positions_are_read_exactly(text, value):
    assert db_module.lsn_value(text) == value


@pytest.mark.parametrize("text", ["", "16B3A28", "0/xyz", "0/16B3A28/1", None, "-1/0"])
def test_malformed_wal_positions_are_refused(text):
    with pytest.raises(ValueError):
        db_module.lsn_value(text)


def test_a_malformed_wal_position_from_the_session_stops_the_shutdown(tmp_path):
    session, stopped = psql_session(tmp_path, lsn="not-a-position")
    try:
        assert session.take_writer_key()
        with pytest.raises(DatabaseError, match="not a WAL position"):
            session.hold_through_shutdown(180)
    finally:
        stopped.touch()
        session.close(kill=True)


def test_the_key_is_proved_again_after_the_wal_position_is_read(tmp_path):
    # The key held when checked, gone by the time the WAL position has been read: that
    # position no longer marks the end of exclusion, so it is refused.
    session, stopped = psql_session(tmp_path, verify_sequence=["4242,1", "4242,0"])
    try:
        assert session.take_writer_key()
        with pytest.raises(DatabaseError, match="no longer held"):
            session.hold_through_shutdown(180)
        assert session.wal_lsn is None
    finally:
        stopped.touch()
        session.close(kill=True)


# == pass 22 corrections ===========================================================================
# -- A1. recovery readiness: bounded and condition-based, never a fixed settle --------------------
def drained_then_blocked(tmp_path, **kw):
    """The API and the dashboard are stopped, then the drained evidence blocks: restoration
    starts them again."""
    return World(tmp_path, ev=Evidence("CLEAR", "BLOCKED"), **kw)


def api_after_start(w, change):
    """Readings of the API after its restart go through `change(n, props)` (n counts them)."""
    real_show, real_start = w.systemd.show, w.systemd.start
    seen = {"started": False, "n": 0}

    def start(unit, timeout):
        real_start(unit, timeout)
        if unit == UNITS["api"]:
            seen["started"] = True

    def show(unit, timeout=None):
        props = dict(real_show(unit, timeout))
        if seen["started"] and unit == UNITS["api"]:
            seen["n"] += 1
            return change(seen["n"], props)
        return props
    w.systemd.start, w.systemd.show = start, show
    return seen


def last_progress(w, unit):
    return [r for r in progress(w) if r["unit"] == unit][-1]


def test_readiness_is_waited_for_by_condition_not_by_a_fixed_sleep(tmp_path):
    w = drained_then_blocked(tmp_path)
    api_after_start(w, lambda n, p: {**p, "ActiveState": "activating"} if n <= 8 else p)
    w.request()
    w.control()
    assert w.txns()[0].state == "RESTORED" and restored(w)
    assert all(s <= controller.READY_POLL_SECONDS for s in w.slept)     # short readings
    assert controller.READY_STABLE_SECONDS <= sum(w.slept) < \
        w.cfg["timeouts"]["start_unit"]                                     # not the deadline
    assert "ready and unchanged" in last_progress(w, UNITS["api"])["detail"]


def test_a_unit_that_never_becomes_ready_is_a_bounded_recorded_failure_that_is_retried(tmp_path):
    w = drained_then_blocked(tmp_path)
    key = provision_key(w)
    api_after_start(w, lambda n, p: {**p, "ActiveState": "activating", "MainPID": "0"})
    w.request()
    assert w.control() == 1                                       # surfaced: exit status
    limit = w.cfg["timeouts"]["start_unit"] + controller.READY_STABLE_SECONDS
    assert limit <= sum(w.slept) <= limit + controller.READY_POLL_SECONDS
    [txn] = w.txns()
    assert txn.state == "RESTORE_FAILED"                          # durable failed state
    last = last_progress(w, UNITS["api"])
    assert last["outcome"] == "not_running" and "not ready within" in last["detail"]
    assert status.decode(status_records(w)[-1], key)["flag"] == "RESTORE_FAILED"   # alerts
    w.systemd.start = FakeSystemd.start.__get__(w.systemd)        # the unit recovers
    w.systemd.show = FakeSystemd.show.__get__(w.systemd)
    assert w.reconcile() == [] and w.txns()[0].state == "RESTORED" and restored(w)
    assert len(w.arm.deallocations) == 0


def test_a_restart_loop_is_never_confirmed_as_recovered(tmp_path):
    w = drained_then_blocked(tmp_path)
    # Correction (pass 23): one reading of the API now shows the unit twice (systemd, then
    # the process check) and requires both to agree; a restart is modelled per reading.
    api_after_start(w, lambda n, p: {**p, "ExecMainStartTimestampMonotonic":
                                     str(MONO + (n + 1) // 2)})
    w.request()
    w.control()
    assert w.txns()[0].state == "RESTORE_FAILED"
    last = last_progress(w, UNITS["api"])
    assert last["outcome"] == "unconfirmed" and "restarted while being verified" in last["detail"]


def test_a_unit_whose_state_cannot_be_read_is_not_recovered(tmp_path):
    w = drained_then_blocked(tmp_path)

    def unreadable(n, props):
        raise CommandFailed("systemctl show quantdesk-api.service timed out")
    api_after_start(w, unreadable)
    w.request()
    w.control()
    assert w.txns()[0].state == "RESTORE_FAILED"
    last = last_progress(w, UNITS["api"])
    assert last["outcome"] == "not_running" and "cannot be read" in last["detail"]


def test_a_resumption_that_never_becomes_ready_fails_durably_and_never_resends(tmp_path):
    w = settled_then_new_boot(tmp_path)
    api_after_start(w, lambda n, p: {**p, "ActiveState": "failed", "MainPID": "0"})
    w.reconcile()
    assert w.txns()[0].state == "RESUME_FAILED"
    assert last_progress(w, UNITS["api"])["outcome"] == "not_running"
    w.systemd.start = FakeSystemd.start.__get__(w.systemd)
    w.systemd.show = FakeSystemd.show.__get__(w.systemd)
    assert w.reconcile() == [] and w.txns()[0].state == "RESUMED"
    assert len(w.arm.deallocations) == 1


def test_the_real_environment_waits_with_real_time():
    env = controller.Env(cfg={}, runner=None, systemd=None, clock=None, boot_id=None,
                         uid_of=None, open_db=None, arm=None, observer_lock=None)
    assert env.sleep is controller.time.sleep and env.monotonic is controller.time.monotonic
    assert "SETTLE_SECONDS" not in vars(controller)


# -- A2. RESTORING / RESUMING must be durable and validated before anything starts -----------------
@pytest.mark.parametrize("failure", ["write", "readback", "invalid"])
def test_restoration_is_withheld_unless_restoring_is_written_and_read_back(tmp_path, monkeypatch,
                                                                          failure):
    w = World(tmp_path, ev=Evidence("BLOCKED"))
    real_append, real_load = journal.Journal.append, journal.Journal.load

    def append(self, txn, state, boot_id, data, clock=journal.now_utc):
        if state == "RESTORING" and failure == "write":
            raise OSError(5, "Input/output error")
        if state == "RESTORING" and failure == "invalid":
            raise journal.CorruptJournal("FROZEN -> RESTORING could not be validated")
        return real_append(self, txn, state, boot_id, data, clock)

    def load(self, txn_id):
        txn = real_load(self, txn_id)
        if failure == "readback" and txn.state == "RESTORING":
            txn.records[-1] = {**txn.records[-1], "data": {}}       # not what was written
        return txn
    monkeypatch.setattr(journal.Journal, "append", append)
    monkeypatch.setattr(journal.Journal, "load", load)
    w.request()
    calls = len(w.systemd.calls)
    with pytest.raises(controller.RestorationWithheld, match="nothing started"):
        w.control()
    assert not [c for c in w.systemd.calls[calls:] if c[0] == "start"]
    monkeypatch.setattr(journal.Journal, "append", real_append)
    monkeypatch.setattr(journal.Journal, "load", real_load)
    on_disk = w.states()
    # The journal keeps exactly what it had (a record read back wrong is not repaired).
    assert on_disk[-1] == ("RESTORING" if failure == "readback" else "FROZEN")
    assert not progress(w)
    assert w.reconcile() == [] and w.txns()[0].state == "RESTORED" and restored(w)


def test_a_crash_between_writing_and_validating_restoring_starts_nothing(tmp_path, monkeypatch):
    w = World(tmp_path, ev=Evidence("BLOCKED"))
    real = journal.Journal.append_durably

    def append_durably(self, txn, state, boot_id, data, clock=journal.now_utc):
        self.append(txn, state, boot_id, data, clock)
        raise Crash("killed before the read-back")
    monkeypatch.setattr(journal.Journal, "append_durably", append_durably)
    w.request()
    calls = len(w.systemd.calls)
    with pytest.raises(Crash):
        w.control()
    assert not [c for c in w.systemd.calls[calls:] if c[0] == "start"]
    monkeypatch.setattr(journal.Journal, "append_durably", real)
    assert w.states()[-1] == "RESTORING"          # written, never validated, nothing started
    assert w.reconcile() == [] and restored(w)


def test_resumption_is_withheld_unless_resuming_is_durable(tmp_path, monkeypatch):
    w = settled_then_new_boot(tmp_path)
    real = journal.Journal.append

    def append(self, txn, state, boot_id, data, clock=journal.now_utc):
        if state == "RESUMING":
            raise OSError(28, "No space left on device")
        return real(self, txn, state, boot_id, data, clock)
    monkeypatch.setattr(journal.Journal, "append", append)
    calls = len(w.systemd.calls)
    with pytest.raises(controller.RestorationWithheld, match="resumption withheld"):
        w.reconcile()
    assert not [c for c in w.systemd.calls[calls:] if c[0] == "start"]
    assert w.txns()[0].state == "POLL_SUCCEEDED"
    monkeypatch.setattr(journal.Journal, "append", real)
    assert w.reconcile() == [] and w.txns()[0].state == "RESUMED"
    assert len(w.arm.deallocations) == 1                          # never resent


@pytest.mark.parametrize("state", ["SUBMITTING", "ACCEPTED", "UNKNOWN", "UNKNOWN_RETAINED",
                                   "OBSERVED_NEW_BOOT"])
def test_nothing_is_ever_started_across_an_unresolved_azure_outcome(tmp_path, state):
    w = World(tmp_path, arm=FakeArm(follow=("InProgress", "budget")))
    w.request()
    w.control()
    [txn] = w.txns()
    txn.records[-1] = {**txn.records[-1], "state": state}
    calls = len(w.systemd.calls)
    with pytest.raises(journal.CorruptJournal, match="not permitted"):
        controller.restore(w.env, w.journal(), txn, "test")
    assert w.systemd.calls[calls:] == []


# -- A3. the SYSTEM_CLEAR recheck of the API's identity --------------------------------------------
API_FAULTS = {
    "pid_changes": ({"MainPID": "951"}, "951"),
    "start_changes": ({"ExecMainStartTimestampMonotonic": str(MONO + 1)}, None),
    "pid_missing": ({"MainPID": "0"}, None),
    "start_missing": ({"ExecMainStartTimestampMonotonic": None}, None),
    "disappears": ({"ActiveState": "inactive", "MainPID": "0"}, ""),
    "restarts": ({"MainPID": "951", "ExecMainStartTimestampMonotonic": str(MONO + 7)}, "951"),
    "show_fails": ("raise", None),
    "ps_fails": ({}, "fail"),
    "second_process": ({}, "943+951"),
}


def api_changes_after_first_evidence(w, monkeypatch, fault):
    props_change, table = API_FAULTS[fault] if fault != "unchanged" else ({}, None)
    on = {"value": False}
    real_show, real_run, real_ev = w.systemd.show, w.runner.run, w.env.obtain_evidence

    def show(unit, timeout=None):
        props = dict(real_show(unit, timeout))
        if on["value"] and unit == UNITS["api"]:
            if props_change == "raise":
                raise CommandFailed("systemctl show quantdesk-api.service timed out")
            props.update(props_change)
            return {k: v for k, v in props.items() if v is not None}
        return props

    def run(argv, timeout, **kw):
        if on["value"] and os.path.basename(argv[0]) == "ps" and table is not None:
            if table == "fail":
                return Result(argv, 1, "", "ps: cannot read /proc")
            rows = ["1 /sbin/init"] + [f"{pid} /venv/bin/uvicorn app.main:app --port 8000"
                                       for pid in table.split("+") if pid]
            return Result(argv, 0, "\n".join(rows) + "\n", "")
        return real_run(argv, timeout, **kw)

    def obtain(*args, **kwargs):
        record = real_ev(*args, **kwargs)
        on["value"] = True                      # between the evidence and the recheck
        return record
    real_append = journal.Journal.append_durably

    def append_durably(self, txn, state, *a, **k):
        if state == "RESTORING":
            on["value"] = False                 # the API is itself again for restoration
        return real_append(self, txn, state, *a, **k)
    w.systemd.show, w.runner.run, w.env.obtain_evidence = show, run, obtain
    monkeypatch.setattr(journal.Journal, "append_durably", append_durably)


@pytest.mark.parametrize("fault", sorted(API_FAULTS))
def test_any_change_or_doubt_in_the_apis_identity_at_the_recheck_stops_before_the_drain(
        tmp_path, monkeypatch, fault):
    w = World(tmp_path)
    api_changes_after_first_evidence(w, monkeypatch, fault)
    w.request()
    w.control()
    states = w.states()
    assert "SYSTEM_CLEAR" not in states and states[:3] == FORWARD[:3]
    assert states[3:] == ["RESTORING", "RESTORED"] and restored(w)
    assert "a fact changed" in w.txns()[0].data("RESTORING")["reason"]
    stops = [u for op, u in w.systemd.calls if op == "stop"]
    assert UNITS["api"] not in stops and UNITS["web"] not in stops
    assert w.arm.deallocations == [] and w.db.calls.count("key") == 0


def test_an_unchanged_api_identity_passes_the_recheck(tmp_path, monkeypatch):
    w = World(tmp_path)
    api_changes_after_first_evidence(w, monkeypatch, "unchanged")
    w.request()
    w.control()
    assert "SYSTEM_CLEAR" in w.states() and len(w.arm.deallocations) == 1


# -- A5. holds and missing sources -----------------------------------------------------------------
def test_a_missing_hold_directory_is_unknown_and_never_authorizes_a_shutdown(tmp_path):
    w = World(tmp_path)
    key = provision_key(w)
    Path(w.cfg["holds_dir"]).rmdir()
    assert facts.read_holds(w.cfg, w.now)[1].status == facts.UNKNOWN
    w.request()
    w.control()
    assert w.states() == ["ACCEPTED_REQUEST", "ABORTED"] and w.arm.deallocations == []
    assert status.decode(status_records(w)[-1], key)["cycle"] == "-"   # the monitor alerts


def test_a_hold_never_delays_or_suppresses_recovery(tmp_path):
    w = settled_then_new_boot(tmp_path)
    hold = json.loads((POWER / "config/power_hold.example.json").read_text())
    hold.update(from_ist="2026-10-27T00:00:00+05:30", to_ist="2026-10-29T00:00:00+05:30")
    (Path(w.cfg["holds_dir"]) / "keep.json").write_text(json.dumps(hold))
    assert facts.read_holds(w.cfg, w.now)[1].status == facts.BLOCKED
    assert w.reconcile() == [] and w.txns()[0].state == "RESUMED"


def test_a_withheld_restoration_is_published_as_in_progress(tmp_path, monkeypatch):
    w = World(tmp_path, ev=Evidence("BLOCKED"))
    key = provision_key(w)
    real = journal.Journal.append

    def append(self, txn, state, boot_id, data, clock=journal.now_utc):
        if state == "RESTORING":
            raise OSError(28, "No space left on device")
        return real(self, txn, state, boot_id, data, clock)
    monkeypatch.setattr(journal.Journal, "append", append)
    w.request()
    with pytest.raises(controller.RestorationWithheld):
        w.control()
    assert status.decode(status_records(w)[-1], key)["flag"] == "IN_PROGRESS"


# == pass 23 corrections ===========================================================================
# -- 1. workload process identity: valid, present, stable - or not ready --------------------------
SERVICES = [UNITS["postgres"], UNITS["redis"], UNITS["api"], UNITS["web"]]


def readings_of(w, unit, change):
    """Every systemd reading of `unit` goes through `change(n, props)` (n counts them)."""
    real = w.systemd.show
    seen = {"n": 0}

    def show(name, timeout=None):
        props = dict(real(name, timeout))
        if name == unit:
            seen["n"] += 1
            return change(seen["n"], props)
        return props
    w.systemd.show = show
    return seen


def without(props, key):
    return {k: v for k, v in props.items() if k != key}


IDENTITY_FAULTS = {
    "pid_missing": lambda n, p: without(p, "MainPID"),
    "pid_zero": lambda n, p: {**p, "MainPID": "0"},
    "pid_empty": lambda n, p: {**p, "MainPID": ""},
    "pid_text": lambda n, p: {**p, "MainPID": "abc"},
    "pid_negative": lambda n, p: {**p, "MainPID": "-901"},
    "pid_signed": lambda n, p: {**p, "MainPID": "+901"},
    "pid_padded": lambda n, p: {**p, "MainPID": " 901"},
    "pid_leading_zero": lambda n, p: {**p, "MainPID": "0901"},
    "pid_too_large": lambda n, p: {**p, "MainPID": "4194305"},
    "pid_wrong_type": lambda n, p: {**p, "MainPID": 901},
    "start_missing": lambda n, p: without(p, "ExecMainStartTimestampMonotonic"),
    "start_zero": lambda n, p: {**p, "ExecMainStartTimestampMonotonic": "0"},
    "start_text": lambda n, p: {**p, "ExecMainStartTimestampMonotonic": "n/a"},
    "start_negative": lambda n, p: {**p, "ExecMainStartTimestampMonotonic": "-1"},
    "start_wrong_type": lambda n, p: {**p, "ExecMainStartTimestampMonotonic": MONO},
    "substate_exited": lambda n, p: {**p, "SubState": "exited"},
    "substate_auto_restart": lambda n, p: {**p, "SubState": "auto-restart"},
    "substate_missing": lambda n, p: without(p, "SubState"),
    "activating": lambda n, p: {**p, "ActiveState": "activating", "SubState": "start"},
}


@pytest.mark.parametrize("fault", sorted(IDENTITY_FAULTS))
@pytest.mark.parametrize("unit", SERVICES)
def test_missing_malformed_or_zero_service_identity_is_never_ready(tmp_path, unit, fault):
    w = World(tmp_path)
    readings_of(w, unit, IDENTITY_FAULTS[fault])
    result = controller.await_ready(w.env, SERVICES)
    outcome, detail = result[unit]
    # A fault that never clears is not ready at the deadline; repeated missing values are
    # never mistaken for a stable identity.
    assert outcome == "not_running", (fault, detail)
    assert "not ready within" in detail
    limit = w.cfg["timeouts"]["start_unit"] + controller.READY_STABLE_SECONDS
    assert limit <= sum(w.slept) <= limit + controller.READY_POLL_SECONDS


@pytest.mark.parametrize("change", ["pid", "start", "both"])
@pytest.mark.parametrize("unit", SERVICES)
def test_a_changing_service_identity_is_never_confirmed(tmp_path, unit, change):
    w = World(tmp_path)
    per_reading = 2 if unit == UNITS["api"] else 1     # the API is shown twice per reading

    def restart(n, props):
        k = (n + per_reading - 1) // per_reading
        if change in ("pid", "both"):
            props["MainPID"] = str(int(props["MainPID"]) + k) if unit != UNITS["api"] \
                else props["MainPID"]
        if change in ("start", "both") or unit == UNITS["api"]:
            props["ExecMainStartTimestampMonotonic"] = str(MONO + k)
        return props
    readings_of(w, unit, restart)
    outcome, detail = controller.await_ready(w.env, SERVICES)[unit]
    assert outcome == "unconfirmed" and "restarted while being verified" in detail


@pytest.mark.parametrize("unit", SERVICES)
def test_an_unreadable_service_is_never_ready(tmp_path, unit):
    w = World(tmp_path)

    def fail(n, props):
        raise CommandFailed(f"systemctl show {unit} timed out")
    readings_of(w, unit, fail)
    outcome, detail = controller.await_ready(w.env, SERVICES)[unit]
    assert outcome == "not_running" and "cannot be read" in detail


def test_an_api_whose_identity_disagrees_within_one_reading_is_not_ready(tmp_path):
    w = World(tmp_path)
    readings_of(w, UNITS["api"], lambda n, p: {**p, "ExecMainStartTimestampMonotonic":
                                               str(MONO + n % 2)})
    outcome, detail = controller.await_ready(w.env, [UNITS["api"]])[UNITS["api"]]
    assert outcome == "not_running" and "inconsistent within one reading" in detail


def test_an_api_whose_process_is_not_systemds_main_pid_is_not_ready(tmp_path):
    w = World(tmp_path)
    readings_of(w, UNITS["api"], lambda n, p: {**p, "MainPID": "951"})   # ps still lists 943
    outcome, detail = controller.await_ready(w.env, [UNITS["api"]])[UNITS["api"]]
    assert outcome == "not_running" and "not exactly" in detail


def test_valid_stable_service_identities_are_verified(tmp_path):
    w = World(tmp_path)
    result = controller.await_ready(w.env, SERVICES)
    assert {u: o for u, (o, _) in result.items()} == {u: "verified" for u in SERVICES}
    assert controller.READY_STABLE_SECONDS <= sum(w.slept) < 60
    assert controller._reading(w.env, UNITS["postgres"]) == (
        True, "active and running as MainPID 901", (901, MONO))


@pytest.mark.parametrize("variant", ["pid_zero", "pid_missing", "no_start", "running"])
def test_an_active_timer_needs_no_process_identity(tmp_path, variant):
    w = World(tmp_path)
    timer = TIMERS[0]
    change = {"pid_zero": lambda n, p: p,
              "pid_missing": lambda n, p: without(p, "MainPID"),
              "no_start": lambda n, p: without(p, "ExecMainStartTimestampMonotonic"),
              "running": lambda n, p: {**p, "SubState": "running"}}[variant]
    readings_of(w, timer, change)
    assert controller.await_ready(w.env, [timer])[timer][0] == "verified"


@pytest.mark.parametrize("state", ["inactive", "failed", "unreadable"])
def test_a_timer_that_is_not_active_or_unreadable_is_not_ready(tmp_path, state):
    w = World(tmp_path)
    timer = TIMERS[0]

    def change(n, props):
        if state == "unreadable":
            raise CommandFailed("systemctl show failed")
        return {**props, "ActiveState": state, "SubState": "dead"}
    readings_of(w, timer, change)
    assert controller.await_ready(w.env, [timer])[timer][0] == "not_running"


@pytest.mark.parametrize("unit", [UNITS["postgres"], UNITS["web"]])
def test_restoration_fails_when_any_service_lacks_a_valid_identity(tmp_path, unit):
    w = drained_then_blocked(tmp_path)
    readings_of(w, unit, lambda n, p: {**p, "MainPID": "0"} if n > 3 else p)
    w.request()
    assert w.control() == 1
    assert w.txns()[0].state == "RESTORE_FAILED"
    assert last_progress(w, unit)["outcome"] == "not_running"
    assert len(w.arm.deallocations) == 0


# == pass 24 corrections ===========================================================================
# -- A. one absolute readiness deadline -----------------------------------------------------------
def allowance(w):
    return w.cfg["timeouts"]["start_unit"] + controller.READY_STABLE_SECONDS


def timed(w, unit, cost):
    """Readings of `unit` take `cost(n)` seconds of the fake monotonic clock; every call's
    (clock at the call, timeout given) is recorded."""
    real = w.systemd.show
    calls = []

    def show(name, timeout=None):
        if name == unit:
            calls.append((w.mono, timeout))
            w.mono += cost(len(calls))
        return real(name, timeout)
    w.systemd.show = show
    return calls


def test_one_slow_inspection_cannot_carry_a_reading_past_the_deadline(tmp_path):
    w = World(tmp_path)
    calls = timed(w, UNITS["postgres"], lambda n: 10_000.0)        # it hangs past the bound
    result = controller.await_ready(w.env, SERVICES)
    assert calls == [(0.0, allowance(w))]                          # capped to the budget
    assert result[UNITS["postgres"]] == ("not_running", f"not ready within "
                                         f"{allowance(w):.0f} s: its reading finished at or "
                                         "after the readiness deadline")
    for unit in SERVICES[1:]:                       # nothing is inspected after the deadline
        assert result[unit][0] == "not_running" and "never read" in result[unit][1]
    assert w.slept == []


def test_inspections_whose_total_time_exceeds_the_deadline_fail_and_each_is_capped(tmp_path):
    w = World(tmp_path)
    clocks = []
    for unit in SERVICES:
        clocks.append(timed(w, unit, lambda n: 40.0))
    result = controller.await_ready(w.env, SERVICES)
    budget = allowance(w)
    seen = [c for unit_calls in clocks for c in unit_calls]
    # PostgreSQL, Redis, then the API's two inspections (unit, then process check): the
    # clock is out before the dashboard is ever read.
    assert sorted(t for t, _ in seen) == [0.0, 40.0, 80.0, 120.0]
    assert all(timeout == budget - at for at, timeout in seen)     # never more than is left
    assert all(outcome != "verified" for outcome, _ in result.values())
    assert result[UNITS["api"]] == ("not_running", f"not ready within {budget:.0f} s: its "
                                    "reading finished at or after the readiness deadline")
    assert result[UNITS["web"]] == ("not_running", f"not ready within {budget:.0f} s: it was "
                                    "never read before the readiness deadline")


@pytest.mark.parametrize("finish, verified", [(0.0, False), (-0.001, True)])
def test_a_reading_completed_at_the_deadline_fails_and_just_before_it_passes(tmp_path, finish,
                                                                            verified):
    w = World(tmp_path)
    budget = allowance(w)
    # First reading instant (stability starts at 0); the second ends at deadline + finish.
    timed(w, UNITS["postgres"], lambda n: 0.0 if n == 1 else
          budget - controller.READY_POLL_SECONDS + finish)
    outcome, detail = controller.await_ready(w.env, [UNITS["postgres"]])[UNITS["postgres"]]
    assert (outcome == "verified") is verified, detail
    if not verified:
        assert outcome == "not_running" and "at or after the readiness deadline" in detail


def test_stability_is_timed_from_a_complete_successful_reading(tmp_path):
    w = World(tmp_path)
    timed(w, UNITS["postgres"], lambda n: 20.0 if n == 1 else 0.0)  # a slow first reading
    outcome, detail = controller.await_ready(w.env, [UNITS["postgres"]])[UNITS["postgres"]]
    assert outcome == "verified"
    assert w.mono >= 20.0 + controller.READY_STABLE_SECONDS         # not counted from 0


def test_an_identity_change_during_slow_polling_restarts_the_stable_period(tmp_path):
    w = World(tmp_path)
    readings = {"n": 0}
    timed(w, UNITS["postgres"], lambda n: 3.0)
    real = w.systemd.show

    def show(name, timeout=None):
        props = dict(real(name, timeout))
        if name == UNITS["postgres"]:
            readings["n"] += 1
            if readings["n"] >= 3:                                  # restarted at reading 3
                props["MainPID"], props["ExecMainStartTimestampMonotonic"] = "977", str(MONO + 9)
            if readings["n"] == 3:
                readings["changed_at"] = w.mono
        return props
    w.systemd.show = show
    outcome, detail = controller.await_ready(w.env, [UNITS["postgres"]])[UNITS["postgres"]]
    assert outcome == "verified"
    assert w.mono >= readings["changed_at"] + controller.READY_STABLE_SECONDS


@pytest.mark.parametrize("failure", ["show_timeout", "ps_timeout", "ps_out_of_budget"])
def test_inspection_timeouts_and_errors_are_not_ready(tmp_path, failure):
    w = World(tmp_path)
    budget = allowance(w)
    real_show, real_run = w.systemd.show, w.runner.run
    ps_calls = []

    def show(name, timeout=None):
        if name == UNITS["api"]:
            if failure == "show_timeout":
                raise system_module.CommandTimeout("systemctl show did not finish within 15 s")
            if failure == "ps_out_of_budget" and timeout is not None and timeout > 2:
                w.mono += budget                     # the unit read uses up all the time
        return real_show(name, timeout)

    def run(argv, timeout, **kw):
        if os.path.basename(argv[0]) == "ps":
            ps_calls.append(timeout)
            if failure == "ps_timeout":
                raise system_module.CommandTimeout("ps did not finish within 30 s")
        return real_run(argv, timeout, **kw)
    w.systemd.show, w.runner.run = show, run
    outcome, detail = controller.await_ready(w.env, [UNITS["api"]])[UNITS["api"]]
    assert outcome == "not_running"
    if failure == "ps_out_of_budget":
        assert ps_calls == []                        # never started without time left
    if failure == "ps_timeout":
        assert ps_calls and all(t <= w.cfg["timeouts"]["command"] for t in ps_calls)


class RecordingRunner:
    def __init__(self, out="ActiveState=active\nLoadState=loaded\n"):
        self.out, self.timeouts = out, []

    def run(self, argv, timeout, **kw):
        self.timeouts.append(timeout)
        return Result(argv, 0, self.out, "")


def test_systemd_show_keeps_its_default_and_only_ever_shortens_it():
    runner = RecordingRunner()
    systemd = system_module.Systemd(runner, show_timeout=15.0)
    systemd.show("x.service")
    systemd.show("x.service", timeout=4.0)
    systemd.show("x.service", timeout=400.0)
    assert runner.timeouts == [15.0, 4.0, 15.0]
    for spent in (0.0, -1.0):
        with pytest.raises(system_module.CommandTimeout):
            systemd.show("x.service", timeout=spent)
    assert len(runner.timeouts) == 3                 # nothing ran without time left


def test_api_identity_keeps_its_defaults_for_existing_callers(tmp_path):
    w = World(tmp_path)

    class OldStyleSystemd:                          # show(unit) only: no timeout parameter
        def show(self, unit):
            return w.systemd.show(unit)
    timeouts = []
    real_run = w.runner.run

    def run(argv, timeout, **kw):
        timeouts.append(timeout)
        return real_run(argv, timeout, **kw)
    w.runner.run = run
    assert facts.api_identity(w.cfg, w.runner, OldStyleSystemd()).status == facts.PASS
    assert timeouts == [w.cfg["timeouts"]["command"]]
    assert facts.api_identity(w.cfg, w.runner, w.systemd, budget=lambda: 3.0).status == \
        facts.PASS
    assert timeouts[-1] == 3.0 and w.systemd.show_timeouts[-1] == (UNITS["api"], 3.0)
    spent = facts.api_identity(w.cfg, w.runner, w.systemd, budget=lambda: 0.0)
    assert spent.status == facts.UNKNOWN and "no time left" in spent.detail


def test_the_shutdown_path_still_inspects_with_default_timeouts(tmp_path):
    w = World(tmp_path)
    w.request()
    w.control()
    assert len(w.arm.deallocations) == 1
    assert w.systemd.show_timeouts and all(t is None for _, t in w.systemd.show_timeouts)


def test_the_clock_is_read_again_immediately_before_verified(tmp_path):
    # Time passes after the last complete reading and before the verdict (a scheduler
    # stall): the deadline is crossed exactly there, so the result must not be verified.
    import inspect
    w = World(tmp_path)
    source, first = inspect.getsourcelines(controller.await_ready)
    [offset] = [i for i, line in enumerate(source) if "and env.monotonic() < deadline" in line]
    target, stalled = first + offset, {"done": False}

    def monotonic():
        frame = sys._getframe(1)
        if frame.f_code is controller.await_ready.__code__ and frame.f_lineno == target \
                and not stalled["done"]:
            stalled["done"] = True
            w.mono += allowance(w)                  # the whole allowance passes right here
        return w.mono
    w.env.monotonic = monotonic
    outcome, detail = controller.await_ready(w.env, [UNITS["postgres"]])[UNITS["postgres"]]
    assert stalled["done"] and outcome == "unconfirmed", detail
