"""The operational observer (scripts/native/observer), on synthetic evidence only.

Nothing here reaches a real machine, the QuantDesk API, a database or
Healthchecks.io: the observer's probe is replaced by `FakeProbe`, pings go to
a recording function, and records go to a temporary directory. The two tests
that open a socket talk to a throwaway server on 127.0.0.1.
"""
import base64
import gzip
import hashlib
import http.server
import json
import os
import re
import signal
import ssl
import stat
import subprocess
import sys
import textwrap
import threading
import types
import urllib.request
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from time import monotonic

import pytest

OBSERVER = Path(__file__).resolve().parents[1] / "scripts/native/observer"
sys.path.insert(0, str(OBSERVER))

import operator_acknowledge  # noqa: E402
import operator_approval  # noqa: E402
import operator_maintenance  # noqa: E402
from quantdesk_observer import VERSION, checks, cli, healthchecks, probes, store  # noqa: E402
from quantdesk_observer.checks import IST  # noqa: E402
from quantdesk_observer.probes import ProbeError, RunTimeout  # noqa: E402
from quantdesk_observer.store import CorruptRecord, Overlap, Store  # noqa: E402

REV = "2d1d25a71394455588ba1398f47735fac8a9fdcd"
PARAMS, V2 = "p" * 64, "v" * 64
DAY = date(2026, 10, 14)
INIT = "08:20:10"                      # when the scheduler started; its slots follow from it
LISTENERS = ["0.0.0.0:22", "127.0.0.1:8000"]
PING_IDS = {name: f"00000000-0000-4000-8000-00000000000{i}"
            for i, name in enumerate(healthchecks.CHECKS, 1)}


def at(clock: str, day: date = DAY) -> datetime:
    return datetime.combine(day, time.fromisoformat(clock), tzinfo=IST)


def write_calendar(root: Path, reviewed: bool = True, closed=(), special=(),
                   until: int = 40) -> str:
    """A synthetic session calendar: weekdays are normal sessions unless listed otherwise."""
    dates, day = {}, date(2026, 10, 10)
    for _ in range(until):
        kind = ("SPECIAL" if day in special else
                "CLOSED" if day.weekday() >= 5 or day in closed else "NORMAL")
        entry = {"session_type": kind, "hours": "09:15-15:30" if kind == "NORMAL" else None,
                 "segment": "Equity Derivatives (F&O)", "superseding_notice": None,
                 "review_status": "UNREVIEWED", "reviewer": None, "reviewed_at": None,
                 "official_reference": None}
        if reviewed:
            entry.update(review_status="REVIEWED", reviewer="reviewer",
                         reviewed_at="2026-10-13T18:00:00+05:30",
                         official_reference="synthetic circular 1/2026")
        dates[day.isoformat()] = entry
        day += timedelta(days=1)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "calendar.json"
    path.write_text(json.dumps({"source": "synthetic", "dates": dates}))
    return str(path)


def config(root, reviewed=True, closed=(), special=(), hc=False, **over) -> dict:
    root = Path(root)
    cfg = {**cli.DEFAULTS, "report_root": str(root / "records"),
           "operator_root": str(root / "operator"),
           "calendar_file": write_calendar(root, reviewed, closed, special),
           "expected_revision": REV, "expected_parameter_hash": PARAMS,
           "expected_v2_config_hash": V2, "expected_strategy_version": "nifty-signal-engine/1",
           "expected_listeners": LISTENERS, "disk_paths": ["/"],
           "baseline": {"positions_total": 1, "journal_total": 1}, **over}
    if hc:
        secrets = root / "healthchecks.json"
        secrets.write_text(json.dumps(PING_IDS))
        secrets.chmod(0o600)
        cfg["healthchecks"] = {"enabled": True, "base_url": "https://hc.invalid",
                               "credentials_file": str(secrets)}
    return cfg


def records(root) -> Store:
    return Store(Path(root) / "records", Path(root) / "operator")


def trace(stop: str | None, code: str | None = None, status: str = "failed") -> list[dict]:
    """A consistent gate trace that passes up to `stop`, which fails with `code`."""
    out, reached = [], True
    for gate in checks.GATE_ORDER:
        if gate == stop:
            row = {"gate": gate, "enabled": True, "status": status}
            if status == "failed":
                row["code"] = code
            reached = status == "passed"
        else:
            row = {"gate": gate, "enabled": True,
                   "status": "passed" if reached else "not_reached"}
        out.append(row)
    return out


def contract(reached: bool, observation_id: str) -> dict:
    """QuantDesk's contract_evidence, in either of the two shapes the producer writes."""
    if not reached:
        return {"status": "selection_not_reached", "observation_id": observation_id,
                "note": "the decision ended before contract selection"}
    return {"status": "selection_reached", "observation_id": observation_id,
            "expiry": "2026-10-19",
            "universe": {"definition": "LiveChain.quotes(now)", "quotes": [
                {"token": "1", "strike": 22500, "bid": 101.5, "ask": 102.0,
                 "freshness": "fresh", "source_time": "2026-10-14T06:05:00+00:00"}]},
            "alternatives": [{"strike": 22550, "rejected": "spread_too_wide"}],
            "result": {"selected": "NIFTY19OCT22500CE"}, "pick": {"strike": 22500},
            "trace_consistent_with_decision": True, "levels": {"premium_stop": 80.0}}


def decision(signal: dict, gate_trace=None, code="entry_state_not_ready",
             outcome="rejected") -> dict:
    """A decision row carrying the full v2_decision_evidence/2 envelope."""
    if gate_trace is None:
        gate_trace = trace("signal.entry_state", code)
    selector = next(g["status"] for g in gate_trace if g["gate"] == "contract_selection")
    vector = {
        "identity": {"observation_id": signal["observation_id"], "signal_id": signal["id"],
                     "linkage": "linked", "observation_schema": "signal_observation/2",
                     "seen_in_recent_history": False, "duplicate_check": {"capacity": 512},
                     "symbol": "NIFTY", "timeframe": "5m", "strategy": "v2",
                     "opportunity_id": None, "opportunity_id_status": "unset",
                     "strategy_family": None, "strategy_family_status": "unknown"},
        "causality": dict.fromkeys(checks.VECTOR_SECTIONS["causality"], None)
        | {"bar_open_time": signal["bar_open_time"], "v2_decided_at": signal["created_at"]},
        "market_state": dict.fromkeys(checks.VECTOR_SECTIONS["market_state"], None),
        "setup": dict.fromkeys(checks.VECTOR_SECTIONS["setup"], None)
        | {"action": signal["action"], "score": signal["confidence"], "score_calibrated": False},
        "evidence": {"checks": [dict(c, reason=c.get("reason", "")) for c in signal["checks"]],
                     "threshold_used": signal["threshold_used"]},
        "data_status": {"disabled_checks": ["vwap", "volume"],
                        "signal_option_chain_present": True, "feed_healthy_at_decision": None,
                        "chain_quotes_supplied": None, "chain_max_age_seconds": None},
        "versions": {"evidence_schema": "v2_decision_evidence/2",
                     "signal_strategy_version": signal["strategy_version"],
                     "parameter_hash": signal["parameter_hash"], "input_fingerprint": "f" * 64,
                     "input_fingerprint_note": "identifies the input", "code_id": signal["code_id"],
                     "code_id_basis": "git state on disk", "data_source": "free",
                     "v2_strategy": "v2", "v2_version": "2.0", "v2_config_hash": V2,
                     "api_init_provenance": signal["api_init"]},
        "decision_context": {"role": "the desk's conclusions", "outcome": outcome, "code": code,
                             "gate_trace": gate_trace, "kill_switch": None,
                             "listed_expiries": None, "chosen_expiry": None, "equity": None,
                             "risk": None, "position_id": None}}
    return {"id": signal["id"] + 10000, "action": signal["action"], "outcome": outcome,
            "code": code, "evidence": {
                "schema": "v2_decision_evidence/2", "status": "complete", "issues": [],
                "vector": vector,
                "contract_evidence": contract(selector in ("passed", "failed"),
                                              signal["observation_id"]),
                "outcome_label": {"status": "not_implemented"},
                "diagnostics": {"rejected_predictors": []}, "bases": {}, "missing": [],
                "missing_scope": "clocks and identity"}}


def schedule_times(day: date = DAY, init: str = INIT, until: str = "16:10") -> list[str]:
    """What QuantDesk's scheduler would have logged: one run every five minutes from its start."""
    out, moment = [], at(init, day) + timedelta(minutes=5)
    while moment <= at(until, day):
        out.append(moment.isoformat())
        moment += timedelta(minutes=5)
    return out


def schedule_log(times=None, day: date = DAY, **over) -> list[dict]:
    """What `probes.parse_scheduler_log` returns for one scheduler start of the frozen
    application: three `tick` jobs, each declaring its interval, and the agent's runs."""
    times = schedule_times(day) if times is None else times
    start = {"start_logged": True, "scheduler_started": True, "added": ["tick"] * 3,
             "agent_minutes": ["5"], "ticker_seconds": ["5"], "collector_seconds": ["60"],
             "collector_disabled": False,
             "jobs_seen": [["tick", "interval[0:05:00]"], ["tick", "interval[0:00:05]"],
                           ["tick", "interval[0:01:00]"]],
             "runs": [["tick", "interval[0:05:00]", t] for t in times], "runs_not_kept": 0,
             # v0.4.9: which file the start was read from and whose start it is.
             "startup": {"source": "backend.log", "logged_at": at(INIT, day).isoformat()},
             "attribution": {"status": "current", "api_unit": "quantdesk-api.service",
                             "api_main_pid": 943, "admitted_from_seconds": 1,
                             "admitted_before_seconds": 60,
                             "api_main_started": (at(INIT, day)
                                                  - timedelta(seconds=10)).isoformat()}}
    return [{**start, **over}]


def session(day: date = DAY, n: int = 75, init: str = INIT, lag: float = 6.0):
    """`n` reconciled observations on the scheduler's own slots, with linked decisions."""
    start = at(init, day)
    steps = -(-(at("09:15", day) - start).total_seconds() // 300)
    first = start + timedelta(seconds=steps * 300)
    signals, decisions = [], []
    for i in range(n):
        made = first + timedelta(minutes=5 * i, seconds=lag)
        signal_checks = [
            {"name": "structure", "score": 0.5, "weight": 0.22, "disabled": False},
            {"name": "vwap", "score": 0.0, "weight": 0.16, "disabled": True},
            {"name": "trend", "score": 1.0, "weight": 0.14, "disabled": False},
            {"name": "liquidity", "score": 0.0, "weight": 0.14, "disabled": False},
            {"name": "fvg", "score": 0.4, "weight": 0.10, "disabled": False},
            {"name": "option_chain", "score": 0.0, "weight": 0.16, "disabled": False,
             "reason": "Chain reads neutral"},
            {"name": "volume", "score": 0.0, "weight": 0.08, "disabled": True},
        ]
        for c in signal_checks:
            c["contribution"] = 0 if c["disabled"] else round(c["score"] * c["weight"], 4)
        signals.append({
            "id": 3000 + i, "created_at": made.isoformat(), "action": "BUY",
            "confidence": round(0.29 / 0.76, 4), "checks": signal_checks, "code_id": REV[:12],
            "strategy_version": "nifty-signal-engine/1", "parameter_hash": PARAMS,
            "bar_open_time": (made - timedelta(minutes=8)).isoformat(),
            "api_init": {"pid": 943, "initialized_at": start.isoformat(),
                         "repo_state": {"git_commit": REV, "dirty_worktree": False}},
            "observation_id": f"obs-{day}-{i}", "oi_status": "available",
            "threshold_used": 0.35})
        decisions.append(decision(signals[-1]))
    return signals, decisions


class FakeProbe:
    """The same methods as `probes.Probe`, answered from a dictionary.

    A value that is an exception is raised, which is how a fact that cannot
    be read — or a run that is cancelled — is simulated. A value that is a
    `Sequence` answers one call after another, for intermittent reads.
    """

    def __init__(self, day: date = DAY, **over):
        signals, decisions = session(day)
        counters = {"logins": 1, "login_failures": 0, "malformed": 0, "stale_events": 0,
                    "reconnects": 2, "fallbacks": 0}
        jobs = {j: {"successes": 9, "errors": 0, "skips": 0, "missed": 0, "starved": False}
                for j in checks.REQUIRED_JOBS}
        self.calls = []
        self.facts = {
            "ntp": True, "boot": at("08:19", day).timestamp(), "reboot_required": False,
            "unit": {"ActiveState": "active", "MainPID": "943", "NRestarts": "0",
                     "ActiveEnterTimestamp": "today 08:20", "ExecMainStartTimestamp": "today"},
            "processes": [(943, "python uvicorn app.main:app --port 8000"), (1, "init")],
            "listeners": list(LISTENERS), "caddy": False, "disk": (50 * 1024 ** 3, 100 * 1024 ** 3),
            "log": ["2026-10-14 06:00 INFO No packages found"], "revision": REV, "changes": [],
            "/health": {"status": "ok", "live_trading": False, "redis": True},
            "/health/scheduler": {"healthy": True, "jobs": jobs, "problems": [],
                                  "counting_since": at(INIT, day).isoformat()},
            "/health/feed": {"angel": {"enabled": True, "state": "live", "healthy": True,
                                       "counters": counters,
                                       "started_at": at("08:20:11", day).isoformat(),
                                       "session": {"has_auth_token": True,
                                                   "has_feed_token": True}},
                             "option_chain": {"ready": True}},
            "/v2/status": {"position": None},
            "signals": signals, "decisions": decisions, "signal_days": [],
            "schedule": schedule_log(day=day),
            "positions": {"total": 1, "open": 0, "max_id": 1, "session": []},
            "journal": {"total": 1, "max_id": 1},
            "archive": {"rows": 14000, "with_exchange_time": 14000, "captures": 75,
                        "exchange_after_capture": 0, "exchange_other_date": 0},
            "vix": {"close": 14.12, "source": "angel_live",
                    "ingested_at": at("15:30:00.4", day).isoformat()},
        }
        self.facts.update(over)

    def _get(self, key):
        self.calls.append(key)
        value = self.facts[key]
        if isinstance(value, Sequence):
            value = value.next()
        if isinstance(value, BaseException):
            raise value
        return value

    def ntp_synchronized(self): return self._get("ntp")
    def boot_time_epoch(self): return self._get("boot")
    def reboot_required(self): return self._get("reboot_required")
    def unit(self, name): return dict(self._get("unit"))
    def processes(self): return self._get("processes")
    def listeners(self): return self._get("listeners")
    def program_present(self, name): return self._get("caddy")
    def disk_free(self, path): return self._get("disk")
    def log_lines(self, path, needles, limit=40): return self._get("log")
    def revision(self): return self._get("revision")
    def worktree_changes(self): return self._get("changes")
    def api(self, path): return self._get(path)
    def signals(self, day): return self._get("signals")
    def decisions(self, day): return self._get("decisions")
    def positions(self, day): return self._get("positions")
    def signal_days(self, first, last): return self._get("signal_days")
    def agent_schedule(self, day, captures=None):   # v0.4.12: the run's capture store
        self.captures = captures
        return self._get("schedule")
    def journal(self): return self._get("journal")
    def archive(self, day): return self._get("archive")
    def vix_close(self, day): return self._get("vix")


class Sequence:
    """Answers that change from one read to the next; the last one repeats."""

    def __init__(self, *values):
        self.values, self.used = list(values), 0

    def next(self):
        value = self.values[min(self.used, len(self.values) - 1)]
        self.used += 1
        return value


class Clock:
    """A clock that moves a little on every reading and jumps when the observer sleeps."""

    def __init__(self, start: datetime, step: float = 1.0):
        self.now, self.step, self.slept = start, step, []

    def __call__(self):
        self.now += timedelta(seconds=self.step)
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


class Scripted:
    """A clock that returns exactly the given times, in order; the last one repeats."""

    def __init__(self, *moments: datetime):
        self.moments, self.used, self.slept = list(moments), 0, []

    def __call__(self):
        moment = self.moments[min(self.used, len(self.moments) - 1)]
        self.used += 1
        return moment

    def sleep(self, seconds):
        self.slept.append(seconds)


class Pings:
    """Stands in for the HTTPS transport and records what would have been sent."""

    def __init__(self, fail_first: int = 0, error: Exception | None = None, status: int = 200):
        self.sent, self.fail_first, self.error, self.status = [], fail_first, error, status

    def __call__(self, url, body, timeout):
        self.sent.append((url, body.decode(), timeout))
        if len(self.sent) <= self.fail_first:
            raise self.error or OSError("network unreachable")
        return self.status

    def kinds(self):
        out = []
        for url, _, _ in self.sent:
            name = next(n for n, uuid in PING_IDS.items() if uuid in url)
            out.append((name, "fail" if url.endswith("/fail") else "success"))
        return out


def run(check, root, probe=None, start="08:35:00", day=DAY, cfg=None, step=1.0, post=None,
        clock=None, **kw):
    clock = clock or Clock(at(start, day) - timedelta(seconds=step), step)
    cfg = cfg or config(root)
    report = cli.run_check(check, cfg, records(root), probe or FakeProbe(day), clock,
                           clock.sleep, post=post or Pings(), **kw)
    return report, clock


def named(report, name):
    return next(c for c in report["checks"] if c["name"].startswith(name))


def midday(day=DAY, n=39, **over):
    signals, decisions = session(day, n)
    return FakeProbe(day, signals=signals, decisions=decisions, **over)


def day_run(root, day=DAY, probe=None, premarket="08:35:00", mid=True, cfg=None,
            premarket_step=1.0, post=None, **kw):
    """One whole session: pre-market, mid-session and after-close."""
    probe = probe or FakeProbe(day)
    cfg = cfg or config(root)
    if premarket:
        run("premarket", root, probe, premarket, day, cfg, step=premarket_step, post=post)
    if mid:
        run("midsession", root, midday(day), "12:30:00", day, cfg, post=post)
    return run("afterclose", root, probe, "16:00:00", day, cfg, post=post, **kw)[0]


def phase(root, cfg=None, now=None):
    cfg = cfg or config(root)
    return cli.phase_state(records(root), cfg, cli.load_calendar(cfg), now or at("23:00"))


def events(root):
    return {(e["type"], e["subject"]) for e in records(root).events()}


def event_types(root):
    return [e["type"] for e in records(root).events()]


def with_gate(index_to_trace: dict, day=DAY):
    signals, decisions = session(day)
    for i, (gate_trace, code) in index_to_trace.items():
        decisions[i] = decision(signals[i], gate_trace, code)
    return signals, decisions


def vix_stop():
    """A decision trace that passes expiry_day and is refused at the VIX gate."""
    return trace("vix", "vix_percentile_too_high"), "vix_percentile_too_high"


# ===========================================================================
# 08:35 pre-market
# ===========================================================================
def test_premarket_passes_in_time(tmp_path):
    report, clock = run("premarket", tmp_path)
    assert report["result"] == "PASS" and report["timely"] and not clock.slept
    assert report["failure_reasons"] == [] and len(report["attempts"]) == 1
    assert report["scheduled_at_ist"].startswith("2026-10-14T08:35:00+05:30")
    assert report["deadline_at_ist"].startswith("2026-10-14T08:45:00+05:30")
    assert report["quantdesk_revision_observed"] == REV
    assert named(report, "Mac QuantDesk")["status"] == "OPERATOR-MAINTAINED PREREQUISITE"
    assert records(tmp_path).events() == []


def test_premarket_does_not_need_live_ticks(tmp_path):
    probe = FakeProbe()
    probe.facts["/health/feed"]["angel"].update(state="stale", healthy=False)
    assert run("premarket", tmp_path, probe)[0]["result"] == "PASS"


def test_premarket_retries_then_passes_before_the_deadline(tmp_path):
    probe, clock = FakeProbe(ntp=False), Clock(at("08:35:00"))

    def sleep(seconds):
        clock.sleep(seconds)
        if len(clock.slept) == 2:
            probe.facts["ntp"] = True
    report = cli.run_check("premarket", config(tmp_path), records(tmp_path), probe, clock, sleep,
                           post=Pings())
    assert report["result"] == "PASS" and report["timely"]
    assert [a["passed"] for a in report["attempts"]] == [False, False, True]
    assert report["completed_at_ist"] < "2026-10-14T08:45"


def test_premarket_fails_at_the_deadline_and_stops_retrying(tmp_path):
    report, clock = run("premarket", tmp_path, FakeProbe(revision="f" * 40))
    assert report["result"] == "FAIL"
    assert clock.now <= at("08:45:00") and 1 < len(report["attempts"]) <= 10
    assert any("QuantDesk revision" in r for r in report["failure_reasons"])
    assert events(tmp_path) == {("operational_failure", "premarket")}


@pytest.mark.parametrize("fact, value", [
    ("changes", [" M backend/app/main.py"]),
    ("/health", {"status": "ok", "live_trading": True, "redis": True}),
    ("processes", [(943, "uvicorn app.main:app"), (950, "uvicorn app.main:app")]),
    ("listeners", [*LISTENERS, "0.0.0.0:443"]), ("caddy", True),
    ("disk", (1024, 100 * 1024 ** 3)), ("/v2/status", {"position": {"id": 7}}),
    ("journal", {"total": 2, "max_id": 2}),
    ("positions", {"total": 2, "open": 0, "max_id": 2, "session": []}),
])
def test_premarket_required_checks_fail_closed(tmp_path, fact, value):
    assert run("premarket", tmp_path, FakeProbe(**{fact: value}))[0]["result"] == "FAIL"


def test_pending_reboot_is_maintenance_information_not_a_failure(tmp_path):
    report, _ = run("premarket", tmp_path, FakeProbe(reboot_required=True))
    pending = named(report, "pending restart requirements")
    assert report["result"] == "PASS" and pending["status"] == "RECORDED"
    assert pending["evidence"] == {"reboot_required": True}


def test_restart_counts_are_recorded_with_times_and_not_judged_alone(tmp_path):
    probe = FakeProbe()
    probe.facts["unit"]["NRestarts"] = "2"
    report, _ = run("premarket", tmp_path, probe)
    review = named(report, "overnight update/reboot/service-start review")
    assert report["result"] == "PASS" and review["status"] == "PASS"
    assert review["evidence"]["units"]["api"]["automatic_restarts"] == "2"
    assert "does not prove" in review["evidence"]["note"]


def test_late_catch_up_run_is_recorded_as_late_and_never_passes(tmp_path):
    report, _ = run("premarket", tmp_path, start="09:03:27")
    assert report["result"] == "FAIL" and not report["timely"]
    assert report["lateness_seconds"] == pytest.approx(1708, abs=2)
    assert checks.all_pass(report["checks"])  # healthy, and still not a timely PASS
    assert "not timely" in report["failure_reasons"][0]
    assert cli.check_status(records(tmp_path), DAY.isoformat(), "premarket") == "MISSING"


def test_catch_up_before_the_scheduled_time_checks_nothing(tmp_path):
    report, _ = run("premarket", tmp_path, start="07:00:00")
    assert report["result"] == "FAIL" and not report["ran_checks"] and report["checks"] == []
    assert cli.check_status(records(tmp_path), DAY.isoformat(), "premarket") == "MISSING"


def test_missing_premarket_report_is_missing_not_pass(tmp_path):
    report = day_run(tmp_path, premarket=None)
    assert report["readiness_0845"] == "MISSING"
    assert report["outcome"] == "OPERATIONAL EXCEPTION REQUIRED"
    assert {("missing_check", "premarket"),
            ("operational_exception_required", "session")} <= events(tmp_path)


# ===========================================================================
# 12:30 mid-session
# ===========================================================================
def test_healthy_midsession_check(tmp_path):
    run("premarket", tmp_path)
    report, _ = run("midsession", tmp_path, midday(), "12:30:00")
    cover = named(report, "collection coverage")["evidence"]
    assert report["result"] == "PASS" and report["timely"]
    assert (cover["expected"], cover["actual"], cover["missing"], cover["duplicates"]) == \
        (39, 39, 0, 0)
    assert report["early_review_candidates_seen"] == []


def test_medium_a_a_midsession_run_that_finishes_after_its_deadline_is_not_timely(tmp_path):
    # Lock attempt, acquisition and start read at 12:30; everything after at 12:45:01.
    clock = Scripted(at("12:30:00"), at("12:30:00"), at("12:30:00"), at("12:30:00"),
                     at("12:45:01"))
    report, _ = run("midsession", tmp_path, midday(), clock=clock)
    assert report["started_in_window"] and report["result"] == "PASS"
    assert report["timely"] is False and "not timely" in " ".join(report["failure_reasons"])
    assert cli.check_status(records(tmp_path), DAY.isoformat(), "midsession") == "MISSING"


def test_medium_a_premarket_completing_after_0845_fails_even_when_started_on_time(tmp_path):
    report, _ = run("premarket", tmp_path, start="08:44:40", step=5.0)   # starts 08:44:55
    assert report["ran_checks"] and report["assessment_started_at_ist"].endswith("08:44:55+05:30")
    assert report["started_in_window"] and not report["timely"] and report["result"] == "FAIL"
    assert cli.check_status(records(tmp_path), DAY.isoformat(), "premarket") == "FAIL"


def test_gaps_and_duplicates_are_counted_not_merged(tmp_path):
    signals, decisions = session(n=39)
    del signals[10:12]
    signals.append({**signals[5], "id": 9999, "observation_id": "obs-copy"})
    report, _ = run("midsession", tmp_path, FakeProbe(signals=signals, decisions=decisions),
                    "12:30:00")
    cover = named(report, "collection coverage")
    assert cover["status"] == "FAIL" and report["result"] == "FAIL"
    assert cover["evidence"]["missing_slots_ist"] == ["10:05:10", "10:10:10"]
    assert cover["evidence"]["duplicates"] == 1


def cover(signals, schedule=None, now="16:00", final=True):
    logged = schedule if schedule and isinstance(schedule[0], dict) else schedule_log(schedule)
    basis = checks.schedule_basis(logged, DAY, None if final else at(now))
    return checks.collection(signals, basis, DAY, at(now), final)


def test_slot_tolerance_matches_one_observation_and_does_nothing_else():
    def shifted(lag):
        signals, _ = session(lag=6.0)
        made = datetime.fromisoformat(signals[30]["created_at"]) + timedelta(seconds=lag - 6.0)
        signals[30]["created_at"] = made.isoformat()
        return cover(signals)
    assert shifted(60.0)["status"] == "PASS" and shifted(-60.0)["status"] == "PASS"
    late = shifted(61.0)
    assert late["status"] == "FAIL" and late["evidence"]["off_schedule"] == [3030]
    assert late["evidence"]["missing"] == 1  # the slot it missed is not excused
    assert shifted(6.0)["evidence"]["seconds_after_slot"] == {"min": 6.0, "max": 6.0}
    signals, _ = session()
    signals.append({**signals[5], "id": 9999, "observation_id": "copy",
                    "created_at": (datetime.fromisoformat(signals[5]["created_at"])
                                   + timedelta(seconds=20)).isoformat()})
    doubled = cover(signals)   # two observations in one slot are not merged into one
    assert doubled["status"] == "FAIL" and doubled["evidence"]["duplicates"] == 1


def test_schedule_comes_from_the_schedulers_own_log_not_from_the_api_start_or_the_rows():
    """Codex's finding: slots were derived from the API initialization time."""
    signals, _ = session(init="08:20:10")
    for s in signals:   # the API claims a start 100 s away from the scheduler's real phase
        s["api_init"]["initialized_at"] = at("08:18:30").isoformat()
    assert cover(signals)["status"] == "PASS"            # the logged schedule decides
    wrong = cover(signals, schedule_times(init="08:18:30"))
    assert wrong["status"] == "FAIL" and len(wrong["evidence"]["off_schedule"]) == 20
    signals, _ = session()
    del signals[0:6]   # the first six scheduled observations never happened
    gap = cover(signals)
    assert gap["status"] == "FAIL" and gap["evidence"]["missing"] == 6
    assert gap["evidence"]["missing_slots_ist"][0] == "09:15:10"   # not shifted to fit the rows
    for init in ("08:20:10", "08:47:42", "09:03:26"):
        signals, _ = session(init=init)
        assert cover(signals, schedule_times(init=init))["status"] == "PASS", init
    basis = checks.schedule_basis(schedule_log(), DAY)
    assert basis["status"] == "ESTABLISHED" and len(basis["slots"]) == 75
    assert basis["logged_before_the_open"] and basis["slots_the_scheduler_did_not_log"] == []


TWO_STARTS = (schedule_log(schedule_times(init="08:20:10", until="11:00"))
              + schedule_log(schedule_times(init="11:02:40")))


@pytest.mark.parametrize("logged, reason", [
    (None, "could not be read"), (schedule_log([]), "logged no run time"),
    (TWO_STARTS, "2 scheduler start(s)"),
    (schedule_log(["not a time"]), "unreadable run time"),
    (schedule_log(schedule_times(DAY - timedelta(days=1))), "logged no run time"),
    # Conflicting timing evidence inside one start, and a log with lines missing.
    (schedule_log(schedule_times() + schedule_times(init="08:21:40")), "conflicts"),
    (schedule_log(schedule_times()[:20] + schedule_times()[23:]), "not in the retained log"),
    (schedule_log(schedule_times(init="09:31:00")), "not in the retained log"),
    (schedule_log(schedule_times(until="13:00")), "not in the retained log"),
])
def test_unknown_schedule_is_unknown_and_never_invented(tmp_path, logged, reason):
    signals, _ = session()
    result = checks.collection(signals, checks.schedule_basis(logged, DAY), DAY, at("16:00"),
                               True)
    assert result["status"] == "UNKNOWN" and reason in result["detail"]
    assert result["evidence"]["actual"] == 75    # the observations are still counted
    probe = FakeProbe(schedule=ProbeError("log unreadable") if logged is None else logged)
    report = day_run(tmp_path, probe=probe)
    assert named(report, "collection coverage")["status"] == "UNKNOWN"
    assert report["outcome"] == "INCOMPLETE" and "collection coverage" in report["unknown_fields"]


def log_line(stamp, logger, message, level="INFO"):
    return f"2026-10-14 {stamp},101 {level} {logger}: {message}\n"


def run_line(name, trigger, scheduled):
    return log_line(scheduled.strftime("%H:%M:%S"), "apscheduler.executors.default",
                    f'Running job "{name} (trigger: {trigger}, next run at: 2026-10-14 x IST)" '
                    f"(scheduled at {scheduled.isoformat(sep=' ')})")


def startup_lines(stamp="08:20:10", agent="5", ticker="5", collector="60", extra=()):
    """The lines the frozen application's one scheduler writes as it starts
    (format reproduced locally from the same APScheduler release)."""
    sched = "apscheduler.scheduler"
    added = 'Added job "tick" to job store "default"'
    lines = [log_line(stamp, sched, "Adding job tentatively -- it will be properly scheduled "
                                    "when the scheduler starts"),
             log_line(stamp, sched, added), log_line(stamp, sched, "Scheduler started")]
    if agent is not None:
        lines.append(log_line(stamp, "app.workers.agent",
                              f"Nifty agent scheduled every {agent} minutes."))
    lines += [log_line(stamp, sched, added),
              log_line(stamp, "app.workers.ticker",
                       f"price ticker running every {ticker}s (fallback only — Angel feed is "
                       "preferred)"),
              log_line(stamp, sched, added),
              log_line(stamp, "app.workers.option_collector",
                       f"option chain collector running every {collector}s (bars are 5m "
                       "buckets)")]
    return lines + [log_line(stamp, sched, f'Added job "{name}" to job store "default"')
                    for name in extra]


def agent_runs(init="08:20:10", until="16:10", name="tick", trigger="interval[0:05:00]"):
    return [run_line(name, trigger, datetime.fromisoformat(t))
            for t in schedule_times(init=init, until=until)]


# v0.4.10: a synthetic, explicitly assumed basis. It is not evidence about the Azure VM,
# whose logging zone and single writer remain deployment prerequisites.
LOG_BASIS = {"timezone": "Asia/Kolkata", "single_writer": True,
             "timezone_basis": "synthetic test assumption: the API logs in Asia/Kolkata",
             "single_writer_basis": "synthetic test assumption: only the API writes the log",
             "verified_by": "tests", "verified_at": "2026-10-16T00:00:00+05:30"}
API_ARGS = "python -m uvicorn app.main:app --port 8000"
BOOT = "0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e"       # v0.4.12: synthetic boot id
MONOTONIC = "9184000000"                             # µs since boot, synthetic


class LogProbe(probes.Probe):
    """The real log reader, with systemd's answer for the API unit supplied (v0.4.9),
    and (v0.4.10) the recorded log basis and the process table."""

    def __init__(self, log, started="Wed 2026-10-14 08:20:00 IST", pid="943", units=None,
                 basis=LOG_BASIS, runtimes=None, boots=None):
        super().__init__({"quantdesk_log": str(log),
                          "units": {"api": "quantdesk-api.service"},
                          "api_process_match": "uvicorn app.main:app",
                          **({"backend_log_basis": basis} if basis is not None else {})})
        self.answers = list(units or [{"ActiveState": "active", "MainPID": pid,
                                       "ExecMainStartTimestamp": started}])
        self.unit_reads = 0
        self.runtimes = list(runtimes or [[(943, API_ARGS), (1, "init")]])
        self.process_reads = 0
        self.boots = list(boots or [BOOT])        # v0.4.12
        self.boot_reads = 0

    def unit(self, name):
        assert name == "quantdesk-api.service"
        answer = self.answers[min(self.unit_reads, len(self.answers) - 1)]
        self.unit_reads += 1
        # v0.4.12: systemd's monotonic start, unless the case under test sets it.
        return {"ExecMainStartTimestampMonotonic": MONOTONIC, **answer}

    def boot_id(self):
        answer = self.boots[min(self.boot_reads, len(self.boots) - 1)]
        self.boot_reads += 1
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def processes(self):
        answer = self.runtimes[min(self.process_reads, len(self.runtimes) - 1)]
        self.process_reads += 1
        return list(answer)


def read_log(tmp_path, lines):
    log = tmp_path / "backend.log"
    log.write_text("".join(lines))
    return LogProbe(log).agent_schedule(DAY)


def test_supported_identity_and_schedule_evidence_establishes_the_timing(tmp_path):
    """Synthetic lines in the format APScheduler 3.11 and the frozen application write.
    This shows what the parser accepts; it is not evidence that the Azure log holds
    these lines, which nothing here has read."""
    lines = startup_lines() + agent_runs() + [
        run_line("tick", "interval[0:00:05]", at("09:18:31")),
        run_line("tick", "interval[0:01:00]", at("09:18:10")),
        log_line("09:23:26", "apscheduler.executors.default",
                 'Job "tick (trigger: interval[0:05:00], next run at: x)" executed '
                 "successfully")]
    logged = read_log(tmp_path, lines)
    assert len(logged) == 1 and logged[0]["agent_minutes"] == ["5"]
    assert logged[0]["added"] == ["tick"] * 3
    basis = checks.schedule_basis(logged, DAY)
    assert basis["status"] == "ESTABLISHED" and len(basis["slots"]) == 75
    assert basis["slots"][0] == at("09:15:10") and basis["logged_run_times"] == 93
    assert "do not carry the job id" in basis["identity_note"]
    assert "declaration" in basis["basis"]
    signals, _ = session()
    assert checks.collection(signals, basis, DAY, at("16:00"), True)["status"] == "PASS"
    with pytest.raises(ProbeError):
        probes.Probe({"quantdesk_log": str(tmp_path / "none")}).agent_schedule(DAY)


def unknown_basis(tmp_path, lines, reason):
    basis = checks.schedule_basis(read_log(tmp_path, lines), DAY)
    assert basis["status"] == "UNKNOWN" and "slots" not in basis, basis
    assert reason in basis["reason"], basis["reason"]
    signals, _ = session()
    cover_ = checks.collection(signals, basis, DAY, at("16:00"), True)
    assert cover_["status"] == "UNKNOWN"
    return basis


def test_an_unrelated_five_minute_job_is_not_the_signal_agent(tmp_path):
    """Codex's finding: any job with interval[0:05:00] was taken for the agent."""
    # The exact case: a maintenance job's lines alone, on a perfect five-minute grid.
    unknown_basis(tmp_path, agent_runs(name="maintenance"), "cannot be identified")
    unknown_basis(tmp_path, agent_runs(), "does not retain this scheduler's start")
    # The same job on QuantDesk's scheduler, beside a correctly declared agent.
    unknown_basis(tmp_path, startup_lines(extra=["maintenance"]) + agent_runs()
                  + agent_runs(init="08:21:00", name="maintenance"),
                  "not the 3 jobs the frozen application defines")
    # A fourth job that is also called `tick` and also runs every five minutes.
    unknown_basis(tmp_path, startup_lines(extra=["tick"]) + agent_runs(),
                  "not the 3 jobs the frozen application defines")
    # A stranger that never logged its registration but did run.
    unknown_basis(tmp_path, startup_lines() + agent_runs()
                  + [run_line("cleanup", "interval[0:10:00]", at("09:00:00"))],
                  "a job the frozen application does not define")
    # The words of the agent's declaration inside another logger's message prove nothing.
    forged = startup_lines(agent=None) + [log_line(
        "08:20:10", "app.api.news", "headline: Nifty agent scheduled every 5 minutes.")]
    unknown_basis(tmp_path, forged + agent_runs(), "declared its interval 0 times")


def test_missing_job_identity_is_unknown(tmp_path):
    unknown_basis(tmp_path, startup_lines(agent=None) + agent_runs(),
                  "the signal agent (`Nifty agent scheduled`) declared its interval 0 times")
    lines = [line for line in startup_lines() if "price ticker" not in line]
    unknown_basis(tmp_path, lines + agent_runs(), "the price ticker declared its interval 0")
    lines = [line for line in startup_lines() if "Scheduler started" not in line]
    unknown_basis(tmp_path, lines + agent_runs(), "does not retain this scheduler's start")


def test_ambiguous_job_identity_is_unknown(tmp_path):
    # Two QuantDesk jobs on the same five-minute trigger: the lines cannot be told apart.
    unknown_basis(tmp_path, startup_lines(collector="300") + agent_runs(),
                  "another QuantDesk job declares the same five-minute interval")
    unknown_basis(tmp_path, startup_lines(agent="15") + agent_runs(),
                  "declared a run every 15 minutes")
    twice = startup_lines() + [log_line("08:20:11", "app.workers.agent",
                                        "Nifty agent scheduled every 5 minutes.")]
    unknown_basis(tmp_path, twice + agent_runs(), "declared its interval 2 times")
    unknown_basis(tmp_path, startup_lines(agent="soon") + agent_runs(), "unreadable interval")


def test_retention_and_log_gaps_are_unknown(tmp_path):
    # Rotated away: the day's runs survive, the start that declared them does not.
    unknown_basis(tmp_path, agent_runs(), "does not retain this scheduler's start")
    runs = agent_runs()
    unknown_basis(tmp_path, startup_lines() + runs[:30] + runs[34:], "4 scheduled slot(s)")
    unknown_basis(tmp_path, startup_lines() + agent_runs(until="14:00"),
                  "not in the retained log")
    # A restart during the day: two starts, two grids, no single schedule.
    unknown_basis(tmp_path, startup_lines() + agent_runs(until="11:00")
                  + startup_lines("11:02:40") + agent_runs(init="11:02:40"),
                  "2 scheduler start(s)")
    # Yesterday's start and runs do not stand in for today's.
    assert checks.schedule_basis(read_log(tmp_path, startup_lines()), DAY)["status"] == "UNKNOWN"


def test_schedule_is_not_taken_from_api_start_rows_another_job_or_the_open(tmp_path):
    """With the log unusable, nothing else is allowed to supply a schedule."""
    signals, decisions = session()          # 75 rows on a perfect grid, API start known
    probe = FakeProbe(signals=signals, decisions=decisions,
                      schedule=read_log(tmp_path, agent_runs(name="maintenance")))
    report = day_run(tmp_path, probe=probe)
    cover_ = named(report, "collection coverage")
    assert cover_["status"] == "UNKNOWN" and report["outcome"] == "INCOMPLETE"
    assert "slots" not in cover_["evidence"]["schedule"]
    assert cover_["evidence"]["schedule"]["identity_problems"]


# ===========================================================================
# v0.4.9 — the running API's start, read across the backend log's rotations
# ===========================================================================
FILLER = log_line("23:59:00", "app.main", "still running")


def on(day_text: str, lines: list[str]) -> list[str]:
    """The same lines logged on another date (only the leading asctime moves)."""
    return [line.replace("2026-10-14 ", f"{day_text} ", 1) for line in lines]


def write_rotation(directory: Path, files: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, lines in files.items():
        data = "".join(lines).encode() if isinstance(lines, list) else lines
        (directory / name).write_bytes(gzip.compress(data) if name.endswith(".gz")
                                       and isinstance(lines, list) else data)
    return directory / "backend.log"


def rotated(directory: Path, files: dict, started="Wed 2026-10-14 08:20:00 IST", **kw):
    return LogProbe(write_rotation(directory, files), started=started, **kw).agent_schedule(DAY)


def rotation_cases(root: Path) -> list[list[dict]]:
    """One API start, read before rotation, after a numbered rotation and after compression."""
    runs = agent_runs()
    return [rotated(root / "current", {"backend.log": startup_lines() + runs}),
            rotated(root / "numbered", {"backend.log.1": on("2026-10-13", startup_lines()
                                                            + [FILLER]),
                                        "backend.log": runs},
                    started="Tue 2026-10-13 08:20:00 IST"),
            rotated(root / "compressed", {"backend.log.2.gz": on("2026-10-12", startup_lines()),
                                          "backend.log.1": on("2026-10-13", [FILLER]),
                                          "backend.log": runs},
                    started="Mon 2026-10-12 08:20:00 IST")]


def test_the_current_start_is_found_before_and_after_rotation_and_compression(tmp_path):
    cases = rotation_cases(tmp_path)
    for logged in cases:
        assert [s["attribution"]["status"] for s in logged] == ["current"]
    bases = [checks.schedule_basis(logged, DAY) for logged in cases]
    for basis in bases:
        assert basis["status"] == "ESTABLISHED" and len(basis["slots"]) == 75
    def without_startup(basis): return {k: v for k, v in basis.items() if k != "startup"}
    assert without_startup(bases[0]) == without_startup(bases[1]) == without_startup(bases[2])
    assert [b["startup"]["source"] for b in bases] == ["backend.log", "backend.log.1",
                                                       "backend.log.2.gz"]
    assert bases[2]["startup"] == {
        "source": "backend.log.2.gz", "logged_at": "2026-10-12T08:20:10.101000+05:30",
        "status": "current", "api_unit": "quantdesk-api.service", "api_main_pid": 943,
        "api_main_started": "2026-10-12T08:20:00+05:30",
        "admitted_from_seconds": 1, "admitted_before_seconds": 60, "log_basis": LOG_BASIS}
    middays = [checks.schedule_basis(logged, DAY, at("12:30")) for logged in cases]
    assert len({json.dumps(without_startup(b), default=str) for b in middays}) == 1
    signals, _ = session()
    for basis in bases:
        assert checks.collection(signals, basis, DAY, at("16:00"), True)["status"] == "PASS"


def test_a_rotated_current_start_gives_the_same_report_as_an_unrotated_one(tmp_path):
    cases = rotation_cases(tmp_path / "logs")
    reports = [day_run(tmp_path / f"run{i}", probe=FakeProbe(schedule=logged))
               for i, logged in enumerate(cases)]
    shapes = [(r["result"], r["outcome"], r["unknown_fields"],
               [(c["name"], c["status"]) for c in r["checks"]]) for r in reports]
    assert shapes[0] == shapes[1] == shapes[2]
    assert reports[0]["outcome"] == day_run(tmp_path / "plain")["outcome"]


def test_an_earlier_api_start_cannot_establish_the_current_schedule(tmp_path):
    # The API restarted at 11:00; its own start is not in the log, the old process's is.
    logged = rotated(tmp_path / "a", {"backend.log": startup_lines() + agent_runs()},
                     started="Wed 2026-10-14 11:00:00 IST")
    assert [s["attribution"]["status"] for s in logged] == ["earlier"]
    basis = checks.schedule_basis(logged, DAY)
    assert basis["status"] == "UNKNOWN" and "slots" not in basis
    assert "not the running API process's own start (earlier)" in basis["reason"]
    # The same after rotation: an older file's start does not stand in for a lost one.
    logged = rotated(tmp_path / "b", {"backend.log.1": on("2026-10-13", startup_lines()),
                                      "backend.log": agent_runs()},
                     started="Wed 2026-10-14 08:00:00 IST")
    assert [(s["startup"]["source"], s["attribution"]["status"]) for s in logged] == [
        ("backend.log.1", "earlier")]
    assert checks.schedule_basis(logged, DAY)["status"] == "UNKNOWN"
    # The old process ran the session; the current one started after the close. The
    # file that began before the current process is the last one read, so the old
    # start in backend.log.1 is not even consulted.
    logged = rotated(tmp_path / "c", {"backend.log.1": on("2026-10-13", startup_lines()),
                                      "backend.log": agent_runs()
                                      + startup_lines("16:30:05")},
                     started="Wed 2026-10-14 16:30:00 IST")
    assert [(s["start_logged"], s["attribution"]["status"]) for s in logged] == [
        (False, "unattributed"), (True, "current")]
    basis = checks.schedule_basis(logged, DAY)
    assert basis["status"] == "UNKNOWN" and "slots" not in basis
    assert "does not retain this scheduler's start" in basis["reason"]
    # A restart before the open with both processes' runs on the day stays a conflict.
    logged = rotated(tmp_path / "d", {"backend.log": startup_lines("00:10:00")
                                      + agent_runs(init="00:10:00", until="08:00")
                                      + startup_lines() + agent_runs()})
    assert [s["attribution"]["status"] for s in logged] == ["earlier", "current"]
    assert "2 scheduler start(s)" in checks.schedule_basis(logged, DAY)["reason"]


@pytest.mark.parametrize("lines, started, status", [
    # v0.4.10: 08:21:00 is now exactly T+60s and so `later`; the second start is moved
    # inside the admission interval to keep this case two candidates.
    (startup_lines() + startup_lines("08:20:40") + agent_runs(init="08:20:40"),
     "Wed 2026-10-14 08:20:00 IST", "ambiguous"),
    (startup_lines() + agent_runs(), "Wed 2026-10-14 08:10:00 IST", "later"),
    ([startup_lines()[0].replace("2026-10-14", "2026-13-14", 1)] + startup_lines()[1:]
     + agent_runs(), "Wed 2026-10-14 08:20:00 IST", "unattributed"),
])
def test_an_ambiguous_late_or_unreadable_start_establishes_nothing(tmp_path, lines, started,
                                                                    status):
    logged = rotated(tmp_path, {"backend.log": lines}, started=started)
    assert logged[-1]["attribution"]["status"] == status
    basis = checks.schedule_basis(logged, DAY)
    assert basis["status"] == "UNKNOWN" and "slots" not in basis
    assert f"({status})" in basis["reason"] and basis["attribution"]["status"] == status


def test_a_segment_without_attribution_establishes_nothing():
    (segment,) = schedule_log()
    del segment["attribution"]
    basis = checks.schedule_basis([segment], DAY)
    assert basis["status"] == "UNKNOWN" and "(not attributed)" in basis["reason"]


@pytest.mark.parametrize("answer, cfg_units", [
    ({"ActiveState": "inactive", "MainPID": "943",
      "ExecMainStartTimestamp": "Wed 2026-10-14 08:20:00 IST"}, True),
    ({"ActiveState": "active", "MainPID": "0",
      "ExecMainStartTimestamp": "Wed 2026-10-14 08:20:00 IST"}, True),
    ({"ActiveState": "active", "MainPID": "943", "ExecMainStartTimestamp": "n/a"}, True),
    ({"ActiveState": "active", "MainPID": "943",
      "ExecMainStartTimestamp": "Wed 2026-10-14 08:20:00 PDT"}, True),
    ({"ActiveState": "active", "MainPID": "943",
      "ExecMainStartTimestamp": "Wed 2026-10-14 08:20:00 IST"}, False),
])
def test_without_the_running_api_process_the_log_is_not_read(tmp_path, answer, cfg_units):
    log = write_rotation(tmp_path, {"backend.log": startup_lines() + agent_runs()})
    probe = LogProbe(log, units=[answer])
    if not cfg_units:
        probe.cfg = {"quantdesk_log": str(log)}
    with pytest.raises(ProbeError):
        probe.agent_schedule(DAY)


def test_missing_corrupt_and_ambiguous_files_fail_safely(tmp_path):
    good = on("2026-10-12", startup_lines())
    with pytest.raises(ProbeError, match="could not be read"):
        LogProbe(tmp_path / "absent" / "backend.log").agent_schedule(DAY)
    cases = {
        "corrupt": {"backend.log.1.gz": b"not gzip at all\n"},
        "truncated": {"backend.log.1.gz": gzip.compress("".join(good).encode())[:-12]},
        "both names": {"backend.log.1": good, "backend.log.1.gz": good},
    }
    for label, files in cases.items():
        with pytest.raises(ProbeError):
            rotated(tmp_path / label, {**files, "backend.log": agent_runs()},
                    started="Mon 2026-10-12 08:20:00 IST")
    # A symlink or a directory under a supported name is refused, never followed.
    outside = write_rotation(tmp_path / "outside", {"backend.log": good})
    link = tmp_path / "link"
    write_rotation(link, {"backend.log": agent_runs()})
    (link / "backend.log.1").symlink_to(outside)
    with pytest.raises(ProbeError):
        LogProbe(link / "backend.log", started="Mon 2026-10-12 08:20:00 IST").agent_schedule(DAY)
    folder = tmp_path / "folder"
    write_rotation(folder, {"backend.log": agent_runs()})
    (folder / "backend.log.1").mkdir()
    with pytest.raises(ProbeError, match="not a regular file"):
        LogProbe(folder / "backend.log", started="Mon 2026-10-12 08:20:00 IST").agent_schedule(DAY)


def test_a_gap_in_the_rotation_set_stops_the_search(tmp_path):
    logged = rotated(tmp_path, {"backend.log.2.gz": on("2026-10-12", startup_lines()),
                                "backend.log": agent_runs()},
                     started="Mon 2026-10-12 08:20:00 IST")
    basis = checks.schedule_basis(logged, DAY)
    assert basis["status"] == "UNKNOWN" and "does not retain this scheduler's start" in basis[
        "reason"]


@pytest.mark.parametrize("limit, value, message", [
    ("LOG_FILE_MAX_BYTES", 2000, "larger than the observer reads"),
    ("LOG_COMPRESSED_MAX_BYTES", 10, "larger than the observer reads"),
    ("LOG_TOTAL_MAX_BYTES", 2000, "exceed the observer's read limit"),
    ("LOG_READ_SECONDS", -1.0, "took longer than"),
])
def test_oversized_or_slow_evidence_fails_safely(tmp_path, monkeypatch, limit, value, message):
    monkeypatch.setattr(probes, limit, value)
    with pytest.raises(ProbeError, match=message):
        rotated(tmp_path, {"backend.log.2.gz": on("2026-10-12", startup_lines()),
                           "backend.log.1": on("2026-10-13", [FILLER]),
                           "backend.log": agent_runs()},
                started="Mon 2026-10-12 08:20:00 IST")


def test_an_overlong_line_is_dropped_whole_and_never_split(tmp_path, monkeypatch):
    monkeypatch.setattr(probes, "LOG_LINE_MAX_BYTES", 200)
    forged = "y" * 201 + startup_lines()[0]       # a start line hidden after 201 bytes
    logged = rotated(tmp_path, {"backend.log": [forged] + startup_lines()[1:] + agent_runs()})
    assert not any(s["start_logged"] for s in logged)
    basis = checks.schedule_basis(logged, DAY)
    assert basis["status"] == "UNKNOWN" and "does not retain this scheduler's start" in basis[
        "reason"]


def test_only_the_supported_names_are_opened_and_only_for_reading(tmp_path, monkeypatch):
    files = {"backend.log": agent_runs(), "backend.log.1": on("2026-10-13", [FILLER])}
    for n in range(2, 21):
        files[f"backend.log.{n}.gz"] = on(f"2026-09-{30 - n:02d}", [FILLER])
    files["backend.log-20261013.gz"] = b"never read"
    files["other.log.1"] = b"never read"
    log = write_rotation(tmp_path, files)
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "backend.log.1").write_text("never read")
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()
              if p.is_file()}
    attempts, real_open = [], os.open

    def spy(path, flags, *args, dir_fd=None, **kw):
        # v0.4.10 opens names relative to a held directory; record which directory.
        held = None if dir_fd is None else probes._identity(os.fstat(dir_fd))
        attempts.append((str(path), flags, held))
        return real_open(path, flags, *args, dir_fd=dir_fd, **kw)
    monkeypatch.setattr(probes.os, "open", spy)
    logged = LogProbe(log, started="Mon 2026-10-12 08:20:00 IST").agent_schedule(DAY)
    monkeypatch.undo()
    supported = {"backend.log"} | {f"backend.log.{n}{z}" for n in range(1, 15)
                                   for z in ("", ".gz")}
    files = [(name, held) for name, flags, held in attempts if not flags & os.O_DIRECTORY]
    folders = [(name, held) for name, flags, held in attempts if flags & os.O_DIRECTORY]
    assert {name for name, _ in files} == supported
    assert {held for _, held in files} == {probes._identity(os.stat(tmp_path))}
    # The directory is reached from `/` one component at a time, opened and verified.
    assert [name for name, _ in folders] == (["/", *tmp_path.parts[1:]]) * 2
    for name, flags, held in attempts:
        assert flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND) == 0
        assert name == "/" or (flags & os.O_NOFOLLOW and held is not None and "/" not in name)
    assert not any(s.get("start_logged") for s in logged)    # nothing beyond .14 was used
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()
             if p.is_file()}
    assert after == before


def rotate_now(directory: Path, copy_only=False):
    """What logrotate's copytruncate does to the set, in its order."""
    log = directory / "backend.log"
    if not copy_only:
        for n in range(3, 0, -1):
            for z in ("", ".gz"):
                if (directory / f"backend.log.{n}{z}").exists():
                    (directory / f"backend.log.{n}{z}").rename(
                        directory / f"backend.log.{n + 1}{z}")
    (directory / "backend.log.1").write_bytes(log.read_bytes())
    if not copy_only:
        log.write_bytes(b"")


@pytest.mark.parametrize("change", ["rotate", "truncate", "rotated file grows", "api restart"])
def test_rotation_during_inspection_is_never_a_confirmed_schedule(tmp_path, monkeypatch, change):
    log = write_rotation(tmp_path, {"backend.log.1": on("2026-10-13", startup_lines()),
                                    "backend.log": agent_runs()})
    units = [{"ActiveState": "active", "MainPID": "943",
              "ExecMainStartTimestamp": "Tue 2026-10-13 08:20:00 IST"}]
    # Left alone, the same files establish the schedule.
    control = checks.schedule_basis(LogProbe(log, units=units).agent_schedule(DAY), DAY)
    assert control["status"] == "ESTABLISHED" and control["startup"]["source"] == "backend.log.1"
    real_parse = probes.parse_scheduler_log

    def parse_then_change(lines, day, source=None):
        found = real_parse(lines, day, source)
        if change == "rotate":
            rotate_now(tmp_path)
        elif change == "truncate":
            log.write_bytes(b"")
        elif change == "rotated file grows":
            with open(tmp_path / "backend.log.1", "a") as handle:
                handle.write(FILLER)
        return found
    monkeypatch.setattr(probes, "parse_scheduler_log", parse_then_change)
    if change == "api restart":
        units.append({**units[0], "MainPID": "1200",
                      "ExecMainStartTimestamp": "Wed 2026-10-14 12:00:00 IST"})
    with pytest.raises(ProbeError, match="changed while"):
        LogProbe(log, units=units).agent_schedule(DAY)


def test_a_copy_not_yet_truncated_is_an_overlap_not_a_schedule(tmp_path):
    log = write_rotation(tmp_path, {"backend.log": startup_lines() + agent_runs()})
    rotate_now(tmp_path, copy_only=True)       # logrotate copied, has not truncated yet
    with pytest.raises(ProbeError, match="overlap"):
        LogProbe(log, started="Wed 2026-10-14 08:19:00 IST").agent_schedule(DAY)


def test_an_unattributed_schedule_changes_only_collection_coverage(tmp_path):
    plain = day_run(tmp_path / "plain")
    (segment,) = schedule_log()
    earlier = day_run(tmp_path / "earlier", probe=FakeProbe(schedule=[{
        **segment, "attribution": {**segment["attribution"], "status": "earlier"}}]))
    assert named(plain, "collection coverage")["status"] == "PASS"
    assert named(earlier, "collection coverage")["status"] == "UNKNOWN"
    assert earlier["outcome"] == "INCOMPLETE" and "collection coverage" in earlier[
        "unknown_fields"]
    others = [(c["name"], c["status"]) for c in plain["checks"]
              if c["name"] != "collection coverage"]
    assert others == [(c["name"], c["status"]) for c in earlier["checks"]
                      if c["name"] != "collection coverage"]


# ===========================================================================
# v0.4.10 — second-resolution start times, the log's basis, ancestor-safe paths
# ===========================================================================
def startup_at(clock_ms: str) -> list[str]:
    """`startup_lines` logged at an exact millisecond, e.g. "08:20:00,300"."""
    clock, _ = clock_ms.split(",")
    return [line.replace(f"{clock},101", clock_ms, 1) for line in startup_lines(clock)]


@pytest.mark.parametrize("logged_at, status", [
    ("08:20:00,000", "within_start_second"),   # T itself
    ("08:20:00,300", "within_start_second"),   # the reproduced finding
    ("08:20:00,999", "within_start_second"),
    ("08:20:01,000", "current"),               # T+1s exactly is admitted
    ("08:20:59,999", "current"),
    ("08:21:00,000", "later"),                 # T+60s exactly is rejected
])
def test_the_displayed_second_is_an_uncertainty_interval(tmp_path, logged_at, status):
    clock = logged_at.split(",")[0]
    logged = rotated(tmp_path, {"backend.log": startup_at(logged_at) + agent_runs(init=clock)},
                     started="Wed 2026-10-14 08:20:00 IST")
    assert [s["attribution"]["status"] for s in logged] == [status]
    basis = checks.schedule_basis(logged, DAY)
    if status == "current":
        assert basis["status"] == "ESTABLISHED"
        assert basis["startup"]["admitted_from_seconds"] == 1
        assert basis["startup"]["admitted_before_seconds"] == 60
    else:
        assert basis["status"] == "UNKNOWN" and "slots" not in basis
        assert f"({status})" in basis["reason"]


def test_a_start_inside_the_displayed_second_makes_a_later_one_ambiguous(tmp_path):
    lines = startup_at("08:20:00,400") + startup_lines() + agent_runs()
    logged = rotated(tmp_path, {"backend.log": lines}, started="Wed 2026-10-14 08:20:00 IST")
    assert [s["attribution"]["status"] for s in logged] == ["ambiguous", "ambiguous"]
    assert "(ambiguous)" in checks.schedule_basis(logged, DAY)["reason"]


def test_systemds_display_zone_is_not_the_logs_zone(tmp_path):
    """systemd showing UTC changes when the process started, not how the log is read."""
    logged = rotated(tmp_path, {"backend.log": startup_lines() + agent_runs()},
                     started="Wed 2026-10-14 02:50:00 UTC")      # = 08:20:00 IST
    assert [s["attribution"]["status"] for s in logged] == ["current"]
    assert logged[0]["attribution"]["log_basis"]["timezone"] == "Asia/Kolkata"
    assert checks.schedule_basis(logged, DAY)["status"] == "ESTABLISHED"


START = {"ActiveState": "active", "MainPID": "943",
         "ExecMainStartTimestamp": "Wed 2026-10-14 08:20:00 IST"}


@pytest.mark.parametrize("units, runtimes, message", [
    ([START, {**START, "MainPID": "1200"}], None, "API process changed"),
    ([START, {**START, "ExecMainStartTimestamp": "Wed 2026-10-14 08:20:01 IST"}], None,
     "API process changed"),
    (None, [[(943, API_ARGS)], [(1200, API_ARGS)]], "API process changed"),
    (None, [[(943, API_ARGS)], [(943, API_ARGS), (944, API_ARGS)]], "single writer"),
    (None, [[(943, API_ARGS), (944, API_ARGS)]], "single writer"),
    (None, [[(1, "init")]], "single writer"),
])
def test_the_api_process_must_be_one_and_the_same_before_and_after(tmp_path, units, runtimes,
                                                                    message):
    log = write_rotation(tmp_path, {"backend.log": startup_lines() + agent_runs()})
    probe = LogProbe(log, units=units, runtimes=runtimes)
    with pytest.raises(ProbeError, match=message):
        probe.agent_schedule(DAY)


@pytest.mark.parametrize("basis", [
    None,
    {**LOG_BASIS, "timezone": "UTC"},
    {**LOG_BASIS, "timezone": "Asia/Calcutta"},
    {**LOG_BASIS, "single_writer": False},
    {**LOG_BASIS, "single_writer": "true"},
    {k: v for k, v in LOG_BASIS.items() if k != "timezone_basis"},
    {**LOG_BASIS, "single_writer_basis": "  "},
    {**LOG_BASIS, "verified_by": None},
    {**LOG_BASIS, "verified_at": "yesterday"},
    {**LOG_BASIS, "verified_at": "2026-10-16T00:00:00"},
    "Asia/Kolkata",
], ids=["absent", "utc", "alias", "not-single", "string-true", "no-tz-basis", "blank-writer",
        "no-verifier", "bad-time", "naive-time", "not-a-record"])
def test_without_an_established_log_basis_nothing_is_attributed(tmp_path, basis):
    log = write_rotation(tmp_path, {"backend.log": startup_lines() + agent_runs()})
    probe = LogProbe(log)
    if basis is None:
        del probe.cfg["backend_log_basis"]
    else:
        probe.cfg["backend_log_basis"] = basis
    with pytest.raises(ProbeError, match="basis is not established"):
        probe.agent_schedule(DAY)


def test_symlinked_ancestors_leaves_and_unapproved_paths_are_refused(tmp_path):
    real = write_rotation(tmp_path / "real" / "deep" / "logs",
                          {"backend.log": startup_lines() + agent_runs()})
    assert checks.schedule_basis(LogProbe(real).agent_schedule(DAY), DAY)["status"] == \
        "ESTABLISHED"
    (tmp_path / "parent-link").symlink_to(tmp_path / "real" / "deep")
    (tmp_path / "grand-link").symlink_to(tmp_path / "real")
    for path in (tmp_path / "parent-link" / "logs" / "backend.log",
                 tmp_path / "grand-link" / "deep" / "logs" / "backend.log"):
        with pytest.raises(ProbeError, match="without following a link"):
            LogProbe(path).agent_schedule(DAY)
    leaf = tmp_path / "leaf"
    leaf.mkdir()
    (leaf / "backend.log").symlink_to(real)
    with pytest.raises(ProbeError):
        LogProbe(leaf / "backend.log").agent_schedule(DAY)
    for configured in ("backend.log", str(tmp_path / "real" / ".." / "real" / "deep" / "logs"
                                          / "backend.log")):
        probe = LogProbe(real)
        probe.cfg["quantdesk_log"] = configured
        with pytest.raises(ProbeError, match="absolute path without"):
            probe.agent_schedule(DAY)


@pytest.mark.parametrize("change", ["directory replaced", "parent replaced by a link",
                                    "directory removed", "same files, new directory"])
def test_directory_replacement_during_inspection_is_never_confirmed(tmp_path, monkeypatch,
                                                                    change):
    logs = tmp_path / "a" / "logs"
    log = write_rotation(logs, {"backend.log.1": on("2026-10-13", startup_lines()),
                                "backend.log": agent_runs()})
    started = "Tue 2026-10-13 08:20:00 IST"
    control = checks.schedule_basis(LogProbe(log, started=started).agent_schedule(DAY), DAY)
    assert control["status"] == "ESTABLISHED"
    real_parse = probes.parse_scheduler_log

    def parse_then_change(lines, day, source=None):
        found = real_parse(lines, day, source)
        if change == "directory replaced":       # same names, same bytes, another directory
            logs.rename(tmp_path / "a" / "old")
            write_rotation(logs, {p.name: p.read_bytes()
                                  for p in (tmp_path / "a" / "old").iterdir()})
        elif change == "same files, new directory":   # hard links: every file's inode kept
            logs.rename(tmp_path / "a" / "old")
            logs.mkdir()
            for item in (tmp_path / "a" / "old").iterdir():
                os.link(item, logs / item.name)
        elif change == "parent replaced by a link":
            (tmp_path / "a").rename(tmp_path / "a-real")
            (tmp_path / "a").symlink_to(tmp_path / "a-real")
        else:
            logs.rename(tmp_path / "a" / "gone")
        return found
    monkeypatch.setattr(probes, "parse_scheduler_log", parse_then_change)
    with pytest.raises(ProbeError, match="changed while"):
        LogProbe(log, started=started).agent_schedule(DAY)


# ===========================================================================
# v0.4.11 — the API process must be systemd's MainPID, before and after
# ===========================================================================
def count_opens(monkeypatch) -> list[str]:
    opened, real_open = [], os.open

    def spy(path, flags, *args, **kw):
        opened.append(str(path))
        return real_open(path, flags, *args, **kw)
    monkeypatch.setattr(probes.os, "open", spy)
    return opened


def test_a_runtime_that_is_not_systemds_main_process_opens_no_log(tmp_path, monkeypatch):
    """Codex's finding: MainPID 943 and one API process 1200, both stable, established
    a schedule. Now nothing is opened and the schedule is unknown."""
    log = write_rotation(tmp_path, {"backend.log": startup_lines() + agent_runs()})
    opened = count_opens(monkeypatch)
    probe = LogProbe(log, pid="943", runtimes=[[(1200, API_ARGS), (1, "init")]])
    with pytest.raises(ProbeError, match="1200 is not systemd's MainPID 943"):
        probe.agent_schedule(DAY)
    assert opened == []
    assert probe.unit_reads == 1 and probe.process_reads == 1


@pytest.mark.parametrize("units, runtimes, message", [
    # B: matching at first, the process table names another PID at the end.
    ([START], [[(943, API_ARGS)], [(1200, API_ARGS)]], "is not systemd's MainPID"),
    # B: the process table keeps 943, systemd names another main process at the end.
    ([START, {**START, "MainPID": "1200"}], [[(943, API_ARGS)]], "is not systemd's MainPID"),
    # C: both sources move together to the same new PID.
    ([START, {**START, "MainPID": "1200"}], [[(943, API_ARGS)], [(1200, API_ARGS)]],
     "API process changed while the log was read$"),
    # C: and with a new start time as well.
    ([START, {**START, "MainPID": "1200",
              "ExecMainStartTimestamp": "Wed 2026-10-14 08:30:00 IST"}],
     [[(943, API_ARGS)], [(1200, API_ARGS)]], "API process changed while the log was read$"),
])
def test_pid_evidence_must_agree_and_hold_through_the_read(tmp_path, units, runtimes, message):
    log = write_rotation(tmp_path, {"backend.log": startup_lines() + agent_runs()})
    probe = LogProbe(log, units=units, runtimes=runtimes)
    with pytest.raises(ProbeError, match="changed while the log was read") as raised:
        probe.agent_schedule(DAY)
    assert re.search(message, str(raised.value))
    assert probe.unit_reads == 2 and probe.process_reads == 2


@pytest.mark.parametrize("runtime", [[(1, "init")], [(943, API_ARGS), (944, API_ARGS)],
                                     [(943, API_ARGS), (1200, API_ARGS)]],
                         ids=["none", "two", "main-and-another"])
def test_missing_or_several_api_processes_open_no_log(tmp_path, monkeypatch, runtime):
    log = write_rotation(tmp_path, {"backend.log": startup_lines() + agent_runs()})
    opened = count_opens(monkeypatch)
    with pytest.raises(ProbeError, match="single writer is not established"):
        LogProbe(log, runtimes=[runtime]).agent_schedule(DAY)
    assert opened == []


def test_equal_stable_pid_evidence_still_establishes_the_schedule(tmp_path):
    log = write_rotation(tmp_path, {"backend.log": startup_lines() + agent_runs()})
    probe = LogProbe(log, pid="943", runtimes=[[(943, API_ARGS), (1, "init")]])
    logged = probe.agent_schedule(DAY)
    basis = checks.schedule_basis(logged, DAY)
    assert basis["status"] == "ESTABLISHED" and len(basis["slots"]) == 75
    assert basis["startup"]["api_main_pid"] == 943 and basis["startup"]["status"] == "current"
    assert probe.unit_reads == 2 and probe.process_reads == 2


# ===========================================================================
# v0.4.12 — the running process's original start-up bytes, kept past rotation
# ===========================================================================
STARTED_13 = "Tue 2026-10-13 08:20:00 IST"
START_13 = {"ActiveState": "active", "MainPID": "943", "ExecMainStartTimestamp": STARTED_13}
OTHER_BOOT = "11111111-2222-4333-8444-555555555555"


def capture_setup(tmp_path):
    """The start in backend.log.1 (13 Oct), the session's runs in backend.log (14 Oct)."""
    log = write_rotation(tmp_path / "logs", {"backend.log.1": on("2026-10-13", startup_lines()),
                                             "backend.log": agent_runs()})
    return log, store.Store(tmp_path / "state")


def schedule_with(log, keeper, run="run-1", **kw):
    kw.setdefault("started", STARTED_13)
    return LogProbe(log, **kw).agent_schedule(DAY, keeper.startup_captures(run))


def capture_files(keeper) -> list[Path]:
    base = keeper.root / "startup"
    return sorted(base.glob("*/*.json")) if base.is_dir() else []


def rotate_start_away(log):
    (log.parent / "backend.log.1").unlink()


def test_the_first_eligible_inspection_keeps_the_original_bytes(tmp_path):
    log, keeper = capture_setup(tmp_path)
    basis = checks.schedule_basis(schedule_with(log, keeper), DAY)
    assert basis["status"] == "ESTABLISHED" and basis["startup_capture"]["status"] == "captured"
    (path,) = capture_files(keeper)
    record = store.read_record(path, store.STARTUP_CAPTURE_SCHEMA)
    original = (log.parent / "backend.log.1").read_bytes()
    kept = [base64.b64decode(item["bytes_base64"]) for item in record["lines"]]
    assert b"".join(kept) == original                  # all eight lines are declarations
    for item, data in zip(record["lines"], kept, strict=True):
        assert original[item["offset"]:item["offset"] + item["length"]] == data
        assert item["source"] == "backend.log.1"
        assert item["inode"] == os.stat(log.parent / "backend.log.1").st_ino
    assert record["binding"] == {
        "boot_id": BOOT, "unit": "quantdesk-api.service", "main_pid": 943,
        "main_started": "2026-10-13T08:20:00+05:30",
        "main_started_monotonic_usec": int(MONOTONIC), "api_pid": 943, "log_basis": LOG_BASIS}
    assert record["binding_sha256"] == store.capture_key(record["binding"]) == path.parent.name
    assert record["content_sha256"] == hashlib.sha256(original).hexdigest() == path.stem
    assert record["startup_logged_at"] == "2026-10-13T08:20:10.101000+05:30"
    assert record["captured_by_run"] == "run-1" and record["observer_version"] == VERSION
    # Original evidence only: no slots, schedule, status or result is kept.
    assert set(record) == {"schema", "binding", "binding_sha256", "content_sha256",
                           "startup_logged_at", "lines", "captured_at_utc", "captured_by_run",
                           "observer_version", "integrity"}
    assert path.stat().st_mode & 0o777 == 0o440


def test_a_kept_start_stands_in_once_the_log_has_rotated_it_away(tmp_path):
    log, keeper = capture_setup(tmp_path)
    first = checks.schedule_basis(schedule_with(log, keeper), DAY)
    rotate_start_away(log)
    without = checks.schedule_basis(LogProbe(log, started=STARTED_13).agent_schedule(DAY), DAY)
    assert "does not retain this scheduler's start" in without["reason"]       # v0.4.11
    basis = checks.schedule_basis(schedule_with(log, keeper, run="run-2"), DAY)
    assert basis["status"] == "ESTABLISHED" and basis["slots"] == first["slots"]
    assert basis["startup_capture"]["status"] == "reused"
    assert basis["startup_capture"]["captured_by_run"] == "run-1"
    (path,) = capture_files(keeper)
    assert basis["startup"]["source"] == f"capture {path.stem[:12]}"
    assert basis["startup"]["status"] == "current"
    assert basis["startup"]["logged_at"] == first["startup"]["logged_at"]
    signals, _ = session()
    assert checks.collection(signals, basis, DAY, at("16:00"), True)["status"] == "PASS"


@pytest.mark.parametrize("retained, reason", [
    (agent_runs()[:30] + agent_runs()[34:], "4 scheduled slot(s)"),
    ([FILLER], "logged no run time"),
], ids=["slots-missing", "no-runs"])
def test_a_kept_start_never_replaces_the_sessions_run_lines(tmp_path, retained, reason):
    log, keeper = capture_setup(tmp_path)
    schedule_with(log, keeper)
    rotate_start_away(log)
    log.write_text("".join(retained))
    basis = checks.schedule_basis(schedule_with(log, keeper, run="run-2"), DAY)
    assert basis["status"] == "UNKNOWN" and "slots" not in basis and reason in basis["reason"]


def test_the_same_evidence_again_publishes_nothing_even_under_a_new_name(tmp_path):
    log, keeper = capture_setup(tmp_path)
    schedule_with(log, keeper)
    (path,) = capture_files(keeper)
    before = path.read_bytes()
    again = checks.schedule_basis(schedule_with(log, keeper, run="run-2"), DAY)
    assert again["startup_capture"]["status"] == "already captured"
    # Rotated on: the start is now in backend.log.2.gz, a new name and inode, same bytes.
    one = log.parent / "backend.log.1"
    (log.parent / "backend.log.2.gz").write_bytes(gzip.compress(one.read_bytes()))
    one.write_text("".join(on("2026-10-13", [FILLER])))
    moved = checks.schedule_basis(schedule_with(log, keeper, run="run-3"), DAY)
    assert moved["status"] == "ESTABLISHED" and moved["startup"]["source"] == "backend.log.2.gz"
    assert moved["startup_capture"]["status"] == "already captured"
    assert capture_files(keeper) == [path] and path.read_bytes() == before


def altered_start(log):
    """The same start, but its three registrations logged one millisecond later."""
    lines = [line.replace("08:20:10,101", "08:20:10,102") if "Added job" in line else line
             for line in on("2026-10-13", startup_lines())]
    (log.parent / "backend.log.1").write_text("".join(lines))


def test_different_evidence_for_the_same_process_is_kept_and_never_chosen(tmp_path):
    log, keeper = capture_setup(tmp_path)
    schedule_with(log, keeper)
    altered_start(log)
    with pytest.raises(ProbeError, match="differs from the evidence captured"):
        schedule_with(log, keeper, run="run-2")
    files = capture_files(keeper)
    assert len(files) == 2
    rotate_start_away(log)
    with pytest.raises(ProbeError, match="2 different startup captures"):
        schedule_with(log, keeper, run="run-3")
    assert capture_files(keeper) == files                   # nothing deleted or replaced


class StaleView(store.StartupCaptures):
    """A run that read the store before another run published."""

    def load(self, binding):
        return []


def test_concurrent_runs_publish_once_or_leave_a_visible_conflict(tmp_path):
    log, keeper = capture_setup(tmp_path)
    first = LogProbe(log, started=STARTED_13).agent_schedule(DAY, StaleView(keeper, "run-a"))
    second = LogProbe(log, started=STARTED_13).agent_schedule(
        DAY, StaleView(store.Store(tmp_path / "state"), "run-b"))
    assert [first[0]["capture"]["status"], second[0]["capture"]["status"]] == [
        "captured", "already captured"]
    assert len(capture_files(keeper)) == 1
    altered_start(log)
    # v0.4.13: the invocation that detects the conflict fails closed itself, even when it
    # loaded a stale view; the stored contents are re-read under the lock.
    with pytest.raises(ProbeError, match="2 different startup captures"):
        LogProbe(log, started=STARTED_13).agent_schedule(DAY, StaleView(keeper, "run-c"))
    assert len(capture_files(keeper)) == 2
    with pytest.raises(ProbeError, match="2 different startup captures"):
        schedule_with(log, keeper, run="run-d")


def reseal(path, edit, rename=False):
    record = json.loads(path.read_text())
    edit(record)
    record.pop("integrity")
    target = path.with_name(f"{record['content_sha256']}.json") if rename else path
    path.chmod(0o640)
    if rename:
        path.unlink()
    target.write_text(json.dumps(store.seal(record)))
    target.chmod(0o440)


def drop_first_line(record):
    record["lines"] = record["lines"][1:]
    data = b"".join(base64.b64decode(item["bytes_base64"]) for item in record["lines"])
    record["content_sha256"] = hashlib.sha256(data).hexdigest()


def recollect(record):
    """A well-formed start whose collector line now says 61s, under the old content hash."""
    item = record["lines"][-1]
    data = base64.b64decode(item["bytes_base64"]).replace(b"every 60s", b"every 61s")
    item.update(bytes_base64=base64.b64encode(data).decode(), length=len(data))


@pytest.mark.parametrize("damage", ["truncated", "edited", "unreadable", "bytes-altered",
                                    "bytes-altered-validly", "rebound", "not-base64",
                                    "not-a-start", "misfiled"])
def test_a_damaged_capture_is_unknown_and_left_in_place(tmp_path, damage):
    log, keeper = capture_setup(tmp_path)
    schedule_with(log, keeper)
    rotate_start_away(log)
    (path,) = capture_files(keeper)
    path.chmod(0o640)
    if damage == "truncated":
        path.write_bytes(path.read_bytes()[:-40])
    elif damage == "edited":
        path.write_text(path.read_text().replace('"run-1"', '"run-9"'))
    path.chmod(0o440)
    if damage == "unreadable":
        path.chmod(0o000)
    elif damage == "bytes-altered":
        reseal(path, lambda r: r["lines"][0].update(
            bytes_base64=base64.b64encode(b"x\n").decode(), length=2))
    elif damage == "bytes-altered-validly":
        reseal(path, recollect)
    elif damage == "rebound":
        reseal(path, lambda r: r["binding"].update(main_pid=944))
    elif damage == "not-base64":
        reseal(path, lambda r: r["lines"][0].update(bytes_base64="***"))
    elif damage == "not-a-start":
        reseal(path, drop_first_line, rename=True)
    elif damage == "misfiled":
        path.rename(path.with_name("0" * 64 + ".json"))
    with pytest.raises(ProbeError, match="startup capture"):
        schedule_with(log, keeper, run="run-2")
    assert capture_files(keeper)
    for left in capture_files(keeper):
        left.chmod(0o440)


def test_an_interrupted_publication_is_never_evidence(tmp_path, monkeypatch):
    log, keeper = capture_setup(tmp_path)

    def no_link(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(store.os, "link", no_link)
    basis = checks.schedule_basis(schedule_with(log, keeper), DAY)
    monkeypatch.undo()
    # The live log established this run; nothing was kept for the next.
    assert basis["status"] == "ESTABLISHED"
    assert basis["startup_capture"]["status"] == "capture failed: OSError"
    assert capture_files(keeper) == []
    (folder,) = (keeper.root / "startup").iterdir()
    # No scratch file is left behind; only the binding's lock (v0.4.13) is there.
    assert [p.name for p in folder.iterdir()] == [store.STARTUP_CAPTURE_LOCK]
    # A scratch file a crash might leave between write and link is never read as a record.
    (folder / ".tmp-0123abcd").write_text('{"schema": "observer_startup_capture/1"')
    rotate_start_away(log)
    later = checks.schedule_basis(schedule_with(log, keeper, run="run-2"), DAY)
    assert later["status"] == "UNKNOWN" and "does not retain this scheduler's start" in later[
        "reason"]


@pytest.mark.parametrize("change", ["restart", "pid-reuse", "reboot", "log-basis"])
def test_a_kept_start_is_never_used_for_another_process_boot_or_basis(tmp_path, change):
    log, keeper = capture_setup(tmp_path)
    schedule_with(log, keeper)
    rotate_start_away(log)
    kw = {"restart": {"units": [{**START_13, "MainPID": "1200",
                                 "ExecMainStartTimestamp": "Wed 2026-10-14 07:00:00 IST",
                                 "ExecMainStartTimestampMonotonic": "99000000000"}],
                      "runtimes": [[(1200, API_ARGS)]]},
          "pid-reuse": {"units": [{**START_13, "ExecMainStartTimestampMonotonic": "9185000000"}]},
          "reboot": {"boots": [OTHER_BOOT]},
          "log-basis": {"basis": {**LOG_BASIS, "verified_at": "2026-10-17T00:00:00+05:30"}},
          }[change]
    basis = checks.schedule_basis(schedule_with(log, keeper, run="run-2", **kw), DAY)
    assert basis["status"] == "UNKNOWN" and "slots" not in basis
    assert "does not retain this scheduler's start" in basis["reason"]
    assert len(capture_files(keeper)) == 1                  # the old process's capture is kept


@pytest.mark.parametrize("phase", ["capture", "reuse"])
@pytest.mark.parametrize("change", ["boot", "monotonic"])
def test_a_runtime_change_during_capture_or_reuse_keeps_and_uses_nothing(tmp_path, phase,
                                                                          change):
    log, keeper = capture_setup(tmp_path)
    if phase == "reuse":
        schedule_with(log, keeper)
        rotate_start_away(log)
    files = capture_files(keeper)
    kw = ({"boots": [BOOT, OTHER_BOOT]} if change == "boot" else
          {"units": [START_13, {**START_13, "ExecMainStartTimestampMonotonic": "9185000000"}]})
    with pytest.raises(ProbeError, match="changed while the log was read"):
        schedule_with(log, keeper, run="run-x", **kw)
    assert capture_files(keeper) == files


@pytest.mark.parametrize("boot", [ProbeError("the boot id could not be read: absent"),
                                  "not-a-boot-id"], ids=["unreadable", "malformed"])
def test_without_a_boot_identity_nothing_is_read_or_kept(tmp_path, monkeypatch, boot):
    log, keeper = capture_setup(tmp_path)
    opened = count_opens(monkeypatch)
    probe = LogProbe(log, started=STARTED_13)
    if isinstance(boot, str):
        monkeypatch.setattr(probes, "BOOT_ID_PATH", str(write_rotation(
            tmp_path / "proc", {"backend.log": [boot + "\n"]})))
        probe.boot_id = types.MethodType(probes.Probe.boot_id, probe)
    else:
        probe.boots = [boot]
    with pytest.raises(ProbeError, match="boot id"):
        probe.agent_schedule(DAY, keeper.startup_captures("run-1"))
    assert [p for p in opened if p.startswith("backend.log")] == []
    assert capture_files(keeper) == []


@pytest.mark.parametrize("damage", ["startup-link", "binding-link", "group-writable",
                                    "record-link"])
def test_capture_storage_is_contained_and_owner_only(tmp_path, damage):
    log, keeper = capture_setup(tmp_path)
    schedule_with(log, keeper)
    rotate_start_away(log)
    startup = keeper.root / "startup"
    (folder,) = startup.iterdir()
    (path,) = folder.glob("*.json")
    elsewhere = tmp_path / "elsewhere"
    if damage == "startup-link":
        startup.rename(elsewhere)
        startup.symlink_to(elsewhere)
    elif damage == "binding-link":
        folder.rename(elsewhere)
        folder.symlink_to(elsewhere)
    elif damage == "group-writable":
        folder.chmod(0o770)
    else:
        path.rename(elsewhere)
        path.symlink_to(elsewhere)
    with pytest.raises(ProbeError, match="startup capture"):
        schedule_with(log, keeper, run="run-2")


def test_a_capture_is_never_written_through_a_link(tmp_path):
    log, keeper = capture_setup(tmp_path)
    keeper.root.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (keeper.root / "startup").symlink_to(elsewhere)
    # Refused when the kept captures are read, before any log is: nothing is
    # established, and nothing is written where the link points.
    with pytest.raises(ProbeError, match="without following a link"):
        schedule_with(log, keeper)
    assert list(elsewhere.iterdir()) == []
    # Publishing alone refuses the link too: it needs the binding's lock (v0.4.13), and the
    # lock is refused before anything is created.
    with pytest.raises(store.CorruptRecord, match="without following a link"), \
            keeper.startup_captures("run-1").lock({"any": "binding"}):
        pass
    assert list(elsewhere.iterdir()) == []


def test_a_kept_start_is_not_combined_with_retained_declarations(tmp_path):
    log, keeper = capture_setup(tmp_path)
    schedule_with(log, keeper)
    rotate_start_away(log)
    stray = log_line("08:21:00", "apscheduler.scheduler", 'Added job "tick" to job store "default"')
    log.write_text("".join([stray] + agent_runs()))
    with pytest.raises(ProbeError, match="not combined"):
        schedule_with(log, keeper, run="run-2")


def test_a_later_start_in_the_log_is_never_overridden_by_a_kept_one(tmp_path):
    log, keeper = capture_setup(tmp_path)
    schedule_with(log, keeper)
    rotate_start_away(log)
    log.write_text("".join(agent_runs() + startup_lines("12:00:00")))
    logged = schedule_with(log, keeper, run="run-2")
    assert [s["attribution"]["status"] for s in logged] == ["unattributed", "later"]
    assert all("capture" not in s for s in logged)
    assert checks.schedule_basis(logged, DAY)["status"] == "UNKNOWN"


def test_declarations_beyond_the_capture_bounds_are_not_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(probes, "CAPTURE_MAX_LINES", 3)
    log, keeper = capture_setup(tmp_path)
    basis = checks.schedule_basis(schedule_with(log, keeper), DAY)
    assert basis["status"] == "ESTABLISHED"
    assert basis["startup_capture"]["status"].startswith("not captured")
    assert capture_files(keeper) == []


def test_assessments_pass_their_own_capture_store_and_change_nothing_else(tmp_path):
    probe = FakeProbe()
    report = day_run(tmp_path, probe=probe)
    assert isinstance(probe.captures, store.StartupCaptures)
    assert probe.captures.run_id == report["report_id"]
    assert probe.captures.store.root == tmp_path / "records"
    assert not (tmp_path / "records" / "startup").exists()   # a fake probe keeps nothing
    assert report["outcome"] == day_run(tmp_path / "again")["outcome"]


# ===========================================================================
# v0.4.13 — contradictions, per-binding locking, contained storage, provenance
# ===========================================================================
@pytest.mark.parametrize("later", [
    startup_lines("16:30:00"),                                         # no session runs
    startup_lines("12:00:00") + agent_runs(init="12:00:00"),           # with session runs
    [startup_lines()[0].replace("2026-10-14 08:20:10", "2026-10-14 2:0:0", 1)],  # no time
], ids=["later-no-runs", "later-with-runs", "unreadable-time"])
def test_a_later_start_contradicts_the_current_one(tmp_path, later):
    lines = startup_lines() + agent_runs() + later
    logged = rotated(tmp_path, {"backend.log": lines}, started="Wed 2026-10-14 08:20:00 IST")
    assert logged[0]["attribution"]["status"] == "contradicted"
    basis = checks.schedule_basis(logged, DAY)
    assert basis["status"] == "UNKNOWN" and "slots" not in basis


def test_an_earlier_processes_start_does_not_contradict_the_current_one(tmp_path):
    earlier = on("2026-10-13", startup_lines("09:00:00"))           # yesterday's process
    logged = rotated(tmp_path, {"backend.log.1": earlier,
                                "backend.log": startup_lines() + agent_runs()},
                     started="Wed 2026-10-14 08:20:00 IST")
    assert [s["attribution"]["status"] for s in logged] == ["earlier", "current"]
    assert checks.schedule_basis(logged, DAY)["status"] == "ESTABLISHED"


def test_a_later_start_contradicts_a_kept_capture_too(tmp_path):
    log, keeper = capture_setup(tmp_path)
    schedule_with(log, keeper)
    rotate_start_away(log)
    log.write_text("".join(agent_runs() + startup_lines("16:30:00")))
    logged = schedule_with(log, keeper, run="run-2")
    assert checks.schedule_basis(logged, DAY)["status"] == "UNKNOWN"


def stored_binding(keeper) -> dict:
    (path,) = capture_files(keeper)
    return json.loads(path.read_text())["binding"]


def test_lock_contention_is_unknown_at_once_and_writes_nothing(tmp_path):
    log, keeper = capture_setup(tmp_path)
    schedule_with(log, keeper)
    binding, files = stored_binding(keeper), capture_files(keeper)
    altered_start(log)                       # a different start would otherwise be kept
    with store.Store(tmp_path / "state").startup_captures("holder").lock(binding):
        began = monotonic()
        with pytest.raises(ProbeError, match="holds the startup capture lock"):
            schedule_with(log, keeper, run="run-2")
        assert monotonic() - began < 2.0                         # no waiting, no retries
    assert capture_files(keeper) == files


def test_the_lock_is_held_through_the_whole_decision(tmp_path, monkeypatch):
    log, keeper = capture_setup(tmp_path)
    schedule_with(log, keeper)
    binding = stored_binding(keeper)
    real_parse, seen = probes.parse_scheduler_log, []

    def parse_while_held(lines, day, source=None):
        found = real_parse(lines, day, source)
        try:
            with store.Store(tmp_path / "state").startup_captures("other").lock(binding):
                seen.append("acquired")
        except store.CaptureContention:
            seen.append("contended")
        return found
    monkeypatch.setattr(probes, "parse_scheduler_log", parse_while_held)
    schedule_with(log, keeper, run="run-2")
    assert seen == ["contended"]


def test_a_killed_holder_releases_the_lock(tmp_path):
    log, keeper = capture_setup(tmp_path)
    schedule_with(log, keeper)
    binding = stored_binding(keeper)
    holder = subprocess.Popen(
        [sys.executable, "-I", "-c", textwrap.dedent(f"""
            import json, sys, time
            sys.path.insert(0, {str(OBSERVER)!r})
            from quantdesk_observer import store
            keeper = store.Store({str(tmp_path / "state")!r})
            with keeper.startup_captures("holder").lock(json.loads(sys.argv[1])):
                print("held", flush=True)
                time.sleep(60)
            """), json.dumps(binding)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(ProbeError, match="holds the startup capture lock"):
            schedule_with(log, keeper, run="run-2")
    finally:
        holder.kill()
        holder.wait()
    basis = checks.schedule_basis(schedule_with(log, keeper, run="run-3"), DAY)
    assert basis["status"] == "ESTABLISHED"
    assert basis["startup_capture"]["status"] == "already captured"


def test_the_state_root_under_a_symlinked_ancestor_is_refused(tmp_path):
    log, _ = capture_setup(tmp_path)
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    keeper = store.Store(tmp_path / "link" / "state")
    with pytest.raises(ProbeError, match="without following a link"):
        schedule_with(log, keeper)
    assert list((tmp_path / "real").rglob("*")) == []          # nothing written through it


@pytest.mark.parametrize("replace", ["binding", "startup", "root"])
def test_directory_replacement_during_publication_is_unknown(tmp_path, monkeypatch, replace):
    log, keeper = capture_setup(tmp_path)
    real_link = os.link
    target = {"binding": None, "startup": keeper.root / "startup", "root": keeper.root}[replace]

    def link_then_replace(*args, **kwargs):
        moving = target or next((keeper.root / "startup").iterdir())
        result = real_link(*args, **kwargs)
        moving.rename(moving.with_name(moving.name + "-moved"))
        moving.mkdir(mode=0o750)
        return result
    monkeypatch.setattr(store.os, "link", link_then_replace)
    with pytest.raises(ProbeError, match="could not be confirmed|changed|replaced"):
        schedule_with(log, keeper)


def test_publication_itself_rejects_a_replaced_directory(tmp_path, monkeypatch):
    log, keeper = capture_setup(tmp_path)
    schedule_with(log, keeper)
    (path,) = capture_files(keeper)
    record = {k: v for k, v in json.loads(path.read_text()).items()
              if k not in ("captured_at_utc", "captured_by_run", "observer_version", "integrity")}
    record["lines"] = record["lines"][:-1]           # different evidence, so a new file
    record["content_sha256"] = "e" * 64
    real_link = os.link

    def link_then_replace(*args, **kwargs):
        result = real_link(*args, **kwargs)
        path.parent.rename(path.parent.with_name(path.parent.name + "-moved"))
        path.parent.mkdir(mode=0o750)
        return result
    captures = keeper.startup_captures("run-2")
    with captures.lock(record["binding"]):
        monkeypatch.setattr(store.os, "link", link_then_replace)
        with pytest.raises(store.CorruptRecord, match="replaced|changed"):
            captures.publish(record)


def test_capture_work_writes_only_inside_the_validated_state(tmp_path):
    log, keeper = capture_setup(tmp_path)

    def outside():
        return {p: (p.read_bytes(), p.stat().st_mtime_ns) if p.is_file() else None
                for p in tmp_path.rglob("*") if keeper.root not in p.parents and p != keeper.root}
    before = outside()
    schedule_with(log, keeper)
    assert outside() == before                         # capturing wrote only under the root
    rotate_start_away(log)
    before = outside()
    schedule_with(log, keeper, run="run-2")
    assert outside() == before                         # so did reusing
    for path in keeper.root.rglob("*"):
        assert path.stat().st_mode & 0o022 == 0 and not path.is_symlink()


def strip(field):
    return lambda record: record.pop(field)


@pytest.mark.parametrize("edit", [
    strip("captured_at_utc"), strip("captured_by_run"), strip("observer_version"),
    lambda r: r.update(captured_at_utc="2026-10-16T00:00:00"),
    lambda r: r.update(captured_by_run=""),
    lambda r: r.update(captured_by_run="run 1; rm"),
    lambda r: r.update(observer_version="0.4.11"),
    lambda r: r.update(observer_version="9.9.9"),
    lambda r: r.update(observer_version="latest"),
    lambda r: r.update(extra="field"),
    lambda r: r["binding"].pop("api_pid"),
    lambda r: r["binding"].update(boot_id="not-a-boot"),
    lambda r: r["lines"][0].pop("inode"),
    lambda r: r["lines"][0].update(source="other.log"),
    lambda r: r["lines"][0].update(source="backend.log.15.gz"),
    lambda r: r["lines"][1].update(offset=0),
    lambda r: r["lines"][1].update(inode=r["lines"][1]["inode"] + 1),
    lambda r: r["lines"][-1].update(source="backend.log.2"),
], ids=["no-captured-at", "no-run", "no-version", "naive-captured-at", "empty-run", "bad-run",
        "version-too-old", "version-from-the-future", "version-not-a-version", "extra-field",
        "binding-incomplete", "binding-boot", "line-incomplete", "foreign-source",
        "unsupported-rotation", "overlapping-offset", "inode-changes-within-a-file",
        "out-of-log-order"])
def test_a_resealed_capture_with_bad_provenance_is_unknown(tmp_path, edit):
    log, keeper = capture_setup(tmp_path)
    schedule_with(log, keeper)
    rotate_start_away(log)
    (path,) = capture_files(keeper)
    reseal(path, edit)                      # its own content hash is valid again
    store.read_record(path, store.STARTUP_CAPTURE_SCHEMA)
    with pytest.raises(ProbeError, match="startup capture"):
        schedule_with(log, keeper, run="run-2")
    assert capture_files(keeper) == [path]                   # left as it is


def test_a_capture_written_by_v0_4_12_is_still_used(tmp_path):
    log, keeper = capture_setup(tmp_path)
    schedule_with(log, keeper)
    rotate_start_away(log)
    (path,) = capture_files(keeper)
    reseal(path, lambda r: r.update(observer_version="0.4.12"))
    basis = checks.schedule_basis(schedule_with(log, keeper, run="run-2"), DAY)
    assert basis["status"] == "ESTABLISHED" and basis["startup_capture"]["status"] == "reused"


# ===========================================================================
# High B — the expiry_day trigger
# ===========================================================================
def test_valid_expiry_day_pass_is_reported_with_its_downstream_stages(tmp_path):
    signals, decisions = with_gate({40: vix_stop()})
    report = day_run(tmp_path, probe=FakeProbe(signals=signals, decisions=decisions))
    (found,) = report["expiry_day_passed_candidates"]
    assert found["valid_early_review_trigger"] and found["signal_id"] == 3040
    assert found["stages"]["feed"] == "PASSED" and found["stages"]["levels_vs_spot"] == "PASSED"
    assert found["stages"]["vix"] == "REJECTED"
    assert found["stages"]["contract_selection"] == "NOT REACHED"
    assert found["stages"]["paper_position"] == "NOT REACHED"
    assert found["contract_evidence"]["status"] == "selection_not_reached"
    assert found["bid_parser_evidence"].startswith("NOT ADVANCED")
    assert report["decision_funnel"]["furthest_gate_reached"] == "vix"
    assert report["decision_funnel"]["selector_attempts"] == 0
    state = phase(tmp_path)
    assert state["phase"] == "REVIEW PENDING" and state["reasons"] == ["EARLY_REVIEW"]


def contradictory(d):
    """The original defect: rejected at entry state, yet a later gate reads passed."""
    rows = d["evidence"]["vector"]["decision_context"]["gate_trace"]
    for row in rows:
        if row["gate"] in ("signal.bias", "position", "kill_switch", "entry_window",
                           "instrument_master", "expiry_day"):
            row["status"] = "passed"


def duplicated(d):
    rows = d["evidence"]["vector"]["decision_context"]["gate_trace"]
    rows.append({"gate": "expiry_day", "enabled": True, "status": "passed"})


def truncated(d):
    context = d["evidence"]["vector"]["decision_context"]
    context["gate_trace"] = [{"gate": "expiry_day", "enabled": True, "status": "passed"}]


def reordered(d):
    rows = d["evidence"]["vector"]["decision_context"]["gate_trace"]
    rows[0], rows[1] = rows[1], rows[0]


def wrong_code(d):
    d["code"] = "entry_state_not_ready"


def no_failure(d):
    for row in d["evidence"]["vector"]["decision_context"]["gate_trace"]:
        row.pop("code", None)
        row["status"] = "passed"


@pytest.mark.parametrize("damage, start", [
    (contradictory, "entry"), (duplicated, "vix"), (truncated, "vix"), (reordered, "vix"),
    (wrong_code, "vix"), (no_failure, "vix"),
])
def test_high_b_an_inconsistent_trace_is_never_an_expiry_day_pass(tmp_path, damage, start):
    signals, decisions = session()
    if start == "vix":
        decisions[40] = decision(signals[40], *vix_stop())
    damage(decisions[40])
    assert checks.claims_expiry_day_pass(decisions[40])   # the trace does say "passed"
    assert checks.trace_problems(decisions[40])
    assert not checks.expiry_day_passed(decisions[40])
    report = day_run(tmp_path, probe=FakeProbe(signals=signals, decisions=decisions))
    (found,) = report["expiry_day_passed_candidates"]
    assert not found["valid_early_review_trigger"] and found["not_valid_because"]
    assert set(found["stages"].values()) == {"UNKNOWN"}
    assert report["valid_early_review_candidates"] == 0
    assert named(report, "gate traces internally consistent")["status"] == "FAIL"
    assert report["outcome"] == "INCOMPLETE" and phase(tmp_path)["phase"] == "BOUNDED OBSERVATION"


@pytest.mark.parametrize("gate", [
    {"gate": "expiry_day", "enabled": True, "status": "not_reached"},
    {"gate": "expiry_day", "enabled": True, "status": "failed", "code": "expiry_day"},
    {"gate": "expiry_day", "enabled": False, "status": "passed"},
    {"gate": "expiry_day", "enabled": False, "status": "disabled"},
    {"gate": "expiry_day", "enabled": True, "status": "unknown"},
    {"gate": "expiry_day", "status": "passed"},
    {"gate": "expiry_day", "enabled": "true", "status": "passed"},
])
def test_anything_short_of_an_enabled_pass_does_not_qualify(gate):
    signals, _ = session(n=1)
    rows = trace("vix", "vix_too_high")
    rows[checks.GATE_ORDER.index("expiry_day")] = gate
    assert not checks.expiry_day_passed(decision(signals[0], rows, "vix_too_high"))


@pytest.mark.parametrize("damage", [
    lambda d, s: d["evidence"].update(status="partial"),
    lambda d, s: d["evidence"].update(schema="v2_decision_evidence/1"),
    lambda d, s: d["evidence"]["vector"]["identity"].update(linkage="unlinked"),
    lambda d, s: d["evidence"]["vector"]["identity"].update(signal_id=1),
    lambda d, s: d["evidence"]["vector"]["versions"].update(v2_config_hash="changed"),
    lambda d, s: s.update(code_id="aaaaaaaaaaaa"),
    lambda d, s: s.update(parameter_hash="other"),
])
def test_a_pass_on_incomplete_or_changed_evidence_is_not_a_trigger(tmp_path, damage):
    signals, decisions = with_gate({40: vix_stop()})
    damage(decisions[40], signals[40])
    report = day_run(tmp_path, probe=FakeProbe(signals=signals, decisions=decisions))
    assert report["valid_early_review_candidates"] == 0 and report["outcome"] == "INCOMPLETE"
    assert phase(tmp_path)["phase"] == "BOUNDED OBSERVATION"


def test_early_trigger_with_failed_readiness_is_review_pending_but_not_counted(tmp_path):
    signals, decisions = with_gate({40: vix_stop()})
    report = day_run(tmp_path, probe=FakeProbe(signals=signals, decisions=decisions),
                     premarket="09:03:27")
    assert report["readiness_0845"] == "MISSING"
    assert report["outcome"] == "OPERATIONAL EXCEPTION REQUIRED"
    assert report["bounded_phase"]["phase"] == "REVIEW PENDING"
    assert report["attention_required"] and report["attention_reasons"] == ["EARLY_REVIEW"]
    state = phase(tmp_path)
    assert state["phase"] == "REVIEW PENDING" and state["reasons"] == ["EARLY_REVIEW"]
    assert state["qualifying_sessions"] == []       # reviewing it is not counting it
    assert "early_review_trigger" in event_types(tmp_path)


def test_medium_e_stored_contract_evidence_is_reported_as_stored(tmp_path):
    signals, decisions = with_gate({40: (trace("risk_manager", "risk_manager_vetoed"),
                                         "risk_manager_vetoed")})
    stored = decisions[40]["evidence"]["contract_evidence"]
    assert stored["status"] == "selection_reached" and stored["universe"]["quotes"][0]["bid"]
    report = day_run(tmp_path, probe=FakeProbe(signals=signals, decisions=decisions))
    (found,) = report["expiry_day_passed_candidates"]
    assert found["contract_evidence"] == stored
    assert found["stages"]["contract_selection"] == "PASSED"
    assert found["stages"]["risk_manager"] == "REJECTED"
    assert report["decision_funnel"]["selector_attempts"] == 1
    assert found["bid_parser_evidence"].startswith("NOT ADVANCED")
    del decisions[40]["evidence"]["contract_evidence"]
    (missing,) = checks.candidates(signals, decisions, None, config(tmp_path / "x"))
    assert missing["contract_evidence"] == "UNKNOWN"   # nothing is made up for it
    assert not missing["valid_early_review_trigger"]


# ===========================================================================
# 16:00 after-close: outcomes and Astra's policies
# ===========================================================================
def test_qualified_session(tmp_path):
    report = day_run(tmp_path)
    assert report["outcome"] == "QUALIFIED" and report["result"] == "PASS"
    assert report["readiness_0845"] == "PASS" and report["midsession_1230"] == "PASS"
    cover = named(report, "collection coverage")["evidence"]
    assert (cover["expected"], cover["actual"], cover["signal_ids"]) == (75, 75, [3000, 3074])
    chain = named(report, "option chain enabled")["evidence"]
    assert (chain["satisfied"], chain["neutral"], chain["positive"]) == (75, 75, 0)
    assert report["decision_funnel"]["decision_codes"] == {"entry_state_not_ready": 75}
    assert report["decision_funnel"]["furthest_gate_reached"] == "signal.entry_state"
    assert report["unknown_fields"] == [] and records(tmp_path).events() == []
    assert phase(tmp_path)["qualifying_sessions"] == ["2026-10-14"]


def test_option_chain_unavailability_is_recorded_and_makes_the_session_incomplete(tmp_path):
    signals, decisions = session()
    signals[3]["checks"][5].update(disabled=True, reason="Option chain unavailable: stale")
    signals[9]["oi_status"] = "unavailable"
    report = day_run(tmp_path, probe=FakeProbe(signals=signals, decisions=decisions))
    chain = named(report, "option chain enabled")
    assert chain["status"] == "FAIL" and report["outcome"] == "INCOMPLETE"
    assert [(f["signal_id"], f["failed_requirement"]) for f in chain["evidence"][
        "not_satisfied"]] == [(3003, "check not enabled"), (3009, "OI unavailable")]
    assert chain["evidence"]["not_satisfied"][0]["stored_reason"].startswith("Option chain")
    assert any("option chain" in r for r in report["failure_reasons"])


def test_vix_close_with_delayed_storage_is_accepted(tmp_path):
    probe = FakeProbe()
    probe.facts["vix"]["ingested_at"] = at("18:42:00").isoformat()
    report = day_run(tmp_path, probe=probe)
    vix = named(report, "session VIX close")
    assert vix["status"] == "PASS" and report["outcome"] == "QUALIFIED"
    assert vix["evidence"]["market_observation_time"].startswith("UNKNOWN")
    assert vix["evidence"]["database_storage_time_ist"].startswith("2026-10-14T18:42")


@pytest.mark.parametrize("change, status", [
    ({"source": "nse_archive"}, "UNKNOWN"),
    ({"ingested_at": at("15:10:00").isoformat()}, "UNKNOWN"),
    ({"ingested_at": at("09:00:00", DAY + timedelta(days=1)).isoformat()}, "UNKNOWN"),
    ({"ingested_at": None}, "UNKNOWN"),
    ({"close": 0}, "FAIL"), ({"close": float("nan")}, "FAIL"), ({"close": None}, "FAIL"),
])
def test_vix_record_that_cannot_be_established_as_the_close(tmp_path, change, status):
    probe = FakeProbe()
    probe.facts["vix"].update(change)
    report = day_run(tmp_path, probe=probe)
    assert named(report, "session VIX close")["status"] == status
    assert report["outcome"] == "INCOMPLETE"


def test_missing_vix_close_fails(tmp_path):
    report = day_run(tmp_path, probe=FakeProbe(vix=None))
    assert named(report, "session VIX close")["status"] == "FAIL"
    assert report["outcome"] == "INCOMPLETE"


def test_no_storage_time_window_remains_in_the_rules():
    source = (OBSERVER / "quantdesk_observer/checks.py").read_text() \
        + (OBSERVER / "quantdesk_observer/cli.py").read_text()
    assert "vix_close_early_seconds" not in source and "vix_close_late_seconds" not in source
    assert "15:45" not in source and "15:29" not in source


@pytest.mark.parametrize("fact, value, check", [
    ("archive", {"rows": 100, "with_exchange_time": 90, "exchange_after_capture": 0,
                 "exchange_other_date": 0}, "NSE archive timestamps"),
    ("/health", {"status": "ok", "live_trading": True, "redis": True}, "LIVE_TRADING=false"),
])
def test_a_failed_evidence_gate_is_incomplete(tmp_path, fact, value, check):
    probe = FakeProbe()
    run("premarket", tmp_path, probe)
    probe.facts[fact] = value
    report, _ = run("afterclose", tmp_path, probe, "16:00:00")
    assert report["outcome"] == "INCOMPLETE" and named(report, check)["status"] == "FAIL"


@pytest.mark.parametrize("change, check", [
    (lambda s: s[3].update(code_id="aaaaaaaaaaaa"), "producer revision"),
    (lambda s: s[3].update(parameter_hash="other"), "configuration identity"),
    (lambda s: s[3].update(confidence=0.9), "score accounting"),
    (lambda s: s[3].update(action="HOLD"), "score accounting"),
    (lambda s: s.pop(20), "collection coverage"),
])
def test_each_observation_level_gate_is_enforced(tmp_path, change, check):
    signals, decisions = session()
    change(signals)
    report = day_run(tmp_path, probe=FakeProbe(signals=signals, decisions=decisions))
    assert named(report, check)["status"] == "FAIL" and report["outcome"] == "INCOMPLETE"


def sell_side(signals, decisions, sign=-1.0):
    for s in signals:
        for c in s["checks"]:
            c["score"] *= sign
            c["contribution"] *= sign
        s["action"] = "SELL"
    for d in decisions:
        d["action"] = "SELL"


def test_medium_d_a_positive_score_is_not_consistent_with_sell(tmp_path):
    signals, decisions = session()
    sell_side(signals, decisions)
    assert checks.score_accounting(signals, decisions)["status"] == "PASS"   # genuine SELL
    signals, decisions = session()
    sell_side(signals, decisions, sign=1.0)   # the original defect: SELL on a positive score
    result = checks.score_accounting(signals, decisions)
    assert result["status"] == "FAIL"
    assert "signed score gives BUY" in result["evidence"]["unreconciled"][3000]


@pytest.mark.parametrize("field, value", [
    ("confidence", float("nan")), ("confidence", float("inf")), ("threshold_used", None),
    ("threshold_used", float("nan")), ("confidence", "0.38"), ("confidence", True),
])
def test_medium_d_non_finite_accounting_inputs_are_rejected(field, value):
    signals, decisions = session(n=3)
    signals[1][field] = value
    result = checks.score_accounting(signals, decisions)
    assert result["status"] == "FAIL" and 3001 in result["evidence"]["unreconciled"]
    signals, decisions = session(n=3)
    signals[1]["checks"][0]["score"] = float("nan")
    assert checks.score_accounting(signals, decisions)["status"] == "FAIL"


def test_readiness_failure_with_clean_data_needs_an_operational_exception(tmp_path):
    report = day_run(tmp_path, premarket="08:44:40", premarket_step=5.0)  # runs past 08:45
    assert report["readiness_0845"] == "FAIL" and report["collection_gates_passed"]
    assert report["outcome"] == "OPERATIONAL EXCEPTION REQUIRED"
    assert phase(tmp_path)["qualifying_sessions"] == []


def test_unknown_evidence_is_kept_unknown_and_never_qualifies(tmp_path):
    probe = FakeProbe()
    run("premarket", tmp_path, probe)
    probe.facts["archive"] = ProbeError("psql did not answer within 30s")
    report, _ = run("afterclose", tmp_path, probe, "16:00:00")
    assert named(report, "NSE archive timestamps")["status"] == "UNKNOWN"
    assert "NSE archive timestamps" in report["unknown_fields"]
    assert report["outcome"] == "INCOMPLETE"


def test_unreadable_database_is_unknown_not_empty(tmp_path):
    probe = FakeProbe(signals=ProbeError("psql exited 2"), decisions=ProbeError("psql exited 2"))
    report, _ = run("afterclose", tmp_path, probe, "16:00:00")
    assert named(report, "collection coverage")["status"] == "UNKNOWN"
    assert report["expiry_day_passed_candidates"] == "UNKNOWN"
    assert report["decision_funnel"] == "UNKNOWN" and report["outcome"] == "INCOMPLETE"


# --- High A: paper activity -------------------------------------------------
def test_high_a_unreadable_positions_cannot_qualify_or_set_a_baseline(tmp_path):
    """The original defect: every other gate passes and the position read fails."""
    probe = FakeProbe()
    run("premarket", tmp_path, probe)
    run("midsession", tmp_path, midday(), "12:30:00")
    probe.facts["positions"] = ProbeError("psql exited 2")
    report, _ = run("afterclose", tmp_path, probe, "16:00:00")
    paper = named(report, "paper activity reconciliation")
    assert paper["status"] == "UNKNOWN" and paper["required"]
    assert "paper activity reconciliation" in report["unknown_fields"]
    assert report["outcome"] == "INCOMPLETE" and report["result"] == "FAIL"
    assert report["paper_positions"] == "UNKNOWN" and report["baseline"] is None
    assert phase(tmp_path)["qualifying_sessions"] == []
    nxt = DAY + timedelta(days=1)
    baseline = cli.trusted_baseline(records(tmp_path), config(tmp_path), nxt.isoformat())
    assert baseline["from_session"] == "configuration"   # the unreadable day set none


def test_high_a_unreadable_journal_or_decisions_also_withhold_qualification(tmp_path):
    for i, (fact, expected) in enumerate((("journal", "INCOMPLETE"),
                                          ("decisions", "INCOMPLETE"))):
        root = tmp_path / str(i)
        probe = FakeProbe()
        run("premarket", root, probe)
        probe.facts[fact] = ProbeError("psql exited 2")
        report, _ = run("afterclose", root, probe, "16:00:00")
        assert named(report, "paper activity reconciliation")["status"] == "UNKNOWN"
        assert report["outcome"] == expected and report["baseline"] is None


def natural_entry(day=DAY):
    signals, decisions = session(day)
    decisions[40] = decision(signals[40], trace(None), code="entered", outcome="entered")
    decisions[40]["evidence"]["vector"]["decision_context"]["position_id"] = 2
    stored = {"id": 2, "status": "closed", "session_date": day.isoformat()}
    return signals, decisions, stored


def test_high_a_natural_paper_activity_is_reconciled_not_rejected(tmp_path):
    signals, decisions, stored = natural_entry()
    probe = FakeProbe(signals=signals, decisions=decisions)
    run("premarket", tmp_path, probe)
    run("midsession", tmp_path, midday(), "12:30:00")
    probe.facts["positions"] = {"total": 2, "open": 0, "max_id": 2, "session": [stored]}
    report, _ = run("afterclose", tmp_path, probe, "16:00:00")
    assert named(report, "paper activity reconciliation")["status"] == "PASS"
    assert report["outcome"] == "QUALIFIED"
    (found,) = report["expiry_day_passed_candidates"]
    assert found["stages"]["risk_manager"] == "PASSED" and found["stored_position"] == stored
    assert found["stages"]["paper_position"] == "PASSED"
    assert found["stages"]["submission_fill"] == "UNKNOWN"
    assert report["baseline"] == {"positions_total": 2, "journal_total": 1}
    nxt = DAY + timedelta(days=1)   # the new counts are the next day's baseline
    probe2 = FakeProbe(nxt, positions={"total": 2, "open": 0, "max_id": 2, "session": []})
    assert run("premarket", tmp_path, probe2, day=nxt)[0]["result"] == "PASS"


@pytest.mark.parametrize("positions, journal", [
    ({"total": 2, "open": 0, "max_id": 2, "session": []}, {"total": 1}),        # appeared
    ({"total": 2, "open": 0, "max_id": 2, "session": [{"id": 2}]}, {"total": 1}),  # no decision
    ({"total": 1, "open": 0, "max_id": 1, "session": []}, {"total": 2}),        # journal moved
    ({"total": 1, "open": 1, "max_id": 1, "session": []}, {"total": 1}),        # engine disagrees
])
def test_high_a_unexplained_paper_activity_is_incomplete(tmp_path, positions, journal):
    probe = FakeProbe()
    run("premarket", tmp_path, probe)
    probe.facts.update(positions=positions, journal=journal)
    report, _ = run("afterclose", tmp_path, probe, "16:00:00")
    assert named(report, "paper activity reconciliation")["status"] == "FAIL"
    assert report["outcome"] == "INCOMPLETE" and report["baseline"] is None


def test_high_a_no_trusted_baseline_is_unknown(tmp_path):
    cfg = config(tmp_path, baseline={"positions_total": None, "journal_total": None})
    report = day_run(tmp_path, cfg=cfg)
    assert named(report, "paper activity reconciliation")["status"] == "UNKNOWN"
    assert report["outcome"] == "INCOMPLETE"


# --- feed continuity and counters (medium C, policy 7) ------------------------
def after_with(tmp_path, mutate, premarket=True, name="x"):
    root = tmp_path / name
    probe = FakeProbe()
    if premarket:
        run("premarket", root, probe)
    run("midsession", root, midday(), "12:30:00")
    mutate(probe.facts)
    report, _ = run("afterclose", root, probe, "16:00:00")
    return report


def test_unplaceable_feed_event_is_unknown_and_needs_an_operational_exception(tmp_path):
    def stale(facts):
        facts["/health/feed"]["angel"]["counters"]["stale_events"] = 1
    report = after_with(tmp_path, stale)
    cont = named(report, "operational continuity")
    assert cont["status"] == "UNKNOWN" and "cannot place" in cont["detail"]
    assert cont["evidence"]["deltas"]["stale_events"] == 1
    assert report["collection_gates_passed"]
    assert report["outcome"] == "OPERATIONAL EXCEPTION REQUIRED"


def test_lifetime_reconnects_and_earlier_errors_are_not_a_qualification_rule(tmp_path):
    probe = FakeProbe()
    probe.facts["/health/feed"]["angel"]["counters"].update(reconnects=40, stale_events=3)
    probe.facts["/health/scheduler"]["jobs"]["nifty-agent"]["errors"] = 5
    report = day_run(tmp_path, probe=probe)   # the pre-market snapshot holds the same values
    assert named(report, "operational continuity")["status"] == "PASS"
    assert report["outcome"] == "QUALIFIED"


@pytest.mark.parametrize("mutate, phrase", [
    (lambda f: f["/health/scheduler"]["jobs"].pop("option-collector"), "not reported"),
    (lambda f: f["/health/scheduler"]["jobs"]["price-ticker"].pop("missed"), "missing"),
    (lambda f: f["/health/feed"]["angel"]["counters"].pop("stale_events"), "missing"),
    (lambda f: f["/health/feed"]["angel"]["counters"].update(malformed=None), "missing"),
    (lambda f: f["unit"].update(MainPID="2222"), "restart"),
    (lambda f: f["/health/scheduler"].update(counting_since=at("11:02:00").isoformat()),
     "restart"),
    (lambda f: f["/health/feed"]["angel"].update(started_at=None), "identity"),
])
def test_medium_c_missing_reset_or_incomparable_counters_are_unknown(tmp_path, mutate, phrase):
    report = after_with(tmp_path, mutate)
    cont = named(report, "operational continuity")
    assert cont["status"] == "UNKNOWN" and phrase in cont["detail"]
    assert report["outcome"] in ("OPERATIONAL EXCEPTION REQUIRED", "INCOMPLETE")
    assert report["outcome"] != "QUALIFIED"


def test_medium_c_a_counter_reset_is_unknown_not_a_negative_delta(tmp_path):
    probe = FakeProbe()
    probe.facts["/health/scheduler"]["jobs"]["nifty-agent"]["errors"] = 4
    run("premarket", tmp_path, probe)
    run("midsession", tmp_path, midday(), "12:30:00")
    probe.facts["/health/scheduler"]["jobs"]["nifty-agent"]["errors"] = 0
    report, _ = run("afterclose", tmp_path, probe, "16:00:00")
    cont = named(report, "operational continuity")
    assert cont["status"] == "UNKNOWN" and "reset" in cont["detail"]


def test_medium_c_a_missing_counter_is_unknown_at_premarket_not_zero(tmp_path):
    probe = FakeProbe()
    probe.facts["/health/scheduler"]["jobs"]["nifty-agent"].pop("errors")
    report, _ = run("premarket", tmp_path, probe)
    assert named(report, "signal agent healthy")["status"] == "UNKNOWN"
    probe = FakeProbe()
    probe.facts["/health/scheduler"]["jobs"].pop("price-ticker")
    report, _ = run("premarket", tmp_path / "b", probe)
    assert named(report, "price ticker healthy")["status"] == "UNKNOWN"
    assert report["result"] == "FAIL"


def test_api_running_since_an_earlier_day_needs_a_premarket_snapshot(tmp_path):
    def older(facts):
        facts["/health/scheduler"]["counting_since"] = at("09:03", DAY - timedelta(days=2)) \
            .isoformat()
    report = after_with(tmp_path, older, premarket=False, name="none")
    assert named(report, "operational continuity")["status"] == "UNKNOWN"
    probe = FakeProbe()
    older(probe.facts)
    probe.facts["/health/feed"]["angel"]["started_at"] = at("09:03", DAY - timedelta(days=2)) \
        .isoformat()
    report = day_run(tmp_path / "snap", probe=probe)
    assert named(report, "operational continuity")["status"] == "PASS"


def test_a_restart_during_collection_is_recorded_for_review_not_called_a_gap(tmp_path):
    signals, decisions = session()
    restarted = at("11:40:10").isoformat()   # same five-minute phase, a second process
    for s in signals[32:]:
        s["api_init"] = {**s["api_init"], "pid": 2222, "initialized_at": restarted}
    report = day_run(tmp_path, probe=FakeProbe(signals=signals, decisions=decisions))
    runtime = named(report, "single runtime during collection")
    assert named(report, "collection coverage")["status"] == "PASS"
    assert runtime["status"] == "FAIL" and "need review" in runtime["detail"]
    assert [r["pid"] for r in runtime["evidence"]["runtimes"]] == [2222, 943]
    assert report["collection_gates_passed"]
    assert report["outcome"] == "OPERATIONAL EXCEPTION REQUIRED"


# --- the missing 12:30 assessment (policy 8) ---------------------------------
def test_missing_1230_is_not_replaced_by_counters_or_a_single_api_start(tmp_path):
    """Codex's finding: unchanged counters plus one API start let the session qualify."""
    report = day_run(tmp_path, mid=False)
    assert report["midsession_1230"] == "MISSING"
    assert named(report, "operational continuity")["status"] == "PASS"     # counters unchanged
    assert named(report, "single runtime during collection")["status"] == "PASS"
    facts = named(report, "mid-session operational facts")
    assert facts["status"] == "UNKNOWN" and not facts["evidence"]["established"]
    for fact in ("PostgreSQL responsiveness", "Redis responsiveness", "Angel feed readiness",
                 "option-stream readiness", "absence of a duplicate runtime"):
        assert fact in facts["detail"]
    assert report["collection_gates_passed"]
    assert report["outcome"] == "OPERATIONAL EXCEPTION REQUIRED"
    assert ("missing_check", "midsession") in events(tmp_path)
    late, _ = run("midsession", tmp_path, midday(n=57), "14:00:00")
    assert not late["timely"]
    assert cli.check_status(records(tmp_path), DAY.isoformat(), "midsession") == "MISSING"


def test_missing_1230_without_equivalent_evidence_needs_an_operational_exception(tmp_path):
    probe = FakeProbe()
    run("premarket", tmp_path, probe)
    probe.facts["/health/feed"]["angel"]["counters"]["stale_events"] = 1
    report, _ = run("afterclose", tmp_path, probe, "16:00:00")
    assert report["midsession_1230"] == "MISSING"
    assert named(report, "mid-session operational facts")["status"] == "UNKNOWN"
    assert report["collection_gates_passed"]
    assert report["outcome"] == "OPERATIONAL EXCEPTION REQUIRED"


def test_a_failed_1230_assessment_is_not_overridden_by_later_evidence(tmp_path):
    probe = FakeProbe()
    run("premarket", tmp_path, probe)
    sick = midday()
    sick.facts["/health/feed"]["angel"].update(state="stale", healthy=False)
    run("midsession", tmp_path, sick, "12:30:00")
    report, _ = run("afterclose", tmp_path, probe, "16:00:00")
    assert report["midsession_1230"] == "FAIL"
    assert named(report, "mid-session operational facts")["status"] == "FAIL"
    assert report["outcome"] == "OPERATIONAL EXCEPTION REQUIRED"


def test_reconstruction_on_a_later_day_cannot_establish_operational_facts(tmp_path):
    report, _ = run("afterclose", tmp_path, FakeProbe(), "07:30:00", DAY + timedelta(days=1),
                    session_date=DAY.isoformat())
    assert named(report, "collection coverage")["status"] == "PASS"
    assert named(report, "operational continuity")["status"] == "UNKNOWN"
    assert named(report, "LIVE_TRADING=false")["status"] == "UNKNOWN"
    assert report["collection_gates_passed"] and report["maintenance_events"] == "UNKNOWN"
    assert report["outcome"] == "OPERATIONAL EXCEPTION REQUIRED"
    assert report["session_date"] == "2026-10-14" and not report["timely"]


# ===========================================================================
# High E — the calendar
# ===========================================================================
def test_high_e_an_unreviewed_calendar_cannot_establish_qualification(tmp_path):
    cfg = config(tmp_path, reviewed=False)
    report = day_run(tmp_path, cfg=cfg)
    listing = named(report, "session date confirmed")
    assert listing["status"] == "UNKNOWN" and "not been reviewed" in listing["detail"]
    assert listing["evidence"]["provisional_listing"] == "NORMAL"
    assert report["collection_gates_passed"]
    assert report["outcome"] == "OPERATIONAL EXCEPTION REQUIRED"
    state = phase(tmp_path, cfg, at("23:00", date(2026, 11, 30)))
    assert state["window"]["status"] == "UNKNOWN" and state["phase"] == "BOUNDED OBSERVATION"
    assert "2026-10-14" in state["calendar_uncertainty"]   # the first date it cannot resolve


def test_high_e_a_provisional_holiday_does_not_hide_real_observations(tmp_path):
    """The original defect: the calendar said closed, so 75 observations were dropped."""
    cfg = config(tmp_path, reviewed=False, closed=[DAY])
    report = day_run(tmp_path, cfg=cfg)
    assert report["result"] != "NO SESSION" and report["ran_checks"]
    assert named(report, "collection coverage")["evidence"]["actual"] == 75
    assert report["outcome"] == "OPERATIONAL EXCEPTION REQUIRED"


def test_high_e_a_failed_read_never_becomes_no_session(tmp_path):
    cfg = config(tmp_path, closed=[DAY])
    for check, start in (("premarket", "08:35:00"), ("afterclose", "16:00:00")):
        report, _ = run(check, tmp_path, FakeProbe(signals=ProbeError("psql exited 2")), start,
                        cfg=cfg)
        assert report["result"] == "FAIL" and "could not be confirmed" in \
            report["failure_reasons"][0]
        assert report["unknown_fields"] == ["activity on a listed non-session day"]
    report, _ = run("afterclose", tmp_path, FakeProbe(positions=ProbeError("x"), signals=[]),
                    "16:05:00", cfg=cfg)
    assert report["result"] == "FAIL"


def test_no_session_needs_a_reviewed_closed_date_successful_reads_and_no_activity(tmp_path):
    cfg = config(tmp_path, closed=[DAY])
    quiet = FakeProbe(signals=[], decisions=[])
    for check, start in (("premarket", "08:35:00"), ("midsession", "12:30:00"),
                         ("afterclose", "16:00:00")):
        report, _ = run(check, tmp_path, quiet, start, cfg=cfg)
        assert report["result"] == "NO SESSION" and "outcome" not in report
        assert report["session_calendar"]["status"] == "CLOSED" and not report["ran_checks"]
    assert records(tmp_path).events() == [] and phase(tmp_path, cfg)["sessions"] == {}


def test_activity_on_a_reviewed_closed_date_is_preserved_and_flagged(tmp_path):
    cfg = config(tmp_path, closed=[DAY])
    report, _ = run("afterclose", tmp_path, FakeProbe(), "16:00:00", cfg=cfg)
    assert report["result"] == "FAIL"
    assert report["unexpected_activity"]["observations"] == 75
    assert report["unexpected_activity"]["signal_ids"][0] == 3000
    assert events(tmp_path) == {("evidence_inconsistency", "non-session-activity")}


def test_unexpected_weekend_observations_are_preserved_and_flagged(tmp_path):
    saturday = date(2026, 10, 17)
    assert saturday.weekday() == 5
    report, _ = run("afterclose", tmp_path, FakeProbe(saturday), "16:00:00", saturday)
    assert report["result"] == "FAIL" and report["unexpected_activity"]["observations"] == 75
    monday = date(2026, 10, 19)
    probe = FakeProbe(monday, signal_days=[{"day": "2026-10-18", "observations": 12},
                                           {"day": "2026-10-16", "observations": 75}])
    report = day_run(tmp_path / "scan", monday, probe)
    rows = {r["day"]: r for r in report["days_with_observations_and_no_afterclose_report"]}
    assert rows["2026-10-18"]["unexpected"] and rows["2026-10-18"]["weekday"] == "Sunday"
    assert not rows["2026-10-16"]["unexpected"]   # a session the observer never assessed
    assert {("evidence_inconsistency", "unreported-2026-10-18"),
            ("missing_check", "unreported-2026-10-16")} <= events(tmp_path / "scan")


def test_no_observations_on_an_unknown_date_is_unknown_not_incomplete_or_closed(tmp_path):
    cfg = config(tmp_path, reviewed=False)
    report, _ = run("afterclose", tmp_path, FakeProbe(signals=[], decisions=[]), "16:00:00",
                    cfg=cfg)
    assert report["outcome"] == "UNKNOWN" and report["result"] == "FAIL"
    assert phase(tmp_path, cfg)["sessions"] == {}


def test_the_shipped_calendar_is_unreviewed_and_therefore_decides_nothing():
    cfg = {**cli.DEFAULTS, "calendar_file": str(OBSERVER / "session_calendar.json")}
    calendar = cli.load_calendar(cfg)
    entries = calendar["dates"]
    assert len(entries) == 79 and {e["review_status"] for e in entries.values()} == {"UNREVIEWED"}
    assert all(e["reviewer"] is None and e["official_reference"] is None
               and "NSE/FAOP/71777" in e["candidate_reference"] for e in entries.values())
    for day in (DAY, date(2026, 10, 20), date(2026, 10, 17), date(2026, 11, 8)):
        assert cli.calendar_status(calendar, day)["status"] == "UNKNOWN"
    assert cli.calendar_status(calendar, date(2026, 10, 20))["provisional_listing"] == "CLOSED"
    assert cli.calendar_status(calendar, date(2026, 11, 8))["provisional_listing"] == "SPECIAL"
    assert entries["2026-11-08"]["hours"] is None            # Muhurat hours are not guessed
    assert cli.window(calendar, cfg)["status"] == "UNKNOWN"
    assert cli.calendar_status(cli.load_calendar({"calendar_file": "/nonexistent"}),
                               DAY)["status"] == "UNKNOWN"


@pytest.mark.parametrize("change", [
    {"review_status": "UNREVIEWED"}, {"reviewer": ""}, {"reviewed_at": None},
    {"official_reference": " "}, {"session_type": "MAYBE"},
])
def test_a_date_is_reviewed_only_with_a_reviewer_a_time_and_an_official_reference(tmp_path,
                                                                                 change):
    cfg = config(tmp_path)
    calendar = json.loads(Path(cfg["calendar_file"]).read_text())
    assert cli.calendar_status(cli.load_calendar(cfg), DAY)["status"] == "NORMAL"
    calendar["dates"][DAY.isoformat()].update(change)
    Path(cfg["calendar_file"]).write_text(json.dumps(calendar))
    assert cli.calendar_status(cli.load_calendar(cfg), DAY)["status"] == "UNKNOWN"
    assert cli.window(cli.load_calendar(cfg), cfg)["unresolved_date"] == DAY.isoformat()


def test_special_session_is_recorded_and_never_run_through_the_normal_template(tmp_path):
    special = date(2026, 10, 16)
    cfg = config(tmp_path, special=[special], hc=True)
    pings = Pings()
    signals, decisions = session(special, n=12)
    probe = FakeProbe(special, signals=signals, decisions=decisions)
    for check, start in (("premarket", "08:35:00"), ("afterclose", "16:00:00")):
        report, _ = run(check, tmp_path, probe, start, special, cfg, post=pings)
        assert report["result"] == "SPECIAL SESSION" and not report["ran_checks"]
        assert "outcome" not in report and report["checks"] == []
        assert report["special_session_activity"]["observations"] == 12
        assert report["session_calendar"]["status"] == "SPECIAL"
    assert ("evidence_inconsistency", "special-session") in events(tmp_path)
    assert pings.kinds() == [("premarket", "fail"), ("afterclose", "fail")]
    state = phase(tmp_path, cfg)
    assert state["sessions"] == {} and special.isoformat() not in state["window"]["sessions"]
    assert state["window"]["sessions"][:3] == ["2026-10-14", "2026-10-15", "2026-10-19"]


# ===========================================================================
# The bounded phase — high C (approvals) and high D (finality)
# ===========================================================================
def weekdays(count, after=date(2026, 10, 13)):
    day, out = after, []
    while len(out) < count:
        day += timedelta(days=1)
        if day.weekday() < 5:
            out.append(day)
    return out


def test_five_qualifying_sessions_reach_the_checkpoint(tmp_path):
    days = weekdays(6)
    for i, day in enumerate(days[:5]):
        report = day_run(tmp_path, day)
        assert report["outcome"] == "QUALIFIED"
        assert report["bounded_phase"]["phase"] == ("REVIEW PENDING" if i == 4 else
                                                    "BOUNDED OBSERVATION")
        assert report["attention_required"] is (i == 4)
        assert not report["outside_bounded_evaluation_period"]
    assert report["attention_reasons"] == ["FIVE_SESSION_CHECKPOINT"]
    state = phase(tmp_path)
    assert state["reasons"] == ["FIVE_SESSION_CHECKPOINT"]
    assert state["checkpoint"]["trigger_session"] == days[4].isoformat()
    assert state["window"]["sessions"] == [d.isoformat() for d in days[:5]]
    assert state["window"]["due_at_ist"].startswith(f"{days[4]}T16:30:00+05:30")
    hits = [e for e in records(tmp_path).events() if e["type"] == "five_session_checkpoint"]
    assert len(hits) == 1 and hits[0]["session_date"] == days[4].isoformat()
    later = day_run(tmp_path, days[5])
    assert later["outcome"] == "QUALIFIED" and later["outside_bounded_evaluation_period"]
    assert later["attention_required"]                 # still unacknowledged
    assert event_types(tmp_path).count("five_session_checkpoint") == 1


def test_the_window_ends_at_1630_on_the_fifth_session_and_is_not_extended(tmp_path):
    days = weekdays(6)
    signals, decisions = session(days[1], n=60)
    for day in days[:4]:
        probe = (FakeProbe(day, signals=signals, decisions=decisions) if day == days[1]
                 else FakeProbe(day))
        day_run(tmp_path, day, probe)
    before = phase(tmp_path, now=at("16:29:59", days[4]))
    assert before["phase"] == "BOUNDED OBSERVATION" and before["reasons"] == []
    fifth = day_run(tmp_path, days[4])     # completes before 16:30: the window is still open
    assert fifth["outcome"] == "QUALIFIED" and fifth["bounded_phase"]["phase"] == \
        "BOUNDED OBSERVATION"
    report, _ = run("premarket", tmp_path, FakeProbe(days[5]), day=days[5])
    assert report["result"] == "PASS"      # readiness itself is unaffected
    assert report["bounded_phase"]["phase"] == "REVIEW PENDING" and report["attention_required"]
    assert report["attention_reasons"] == ["BOUNDED_WINDOW_ENDED_WITH_UNRESOLVED_EVIDENCE"]
    details = report["bounded_phase"]["window_details"]
    assert details["established_qualifying_sessions"] == 4
    assert details["unresolved_session_qualifications"] == {days[1].isoformat(): "INCOMPLETE"}
    assert details["missing_reports"] == []
    assert details["checkpoint_due_at_ist"].startswith(f"{days[4]}T16:30:00")
    checkpoint = phase(tmp_path)["checkpoint"]
    assert checkpoint["detected_at_ist"].startswith(f"{days[5]}T08:35")
    assert "bounded_window_ended" in event_types(tmp_path)
    sixth = day_run(tmp_path, days[5], premarket=None)   # a sixth good session is no replacement
    assert sixth["outside_bounded_evaluation_period"]
    assert phase(tmp_path)["checkpoint"]["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert len(phase(tmp_path, now=at("23:00", days[5]))["qualifying_sessions"]) == 4


def test_a_missing_final_session_report_still_ends_the_window(tmp_path):
    """The observer was down on the fifth session and for days afterwards."""
    days = weekdays(8)
    for day in days[:3]:
        day_run(tmp_path, day)
    assert phase(tmp_path, now=at("16:30:00", days[4]))["reasons"] == [
        "BOUNDED_WINDOW_ENDED_WITH_UNRESOLVED_EVIDENCE"]
    pings, cfg = Pings(), config(tmp_path, hc=True)
    report, _ = run("midsession", tmp_path, midday(days[7]), "12:30:00", days[7], cfg,
                    post=pings)           # the first run after the outage
    assert report["bounded_phase"]["phase"] == "REVIEW PENDING"
    details = report["bounded_phase"]["window_details"]
    assert details["missing_reports"] == [days[3].isoformat(), days[4].isoformat()]
    assert details["established_qualifying_sessions"] == 3    # missing sessions are not counted
    assert "not extended" in details["note"]
    checkpoint = phase(tmp_path)["checkpoint"]
    assert checkpoint["window"]["due_at_ist"].startswith(f"{days[4]}T16:30:00")
    assert checkpoint["detected_at_ist"].startswith(f"{days[7]}T12:30")
    assert checkpoint["trigger_session"] == days[4].isoformat()
    # found outside an after-close run: the after-close check is failed at once
    assert ("afterclose", "fail") in pings.kinds()
    note = next(n for n in records(tmp_path).notifications()
                if n["event_type"] == "review_attention")
    assert note["checkpoint_id"] == checkpoint["checkpoint_id"] and note["delivered"]
    assert f"checkpoint={checkpoint['checkpoint_id']}" in note["body"]


def test_the_fifth_session_is_not_invented_when_dates_are_unreviewed(tmp_path):
    cfg = config(tmp_path)
    calendar = json.loads(Path(cfg["calendar_file"]).read_text())
    calendar["dates"]["2026-10-16"]["review_status"] = "UNREVIEWED"
    Path(cfg["calendar_file"]).write_text(json.dumps(calendar))
    state = phase(tmp_path, cfg, at("23:00", date(2026, 12, 1)))
    assert state["phase"] == "BOUNDED OBSERVATION" and state["window"]["status"] == "UNKNOWN"
    assert state["window"]["unresolved_date"] == "2026-10-16"
    assert state["window"]["sessions_found"] == ["2026-10-14", "2026-10-15"]
    assert "2026-10-16" in state["calendar_uncertainty"]


def test_sessions_up_to_13_oct_are_outside_the_count(tmp_path):
    report = day_run(tmp_path, date(2026, 10, 13))
    assert report["outcome"] == "QUALIFIED" and phase(tmp_path)["qualifying_sessions"] == []


def test_high_d_review_pending_survives_a_later_incomplete_report(tmp_path):
    """The original defect: a later INCOMPLETE report for the same date reopened the phase."""
    signals, decisions = with_gate({40: vix_stop()})
    first = day_run(tmp_path, probe=FakeProbe(signals=signals, decisions=decisions))
    assert phase(tmp_path)["phase"] == "REVIEW PENDING"
    checkpoint = (tmp_path / "records/checkpoints/review-pending.json").read_bytes()
    rerun, _ = run("afterclose", tmp_path, FakeProbe(vix=None), "16:20:00")   # now INCOMPLETE
    assert rerun["outcome"] == "INCOMPLETE" and rerun["report_id"] != first["report_id"]
    recon, _ = run("afterclose", tmp_path, FakeProbe(vix=None), "07:00:00",
                   DAY + timedelta(days=2), session_date=DAY.isoformat())
    state = phase(tmp_path)
    assert state["phase"] == "REVIEW PENDING"
    assert state["sessions"]["2026-10-14"]["report_id"] == first["report_id"]
    assert set(state["sessions"]["2026-10-14"]["other_reports_for_this_date"]) == {
        rerun["report_id"], recon["report_id"]}
    assert (tmp_path / "records/checkpoints/review-pending.json").read_bytes() == checkpoint
    assert len(records(tmp_path).reports(DAY.isoformat(), "afterclose")) == 3
    assert not records(tmp_path).write_checkpoint({"schema": "x", "phase": "BOUNDED"})
    assert json.loads((tmp_path / "records/checkpoints/review-pending.json").read_text())[
        "phase"] == "REVIEW PENDING"


def test_high_d_a_later_report_does_not_change_which_sessions_count(tmp_path):
    days = weekdays(5)
    for day in days:
        day_run(tmp_path, day)
    run("afterclose", tmp_path, FakeProbe(days[2], vix=None), "16:25:00", days[2])
    state = phase(tmp_path)
    assert state["phase"] == "REVIEW PENDING" and len(state["qualifying_sessions"]) == 5
    assert "note" not in state and state["reasons"] == ["FIVE_SESSION_CHECKPOINT"]


def test_high_d_only_a_recorded_supersession_changes_the_standing_report(tmp_path):
    cfg = config(tmp_path)
    first = day_run(tmp_path)
    second, _ = run("afterclose", tmp_path, FakeProbe(vix=None), "16:20:00")
    assert phase(tmp_path)["sessions"]["2026-10-14"]["outcome"] == "QUALIFIED"
    approve(tmp_path, first, tool_scope=cli.SUPERSEDE_SCOPE,
            superseding=second["report_id"])
    entry = phase(tmp_path, cfg)["sessions"]["2026-10-14"]
    assert entry["outcome"] == "INCOMPLETE" and entry["supersedes_report"] == first["report_id"]
    assert len(records(tmp_path).reports(DAY.isoformat(), "afterclose")) == 2   # both kept


def approve(root, report, tool_scope=cli.COUNT_SCOPE, superseding=None, **over):
    (Path(root) / "operator/approvals").mkdir(parents=True, exist_ok=True)
    text = Path(root) / "decision.txt"
    text.write_text("Astra: Session accepted under the recorded operational exception.")
    argv = ["--report-root", str(Path(root) / "records"), "--approvals-root",
            str(Path(root) / "operator/approvals"), "--session-date", report["session_date"],
            "--report-id", report["report_id"], "--scope", tool_scope,
            "--exception", "08:45 readiness MISSING: VM started 09:03",
            "--astra-decision-reference", "Astra decision 2026-10-14 #7",
            "--approval-text-file", str(text), "--recorded-by", "operator"]
    if superseding:
        argv += ["--superseding-report-id", superseding]
    assert operator_approval.main(argv) == 0
    path = max((Path(root) / "operator/approvals").glob("*.json"),
               key=lambda p: p.stat().st_mtime_ns)
    if over:
        record = json.loads(path.read_text())
        record.update(over)
        path.chmod(0o644)
        path.write_text(json.dumps(record))
    return path


def test_high_c_an_ordinary_correction_cannot_count_an_exception_session(tmp_path, capsys):
    """The original defect: `correct --astra-exception-accepted` counted the session."""
    report = day_run(tmp_path, premarket=None)
    assert report["outcome"] == "OPERATIONAL EXCEPTION REQUIRED"
    path = tmp_path / "observer.json"
    path.write_text(json.dumps(config(tmp_path)))
    base = ["correct", "--config", str(path), "--session-date", DAY.isoformat(),
            "--report-id", report["report_id"], "--note", "Astra accepted the exception",
            "--recorded-by", "Astra"]
    with pytest.raises(SystemExit):
        cli.main([*base, "--astra-exception-accepted"])   # the flag no longer exists
    before = sorted(p.read_bytes() for p in tmp_path.rglob("reports/**/*.json"))
    assert cli.main(base) == 0
    (correction,) = records(tmp_path).corrections()
    assert correction["corrects_report_id"] == report["report_id"]
    assert correction["effect"].startswith("none")
    assert phase(tmp_path)["qualifying_sessions"] == []
    assert sorted(p.read_bytes() for p in tmp_path.rglob("reports/**/*.json")) == before
    with pytest.raises(SystemExit):
        cli.main(["correct", "--config", str(path), "--session-date", DAY.isoformat(),
                  "--report-id", "nope", "--note", "x", "--recorded-by", "operator"])


def test_high_c_an_explicit_operator_approval_counts_the_session(tmp_path):
    report = day_run(tmp_path, premarket=None)
    assert phase(tmp_path)["qualifying_sessions"] == []
    path = approve(tmp_path, report)
    approval = json.loads(path.read_text())
    target = records(tmp_path).report_file(DAY.isoformat(), report["report_id"])
    assert approval["report_sha256"] == store.file_sha256(target)
    for field in cli.APPROVAL_FIELDS:
        assert approval[field]
    assert approval["scope"] == cli.COUNT_SCOPE and "Astra" in approval["approval_text"]
    state = phase(tmp_path)
    assert state["qualifying_sessions"] == ["2026-10-14"] and state["rejected_approvals"] == []
    assert state["sessions"]["2026-10-14"]["exception_approval"] == approval["approval_id"]
    assert state["sessions"]["2026-10-14"]["outcome"] == "OPERATIONAL EXCEPTION REQUIRED"
    assert json.loads(target.read_text())["readiness_0845"] == "MISSING"   # still preserved


@pytest.mark.parametrize("forgery, reason", [
    ({"report_sha256": "0" * 64}, "hash does not match"),
    ({"report_id": "abcdefabcdef"}, "does not exist"),
    ({"session_date": "2026-10-15"}, "does not exist"),
    ({"scope": "anything"}, "unknown scope"),
    ({"astra_decision_reference": " "}, "missing or empty"),
    ({"approval_text": None}, "missing or empty"),
    ({"recorded_by": ""}, "missing or empty"),
    ({"schema": "observer_correction/1"}, "not an approval"),
])
def test_high_c_a_forged_or_mismatched_approval_counts_nothing(tmp_path, forgery, reason):
    report = day_run(tmp_path, premarket=None)
    approve(tmp_path, report, **forgery)
    state = phase(tmp_path)
    assert state["qualifying_sessions"] == []
    assert reason in state["rejected_approvals"][0]["reason"]


def test_high_c_an_approval_cannot_be_moved_to_another_report_or_outcome(tmp_path):
    days = weekdays(2)
    exception = day_run(tmp_path, days[0], premarket=None)
    incomplete = day_run(tmp_path, days[1], FakeProbe(days[1], vix=None))
    path = approve(tmp_path, exception)
    target = records(tmp_path).report_file(days[1].isoformat(), incomplete["report_id"])
    moved = json.loads(path.read_text())
    moved.update(report_id=incomplete["report_id"], session_date=days[1].isoformat(),
                 report_sha256=store.file_sha256(target), approval_id="moved")
    (tmp_path / "operator/approvals/moved.json").write_text(json.dumps(moved))
    state = phase(tmp_path)
    assert state["qualifying_sessions"] == [days[0].isoformat()]
    assert "not OPERATIONAL EXCEPTION REQUIRED" in state["rejected_approvals"][0]["reason"]
    with pytest.raises(SystemExit):   # the operator tool refuses the same thing
        approve(tmp_path, incomplete)


def test_high_c_the_observer_has_no_way_to_write_an_approval(tmp_path):
    report = day_run(tmp_path, premarket=None)
    assert not (tmp_path / "operator").exists()   # no run created or wrote it
    parser_choices = ["premarket", "midsession", "afterclose", "liveness", "correct", "status"]
    path = tmp_path / "observer.json"
    path.write_text(json.dumps(config(tmp_path)))
    with pytest.raises(SystemExit):
        cli.main(["approve", "--config", str(path)])
    source = "".join(p.read_text() for p in (OBSERVER / "quantdesk_observer").glob("*.py"))
    assert "operator_paths" in source and "write_approval" not in source
    assert "write_acknowledgment" not in source
    for tool in ("operator_approval", "operator_acknowledge", "operator_maintenance"):
        assert tool not in source   # the service package never imports an operator tool
    assert all(c in (OBSERVER / "quantdesk_observer/cli.py").read_text() for c in parser_choices)
    service = (OBSERVER / "systemd/quantdesk-observer@.service").read_text()
    assert "ReadOnlyPaths=/etc/quantdesk-observer" in service
    assert "ReadWritePaths=/var/lib/quantdesk-observer\n" in service
    assert report["outcome"] == "OPERATIONAL EXCEPTION REQUIRED"


# ===========================================================================
# Medium B, G, H — cancellation, publication, concurrency
# ===========================================================================
def test_medium_b_a_cancelled_run_is_not_swallowed_as_a_check_level_unknown(tmp_path):
    """The original defect: the timeout was caught by a check and recorded as UNKNOWN."""
    probe = FakeProbe(archive=RunTimeout("the run exceeded its 240s limit"))
    report, _ = run("afterclose", tmp_path, probe, "16:00:00")
    assert report["result"] == "FAIL" and report["outcome"] == "INCOMPLETE"
    assert "exceeded" in report["failure_reasons"][0]
    # What had finished before the limit is kept; the timeout is stated; nothing after
    # it is assumed, and no check is turned into an UNKNOWN to cover for it.
    assert report["timed_out"] is True and report["unknown_fields"] == []
    assert [c["name"] for c in report["checks"]] == [
        "collection coverage", "producer revision", "configuration identity",
        "observation/decision linkage and evidence status",
        "gate traces internally consistent", "LIVE_TRADING=false",
        "option chain enabled with available OI on every observation", "score accounting"]
    assert all(c["status"] == "PASS" for c in report["checks"])
    assert report["valid_early_review_candidates"] == 0 and not report["collection_gates_passed"]
    assert ("operational_failure", "afterclose-timeout") in events(tmp_path)
    assert "vix" not in probe.calls   # nothing ran after the cancellation
    assert phase(tmp_path)["sessions"]["2026-10-14"]["outcome"] == "INCOMPLETE"
    with pytest.raises(RunTimeout):
        checks.guarded("x", lambda: (_ for _ in ()).throw(RunTimeout("stop")))
    report, _ = run("premarket", tmp_path / "pm", FakeProbe(ntp=RunTimeout("stop")))
    assert report["result"] == "FAIL" and ("operational_failure", "premarket-timeout") in \
        events(tmp_path / "pm")


def test_medium_b_late_starts_are_still_bounded(tmp_path, monkeypatch):
    seen = []
    real = cli.time_limit

    def spy(seconds, enabled):
        seen.append((seconds, enabled))
        return real(seconds, False)
    monkeypatch.setattr(cli, "time_limit", spy)
    for check, start in (("premarket", "09:03:27"), ("premarket", "08:35:00"),
                         ("midsession", "14:00:00"), ("afterclose", "16:00:00")):
        clock = Clock(at(start))
        cli.run_check(check, config(tmp_path), records(tmp_path), FakeProbe(), clock,
                      clock.sleep, alarm=True, post=Pings())
    # The limit is what is left once the lock is held and the run begins: the two
    # clock readings before that (one second each on this clock) are its own time.
    assert [s for s, _ in seen] == [pytest.approx(178, abs=1), pytest.approx(597, abs=1),
                                    pytest.approx(178, abs=1), pytest.approx(1797, abs=1)]
    assert all(seconds > 0 and enabled for seconds, enabled in seen)


def test_the_alarm_ends_a_hung_run():
    with pytest.raises(RunTimeout), cli.time_limit(1, True):
        import time as real
        real.sleep(3)


def test_medium_g_reports_are_published_whole_or_not_at_all(tmp_path, monkeypatch):
    first, _ = run("premarket", tmp_path)
    reports = tmp_path / "records/reports/2026-10-14"
    (path,) = reports.glob("*.json")
    assert json.loads(path.read_text())["report_id"] == first["report_id"]
    assert path.stat().st_mode & 0o222 == 0 and not list(reports.glob(".tmp-*"))
    with pytest.raises(FileExistsError):
        records(tmp_path).write_report(first)
    assert json.loads(path.read_text())["report_id"] == first["report_id"]   # not replaced

    def interrupted(src, dst):
        raise OSError("power lost before the record was linked")
    monkeypatch.setattr(os, "link", interrupted)
    outcome = {}
    run("premarket", tmp_path, start="08:36:00", publication=outcome)
    assert outcome["state"] == "ABSENT"                         # handled, and said so
    monkeypatch.undo()
    assert [p.name for p in reports.iterdir()] == [path.name]   # no partial record, no scratch
    half = reports / ".tmp-deadbeef"
    half.write_text('{"schema": "observer_report/2", "check": "prem')
    assert len(records(tmp_path).reports(DAY.isoformat())) == 1   # scratch files are not records
    with pytest.raises(ValueError):
        store.publish(reports / "nan.json", {"schema": "x", "value": float("nan")})
    assert not (reports / "nan.json").exists()


def test_medium_h_a_corrupt_report_is_an_explicit_failure_not_a_silent_omission(tmp_path):
    days = weekdays(2)
    day_run(tmp_path, days[0])
    (victim,) = (tmp_path / f"records/reports/{days[0]}").glob("afterclose-*.json")
    victim.chmod(0o644)
    victim.write_text(victim.read_text()[:200])
    with pytest.raises(CorruptRecord):
        records(tmp_path).reports()
    with pytest.raises(CorruptRecord):
        phase(tmp_path)
    report = day_run(tmp_path, days[1])
    assert report["result"] == "FAIL" and report["outcome"] == "INCOMPLETE"
    assert "corrupt" in " ".join(report["failure_reasons"])
    assert any(e[0] == "evidence_inconsistency" and "corrupt" in e[1]
               for e in events(tmp_path))
    path = tmp_path / "observer.json"
    path.write_text(json.dumps(config(tmp_path)))
    assert cli.main(["status", "--config", str(path)]) == 1


def test_medium_h_corrections_and_runs_are_serialized(tmp_path):
    report = day_run(tmp_path)
    path = tmp_path / "observer.json"
    path.write_text(json.dumps(config(tmp_path)))
    held = records(tmp_path).lock()
    with pytest.raises(Overlap):
        records(tmp_path).lock()
    original = store.Store.lock
    store.Store.lock = lambda self, wait_seconds=0.0: original(self, 0.0)
    try:
        with pytest.raises(SystemExit):
            cli.main(["correct", "--config", str(path), "--session-date", DAY.isoformat(),
                      "--report-id", report["report_id"], "--note", "n", "--recorded-by", "o"])
    finally:
        store.Store.lock = original
    assert records(tmp_path).corrections() == []
    probe = FakeProbe()
    blocked, _ = run("midsession", tmp_path, probe, "12:31:00")
    assert blocked["result"] == "FAIL" and not blocked["ran_checks"]
    assert probe.calls == ["revision"] and "another observer run" in blocked["failure_reasons"][0]
    assert ("operational_failure", "midsession-overlap") in events(tmp_path)
    held.close()
    assert run("midsession", tmp_path, midday(), "12:32:00")[0]["ran_checks"]


def test_state_is_rebuilt_from_the_preserved_records(tmp_path):
    day_run(tmp_path)
    (tmp_path / "records/state.json").unlink()
    assert phase(tmp_path)["qualifying_sessions"] == ["2026-10-14"]


def test_an_observer_fault_ends_as_a_recorded_failure_and_touches_nothing_else(tmp_path):
    pings = Pings()
    probe = FakeProbe(signals=RuntimeError("bug in the observer"))
    report, _ = run("afterclose", tmp_path, probe, "16:00:00", cfg=config(tmp_path, hc=True),
                    post=pings)
    assert report["result"] == "FAIL" and report["outcome"] == "INCOMPLETE"
    assert report["failure_reasons"][0] == "observer internal error: RuntimeError"
    assert ("operational_failure", "afterclose-internal-error") in events(tmp_path)
    assert pings.kinds() == [("afterclose", "fail")]
    written = {p.relative_to(tmp_path).parts[0] for p in tmp_path.rglob("*") if p.is_file()}
    assert written <= {"records", "calendar.json", "healthchecks.json"}
    assert run("premarket", tmp_path, start="08:35:00", day=DAY + timedelta(days=1))[0][
        "result"] == "PASS"   # the next run is unaffected


# ===========================================================================
# Medium F — the probe's reach
# ===========================================================================
def test_the_probe_can_only_read():
    assert "/signals/live" not in probes.API_PATHS
    with pytest.raises(ProbeError):
        probes.Probe({"api_base": "http://127.0.0.1:1"}).api("/signals/live")
    for argv in (["systemctl", "restart", "quantdesk-api"], ["reboot"], ["rm", "-rf", "/tmp/x"],
                 ["systemctl", "stop", "quantdesk-api"], ["curl", "http://x"]):
        with pytest.raises(ProbeError):
            probes._run(argv)
    source = (OBSERVER / "quantdesk_observer/probes.py").read_text()
    statements = re.findall(r'self\._sql\(f?"""(.*?)"""', source, re.S) \
        + re.findall(r'self\._sql\("(.*?)"\s*\n?\s*"(.*?)"\)', source, re.S)
    assert len(statements) == 7
    for sql in map(str, statements):
        assert re.search(r"\b(insert|update|delete|alter|drop|create|truncate|grant|copy)\b",
                         sql, re.I) is None
    assert "default_transaction_read_only=on" in source
    whole = "".join(p.read_text() for p in (OBSERVER / "quantdesk_observer").glob("*.py"))
    assert "import app" not in whole and "from app" not in whole and "importlib" not in whole


@pytest.mark.parametrize("base", [
    "http://example.com:8000", "http://localhost:8000", "https://127.0.0.1:8000",
    "http://10.0.0.5:8000", "http://127.0.0.1:8000/signals", "http://user:pw@127.0.0.1:8000",
    "http://127.0.0.1:8000/?x=1", "http://169.254.169.254", "ftp://127.0.0.1", "",
])
def test_medium_f_only_a_literal_loopback_http_origin_is_accepted(base):
    with pytest.raises(ProbeError):
        probes.Probe({"api_base": base}).api("/health")
    assert probes.loopback_base("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
    assert probes.loopback_base("http://[::1]:8000") == "http://[::1]:8000"


@pytest.fixture
def local_api():
    """A throwaway server on 127.0.0.1 that misbehaves the ways the probe must refuse."""
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path == "/health":
                self.send_response(302)
                self.send_header("Location", "/signals/live")
                self.end_headers()
            elif self.path == "/health/feed":
                body = b'{"pad": "' + b"x" * (probes.API_MAX_BYTES + 10) + b'"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/health/scheduler":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'["not", "an", "object"]')
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"position": null}')

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield probes.Probe({"api_base": f"http://127.0.0.1:{server.server_port}"}), hits
    server.shutdown()
    server.server_close()


def test_medium_f_redirects_are_refused_and_responses_are_bounded(local_api):
    probe, hits = local_api
    assert probe.api("/v2/status") == {"position": None}
    with pytest.raises(ProbeError, match="redirect"):
        probe.api("/health")
    assert "/signals/live" not in hits   # the redirect was never followed
    with pytest.raises(ProbeError, match="more than"):
        probe.api("/health/feed")
    with pytest.raises(ProbeError, match="JSON object"):
        probe.api("/health/scheduler")
    with pytest.raises(ProbeError):
        probe.api("/signals/live")
    assert hits == ["/v2/status", "/health", "/health/feed", "/health/scheduler"]


def test_real_probe_reads_the_checkout_without_importing_the_app():
    # In a fresh interpreter: in a full test run other tests have already
    # imported the app into this process, which would say nothing about
    # what the probe itself imports.
    script = textwrap.dedent(f"""
        import re, sys
        sys.path.insert(0, {str(OBSERVER)!r})
        from quantdesk_observer import probes
        probe = probes.Probe({{"quantdesk_repo": {str(OBSERVER.parents[2])!r}}})
        assert re.fullmatch(r"[0-9a-f]{{40}}", probe.revision())
        assert isinstance(probe.worktree_changes(), list)
        print(sorted(m for m in sys.modules if m == "app" or m.startswith("app.")))
    """)
    out = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True,
                         timeout=60, check=False)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_probe_refuses_a_session_date_that_is_not_a_date():
    with pytest.raises(ProbeError):
        probes.Probe._day_bounds("2026-10-14'; drop table signals; --")


# ===========================================================================
# Healthchecks.io: heartbeats, deadlines and failures (synthetic transport)
# ===========================================================================
def test_success_ping_only_for_an_assessment_that_passed(tmp_path):
    pings, cfg = Pings(), config(tmp_path, hc=True)
    report, _ = run("premarket", tmp_path, cfg=cfg, post=pings)
    assert report["result"] == "PASS" and pings.kinds() == [("premarket", "success")]
    url, body, timeout = pings.sent[0]
    assert url == f"https://hc.invalid/{PING_IDS['premarket']}" and timeout == 5
    assert body == (f"check=premarket session=2026-10-14 result=PASS outcome=- timely=True "
                    f"attention=NO reason=- report={report['report_id']} observer={VERSION}")
    (note,) = records(tmp_path).notifications()
    assert note["delivered"] and note["kind"] == "success"
    assert note["event_type"] == "assessment_result" and note["report_id"] == report["report_id"]
    (attempt,) = note["attempts"]
    assert attempt["delivered"] and attempt["notified_at_utc"] and not attempt["failure_reason"]
    for key in ("scheduled_at_ist", "deadline_at_ist", "started_at_utc", "completed_at_utc"):
        assert note[key] == report[key]   # each time kept separately from the notification's
    assert attempt["attempted_at_utc"] >= report["completed_at_utc"]


@pytest.mark.parametrize("check, start, probe_change, step", [
    ("premarket", "08:35:00", {"revision": "f" * 40}, 1.0),      # the assessment failed
    ("premarket", "09:03:27", {}, 1.0),                           # late start
    ("premarket", "08:44:30", {}, 20.0),                          # late completion
    ("midsession", "12:30:00", {"ntp": True, "/health": {"status": "down"}}, 1.0),
    ("afterclose", "16:00:00", {"vix": None}, 1.0),               # INCOMPLETE
])
def test_an_assessment_that_did_not_pass_sends_an_explicit_failure(tmp_path, check, start,
                                                                   probe_change, step):
    pings = Pings()
    report, _ = run(check, tmp_path, FakeProbe(**probe_change), start,
                    cfg=config(tmp_path, hc=True), step=step, post=pings)
    assert report["result"] == "FAIL" and pings.kinds() == [(check, "fail")]
    assert pings.sent[0][0].endswith("/fail")


def test_only_todays_timely_assessment_can_send_a_success(tmp_path):
    pings, cfg = Pings(), config(tmp_path, hc=True)
    late, _ = run("midsession", tmp_path, midday(), cfg=cfg, post=pings,
                  clock=Scripted(at("12:30:00"), at("12:30:00"), at("12:30:00"),
                                 at("12:30:00"), at("12:45:01")))
    assert late["result"] == "PASS" and not late["timely"]   # passed, but after its deadline
    assert pings.kinds() == [("midsession", "fail")]
    run("afterclose", tmp_path, start="07:00:00", cfg=cfg, post=pings)   # started early
    run("afterclose", tmp_path, FakeProbe(), "07:30:00", DAY + timedelta(days=1), cfg=cfg,
        post=pings, session_date=DAY.isoformat())                         # reconstruction
    assert pings.kinds() == [("midsession", "fail")]   # neither says anything to the monitor


def test_a_run_that_did_not_happen_sends_nothing_so_the_hosted_side_alerts(tmp_path):
    pings, cfg = Pings(), config(tmp_path, hc=True)
    day_run(tmp_path, premarket=None, mid=False, cfg=cfg, post=pings)
    assert pings.kinds() == [("afterclose", "fail")]   # no premarket or midsession ping exists
    assert {("missing_check", "premarket"), ("missing_check", "midsession")} <= events(tmp_path)


def test_a_late_recovery_does_not_rewrite_the_earlier_local_record(tmp_path):
    pings, cfg = Pings(), config(tmp_path, hc=True)
    failed, _ = run("premarket", tmp_path, FakeProbe(revision="f" * 40), cfg=cfg, post=pings)
    (path,) = (tmp_path / "records/reports/2026-10-14").glob("premarket-*.json")
    frozen = path.read_bytes()
    late, _ = run("premarket", tmp_path, start="09:10:00", cfg=cfg, post=pings)
    assert checks.all_pass(late["checks"]) and late["result"] == "FAIL"   # healthy but late
    assert pings.kinds()[-1] == ("premarket", "fail") and path.read_bytes() == frozen
    assert cli.check_status(records(tmp_path), DAY.isoformat(), "premarket") == "FAIL"
    nxt = DAY + timedelta(days=1)
    ok, _ = run("premarket", tmp_path, day=nxt, cfg=cfg, post=pings)   # tomorrow's success
    assert ok["result"] == "PASS" and pings.kinds()[-1] == ("premarket", "success")
    assert path.read_bytes() == frozen
    assert cli.check_status(records(tmp_path), DAY.isoformat(), "premarket") == "FAIL"
    report, _ = run("afterclose", tmp_path, start="16:00:00", cfg=cfg, post=pings)
    assert report["readiness_0845"] == "FAIL"


def test_a_network_failure_is_one_request_recorded_and_never_waited_on(tmp_path):
    pings, cfg = Pings(fail_first=99), config(tmp_path, hc=True)
    report, clock = run("premarket", tmp_path, cfg=cfg, post=pings)
    assert len(pings.sent) == 1 and clock.slept == []      # one request, no wait, no second try
    (note,) = records(tmp_path).notifications()
    assert not note["delivered"] and len(note["attempts"]) == 1
    attempt = note["attempts"][0]
    assert attempt["failure_reason"] == "OSError: network unreachable"
    assert attempt["receipt"] == "Notification undelivered; remote receipt unknown."
    assert attempt["request_timeout_seconds"] == 5.0
    assert report["result"] == "PASS"   # the assessment itself is untouched
    (path,) = (tmp_path / "records/reports/2026-10-14").glob("premarket-*.json")
    assert json.loads(path.read_text())["result"] == "PASS"
    assert ("operational_failure", "premarket-notification") in events(tmp_path)


def test_a_timeout_or_a_refusal_is_undelivered_and_not_tried_again_in_the_same_run(tmp_path):
    pings = Pings(fail_first=1, error=TimeoutError("timed out"))
    run("premarket", tmp_path, cfg=config(tmp_path, hc=True), post=pings)
    (note,) = records(tmp_path).notifications()
    assert not note["delivered"] and len(pings.sent) == 1
    # A timeout can follow a request the far side did receive: receipt is unknown, not denied.
    assert note["attempts"][0]["receipt"] == healthchecks.UNDELIVERED
    assert note["attempts"][0]["result"] == "TimeoutError: timed out"
    refused = Pings(status=503)
    run("premarket", tmp_path / "b", cfg=config(tmp_path / "b", hc=True), post=refused)
    (note,) = records(tmp_path / "b").notifications()
    assert not note["delivered"] and len(refused.sent) == 1
    assert note["attempts"][0]["result"] == "HTTP 503"


@pytest.mark.parametrize("change", [
    {"base_url": "http://hc.invalid"}, {"base_url": "https://user:pw@hc.invalid"},
    {"base_url": "https://hc.invalid/prefix"},
    # Timeouts and retries are policy, not configuration: such keys are refused outright.
    {"timeout_seconds": 20}, {"attempts": 3}, {"retry_wait_seconds": 1},
    {"credentials_file": "/nonexistent/hc.json"},
])
def test_invalid_ping_configuration_sends_nothing(tmp_path, change):
    cfg = config(tmp_path, hc=True)
    cfg["healthchecks"].update(change)
    with pytest.raises(healthchecks.PingConfigError):
        healthchecks.load_settings(cfg)
    pings = Pings()
    report, _ = run("premarket", tmp_path, cfg=cfg, post=pings)
    assert pings.sent == [] and report["result"] == "PASS"
    (note,) = records(tmp_path).notifications()
    assert not note["delivered"] and note["attempts"][0]["failure_reason"]
    assert ("operational_failure", "premarket-notification") in events(tmp_path)


@pytest.mark.parametrize("credentials", [
    {**PING_IDS, "premarket": "not-a-uuid"}, {k: PING_IDS["liveness"] for k in PING_IDS},
    {k: v for k, v in PING_IDS.items() if k != "afterclose"}, {**PING_IDS, "extra": "x"}, [],
])
def test_each_check_needs_its_own_well_formed_credential(tmp_path, credentials):
    cfg = config(tmp_path, hc=True)
    Path(cfg["healthchecks"]["credentials_file"]).write_text(json.dumps(credentials))
    with pytest.raises(healthchecks.PingConfigError):
        healthchecks.load_settings(cfg)


def test_ping_credentials_must_be_private_to_the_observer_account(tmp_path):
    cfg = config(tmp_path, hc=True)
    Path(cfg["healthchecks"]["credentials_file"]).chmod(0o644)
    with pytest.raises(healthchecks.PingConfigError, match="readable by other accounts"):
        healthchecks.load_settings(cfg)


def test_ping_credentials_never_reach_a_record_a_message_or_a_log(tmp_path, capsys):
    secret = PING_IDS["premarket"]
    boom = OSError(f"could not connect to https://hc.invalid/{secret}/fail")
    pings, cfg = Pings(fail_first=99, error=boom), config(tmp_path, hc=True)
    run("premarket", tmp_path, FakeProbe(revision="f" * 40), cfg=cfg, post=pings)
    stored = "".join(p.read_text() for p in (tmp_path / "records").rglob("*") if p.is_file())
    assert all(uuid not in stored for uuid in PING_IDS.values()) and "<redacted>" in stored
    assert all(uuid not in capsys.readouterr().out for uuid in PING_IDS.values())
    assert healthchecks.redact(f"x {secret.upper()} y") == "x <redacted> y"
    try:
        Path(cfg["healthchecks"]["credentials_file"]).write_text(
            json.dumps({**PING_IDS, "premarket": "bad"}))
        healthchecks.load_settings(cfg)
    except healthchecks.PingConfigError as exc:
        assert all(uuid not in str(exc) for uuid in PING_IDS.values())


def test_ping_bodies_are_minimal(tmp_path):
    pings = Pings()
    signals, decisions, stored = natural_entry()
    probe = FakeProbe(signals=signals, decisions=decisions)
    probe.facts["positions"] = {"total": 2, "open": 0, "max_id": 2, "session": [stored]}
    day_run(tmp_path, probe=probe, cfg=config(tmp_path, hc=True), post=pings)
    for _, body, _ in pings.sent:
        assert len(body) <= healthchecks.MAX_BODY
        assert set(re.findall(r"(\w+)=", body)) == {"check", "session", "result", "outcome",
                                                    "timely", "attention", "reason", "report",
                                                    "observer"} | (
            {"checkpoint"} if "attention=YES" in body else set())
    for event in records(tmp_path).events():
        assert set(event) == {"schema", "type", "subject", "session_date", "check", "report_id",
                              "created_at_utc", "observer_version", "summary", "integrity"}


def test_the_transport_verifies_tls_and_refuses_redirects_and_proxies(monkeypatch):
    built = {}

    def capture(*handlers):
        built["handlers"] = handlers
        raise OSError("stop before any connection")
    monkeypatch.setattr(urllib.request, "build_opener", capture)
    with pytest.raises(OSError):
        healthchecks.https_post("https://hc.invalid/x", b"body", 5)
    https = next(h for h in built["handlers"] if isinstance(h, urllib.request.HTTPSHandler))
    assert https._context.verify_mode == ssl.CERT_REQUIRED and https._context.check_hostname
    assert any(isinstance(h, healthchecks._NoRedirect) for h in built["handlers"])
    proxy = next(h for h in built["handlers"] if isinstance(h, urllib.request.ProxyHandler))
    assert proxy.proxies == {}


def test_liveness_pings_and_reads_nothing_from_quantdesk(tmp_path, capsys):
    cfg = config(tmp_path, hc=True)
    path = tmp_path / "observer.json"
    path.write_text(json.dumps(cfg))
    pings, probe = Pings(), FakeProbe()
    assert cli.main(["liveness", "--config", str(path)], probe=probe, post=pings,
                    sleep=lambda s: None) == 0
    assert pings.kinds() == [("liveness", "success")] and probe.calls == []
    assert pings.sent[0][1] == f"check=liveness observer={VERSION}"
    assert not (tmp_path / "records/reports").exists()   # it is not an assessment
    down = Pings(fail_first=99)
    assert cli.main(["liveness", "--config", str(path)], post=down, sleep=lambda s: None) == 1
    assert len(down.sent) == 1 and records(tmp_path).notifications() == []
    lost, ok = sorted(records(tmp_path).heartbeats(datetime.now(UTC).date().isoformat()),
                      key=lambda h: h["outcome"]["delivered"])
    assert ok["outcome"]["delivered"] and not lost["outcome"]["delivered"]
    assert lost["outcome"]["receipt"] == healthchecks.UNDELIVERED
    cfg["healthchecks"]["enabled"] = False
    path.write_text(json.dumps(cfg))
    silent = Pings()
    assert cli.main(["liveness", "--config", str(path)], post=silent) == 0 and not silent.sent


def test_pings_are_off_unless_configured(tmp_path):
    pings = Pings()
    run("premarket", tmp_path, post=pings)
    assert pings.sent == [] and records(tmp_path).notifications() == []
    assert cli.DEFAULTS["healthchecks"] == {"enabled": False}


def test_no_session_days_send_a_success_so_the_hosted_check_stays_quiet(tmp_path):
    pings, cfg = Pings(), config(tmp_path, closed=[DAY], hc=True)
    run("premarket", tmp_path, FakeProbe(signals=[], decisions=[]), cfg=cfg, post=pings)
    assert pings.kinds() == [("premarket", "success")] and "result=NO SESSION" in pings.sent[0][1]


# ===========================================================================
# Second review: gate/code contract, evidence structure, snapshot, stored reports
# ===========================================================================
def codex_example(d):
    """Codex's exact case: expiry_day passed, feed failed, code entry_state_not_ready."""
    d["evidence"]["vector"]["decision_context"]["gate_trace"] = trace(
        "feed", "entry_state_not_ready")
    d["code"] = "entry_state_not_ready"
    d["evidence"]["vector"]["decision_context"]["code"] = "entry_state_not_ready"


def test_gate_and_rejection_code_must_be_a_pair_the_producer_can_write(tmp_path):
    signals, decisions = session()
    codex_example(decisions[40])
    assert checks.claims_expiry_day_pass(decisions[40])
    problems = checks.trace_problems(decisions[40])
    assert any("not a code the feed gate produces" in p for p in problems)
    assert not checks.expiry_day_passed(decisions[40])
    report = day_run(tmp_path, probe=FakeProbe(signals=signals, decisions=decisions))
    (found,) = report["expiry_day_passed_candidates"]
    assert not found["valid_early_review_trigger"]
    assert report["valid_early_review_candidates"] == 0 and report["outcome"] == "INCOMPLETE"
    assert phase(tmp_path)["phase"] == "BOUNDED OBSERVATION"
    assert report["attention_required"] is False


@pytest.mark.parametrize("gate, code, ok", [
    ("feed", "live_feed_not_healthy", True), ("vix", "vix_spiking", True),
    ("contract_selection", "spread_too_wide", True), ("chain", "chain_not_ready", True),
    ("levels_vs_spot", "index_past_target", True), ("risk_manager", "risk_manager_vetoed", True),
    ("feed", "entry_state_not_ready", False), ("vix", "expiry_day", False),
    ("expiry_day", "outside_entry_window", False), ("signal.entry_state", "expiry_day", False),
    ("contract_selection", "risk_manager_vetoed", False), ("spot", "entered", False),
    ("feed", None, False), ("feed", "made_up_code", False),
])
def test_every_gate_accepts_only_its_own_codes(gate, code, ok):
    signals, _ = session(n=1)
    d = decision(signals[0], trace(gate, code), code)
    assert (checks.trace_problems(d) == []) is ok
    assert set(checks.GATE_CODES) == set(checks.GATE_ORDER)


@pytest.mark.parametrize("change, problem", [
    (lambda d: d.update(code="risk_manager_vetoed"), "recorded with code"),
    (lambda d: d["evidence"]["vector"]["decision_context"].update(position_id=None),
     "no position id"),
    (lambda d: d["evidence"]["vector"]["decision_context"]["gate_trace"][12].update(
        status="failed", code="index_past_stop"), "does not pass every gate"),
    (lambda d: d["evidence"]["vector"]["decision_context"]["gate_trace"][19].update(
        status="not_reached"), "does not pass every gate"),
    (lambda d: d["evidence"]["vector"]["decision_context"]["gate_trace"][3].update(
        code="entered"), "did not fail carries a rejection code"),
])
def test_an_entered_decision_must_meet_the_producers_entry_requirements(change, problem):
    signals, decisions, _ = natural_entry()
    assert checks.trace_problems(decisions[40]) == [] and checks.expiry_day_passed(decisions[40])
    change(decisions[40])
    decisions[40]["evidence"]["vector"]["decision_context"]["code"] = decisions[40]["code"]
    assert any(problem in p for p in checks.trace_problems(decisions[40]))
    assert not checks.expiry_day_passed(decisions[40])


SECTIONS = [("vector", "identity"), ("vector", "causality"), ("vector", "market_state"),
            ("vector", "setup"), ("vector", "evidence"), ("vector", "data_status"),
            ("vector", "versions"), ("vector", "decision_context"), ("contract_evidence",),
            ("outcome_label",), ("diagnostics",), ("bases",), ("missing",), ("missing_scope",),
            ("issues",), ("vector", "data_status", "chain_max_age_seconds"),
            ("vector", "identity", "observation_schema"), ("vector", "versions", "code_id"),
            ("vector", "decision_context", "position_id"),
            ("vector", "versions", "api_init_provenance"),
            ("contract_evidence", "observation_id")]


@pytest.mark.parametrize("path", SECTIONS, ids=[".".join(p) for p in SECTIONS])
def test_missing_required_decision_evidence_is_not_complete(tmp_path, path):
    """status="complete" alone is not enough: every producer section must be there."""
    signals, decisions = session()
    holder = decisions[7]["evidence"]
    for key in path[:-1]:
        holder = holder[key]
    del holder[path[-1]]
    assert decisions[7]["evidence"]["status"] == "complete"
    found = checks.evidence_problems(decisions[7])
    assert found["missing"] and "labelled complete with required evidence missing" in \
        found["contradictory"]
    report = day_run(tmp_path, probe=FakeProbe(signals=signals, decisions=decisions))
    link = named(report, "observation/decision linkage")
    assert link["status"] == "FAIL" and report["outcome"] == "INCOMPLETE"
    assert 13007 in map(int, link["evidence"]["missing_required_evidence"])


def test_legitimately_unavailable_and_selection_not_reached_are_structurally_valid():
    signals, decisions = session(n=2)
    d = decisions[0]
    assert d["evidence"]["vector"]["market_state"]["vwap"] is None            # A: present, null
    assert d["evidence"]["vector"]["data_status"]["feed_healthy_at_decision"] is None
    assert d["evidence"]["contract_evidence"]["status"] == "selection_not_reached"   # B
    assert checks.evidence_problems(d) == {"missing": [], "contradictory": []}
    assert checks.decision_is_sound(d, {signals[0]["id"]: signals[0]})
    reached = decision(signals[1], trace("risk_manager", "risk_manager_vetoed"),
                       "risk_manager_vetoed")
    assert checks.evidence_problems(reached) == {"missing": [], "contradictory": []}


@pytest.mark.parametrize("change, phrase", [
    (lambda d: d["evidence"]["issues"].append({"kind": "clock"}), "while listing issues"),
    (lambda d: d["evidence"]["vector"]["identity"].update(linkage="partial"),
     "without a linked observation"),
    (lambda d: d["evidence"]["vector"]["versions"].update(evidence_schema="v2_decision_evidence/1"),
     "differs from the envelope"),
    (lambda d: d["evidence"]["vector"]["decision_context"].update(code="expiry_day"),
     "decision_context.code differs"),
    (lambda d: d["evidence"]["vector"]["setup"].update(action="SELL"), "setup.action differs"),
    (lambda d: d["evidence"].update(contract_evidence=contract(True, "x")),
     "does not reach the selector"),
    (lambda d: d["evidence"]["contract_evidence"].update(status="unknown"),
     "contract_evidence.status"),
    (lambda d: d["evidence"].update(status="finished"), "status is 'finished'"),
])
def test_evidence_labelled_complete_but_contradictory_is_rejected(tmp_path, change, phrase):
    signals, decisions = session()
    change(decisions[7])
    found = checks.evidence_problems(decisions[7])
    assert any(phrase in c for c in found["contradictory"]) and not found["missing"]
    assert not checks.decision_is_sound(decisions[7], {s["id"]: s for s in signals})
    report = day_run(tmp_path, probe=FakeProbe(signals=signals, decisions=decisions))
    assert report["outcome"] == "INCOMPLETE"
    assert named(report, "observation/decision linkage")["evidence"][
        "contradictory_or_mislabelled"]


def test_selector_reached_without_contract_evidence_is_contradictory():
    signals, _ = session(n=1)
    d = decision(signals[0], trace("risk_manager", "risk_manager_vetoed"), "risk_manager_vetoed")
    d["evidence"]["contract_evidence"] = contract(False, "x")
    assert any("trace reaches the selector" in c
               for c in checks.evidence_problems(d)["contradictory"])


def test_intermittent_position_read_cannot_give_qualified_with_unknown_positions(tmp_path):
    """Codex's finding: the first read failed, a second succeeded, and the report said both."""
    good = {"total": 1, "open": 0, "max_id": 1, "session": []}
    probe = FakeProbe()
    run("premarket", tmp_path, probe)
    run("midsession", tmp_path, midday(), "12:30:00")
    probe.facts["positions"] = Sequence(ProbeError("psql exited 2"), good, good)
    probe.calls.clear()
    report, _ = run("afterclose", tmp_path, probe, "16:00:00")
    assert probe.calls.count("positions") == 1 and probe.calls.count("journal") == 1
    assert report["paper_positions"] == "UNKNOWN"
    assert named(report, "paper activity reconciliation")["status"] == "UNKNOWN"
    assert "paper activity reconciliation" in report["unknown_fields"]
    assert report["outcome"] == "INCOMPLETE" and report["baseline"] is None
    failed = [r for r in report["evidence_reads"] if r["read"] == "failed"]
    assert failed == [{"source": "positions", "read": "failed", "reason": "psql exited 2"}]
    assert [r["source"] for r in report["evidence_reads"]] == [
        "signals", "decisions", "positions", "journal", "schedule", "engine"]


def test_one_snapshot_feeds_qualification_report_and_baseline(tmp_path):
    """The reverse order: the first read is good and any later one would fail."""
    signals, decisions, stored = natural_entry()
    probe = FakeProbe(signals=signals, decisions=decisions)
    run("premarket", tmp_path, probe)
    run("midsession", tmp_path, midday(), "12:30:00")
    good = {"total": 2, "open": 0, "max_id": 2, "session": [stored]}
    probe.facts["positions"] = Sequence(good, ProbeError("psql exited 2"))
    probe.facts["journal"] = Sequence({"total": 1}, ProbeError("psql exited 2"))
    report, _ = run("afterclose", tmp_path, probe, "16:00:00")
    assert report["outcome"] == "QUALIFIED"
    assert report["paper_positions"]["opened_this_session"] == 1
    assert report["paper_positions"]["stored"] == [stored]
    assert named(report, "paper activity reconciliation")["evidence"]["positions_total"] == 2
    assert report["baseline"] == {"positions_total": 2, "journal_total": 1}
    assert report["expiry_day_passed_candidates"][0]["stored_position"] == stored
    assert all(r["read"] == "ok" for r in report["evidence_reads"])


@pytest.mark.parametrize("body", [
    {"schema": "observer_report/3"},
    {"schema": "observer_report/2", "check": "afterclose"},
    {"schema": "observer_report/3", "check": "afterclose", "outcome": "QUALIFIED",
     "session_date": "2026-10-14", "report_id": "x", "completed_at_utc": "2026-10-14T10:30Z"},
    [], "QUALIFIED", {"schema": 3},
])
def test_structurally_corrupt_but_parseable_report_is_an_explicit_failure(tmp_path, body):
    """Codex's finding: an object holding only a schema label vanished from the accounting."""
    day_run(tmp_path)
    assert phase(tmp_path)["qualifying_sessions"] == ["2026-10-14"]
    folder = tmp_path / "records/reports/2026-10-15"
    folder.mkdir()
    (folder / "afterclose-20261015T103000Z-x.json").write_text(json.dumps(body))
    with pytest.raises(CorruptRecord):
        records(tmp_path).reports()
    with pytest.raises(CorruptRecord):
        phase(tmp_path)
    nxt = DAY + timedelta(days=2)
    report = day_run(tmp_path, nxt)
    assert report["result"] == "FAIL" and report["outcome"] == "INCOMPLETE"
    assert report["bounded_phase"] == "UNKNOWN" and report["attention_required"]
    assert any(e[0] == "evidence_inconsistency" and "corrupt" in e[1] for e in events(tmp_path))


@pytest.mark.parametrize("damage", [
    lambda r: r.pop("outcome"), lambda r: r.pop("result"), lambda r: r.pop("checks"),
    lambda r: r.update(timely="yes"), lambda r: r.update(outcome="GOOD"),
    lambda r: r.update(session_date="2026-10-15"), lambda r: r.update(report_id="other"),
    lambda r: r.update(collection_gates_passed=None), lambda r: r.update(check="nightly"),
    lambda r: r.update(completed_at_utc="yesterday"),
    lambda r: r.update(valid_early_review_candidates=True),
    lambda r: r.update(outcome="INCOMPLETE"),       # a valid-looking edit: the hash catches it
    lambda r: r.pop("integrity"),
])
def test_an_altered_or_incomplete_stored_report_is_refused(tmp_path, damage):
    day_run(tmp_path)
    (path,) = (tmp_path / "records/reports/2026-10-14").glob("afterclose-*.json")
    report = json.loads(path.read_text())
    damage(report)
    if "integrity" in report and report.get("outcome") != "INCOMPLETE":
        report["integrity"]["content_sha256"] = store.content_hash(report)   # re-sealed forgery
    path.chmod(0o644)
    path.write_text(json.dumps(report))
    with pytest.raises(CorruptRecord):
        records(tmp_path).reports(check="afterclose")
    with pytest.raises((CorruptRecord, ValueError, KeyError, TypeError)):
        records(tmp_path).write_report({"schema": "observer_report/3"})


def test_report_storage_keeps_its_guarantees(tmp_path):
    first = day_run(tmp_path)
    stored = records(tmp_path).reports(check="afterclose")[0]
    assert stored["integrity"]["content_sha256"] == store.content_hash(stored)
    assert stored["schema"] == "observer_report/3" and stored["report_id"] == first["report_id"]
    with pytest.raises(FileExistsError):
        records(tmp_path).write_report(first)                   # never overwritten
    assert not list((tmp_path / "records").rglob(".tmp-*"))     # published whole
    (tmp_path / "records/state.json").unlink()
    assert phase(tmp_path)["qualifying_sessions"] == ["2026-10-14"]   # rebuilt from records


# ===========================================================================
# Second review: strict assessment times
# ===========================================================================
TIMES = {"premarket": ("08:35:00", "08:45:00"), "midsession": ("12:30:00", "12:45:00"),
         "afterclose": ("16:00:00", "16:30:00")}


def one_second(clock: str, delta: int) -> datetime:
    return at(clock) + timedelta(seconds=delta)


@pytest.mark.parametrize("check", list(TIMES))
def test_exact_start_and_deadline_boundaries(tmp_path, check):
    begin, deadline = TIMES[check]
    probe = (lambda: midday()) if check == "midsession" else (lambda: FakeProbe())

    def assess(name, start, done):
        root = tmp_path / name
        if check == "afterclose":
            run("premarket", root, FakeProbe())
            run("midsession", root, midday(), "12:30:00")
        # The lock attempt, its acquisition and the start are read at `start`; the rest
        # at `done`.
        return run(check, root, probe(), clock=Scripted(start, start, start, start, done))[0]

    early = assess("early", one_second(begin, -1), one_second(begin, 30))
    assert not early["ran_checks"] and early["checks"] == [] and early["result"] == "FAIL"
    assert "starts early" in early["failure_reasons"][0] and not early["started_in_window"]
    assert cli.check_status(records(tmp_path / "early"), DAY.isoformat(), check) == "MISSING"

    on_time = assess("ontime", at(begin), at(deadline))          # the deadline itself is in time
    assert on_time["started_in_window"] and on_time["timely"] and on_time["result"] == "PASS"
    assert on_time["lateness_seconds"] == 0
    assert on_time["scheduled_at_ist"].endswith(f"T{begin}+05:30")
    assert on_time["deadline_at_ist"].endswith(f"T{deadline}+05:30")
    assert on_time["started_at_ist"].endswith(f"T{begin}+05:30")
    assert on_time["completed_at_ist"].endswith(f"T{deadline}+05:30")

    over = assess("over", at(begin), one_second(deadline, 1))    # one second past it is not
    assert over["started_in_window"] and not over["timely"]

    last = assess("last", at(deadline), at(deadline))            # starting at the deadline:
    assert last["started_in_window"] and not last["timely"]      # no time is left, so it
    assert not last["ran_checks"] and last["assessment_status"] == "MISSING"   # never starts
    assert "lock_acquired_at_ist" not in last and last["lock_attempts"] == 0
    assert cli.check_status(records(tmp_path / "last"), DAY.isoformat(), check) == "MISSING"

    late = assess("late", one_second(deadline, 1), one_second(deadline, 20))
    assert late["ran_checks"] and not late["started_in_window"] and not late["timely"]
    assert late["lateness_seconds"] == 601 if check == "premarket" else late[
        "lateness_seconds"] > 0
    assert checks.all_pass([c for c in late["checks"] if c["name"] != "collection coverage"]
                           ) or check != "premarket"


def test_a_late_afterclose_run_keeps_its_evidence_and_its_lateness(tmp_path):
    run("premarket", tmp_path, FakeProbe())
    run("midsession", tmp_path, midday(), "12:30:00")
    report, _ = run("afterclose", tmp_path, FakeProbe(),
                    clock=Scripted(at("16:00:00"), at("16:00:00"), at("16:00:00"),
                                   at("16:00:00"), at("16:30:01")))
    assert report["collection_gates_passed"] and not report["timely"]
    assert report["outcome"] == "OPERATIONAL EXCEPTION REQUIRED" and report["result"] == "FAIL"
    assert report["operational_requirements_passed"] is False
    assert "not timely" in " ".join(report["failure_reasons"])
    assert phase(tmp_path)["qualifying_sessions"] == []
    started_late, _ = run("afterclose", tmp_path / "b", FakeProbe(), "18:00:00")
    assert started_late["collection_gates_passed"]
    assert started_late["outcome"] == "OPERATIONAL EXCEPTION REQUIRED"


def test_a_later_run_does_not_change_an_earlier_failure(tmp_path):
    failed, _ = run("premarket", tmp_path, FakeProbe(revision="f" * 40))
    (path,) = (tmp_path / "records/reports/2026-10-14").glob("premarket-*.json")
    frozen = path.read_bytes()
    again, _ = run("premarket", tmp_path, FakeProbe(), "08:44:00")   # passes, still in time
    assert again["result"] == "PASS" and path.read_bytes() == frozen
    assert json.loads(frozen)["result"] == "FAIL"


def test_the_60_second_slot_tolerance_does_not_move_an_assessment_start(tmp_path):
    report, _ = run("midsession", tmp_path, midday(), clock=Scripted(at("12:29:01")))
    assert not report["ran_checks"]                       # 59 s early is still early
    assert not hasattr(cli, "EARLY_SLACK")


# ===========================================================================
# Second review: REVIEW PENDING, owner attention, acknowledgment
# ===========================================================================
def early_review_day(root, cfg, pings, day=DAY, **kw):
    signals, decisions = with_gate({40: vix_stop()}, day)
    return day_run(root, day, FakeProbe(day, signals=signals, decisions=decisions), cfg=cfg,
                   post=pings, **kw)


def acknowledge(root, tool_checkpoint=None, **over):
    folder = Path(root) / "operator/acknowledgments"
    folder.mkdir(parents=True, exist_ok=True)
    text = Path(root) / "ack.txt"
    text.write_text("Seen. Review to be scheduled with Astra.")
    checkpoint_id = tool_checkpoint or records(root).checkpoint()["checkpoint_id"]
    assert operator_acknowledge.main([
        "--report-root", str(Path(root) / "records"), "--acknowledgments-root", str(folder),
        "--checkpoint-id", checkpoint_id, "--text-file", str(text),
        "--acknowledged-by", "operator"]) == 0
    path = max(folder.glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
    if over:
        record = json.loads(path.read_text())
        record.update(over)
        path.chmod(0o644)
        path.write_text(json.dumps(record))
    return path


def test_early_review_fails_the_afterclose_check_though_the_session_qualified(tmp_path):
    """Codex's finding: REVIEW PENDING was reached and an ordinary success was sent."""
    pings, cfg = Pings(), config(tmp_path, hc=True)
    report = early_review_day(tmp_path, cfg, pings)
    assert report["outcome"] == "QUALIFIED" and report["result"] == "PASS"   # not altered
    assert report["bounded_phase"]["phase"] == "REVIEW PENDING"
    assert report["attention_required"] is True
    assert report["attention_reasons"] == ["EARLY_REVIEW"]
    assert "does not by itself say" in report["attention_meaning"]
    assert pings.kinds() == [("premarket", "success"), ("midsession", "success"),
                             ("afterclose", "fail")]
    url, body, _ = pings.sent[-1]
    assert url == f"https://hc.invalid/{PING_IDS['afterclose']}/fail"    # the documented /fail
    assert "outcome=QUALIFIED" in body and "attention=YES reason=EARLY_REVIEW" in body
    assert phase(tmp_path, cfg)["qualifying_sessions"] == ["2026-10-14"]
    assert len(PING_IDS) == 4 and set(healthchecks.CHECKS) == {
        "liveness", "premarket", "midsession", "afterclose"}        # no fifth identity


def test_five_session_checkpoint_fails_the_afterclose_check(tmp_path):
    pings, cfg = Pings(), config(tmp_path, hc=True)
    days = weekdays(5)
    for day in days:
        report = day_run(tmp_path, day, cfg=cfg, post=pings)
    afterclose = [k for name, k in pings.kinds() if name == "afterclose"]
    assert afterclose == ["success"] * 4 + ["fail"]
    assert report["outcome"] == "QUALIFIED"
    assert report["attention_reasons"] == ["FIVE_SESSION_CHECKPOINT"]
    assert "reason=FIVE_SESSION_CHECKPOINT" in pings.sent[-1][1]


def test_attention_is_latched_until_the_exact_checkpoint_is_acknowledged(tmp_path):
    pings, cfg = Pings(), config(tmp_path, hc=True)
    days = weekdays(4)
    early_review_day(tmp_path, cfg, pings, days[0])
    checkpoint = records(tmp_path).checkpoint()
    assert checkpoint["reasons"] == ["EARLY_REVIEW"] and checkpoint["checkpoint_id"]
    assert [e["subject"] for e in records(tmp_path).events()
            if e["type"] == "early_review_trigger"] == [
        f"checkpoint-{checkpoint['checkpoint_id']}"]
    ordinary = day_run(tmp_path, days[1], cfg=cfg, post=pings)       # a clean, qualified day
    assert ordinary["outcome"] == "QUALIFIED" and ordinary["attention_required"]
    assert pings.kinds()[-3:] == [("premarket", "success"), ("midsession", "success"),
                                  ("afterclose", "fail")]             # success does not clear it
    for forged in ({"checkpoint_sha256": "0" * 64}, {"checkpoint_id": "abcdefabcdef"},
                   {"scope": cli.COUNT_SCOPE}, {"acknowledged_by": " "},
                   {"schema": "observer_exception_approval/1"}):
        path = acknowledge(tmp_path, **forged)
        state = phase(tmp_path, cfg)
        assert state["attention_required"] and state["rejected_acknowledgments"]
        path.unlink()
    with pytest.raises(SystemExit):
        acknowledge(tmp_path, tool_checkpoint="abcdefabcdef")         # the tool refuses too
    acknowledge(tmp_path)
    state = phase(tmp_path, cfg)
    assert state["attention_required"] is False and len(state["acknowledged_by"]) == 1
    assert state["phase"] == "REVIEW PENDING"                         # not reopened
    assert state["checkpoint"]["checkpoint_id"] == checkpoint["checkpoint_id"]
    after_ack = day_run(tmp_path, days[2], cfg=cfg, post=pings)
    assert after_ack["bounded_phase"]["phase"] == "REVIEW PENDING"
    assert after_ack["attention_required"] is False and pings.kinds()[-1] == (
        "afterclose", "success")
    assert after_ack["outside_bounded_evaluation_period"]


def test_acknowledgment_cannot_approve_an_exception_or_reopen_the_phase(tmp_path):
    cfg = config(tmp_path)
    signals, decisions = with_gate({40: vix_stop()})
    report = day_run(tmp_path, probe=FakeProbe(signals=signals, decisions=decisions),
                     premarket=None, cfg=cfg)
    assert report["outcome"] == "OPERATIONAL EXCEPTION REQUIRED"
    path = acknowledge(tmp_path)
    ack = json.loads(path.read_text())
    assert ack["scope"] == cli.ACK_SCOPE and "no exception" in ack["effect"]
    state = phase(tmp_path, cfg)
    assert state["qualifying_sessions"] == [] and state["phase"] == "REVIEW PENDING"
    assert state["sessions"]["2026-10-14"]["exception_approval"] is None
    (tmp_path / "operator/approvals").mkdir()
    (tmp_path / "operator/approvals/ack-as-approval.json").write_text(path.read_text())
    state = phase(tmp_path, cfg)
    assert state["qualifying_sessions"] == []                 # an ack is not an approval
    assert "not an approval" in state["rejected_approvals"][0]["reason"]
    approve(tmp_path, report)                                 # the separate authority still works
    (tmp_path / "operator/approvals/ack-as-approval.json").unlink()
    assert phase(tmp_path, cfg)["qualifying_sessions"] == ["2026-10-14"]
    assert phase(tmp_path, cfg)["phase"] == "REVIEW PENDING"


def test_the_observer_cannot_fabricate_an_acknowledgment(tmp_path):
    cfg = config(tmp_path)
    signals, decisions = with_gate({40: vix_stop()})
    day_run(tmp_path, probe=FakeProbe(signals=signals, decisions=decisions), cfg=cfg)
    assert not (tmp_path / "operator").exists()
    path = tmp_path / "observer.json"
    path.write_text(json.dumps(cfg))
    with pytest.raises(SystemExit):
        cli.main(["acknowledge", "--config", str(path)])
    report = records(tmp_path).reports(check="afterclose")[0]
    assert cli.main(["correct", "--config", str(path), "--session-date", DAY.isoformat(),
                     "--report-id", report["report_id"], "--note", "acknowledged by owner",
                     "--recorded-by", "owner"]) == 0
    assert phase(tmp_path, cfg)["attention_required"] is True   # a correction is not an ack


# ===========================================================================
# Second review: delivery, retries, liveness, maintenance
# ===========================================================================
def test_the_intent_is_stored_before_anything_is_sent(tmp_path):
    cfg = config(tmp_path, hc=True)
    seen = []

    def post(url, body, timeout):
        notes = records(tmp_path).notifications()
        seen.append(sorted((n["event_type"], n["kind"], len(n["attempts"])) for n in notes))
        raise OSError("down")
    signals, decisions = with_gate({40: vix_stop()})
    run("afterclose", tmp_path, FakeProbe(signals=signals, decisions=decisions), "16:00:00",
        cfg=cfg, post=post)
    # Both on disk, with no attempt yet, when the one request was made.
    # Both intents on disk, and the one request already reserved against both, when it
    # was made: nothing leaves before it is recorded.
    assert seen == [[("assessment_result", "fail", 1), ("review_attention", "fail", 1)]]
    for note in records(tmp_path).notifications():
        assert not note["delivered"] and note["notification_id"]
        assert note["attempts"][0]["failure_reason"] == "OSError: down"
        assert note["attempts"][0]["completion"] == "recorded"
    assert records(tmp_path).checkpoint() is not None        # REVIEW PENDING is not cleared


def test_no_recovery_is_fabricated_for_a_success_that_was_not_delivered(tmp_path):
    cfg = config(tmp_path, hc=True)
    run("premarket", tmp_path, cfg=cfg, post=Pings(fail_first=99))   # passed; ping lost
    later = Pings()
    run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg, post=later)
    assert later.kinds() == [("midsession", "success")]   # the lost success is not sent late
    stale = Pings()
    root = tmp_path / "b"
    cfg_b = config(root, hc=True)
    run("premarket", root, FakeProbe(revision="f" * 40), cfg=cfg_b, post=Pings(fail_first=99))
    nxt = DAY + timedelta(days=1)
    run("premarket", root, FakeProbe(nxt), day=nxt, cfg=cfg_b, post=stale)
    assert stale.kinds() == [("premarket", "success")]    # an old failure is not re-sent after
    assert not hasattr(healthchecks, "recover")           # a newer assessment of the same check


def test_liveness_runs_every_five_minutes_around_the_clock():
    timer = (OBSERVER / "systemd/quantdesk-observer-liveness.timer").read_text()
    assert "OnCalendar=*-*-* *:00/5:00 Asia/Kolkata" in timer
    lines = [line for line in timer.splitlines() if not line.startswith("#")]
    assert not any("Mon..Fri" in line or "08..16" in line for line in lines)
    readme = (OBSERVER / "README.md").read_text()
    assert "*/5 * * * *" in readme or "every 5 minutes" in readme.lower()
    assert "08:00–16:59" not in readme and "8-16" not in readme


def test_a_missed_liveness_heartbeat_is_never_sent_late(tmp_path):
    cfg = config(tmp_path, hc=True)
    path = tmp_path / "observer.json"
    path.write_text(json.dumps(cfg))
    down = Pings(fail_first=99)
    assert cli.main(["liveness", "--config", str(path)], post=down, sleep=lambda s: None) == 1
    today = datetime.now(UTC).date().isoformat()
    (lost,) = records(tmp_path).heartbeats(today)
    assert lost["check"] == "liveness" and not lost["outcome"]["delivered"]
    up = Pings()
    assert cli.main(["liveness", "--config", str(path)], post=up, sleep=lambda s: None) == 0
    assert up.kinds() == [("liveness", "success")]          # one ping for now, none for the past
    assert records(tmp_path).notifications() == []          # a heartbeat is never an intent
    assert len(records(tmp_path).heartbeats(today)) == 2
    assert not (tmp_path / "records/reports").exists()


def test_a_maintenance_record_is_information_and_pauses_nothing(tmp_path):
    cfg = config(tmp_path, hc=True)
    folder = tmp_path / "operator/maintenance"
    folder.mkdir(parents=True)
    assert operator_maintenance.main([
        "--maintenance-root", str(folder), "--start", "2026-10-14T08:00:00+05:30",
        "--end", "2026-10-14T09:00:00+05:30", "--reason", "planned VM stop",
        "--affected-checks", "liveness", "premarket",
        "--authorization-reference", "Astra 2026-10-13 #3", "--recorded-by", "operator"]) == 0
    pings = Pings()
    report, _ = run("premarket", tmp_path, FakeProbe(revision="f" * 40), cfg=cfg, post=pings)
    (active,) = report["maintenance_records_active"]
    assert active["reason"] == "planned VM stop" and active["affected_checks"] == [
        "liveness", "premarket"]
    assert active["start_ist"] and active["end_ist"] and active["authorization_reference"]
    assert report["result"] == "FAIL" and pings.kinds() == [("premarket", "fail")]  # not silenced
    later, _ = run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg, post=Pings())
    assert later["maintenance_records_active"] == []
    for bad in (["--start", "2026-10-14T08:00:00", "--end", "2026-10-14T09:00:00+05:30"],
                ["--start", "2026-10-14T10:00:00+05:30", "--end", "2026-10-14T09:00:00+05:30"]):
        with pytest.raises(SystemExit):
            operator_maintenance.main(["--maintenance-root", str(folder), *bad, "--reason", "x",
                                       "--affected-checks", "liveness",
                                       "--authorization-reference", "x", "--recorded-by", "o"])
    source = "".join(p.read_text() for p in OBSERVER.rglob("*.py"))
    assert "/pause" not in source and "/resume" not in source   # nothing pauses a hosted check


def test_the_observer_has_no_path_to_a_trading_write(tmp_path):
    source = "".join(p.read_text() for p in (OBSERVER / "quantdesk_observer").glob("*.py"))
    for forbidden in ("persist=", "sqlalchemy", "psycopg", "import apscheduler",
                      "from apscheduler", "from app",
                      "import app", "requests.", "systemctl restart", "systemctl stop"):
        assert forbidden not in source, forbidden
    assert "/signals/live" not in probes.API_PATHS
    assert source.count("method=") == 1 and "method=\"POST\"" in source   # only the ping
    probe = FakeProbe()
    day_run(tmp_path, probe=probe)
    assert set(probe.calls) <= {
        "ntp", "boot", "reboot_required", "unit", "processes", "listeners", "caddy", "disk",
        "log", "revision", "changes", "/health", "/health/scheduler", "/health/feed",
        "/v2/status", "signals", "decisions", "positions", "signal_days", "schedule", "journal",
        "archive", "vix"}


# ===========================================================================
# Records, versions, units, examples
# ===========================================================================
def test_report_records_times_versions_and_evidence(tmp_path):
    report, _ = run("premarket", tmp_path)
    for key in ("scheduled_at_ist", "deadline_at_ist", "started_at_utc", "started_at_ist",
                "completed_at_utc", "completed_at_ist", "result", "checks", "unknown_fields",
                "failure_reasons", "quantdesk_revision_observed", "session_calendar"):
        assert key in report
    assert report["observer_version"] == VERSION == (OBSERVER / "VERSION").read_text().strip()
    assert report["observer_version"] != REV and report["expected_quantdesk_revision"] == REV
    assert all({"name", "status", "detail", "evidence", "required"} <= set(c)
               for c in report["checks"])


def test_a_probe_that_cannot_answer_is_unknown(tmp_path):
    report, _ = run("premarket", tmp_path, FakeProbe(unit=ProbeError("systemctl exited 1")))
    assert report["result"] == "FAIL" and "PostgreSQL healthy" in report["unknown_fields"]


def test_events_are_exception_only(tmp_path):
    day_run(tmp_path)
    assert records(tmp_path).events() == []
    day_run(tmp_path / "b", premarket="08:44:40", premarket_step=5.0)
    assert events(tmp_path / "b") == {("operational_failure", "premarket"),
                                      ("operational_exception_required", "session")}


def test_systemd_units_are_short_lived_and_scheduled_in_ist():
    units = {p.name: p.read_text() for p in (OBSERVER / "systemd").iterdir()}
    assert set(units) == {"quantdesk-observer@.service", "quantdesk-observer-premarket.timer",
                          "quantdesk-observer-midsession.timer",
                          "quantdesk-observer-afterclose.timer",
                          "quantdesk-observer-liveness.timer"}
    service = "\n".join(line for line in units["quantdesk-observer@.service"].splitlines()
                        if not line.startswith("#"))
    assert "Type=oneshot" in service and "TimeoutStartSec=" in service
    assert "Restart=" not in service and "/opt/quantdesk-observer/observer.py %i" in service
    assert "ProtectSystem=strict" in service and "NoNewPrivileges=yes" in service
    for name, clock in (("premarket", "08:35"), ("midsession", "12:30"), ("afterclose", "16:00")):
        timer = units[f"quantdesk-observer-{name}.timer"]
        assert f"OnCalendar=Mon..Fri *-*-* {clock}:00 Asia/Kolkata" in timer
        assert f"Unit=quantdesk-observer@{name}.service" in timer and "Persistent=true" in timer
    liveness = units["quantdesk-observer-liveness.timer"]
    assert "Asia/Kolkata" in liveness and "00/5" in liveness
    assert "Unit=quantdesk-observer@liveness.service" in liveness


IPV4_LITERAL = re.compile(r"(?<!\d)(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?!\d)")


def disallowed_ipv4(text: str) -> list[str]:
    """Every IPv4 literal except the loopback network and the unspecified
    address, the only ones the observer's files need. Octets are read as
    numbers, so a zero-padded spelling is caught too."""
    found = []
    for match in IPV4_LITERAL.finditer(text):
        octets = [int(o) for o in match.groups()]
        if any(o > 255 for o in octets):
            continue                                  # not an address
        if octets[0] == 127 or octets == [0, 0, 0, 0]:
            continue
        found.append(match.group(0))
    return found


def test_address_disclosure_check_rejects_every_other_ipv4_literal():
    assert disallowed_ipv4('"api": "http://127.0.0.1:8000", "dns": "127.0.0.53",'
                           ' "bind": "0.0.0.0", "v": "999.1.2.3", "t": "12.30.00"') == []
    for literal in ("11.22.33.44",                    # globally routable
                    "203.0.113.7", "198.51.100.23",   # documentation ranges
                    "10.1.2.3", "192.168.0.9",        # private
                    "169.254.169.254"):               # link-local metadata
        assert disallowed_ipv4(f"host = {literal}") == [literal]
    assert disallowed_ipv4("http://192.0.2.10:8000/x") == ["192.0.2.10"]
    assert disallowed_ipv4("011.022.033.044") == ["011.022.033.044"]
    assert disallowed_ipv4("a 127.0.0.1 b 203.0.113.8 c") == ["203.0.113.8"]


def test_example_configuration_loads_and_carries_no_credentials(tmp_path):
    cfg = cli.load_config(str(OBSERVER / "observer.json.example"))
    assert cfg["expected_revision"] == REV and cfg["https_deployed"] is False
    assert cfg["healthchecks"]["enabled"] is False
    assert cfg["healthchecks"]["base_url"].startswith("https://")
    example = json.loads((OBSERVER / "healthchecks.json.example").read_text())
    assert set(example) == set(healthchecks.CHECKS)
    assert all(v.startswith("00000000-0000-4000-8000-") for v in example.values())
    text = "".join(p.read_text() for p in OBSERVER.rglob("*") if p.is_file()
                   and "__pycache__" not in p.parts)
    assert re.search(r"postgres(ql)?(\+\w+)?://\S+:\S+@", text) is None
    assert disallowed_ipv4(text) == []
    real = [u for u in healthchecks.ANY_UUID.findall(text)
            if not u.startswith("00000000-0000-4000-8000-")]
    assert real == []
    (tmp_path / "bad.json").write_text("{}")
    with pytest.raises(SystemExit):
        cli.load_config(str(tmp_path / "bad.json"))


# ===========================================================================
# Third review A — a checkpoint always has its notification intent
# ===========================================================================
class Crash(BaseException):
    """The process dying at a chosen point."""


def beat(root, cfg, when, post=None, timer=None):
    """One liveness invocation at `when`, as a process of its own."""
    post = Pings() if post is None else post
    return cli.liveness(cfg, records(root), lambda: when, post, timer=timer), post


def intents(root, event_type=None, check=None):
    return [n for n in records(root).notifications()
            if event_type in (None, n["event_type"]) and check in (None, n["check"])]


def exposed(root):
    return json.loads((Path(root) / "records/notification-state.json").read_text())


def trigger_probe(day=DAY):
    signals, decisions = with_gate({40: vix_stop()}, day)
    return FakeProbe(day, signals=signals, decisions=decisions)


def test_checkpoint_recorded_then_interrupted_before_its_intent_is_recovered_by_liveness(
        tmp_path, monkeypatch):
    """Codex's case A: the checkpoint was stored, the run died, and nothing ever told the owner."""
    cfg, pings = config(tmp_path, hc=True), Pings()
    run("premarket", tmp_path, cfg=cfg, post=pings)
    run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg, post=pings)

    def die(*_):
        raise Crash
    monkeypatch.setattr(cli, "ensure_attention_intent", die)
    with pytest.raises(Crash):
        run("afterclose", tmp_path, trigger_probe(), "16:00:00", cfg=cfg, post=pings)
    monkeypatch.undo()
    checkpoint = records(tmp_path).checkpoint()
    assert checkpoint["reasons"] == ["EARLY_REVIEW"]            # persisted ...
    assert intents(tmp_path, "review_attention") == []           # ... and its intent lost
    assert ("afterclose", "fail") not in pings.kinds()

    code, later = beat(tmp_path, cfg, at("16:05:00"))
    assert code == 0 and later.kinds() == [("liveness", "success"), ("afterclose", "fail")]
    (note,) = intents(tmp_path, "review_attention")              # exactly one logical intent
    assert note["notification_id"] == f"attention-{checkpoint['checkpoint_id']}"
    assert note["checkpoint_id"] == checkpoint["checkpoint_id"] and note["delivered"]
    url, body, _ = later.sent[-1]
    assert url.endswith(f"{PING_IDS['afterclose']}/fail")
    assert f"attention=YES reason=EARLY_REVIEW checkpoint={checkpoint['checkpoint_id']}" in body
    # The session's own qualification is whatever its report says; attention is separate.
    (report,) = records(tmp_path).reports(check="afterclose")
    assert report["outcome"] == "QUALIFIED" and report["result"] == "PASS"
    assert report["attention_required"] is True
    _, again = beat(tmp_path, cfg, at("16:10:00"))               # delivered: nothing more to send
    assert again.kinds() == [("liveness", "success")]
    assert len(intents(tmp_path, "review_attention")) == 1


def test_same_day_reconstruction_that_reaches_review_pending_queues_the_fail(tmp_path):
    """Codex's case B: REVIEW PENDING from a same-day reconstruction told the monitor nothing."""
    cfg, pings = config(tmp_path, hc=True), Pings()
    run("premarket", tmp_path, cfg=cfg, post=pings)
    run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg, post=pings)
    report, _ = run("afterclose", tmp_path, trigger_probe(), "16:05:00", cfg=cfg, post=pings,
                    session_date=DAY.isoformat())
    assert report["bounded_phase"]["phase"] == "REVIEW PENDING"
    assert report["outcome"] == "QUALIFIED"                      # not turned INCOMPLETE to alert
    assert intents(tmp_path, "assessment_result", "afterclose") == []   # no assessment heartbeat
    (note,) = intents(tmp_path, "review_attention")
    assert note["kind"] == "fail" and note["check"] == "afterclose" and note["delivered"]
    assert pings.kinds()[-1] == ("afterclose", "fail")
    assert pings.kinds().count(("afterclose", "fail")) == 1
    assert "attention=YES reason=EARLY_REVIEW" in pings.sent[-1][1]
    # The same with pings switched off: the intent is durable all the same.
    root = tmp_path / "off"
    cfg_off = config(root)
    run("premarket", root, cfg=cfg_off)
    run("midsession", root, midday(), "12:30:00", cfg=cfg_off)
    run("afterclose", root, trigger_probe(), "16:05:00", cfg=cfg_off,
        session_date=DAY.isoformat())
    (note,) = intents(root)
    assert note["event_type"] == "review_attention" and note["attempts"] == []
    assert exposed(root)["transport_enabled"] is False
    assert [n["notification_id"] for n in exposed(root)["outstanding"]] == [
        note["notification_id"]]


def test_a_separate_checkpoint_fail_is_dropped_only_when_the_assessment_intent_exists(tmp_path):
    cfg, pings = config(tmp_path, hc=True), Pings()
    early_review_day(tmp_path, cfg, pings)
    checkpoint = records(tmp_path).checkpoint()
    (own,) = intents(tmp_path, "assessment_result", "afterclose")
    (attention,) = intents(tmp_path, "review_attention")
    # One request spoke for both, named the checkpoint, and is recorded against both.
    assert pings.kinds().count(("afterclose", "fail")) == 1
    assert f"checkpoint={checkpoint['checkpoint_id']}" in pings.sent[-1][1]
    assert own["kind"] == "fail" and own["attention_only"] is True and own["delivered"]
    assert own["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert attention["delivered"]
    assert attention["attempts"][0]["carried_by_notification_id"] == own["notification_id"]


def test_concurrent_reconciliation_leaves_one_logical_intent(tmp_path):
    cfg = config(tmp_path)
    early_review_day(tmp_path, cfg, Pings())
    checkpoint = records(tmp_path).checkpoint()
    for path in (tmp_path / "records/outbox/notifications").glob("*"):
        path.chmod(0o644)
        path.unlink()                                           # the intent was never written
    barrier, made = threading.Barrier(8), []

    def reconcile():
        barrier.wait()
        made.append(cli.ensure_attention_intent(records(tmp_path), checkpoint, at("16:05")))
    threads = [threading.Thread(target=reconcile) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(made) == [False] * 7 + [True]
    assert len(list((tmp_path / "records/outbox/notifications").glob("*-intent.json"))) == 1
    (note,) = intents(tmp_path)
    assert note["notification_id"] == f"attention-{checkpoint['checkpoint_id']}"
    # Two whole runs at once: the lock lets one do the housekeeping and the other says so.
    held = records(tmp_path).lock()
    code, _ = beat(tmp_path, cfg, at("16:06"))
    held.close()
    assert code == 0 and len(intents(tmp_path)) == 1


def test_repeated_reconciliation_is_idempotent_and_keeps_a_valid_intent(tmp_path):
    cfg, down = config(tmp_path, hc=True), Pings(fail_first=10 ** 6)
    early_review_day(tmp_path, cfg, down)
    (note,) = intents(tmp_path, "review_attention")
    path = tmp_path / f"records/outbox/notifications/{note['notification_id']}-intent.json"
    frozen = path.read_bytes()
    calendar = cli.load_calendar(cfg)
    for minute in range(1, 6):                 # five more invocations inside the retry gap
        beat(tmp_path, cfg, at(f"16:0{minute}:00"), down)
        cli.housekeeping(records(tmp_path), cfg, calendar, at(f"16:0{minute}:30"), "liveness")
    nxt = DAY + timedelta(days=1)
    day_run(tmp_path, nxt, cfg=cfg, post=down)                    # and a whole further session
    assert len(intents(tmp_path, "review_attention")) == 1
    assert path.read_bytes() == frozen                            # not rewritten, not duplicated
    assert len([e for e in records(tmp_path).events()
                if e["type"] == "early_review_trigger"]) == 1
    assert records(tmp_path).checkpoint()["trigger_session"] == DAY.isoformat()


# ===========================================================================
# Third review B — the selector's own consistency flag
# ===========================================================================
def selector_day(consistent=True, issues=None, status=None, **contract_change):
    """A session whose 41st decision passed expiry_day and was refused by the selector."""
    stop = (trace("contract_selection", "spread_too_wide"), "spread_too_wide")
    signals, decisions = with_gate({40: stop})
    evidence = decisions[40]["evidence"]
    evidence["contract_evidence"]["trace_consistent_with_decision"] = consistent
    evidence["contract_evidence"].update(contract_change)
    if issues is not None:
        evidence["issues"] = issues
    if status is not None:
        evidence["status"] = status
    return signals, decisions


DISAGREEMENT = {"kind": "selector_trace_disagreement",
                "detail": "the traced selection does not match the decision"}
CAPTURE_ERROR = {"kind": "capture_error", "stage": "selector_trace", "detail": "KeyError: 'x'"}


def test_selector_trace_disagreement_prevents_qualified_and_the_early_trigger(tmp_path):
    """Codex's exact contradiction: status complete, no issues,
    trace_consistent_with_decision = false — returned QUALIFIED with a valid trigger."""
    signals, decisions = selector_day(consistent=True)
    good = day_run(tmp_path / "good", probe=FakeProbe(signals=signals, decisions=decisions))
    assert good["outcome"] == "QUALIFIED" and good["valid_early_review_candidates"] == 1
    assert good["bounded_phase"]["phase"] == "REVIEW PENDING"       # the consistent twin triggers

    signals, decisions = selector_day(consistent=False)
    assert decisions[40]["evidence"]["status"] == "complete"
    assert decisions[40]["evidence"]["issues"] == []
    found = checks.evidence_problems(decisions[40])
    assert found["missing"] == [] and any(
        "selector_trace_disagreement" in p for p in found["contradictory"])
    assert not checks.decision_is_sound(decisions[40], {s["id"]: s for s in signals})
    cfg = config(tmp_path, hc=True)
    pings = Pings()
    report = day_run(tmp_path, probe=FakeProbe(signals=signals, decisions=decisions), cfg=cfg,
                     post=pings)
    assert report["outcome"] == "INCOMPLETE" and report["result"] == "FAIL"
    assert report["collection_gates_passed"] is False
    link = named(report, "observation/decision linkage")
    assert link["status"] == "FAIL" and link["evidence"]["sound"] == 74
    assert "selector_trace_disagreement" in " ".join(
        link["evidence"]["contradictory_or_mislabelled"][decisions[40]["id"]])
    (candidate,) = report["expiry_day_passed_candidates"]            # seen, kept, not accepted
    assert candidate["valid_early_review_trigger"] is False
    assert candidate["contract_evidence"]["trace_consistent_with_decision"] is False
    assert report["valid_early_review_candidates"] == 0
    assert report["bounded_phase"]["phase"] == "BOUNDED OBSERVATION"
    assert records(tmp_path).checkpoint() is None and report["attention_required"] is False
    assert phase(tmp_path, cfg)["qualifying_sessions"] == []
    assert pings.kinds()[-1] == ("afterclose", "fail")               # INCOMPLETE, not attention
    assert intents(tmp_path, "review_attention") == []


def test_a_disagreement_the_producer_labelled_honestly_does_not_qualify_either(tmp_path):
    signals, decisions = selector_day(False, [DISAGREEMENT], "inconsistent")
    found = checks.evidence_problems(decisions[40])
    assert found["missing"] == []                   # structurally whole, and still a contradiction
    assert found["contradictory"] == [
        "the selector trace disagrees with the decision (selector_trace_disagreement)"]
    report = day_run(tmp_path, probe=FakeProbe(signals=signals, decisions=decisions))
    assert report["outcome"] == "INCOMPLETE" and report["valid_early_review_candidates"] == 0
    assert records(tmp_path).checkpoint() is None


def test_legitimately_unavailable_selector_values_remain_structurally_acceptable(tmp_path):
    # A: present and null. The producer writes null for what it could not capture.
    signals, decisions = selector_day(True, alternatives=None, result=None, pick=None,
                                      levels=None, expiry=None,
                                      universe={"definition": "LiveChain.quotes(now)",
                                                "quotes": None})
    assert checks.evidence_problems(decisions[40]) == {"missing": [], "contradictory": []}
    report = day_run(tmp_path, probe=FakeProbe(signals=signals, decisions=decisions))
    assert report["outcome"] == "QUALIFIED" and report["valid_early_review_candidates"] == 1
    # B: selection never reached — nothing to be consistent with.
    signals, decisions = session()
    assert decisions[0]["evidence"]["contract_evidence"]["status"] == "selection_not_reached"
    assert checks.evidence_problems(decisions[0]) == {"missing": [], "contradictory": []}
    # The trace was not captured and the producer said so: unavailable, not contradictory.
    # It does not qualify — the record is not `complete` — but for that reason only.
    signals, decisions = selector_day(None, [CAPTURE_ERROR], "capture_incomplete")
    assert checks.evidence_problems(decisions[40]) == {"missing": [], "contradictory": []}
    assert not checks.decision_is_sound(decisions[40], {s["id"]: s for s in signals})
    report = day_run(tmp_path / "b", probe=FakeProbe(signals=signals, decisions=decisions))
    link = named(report, "observation/decision linkage")
    assert report["outcome"] == "INCOMPLETE"
    assert link["evidence"]["contradictory_or_mislabelled"] == {}
    assert link["evidence"]["missing_required_evidence"] == {}


@pytest.mark.parametrize("kwargs, expected", [
    # null is legitimate only beside the capture error that explains it
    ({"consistent": None}, "absent with no capture error"),
    # false must be filed as an issue; complete cannot carry it
    ({"consistent": False}, "not filed among the issues"),
    ({"consistent": False, "issues": [DISAGREEMENT]}, "status is 'complete' where"),
    # an issue with nothing behind it
    ({"consistent": True, "issues": [DISAGREEMENT], "status": "inconsistent"},
     "does not record one"),
    # status_for: the worst issue decides, and no issue cannot be worse than complete
    ({"consistent": True, "status": "inconsistent"}, "gives 'complete'"),
    ({"consistent": None, "issues": [CAPTURE_ERROR], "status": "inconsistent"},
     "gives 'capture_incomplete'"),
    ({"consistent": True, "issues": [{"kind": "something_new"}], "status": "partial"},
     "gives 'capture_incomplete'"),
    ({"consistent": "yes"}, "trace_consistent_with_decision is 'yes'"),
])
def test_producer_defined_conditional_requirements_are_enforced(tmp_path, kwargs, expected):
    signals, decisions = selector_day(**kwargs)
    found = checks.evidence_problems(decisions[40])
    assert any(expected in p for p in found["contradictory"]), found
    report = day_run(tmp_path, probe=FakeProbe(signals=signals, decisions=decisions))
    assert report["outcome"] == "INCOMPLETE" and report["valid_early_review_candidates"] == 0


def test_the_status_rule_accepts_what_the_producer_would_write():
    signals, decisions = session()
    for linkage, issues, status in (
            ("linked", [], "complete"), ("unlinked", [], "partial"),
            ("linked", [{"kind": "clock_inconsistent"}], "inconsistent"),
            ("linked", [{"kind": "gate_unreconciled"}], "capture_incomplete")):
        d = decision(signals[0])
        d["evidence"].update(issues=issues, status=status)
        d["evidence"]["vector"]["identity"]["linkage"] = linkage
        assert checks.evidence_problems(d)["contradictory"] == [], (linkage, status)


# ===========================================================================
# Third review C — the fifth-session deadline is noticed by whatever runs next
# ===========================================================================
def four_sessions(root, cfg, post=None):
    days = weekdays(5)
    for day in days[:4]:
        assert day_run(root, day, cfg=cfg, post=post)["outcome"] == "QUALIFIED"
    return days


def test_liveness_detects_the_missing_fifth_session_report_at_the_deadline(tmp_path):
    """Codex's finding: four reports, the fifth session's 16:30 passes with no report,
    liveness runs, sends success, records nothing."""
    cfg = config(tmp_path, hc=True)
    days = four_sessions(tmp_path, cfg, Pings())
    fifth = days[4]
    code, before = beat(tmp_path, cfg, at("16:29:59", fifth))
    assert code == 0 and before.kinds() == [("liveness", "success")]
    assert records(tmp_path).checkpoint() is None
    seen = []

    def post(url, body, timeout):
        seen.append((url.rsplit("/", 2)[-2:], records(tmp_path).checkpoint() is not None))
        return 200
    assert cli.liveness(cfg, records(tmp_path), lambda: at("16:30:00", fifth), post) == 0
    # The heartbeat went first, before any housekeeping; the /fail followed the checkpoint.
    assert seen[0] == (["hc.invalid", PING_IDS["liveness"]], False)
    assert seen[1] == ([PING_IDS["afterclose"], "fail"], True) and len(seen) == 2
    checkpoint = records(tmp_path).checkpoint()
    assert checkpoint["phase"] == "REVIEW PENDING"
    assert checkpoint["reasons"] == ["BOUNDED_WINDOW_ENDED_WITH_UNRESOLVED_EVIDENCE"]
    assert checkpoint["trigger_session"] == fifth.isoformat()
    assert checkpoint["window_deadline_at_ist"] == at("16:30:00", fifth).isoformat()
    assert checkpoint["detected_at_ist"] == at("16:30:00", fifth).isoformat()
    details = checkpoint["window_details"]
    assert details["missing_reports"] == [fifth.isoformat()]          # not invented
    assert details["established_qualifying_sessions"] == 4            # not rounded up to five
    assert details["unresolved_session_qualifications"] == {}
    assert details["checkpoint_due_at_ist"] == at("16:30:00", fifth).isoformat()
    assert checkpoint["qualifying_sessions"] == [d.isoformat() for d in days[:4]]
    assert records(tmp_path).reports(fifth.isoformat()) == []         # still no fifth report
    (note,) = intents(tmp_path, "review_attention")
    assert note["delivered"] and note["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert ("bounded_window_ended", f"checkpoint-{checkpoint['checkpoint_id']}") in events(
        tmp_path)
    state = json.loads((tmp_path / "records/state.json").read_text())
    assert state["phase"] == "REVIEW PENDING" and state["attention_required"] is True


def test_recovery_after_downtime_detects_the_endpoint_and_keeps_both_times(tmp_path):
    cfg = config(tmp_path, hc=True)
    days = four_sessions(tmp_path, cfg, Pings())
    fifth, woke = days[4], at("03:10:00", days[4] + timedelta(days=3))
    code, pings = beat(tmp_path, cfg, woke)          # the first invocation of any kind since
    checkpoint = records(tmp_path).checkpoint()
    assert code == 0 and pings.kinds() == [("liveness", "success"), ("afterclose", "fail")]
    assert checkpoint["window_deadline_at_ist"] == at("16:30:00", fifth).isoformat()
    assert checkpoint["detected_at_ist"] == woke.isoformat()        # late, and recorded as late
    assert checkpoint["trigger_session"] == fifth.isoformat()       # the window was not extended
    assert checkpoint["window"]["sessions"][-1] == fifth.isoformat()
    # The same identity whoever notices it: an assessment run instead of liveness.
    other = tmp_path / "b"
    cfg_b = config(other, hc=True)
    four_sessions(other, cfg_b, Pings())
    sixth = days[4] + timedelta(days=1)
    run("premarket", other, FakeProbe(sixth), day=sixth, cfg=cfg_b, post=Pings())
    assert records(other).checkpoint()["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert records(other).checkpoint()["reasons"] == checkpoint["reasons"]


def test_unknown_sessions_are_not_counted_and_reports_are_not_invented(tmp_path):
    cfg = config(tmp_path, hc=True)
    days = weekdays(5)
    for day in days[:3]:
        day_run(tmp_path, day, cfg=cfg, post=Pings())
    day_run(tmp_path, days[3], probe=FakeProbe(days[3], vix=None), cfg=cfg, post=Pings())
    beat(tmp_path, cfg, at("16:30:00", days[4]))
    details = records(tmp_path).checkpoint()["window_details"]
    assert details["established_qualifying_sessions"] == 3
    assert details["unresolved_session_qualifications"] == {days[3].isoformat(): "INCOMPLETE"}
    assert details["missing_reports"] == [days[4].isoformat()]
    assert "not extended" in details["note"]


def test_an_unknown_calendar_does_not_manufacture_a_fifth_session_endpoint(tmp_path):
    cfg = config(tmp_path, reviewed=False, hc=True)
    code, pings = beat(tmp_path, cfg, at("23:00:00", date(2026, 12, 1)))
    assert code == 0 and pings.kinds() == [("liveness", "success")]
    assert records(tmp_path).checkpoint() is None and intents(tmp_path) == []
    state = json.loads((tmp_path / "records/state.json").read_text())
    assert state["phase"] == "BOUNDED OBSERVATION" and state["window"]["status"] == "UNKNOWN"
    assert "has not been reviewed" in state["calendar_uncertainty"]
    assert state["window"]["unresolved_date"] == "2026-10-14"
    # One unreviewed date before the fifth session is enough to leave it unnamed.
    cfg = config(tmp_path / "b", hc=True)
    calendar = json.loads(Path(cfg["calendar_file"]).read_text())
    calendar["dates"]["2026-10-16"]["review_status"] = "UNREVIEWED"
    Path(cfg["calendar_file"]).write_text(json.dumps(calendar))
    beat(tmp_path / "b", cfg, at("23:00:00", date(2026, 11, 2)))
    assert records(tmp_path / "b").checkpoint() is None
    # The shipped calendar: every date unreviewed, so the real endpoint stays UNKNOWN.
    shipped = cli.load_calendar({"calendar_file": str(OBSERVER / "session_calendar.json")})
    statuses = {e["review_status"] for e in shipped["dates"].values()}
    assert statuses == {"UNREVIEWED"} and len(shipped["dates"]) == 79
    assert cli.window(shipped, cli.DEFAULTS)["status"] == "UNKNOWN"


@pytest.mark.parametrize("transport", ["down", "broken", "off", "misconfigured"])
def test_transport_failure_does_not_prevent_checkpoint_persistence(tmp_path, transport):
    cfg = config(tmp_path, hc=transport != "off")
    days = four_sessions(tmp_path, cfg, Pings())
    post = {"down": Pings(fail_first=10 ** 6), "off": Pings(), "misconfigured": Pings(),
            "broken": Pings(fail_first=10 ** 6, error=RuntimeError("transport bug"))}[transport]
    if transport == "misconfigured":
        cfg["healthchecks"]["base_url"] = "http://hc.invalid"
    code, _ = beat(tmp_path, cfg, at("16:30:00", days[4]), post)
    checkpoint = records(tmp_path).checkpoint()
    assert checkpoint["reasons"] == ["BOUNDED_WINDOW_ENDED_WITH_UNRESOLVED_EVIDENCE"]
    (note,) = intents(tmp_path, "review_attention")
    assert not note["delivered"] and note["checkpoint_id"] == checkpoint["checkpoint_id"]
    (outstanding,) = exposed(tmp_path)["outstanding"]
    assert outstanding["notification_id"] == note["notification_id"]
    assert code == (0 if transport == "off" else 1)     # a lost heartbeat is itself reported
    if transport == "misconfigured":
        assert post.sent == []


# ===========================================================================
# Third review E — one request per identity, a 30-second budget, spaced rounds
# ===========================================================================
def never_sleep(seconds):
    raise AssertionError("the observer waited between notification attempts")


def cfg_file(root, cfg):
    path = Path(root) / "observer.json"
    path.write_text(json.dumps(cfg))
    return str(path)


def process(path, when, post):
    """A fresh observer process: nothing but the stored files carries over."""
    before = len(post.sent)
    code = cli.main(["liveness", "--config", path], post=post, clock=lambda: when,
                    sleep=never_sleep, alarm=False)
    return code, post.kinds()[before:]


def last_attempt(root, event_type, check):
    (note,) = intents(root, event_type, check)
    return note, datetime.fromisoformat(note["attempts"][-1]["attempted_at_utc"])


def test_rounds_are_five_minutes_apart_then_hourly_across_processes_with_no_cutoff(tmp_path):
    cfg, down = config(tmp_path, hc=True), Pings(fail_first=10 ** 6)
    path = cfg_file(tmp_path, cfg)
    run("premarket", tmp_path, FakeProbe(revision="f" * 40), cfg=cfg, post=down)
    wanted = ("premarket", "fail")
    note, t = last_attempt(tmp_path, "assessment_result", "premarket")
    assert len(note["attempts"]) == 1
    for round_ in range(2, 7):                         # rounds two to six: five minutes apart
        assert wanted not in process(path, t + timedelta(minutes=4, seconds=59), down)[1]
        state = exposed(tmp_path)["outstanding"][0]     # visible while it waits
        assert state["delivery_state"] == cli.WAITING and state["rounds"] == round_ - 1
        assert datetime.fromisoformat(state["next_eligible_at_utc"]) == t + timedelta(minutes=5)
        assert state["last_error"] == "OSError: network unreachable"
        assert process(path, t + timedelta(minutes=5), down)[1].count(wanted) == 1
        note, t = last_attempt(tmp_path, "assessment_result", "premarket")
        assert len(note["attempts"]) == round_
    for gap in ({"minutes": 5}, {"minutes": 59, "seconds": 59}):   # after six: an hour
        assert wanted not in process(path, t + timedelta(**gap), down)[1]
    for round_ in range(7, 31):                        # and no round after which it gives up
        assert len(note["attempts"]) == round_ - 1
        assert process(path, t + timedelta(hours=1), down)[1].count(wanted) == 1
        note, t = last_attempt(tmp_path, "assessment_result", "premarket")
        assert wanted not in process(path, t + timedelta(minutes=30), down)[1]
    assert [a["attempt_number"] for a in note["attempts"]] == list(range(1, 31))
    assert all(a["receipt"] == healthchecks.UNDELIVERED for a in note["attempts"])
    up = Pings()                                       # delivered once, then left alone
    assert process(path, t + timedelta(hours=1), up) == (0, [("liveness", "success"), wanted])
    assert process(path, t + timedelta(hours=2), up) == (0, [("liveness", "success")])
    note, _ = last_attempt(tmp_path, "assessment_result", "premarket")
    assert note["delivered"] and len(note["attempts"]) == 31
    assert exposed(tmp_path)["outstanding"] == []


def test_checkpoint_attention_is_never_given_up_until_acknowledged(tmp_path):
    cfg, down = config(tmp_path, hc=True), Pings(fail_first=10 ** 6)
    path = cfg_file(tmp_path, cfg)
    early_review_day(tmp_path, cfg, down)
    note, t = last_attempt(tmp_path, "review_attention", "afterclose")
    for _ in range(5):
        t += timedelta(minutes=5)
        assert ("afterclose", "fail") in process(path, t, down)[1]
    for _ in range(72):                                # three days of hourly rounds
        t += timedelta(hours=1)
        assert ("afterclose", "fail") in process(path, t, down)[1]
    note, _ = last_attempt(tmp_path, "review_attention", "afterclose")
    assert len(note["attempts"]) == 78 and not note["delivered"]
    history = [a["attempted_at_utc"] for a in note["attempts"]]
    acknowledge(tmp_path)                              # ends its retries, and only that
    for hours in (1, 2, 30):
        assert process(path, t + timedelta(hours=hours), Pings())[1] == [("liveness", "success")]
    note, _ = last_attempt(tmp_path, "review_attention", "afterclose")
    assert [a["attempted_at_utc"] for a in note["attempts"]] == history   # history kept
    assert exposed(tmp_path)["outstanding"] == []
    states = {n["notification_id"]: n["delivery_state"]
              for n in cli.notification_states(records(tmp_path), t + timedelta(hours=40))}
    assert states[note["notification_id"]].endswith("ended by acknowledgment of the checkpoint")
    assert phase(tmp_path, cfg)["phase"] == "REVIEW PENDING"             # not reopened


def troubled_days(root, cfg, post):
    """Day one qualifies and triggers early review; day two fails all three assessments."""
    days = weekdays(2)
    early_review_day(root, cfg, post, days[0])
    two = days[1]
    run("premarket", root, FakeProbe(two, revision="f" * 40), day=two, cfg=cfg, post=post)
    run("midsession", root, midday(two, **{"/health": {"status": "down"}}), "12:30:00", two,
        cfg, post=post)
    report, _ = run("afterclose", root, FakeProbe(two, vix=None), "16:00:00", two, cfg,
                    post=post)
    assert report["outcome"] == "INCOMPLETE"
    return days


def test_one_request_per_identity_and_four_in_all_per_invocation(tmp_path):
    cfg, down = config(tmp_path, hc=True), Pings(fail_first=10 ** 6)
    days = troubled_days(tmp_path, cfg, down)
    outstanding = [n for n in cli.notification_states(records(tmp_path), at("03:00", days[1]
                   + timedelta(days=1))) if n["outstanding"]]
    assert sorted((n["check"], n["event_type"]) for n in outstanding) == [
        ("afterclose", "assessment_result"), ("afterclose", "review_attention"),
        ("midsession", "assessment_result"), ("premarket", "assessment_result")]
    code, up = beat(tmp_path, cfg, at("03:00", days[1] + timedelta(days=1)))
    assert code == 0 and up.kinds() == [("liveness", "success"), ("premarket", "fail"),
                                        ("midsession", "fail"), ("afterclose", "fail")]
    assert all(timeout == 5.0 for _, _, timeout in up.sent)           # five seconds each
    assert healthchecks.REQUEST_TIMEOUT == 5.0 and cli.MAX_REQUESTS == 4
    # The one after-close request spoke for the newest assessment and for the checkpoint.
    checkpoint = records(tmp_path).checkpoint()
    body = up.sent[-1][1]
    assert f"session={days[1]}" in body and f"checkpoint={checkpoint['checkpoint_id']}" in body
    assert all(n["delivered"] for n in intents(tmp_path) if n["notification_id"] in
               {o["notification_id"] for o in outstanding})
    assert exposed(tmp_path)["outstanding"] == []
    assert exposed(tmp_path)["requests_made_by_this_invocation"] == [
        "liveness", "premarket", "midsession", "afterclose"]
    # A fifth request is refused whatever asks for it, and so is a second for one identity.
    out = cli.Outbound(records(tmp_path / "o"), cfg, lambda: at("03:00"), Pings())
    for name in healthchecks.CHECKS:
        assert out.send(name, "fail", "x")["delivered"]
    assert out.send("premarket", "fail", "x") is None and len(out.post.sent) == 4
    assert out.withheld[0]["reason"] == "this identity already had its one request"


class SlowPings(Pings):
    """A transport on which every request takes `seconds` of wall clock."""

    def __init__(self, seconds, **kw):
        super().__init__(**kw)
        self.seconds, self.elapsed = seconds, 0.0

    def __call__(self, url, body, timeout):
        self.elapsed += self.seconds
        return super().__call__(url, body, timeout)

    def timer(self):
        return self.elapsed


def test_notification_work_stops_at_thirty_seconds(tmp_path, capsys):
    cfg, down = config(tmp_path, hc=True), Pings(fail_first=10 ** 6)
    days = troubled_days(tmp_path, cfg, down)
    when = at("03:00", days[1] + timedelta(days=1))
    cache = (tmp_path / "records/notification-state.json").read_bytes()
    capsys.readouterr()
    slow = SlowPings(14.0)
    code, _ = beat(tmp_path, cfg, when, slow, timer=slow.timer)
    assert slow.kinds() == [("liveness", "success"), ("premarket", "fail"),
                            ("midsession", "fail")]                  # 14 s, 28 s, 42 s: stop
    assert [timeout for _, _, timeout in slow.sent] == [5.0, 5.0, 2.0]   # never past the budget
    # 0.4.5: after the deadline the notification-state cache is not rebuilt; what was
    # withheld is said locally instead.
    assert (tmp_path / "records/notification-state.json").read_bytes() == cache
    printed = capsys.readouterr().out
    assert "NOTIFICATION DEADLINE" in printed
    # 0.4.6: past the deadline the after-close alert is not even prepared (its body and
    # kind are preparation), so it is not listed as withheld; the deadline line says where
    # the work stopped.
    assert "preparing the afterclose alert" in printed
    assert "NOTIFICATION WITHHELD: afterclose" not in printed
    shown = {n["check"]: n for n in cli.notification_states(records(tmp_path), when,
                                                            tolerant=True) if n["outstanding"]}
    assert set(shown) == {"afterclose"}                                   # kept, not dropped
    assert shown["afterclose"]["delivery_state"] == cli.DUE
    assert cli.NOTIFY_BUDGET_SECONDS == 30.0 and code == 0
    _, nxt = beat(tmp_path, cfg, when + timedelta(minutes=5))             # the next invocation
    assert nxt.kinds() == [("liveness", "success"), ("afterclose", "fail")]


def test_a_request_that_hangs_is_cut_off_by_the_budget_itself(tmp_path, monkeypatch):
    """The wall-clock limit is enforced, not merely requested of the socket."""
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 2.0)
    cfg = config(tmp_path, hc=True)

    def hang(url, body, timeout):
        threading.Event().wait(20)
        return 200
    out = cli.Outbound(records(tmp_path), cfg, lambda: at("09:00"), hang, alarm=True)
    began = datetime.now()
    outcome = out.send("liveness", "success", "x")
    assert (datetime.now() - began).total_seconds() < 5
    assert outcome["delivered"] is False and "notification budget" in outcome["result"]
    assert outcome["receipt"] == "Notification undelivered; remote receipt unknown."


def test_no_code_path_waits_or_loops_between_attempts(tmp_path, monkeypatch):
    def forbidden(seconds):
        raise AssertionError("slept")
    monkeypatch.setattr(cli.time_module, "sleep", forbidden)
    monkeypatch.setattr(store.time, "sleep", forbidden)
    cfg, down = config(tmp_path, hc=True), Pings(fail_first=10 ** 6)
    troubled_days(tmp_path, cfg, down)
    before = len(down.sent)
    for minute in range(3):
        beat(tmp_path, cfg, at(f"03:0{minute}", date(2026, 10, 16)), down)
    assert len(down.sent) - before <= 4 + 1 + 1          # one round, then heartbeats only
    source = (OBSERVER / "quantdesk_observer/healthchecks.py").read_text()
    assert "sleep" not in source and "while " not in source and "for attempt" not in source
    cli_source = (OBSERVER / "quantdesk_observer/cli.py").read_text()
    for name in ("deliver_round", "notification_states", "Outbound", "housekeeping", "liveness"):
        body = cli_source.split(f"def {name}(")[-1].split("\ndef ")[0] if name != "Outbound" \
            else cli_source.split("class Outbound:")[1].split("\ndef ")[0]
        assert "sleep" not in body and "while " not in body, name
    units = "".join(p.read_text() for p in (OBSERVER / "systemd").iterdir())
    assert "Restart=" not in "\n".join(
        line for line in units.splitlines() if not line.startswith("#"))
    assert len(list((OBSERVER / "systemd").iterdir())) == 5      # no new timer or service


def test_an_obsolete_failure_is_not_replayed_over_a_newer_recovery(tmp_path):
    cfg, down = config(tmp_path, hc=True), Pings(fail_first=10 ** 6)
    run("premarket", tmp_path, FakeProbe(revision="f" * 40), cfg=cfg, post=down)
    nxt = DAY + timedelta(days=1)
    up = Pings()
    run("premarket", tmp_path, FakeProbe(nxt), day=nxt, cfg=cfg, post=up)
    assert up.kinds() == [("premarket", "success")]
    _, later = beat(tmp_path, cfg, at("09:30", nxt))
    assert later.kinds() == [("liveness", "success")]
    old = intents(tmp_path, "assessment_result", "premarket")[0]
    states = cli.notification_states(records(tmp_path), at("09:30", nxt))
    assert "superseded by a newer assessment" in next(
        n["delivery_state"] for n in states if n["notification_id"] == old["notification_id"])
    # Even when the newer recovery itself was not delivered, the old failure stays unsent.
    root = tmp_path / "b"
    cfg_b = config(root, hc=True)
    run("premarket", root, FakeProbe(revision="f" * 40), cfg=cfg_b, post=down)
    run("premarket", root, FakeProbe(nxt), day=nxt, cfg=cfg_b, post=down)
    _, later = beat(root, cfg_b, at("09:30", nxt))
    assert later.kinds() == [("liveness", "success")]      # and no success is sent late either


def test_no_success_is_fabricated_to_force_another_alert_or_a_recovery(tmp_path):
    cfg, pings = config(tmp_path, hc=True), Pings()
    days = weekdays(3)
    early_review_day(tmp_path, cfg, pings, days[0])
    for hours in range(1, 6):                    # delivered /fail: nothing is re-sent, and no
        _, seen = beat(tmp_path, cfg, at("16:00", days[0]) + timedelta(hours=hours))
        assert seen.kinds() == [("liveness", "success")]      # success is used to re-arm it
    acknowledge(tmp_path)
    _, seen = beat(tmp_path, cfg, at("23:00", days[0]))
    assert seen.kinds() == [("liveness", "success")]          # an ack manufactures no recovery
    assert ("afterclose", "success") not in pings.kinds()
    report = day_run(tmp_path, days[1], cfg=cfg, post=pings)  # the next assessment supports it
    assert report["outcome"] == "QUALIFIED" and pings.kinds()[-1] == ("afterclose", "success")
    assert not any(hasattr(module, name) for module in (cli, healthchecks)
                   for name in ("recover", "rearm", "force_alert"))


def test_acknowledgment_ends_checkpoint_retries_but_not_independent_failures(tmp_path):
    cfg, down = config(tmp_path, hc=True), Pings(fail_first=10 ** 6)
    days = troubled_days(tmp_path, cfg, down)
    acknowledge(tmp_path)
    when = at("03:00", days[1] + timedelta(days=1))
    states = {(n["check"], n["event_type"], n["session_date"]): n
              for n in cli.notification_states(records(tmp_path), when)}
    assert not states[("afterclose", "review_attention", days[0].isoformat())]["outstanding"]
    # Day one's after-close failed for the checkpoint alone; day two's failed on its own.
    assert states[("afterclose", "assessment_result", days[1].isoformat())]["outstanding"]
    assert states[("premarket", "assessment_result", days[1].isoformat())]["outstanding"]
    _, up = beat(tmp_path, cfg, when)
    assert up.kinds() == [("liveness", "success"), ("premarket", "fail"),
                          ("midsession", "fail"), ("afterclose", "fail")]
    body = up.sent[-1][1]
    assert "outcome=INCOMPLETE" in body and "checkpoint=" not in body
    assert "attention=ACKNOWLEDGED" in body and "attention=YES" not in body
    assert phase(tmp_path, cfg)["qualifying_sessions"] == [days[0].isoformat()]   # no exception
    # An after-close failure that existed only because of the checkpoint ends with it.
    root = tmp_path / "b"
    cfg_b = config(root, hc=True)
    early_review_day(root, cfg_b, Pings(fail_first=10 ** 6))
    acknowledge(root)
    _, seen = beat(root, cfg_b, at("23:00"))
    assert seen.kinds() == [("liveness", "success")]
    (own,) = intents(root, "assessment_result", "afterclose")
    assert own["attention_only"] is True and not own["delivered"] and len(own["attempts"]) == 1


def test_undelivered_state_is_shown_on_every_invocation_and_claims_no_delivery(tmp_path, capsys):
    cfg, down = config(tmp_path, hc=True), Pings(fail_first=10 ** 6,
                                                 error=TimeoutError("timed out"))
    path = cfg_file(tmp_path, cfg)
    early_review_day(tmp_path, cfg, down)
    note, t = last_attempt(tmp_path, "review_attention", "afterclose")
    capsys.readouterr()
    assert process(path, t + timedelta(minutes=1), down) == (1, [("liveness", "success")])
    printed = capsys.readouterr().out
    assert "Notification undelivered; remote receipt unknown. afterclose review_attention" \
        in printed
    assert "heartbeat: Notification undelivered; remote receipt unknown." in printed
    shown = exposed(tmp_path)
    states = {n["event_type"]: n for n in shown["outstanding"]}
    waiting = states["review_attention"]
    assert waiting["delivery_state"] == cli.WAITING and waiting["rounds"] == 1
    assert waiting["last_attempt_at_utc"] == note["attempts"][0]["attempted_at_utc"]
    assert waiting["last_error"] == "TimeoutError: timed out"
    assert waiting["last_receipt"] == "Notification undelivered; remote receipt unknown."
    assert waiting["next_eligible_at_utc"] and waiting["attention_condition_current"] is True
    assert waiting["checkpoint_id"] == records(tmp_path).checkpoint()["checkpoint_id"]
    assert "not proof" in shown["meaning"] and "cause unknown" in shown["meaning"]
    assert cli.main(["status", "--config", path], clock=lambda: t + timedelta(minutes=2)) == 0
    status = json.loads(capsys.readouterr().out)
    assert {n["event_type"] for n in status["notifications_outstanding"]} == set(states)
    # Delivered means the endpoint answered 2xx — said in those words, and no more.
    up = Pings()
    process(path, t + timedelta(minutes=5), up)
    note, _ = last_attempt(tmp_path, "review_attention", "afterclose")
    assert note["delivered"] and "not observed" in note["attempts"][-1]["receipt"]
    assert "does not promise exactly-once delivery" in (OBSERVER / "README.md").read_text()


# ===========================================================================
# Third review F — what fails stays separate from what works
# ===========================================================================
def test_a_failed_notification_round_does_not_prevent_reconciliation(tmp_path, monkeypatch):
    cfg = config(tmp_path, hc=True)
    days = four_sessions(tmp_path, cfg, Pings())

    def broken(*_, **__):
        raise RuntimeError("delivery code is broken")
    monkeypatch.setattr(cli, "deliver_round", broken)
    code, pings = beat(tmp_path, cfg, at("16:30:00", days[4]))
    assert pings.kinds() == [("liveness", "success")] and code == 1
    assert records(tmp_path).checkpoint() is not None            # reconciled all the same
    assert len(intents(tmp_path, "review_attention")) == 1
    assert ("operational_failure", "liveness-notification-round") in events(tmp_path)
    # And in an assessment run.
    monkeypatch.undo()
    root = tmp_path / "b"
    cfg_b = config(root, hc=True)
    run("premarket", root, cfg=cfg_b, post=Pings())
    run("midsession", root, midday(), "12:30:00", cfg=cfg_b, post=Pings())
    monkeypatch.setattr(cli, "deliver_round", broken)
    report, _ = run("afterclose", root, trigger_probe(), "16:00:00", cfg=cfg_b, post=Pings())
    assert report["outcome"] == "QUALIFIED" and records(root).checkpoint() is not None
    assert len(intents(root, "review_attention")) == 1
    assert ("operational_failure", "afterclose-notification-round") in events(root)


def test_a_delivered_heartbeat_does_not_conceal_a_housekeeping_failure(tmp_path, capsys):
    cfg = config(tmp_path, hc=True)
    run("premarket", tmp_path, cfg=cfg, post=Pings())
    (report,) = (tmp_path / "records/reports/2026-10-14").glob("*.json")
    report.chmod(0o644)
    report.write_text(report.read_text().replace('"result": "PASS"', '"result": "FAIL"'))
    code, pings = beat(tmp_path, cfg, at("09:00:00"))
    assert pings.kinds()[0] == ("liveness", "success")           # the heartbeat was delivered
    assert pings.kinds()[1:] == [("afterclose", "fail")]         # and the fault raised at once
    assert code == 1                                             # and the run still failed
    printed = capsys.readouterr().out
    assert "HOUSEKEEPING FAULT" in printed and "CorruptRecord" in printed
    assert "heartbeat delivered" in printed
    (event,) = [e for e in records(tmp_path).events() if e["subject"] == "liveness-housekeeping"]
    assert event["type"] == "operational_failure" and "does not match its own content hash" \
        in event["summary"]
    (shown,) = exposed(tmp_path)["housekeeping_faults"]
    assert "liveness-housekeeping failed" in shown
    # The assessment checks carry it to the monitor: the next assessment fails outright.
    later = Pings()
    result, _ = run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg, post=later)
    assert result["result"] == "FAIL" and later.kinds() == [("midsession", "fail")]


def test_the_late_run_limit_bounds_execution_and_never_makes_a_run_timely(tmp_path, monkeypatch):
    assert cli.DEFAULTS["late_run_seconds"] == 180
    seen = []
    real = cli.time_limit

    def spy(seconds, enabled):
        seen.append(seconds)
        return real(seconds, False)
    monkeypatch.setattr(cli, "time_limit", spy)
    for check, start, probe in (("premarket", "08:45:01", FakeProbe()),
                                ("midsession", "12:45:01", midday()),
                                ("afterclose", "16:30:01", FakeProbe())):
        report, _ = run(check, tmp_path / check, probe, start)   # done within seconds, and late
        assert 170 <= seen[-1] <= 180 and report["ran_checks"]     # 180 s, wait included
        assert report["timely"] is False and not report["started_in_window"]
        assert "not timely" in " ".join(report["failure_reasons"])
        assert report["deadline_at_ist"].endswith({"premarket": "08:45:00+05:30",
                                                   "midsession": "12:45:00+05:30",
                                                   "afterclose": "16:30:00+05:30"}[check])
        assert cli.check_status(records(tmp_path / check), DAY.isoformat(), check) != "PASS"
    assert report["outcome"] == "OPERATIONAL EXCEPTION REQUIRED"     # evidence kept, not timely
    # A timeout keeps what was finished, says so, and assumes nothing else.
    probe = FakeProbe(vix=RunTimeout("the run exceeded its 180s limit"))
    cut, _ = run("afterclose", tmp_path / "cut", probe, "18:00:00")
    assert cut["timed_out"] is True and cut["result"] == "FAIL" and cut["outcome"] == "INCOMPLETE"
    assert "exceeded its 180s limit" in cut["failure_reasons"][0]
    assert [c["name"] for c in cut["checks"]][-1] == "NSE archive timestamps"
    assert "session VIX close" not in [c["name"] for c in cut["checks"]]
    assert cut["evidence_reads"] and cut["valid_early_review_candidates"] == 0
    assert "journal" in probe.calls and cut["unknown_fields"] == []


def test_synthetic_identity_evidence_claims_no_azure_verification():
    readme = " ".join((OBSERVER / "README.md").read_text().split())
    assert "has not been read on the Azure VM" in readme
    assert "does not carry the job id `nifty-agent`" in readme
    assert "A matching interval is not accepted as identity." in readme
    basis = checks.schedule_basis(schedule_log(), DAY)
    assert basis["status"] == "ESTABLISHED" and "identity_note" in basis
    assert "verified" not in json.dumps({k: v for k, v in basis.items() if k != "slots"},
                                        default=str).lower()


# ===========================================================================
# Fourth review A — a request is reserved on disk before it is made
# ===========================================================================
NOTES = "records/outbox/notifications"


def files(root, pattern):
    return sorted(p.name for p in (Path(root) / NOTES).glob(pattern))


def crash_on(check, after_sending):
    """A transport that dies on `check`: after the request left, or just before it would."""
    pings = Pings()

    def post(url, body, timeout):
        mine = PING_IDS[check] in url
        if mine and not after_sending:
            raise Crash
        status = pings(url, body, timeout)
        if mine:
            raise Crash
        return status
    return pings, post


@pytest.mark.parametrize("after_sending", [True, False])
def test_an_interrupted_request_is_on_record_and_is_not_sent_again_early(tmp_path,
                                                                         after_sending):
    """Codex's finding (after_sending=True): the after-close /fail left, the process died
    before recording it, and liveness one second later sent the same /fail again. The
    other case stops after the reservation and before any transmission."""
    cfg = config(tmp_path, hc=True)
    path = cfg_file(tmp_path, cfg)
    run("premarket", tmp_path, cfg=cfg, post=Pings())
    run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg, post=Pings())
    pings, post = crash_on("afterclose", after_sending)
    with pytest.raises(Crash):
        run("afterclose", tmp_path, trigger_probe(), "16:00:00", cfg=cfg, post=post)
    assert pings.kinds() == ([("afterclose", "fail")] if after_sending else [])
    open_ = [f for f in files(tmp_path, "*-reservation.json")
             if f.replace("reservation", "outcome") not in files(tmp_path, "*-outcome.json")]
    assert len(open_) == 1                                       # durable, and never closed
    assert json.loads((tmp_path / NOTES / open_[0]).read_text())["check"] == "afterclose"
    notes = intents(tmp_path, check="afterclose")
    assert sorted(n["event_type"] for n in notes) == ["assessment_result", "review_attention"]
    for note in notes:
        (attempt,) = note["attempts"]                            # it counts as a round
        assert attempt["completion"] == "unknown" and attempt["delivered"] is False
        assert attempt["receipt"] == "Notification undelivered; remote receipt unknown."
        assert "interrupted" in attempt["failure_reason"] and attempt["attempt_number"] == 1
        assert not note["delivered"]                             # not delivered, not "failed"
    reserved = datetime.fromisoformat(notes[0]["attempts"][0]["attempted_at_utc"])

    code, seen = process(path, reserved + timedelta(seconds=1), Pings())
    assert seen == [("liveness", "success")]                     # no second /fail a second later
    assert len(exposed(tmp_path)["outstanding"]) == 2
    for state in exposed(tmp_path)["outstanding"]:
        assert state["delivery_state"] == cli.WAITING and state["rounds"] == 1
        assert state["last_completion"] == "unknown"
        assert state["last_receipt"] == "Notification undelivered; remote receipt unknown."
        assert datetime.fromisoformat(state["next_eligible_at_utc"]) == reserved + timedelta(
            minutes=5)
    assert process(path, reserved + timedelta(minutes=4, seconds=59), Pings())[1] == [
        ("liveness", "success")]
    up = Pings()
    assert process(path, reserved + timedelta(minutes=5), up)[1] == [
        ("liveness", "success"), ("afterclose", "fail")]         # after the approved interval
    assert files(tmp_path, "*-round-002-reservation.json") != []  # round two, not one again
    for note in intents(tmp_path, check="afterclose"):
        assert [a["attempt_number"] for a in note["attempts"]] == [1, 2]
        assert note["attempts"][0]["completion"] == "unknown"    # history kept as it was
        assert note["attempts"][0]["attempted_at_utc"] == reserved.isoformat()
        assert note["delivered"] and note["attempts"][1]["completion"] == "recorded"
    assert "exactly-once" in (OBSERVER / "README.md").read_text()


def test_interrupted_rounds_count_toward_the_hourly_spacing_across_restarts(tmp_path,
                                                                           monkeypatch):
    cfg = config(tmp_path, hc=True)
    path = cfg_file(tmp_path, cfg)
    run("premarket", tmp_path, FakeProbe(revision="f" * 40), cfg=cfg,
        post=Pings(fail_first=10 ** 6))
    note, t = last_attempt(tmp_path, "assessment_result", "premarket")
    _, dying = crash_on("premarket", after_sending=True)
    for round_ in range(2, 7):                    # five more rounds, every one interrupted
        t += timedelta(minutes=5)
        with pytest.raises(Crash):
            cli.main(["liveness", "--config", path], post=dying, clock=lambda t=t: t,
                     sleep=never_sleep, alarm=False)
        note, _ = last_attempt(tmp_path, "assessment_result", "premarket")
        assert len(note["attempts"]) == round_ and note["attempts"][-1]["round"] == round_
    assert all(a["completion"] == "unknown" for a in note["attempts"][1:])
    down = Pings(fail_first=10 ** 6)
    for gap in ({"minutes": 5}, {"minutes": 59, "seconds": 59}):   # six rounds: hourly now
        assert ("premarket", "fail") not in process(path, t + timedelta(**gap), down)[1]
    assert ("premarket", "fail") in process(path, t + timedelta(hours=1), down)[1]


def test_concurrent_invocations_cannot_reserve_the_same_round(tmp_path):
    cfg, down = config(tmp_path, hc=True), Pings(fail_first=10 ** 6)
    early_review_day(tmp_path, cfg, down)
    note, t = last_attempt(tmp_path, "review_attention", "afterclose")
    when = t + timedelta(minutes=5)
    # Two processes computed the same state before either wrote anything.
    stale = [n for n in cli.notification_states(records(tmp_path), when)
             if n["event_type"] == "review_attention"]
    assert stale[0]["delivery_state"] == cli.DUE and stale[0]["rounds"] == 1
    barrier, results, sent = threading.Barrier(6), [], Pings()

    def race():
        out = cli.Outbound(records(tmp_path), cfg, lambda: when, sent)
        barrier.wait()
        results.append(out.send("afterclose", "fail", "x", stale))
    threads = [threading.Thread(target=race) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(r is not None for r in results) == 1 and len(sent.sent) == 1   # one request
    assert len(files(tmp_path, "attention-*-round-002-reservation.json")) == 1
    note, _ = last_attempt(tmp_path, "review_attention", "afterclose")
    assert [a["round"] for a in note["attempts"] if "round" in a] == [1, 2]
    # And at the entry point: a run that finds the lock held makes no delivery round.
    held = records(tmp_path).lock()
    code, seen = beat(tmp_path, cfg, when + timedelta(hours=2))
    held.close()
    assert seen.kinds() == [("liveness", "success")]


def test_a_consolidated_request_is_one_reservation_shared_by_every_intent(tmp_path,
                                                                           monkeypatch):
    cfg, down = config(tmp_path, hc=True), Pings(fail_first=10 ** 6)
    days = troubled_days(tmp_path, cfg, down)           # checkpoint + an INCOMPLETE day two
    when = at("03:00", days[1] + timedelta(days=1))
    real = cli.phase_state
    monkeypatch.setattr(cli, "phase_state", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("accounting bug")))
    before = set(files(tmp_path, "*-reservation.json"))
    code, up = beat(tmp_path, cfg, when)
    monkeypatch.setattr(cli, "phase_state", real)
    assert up.kinds().count(("afterclose", "fail")) == 1 and len(up.sent) == 4
    (new,) = [f for f in files(tmp_path, "*-reservation.json")
              if f not in before and json.loads(
                  (tmp_path / NOTES / f).read_text())["check"] == "afterclose"]
    reservation = json.loads((tmp_path / NOTES / new).read_text())
    kinds = {n["notification_id"]: n["event_type"] for n in intents(tmp_path)}
    assert sorted(kinds[i] for i in reservation["notification_ids"]) == [
        "assessment_result", "observer_fault", "review_attention"]     # all three, one request
    assert kinds[reservation["notification_ids"][0]] == "assessment_result"
    assert set(reservation["rounds"]) == set(reservation["next_eligible_at_utc"]) == set(
        reservation["notification_ids"])
    body = up.sent[-1][1]
    assert "attention=YES" in body and "checkpoint=" in body
    assert "fault=observer_housekeeping_failure" in body
    for notification_id in reservation["notification_ids"]:
        note = next(n for n in intents(tmp_path) if n["notification_id"] == notification_id)
        last = note["attempts"][-1]
        assert last["reservation_id"] == reservation["reservation_id"] and note["delivered"]
        assert last["round"] == reservation["rounds"][notification_id]   # its own round number
        assert set(last["shared_with_notification_ids"]) == set(
            reservation["notification_ids"]) - {notification_id}
    # Three local records of three events, one transmission; none replaced another.
    assert records(tmp_path).checkpoint() is not None
    assert [f["reason_code"] for f in records(tmp_path).faults()] == [
        "observer_housekeeping_failure"]
    assert exposed(tmp_path)["requests_made_by_this_invocation"].count("afterclose") == 1
    assert "one request may speak for several" in exposed(tmp_path)["meaning"].lower()


def test_every_request_has_a_reservation_and_the_limits_still_hold(tmp_path):
    cfg, down = config(tmp_path, hc=True), Pings(fail_first=10 ** 6)
    days = troubled_days(tmp_path, cfg, down)
    when = at("03:00", days[1] + timedelta(days=1))
    before = len(files(tmp_path, "*-reservation.json"))
    slow = SlowPings(14.0)
    beat(tmp_path, cfg, when, slow, timer=slow.timer)
    assert slow.kinds() == [("liveness", "success"), ("premarket", "fail"),
                            ("midsession", "fail")]                    # 30 s budget
    assert [timeout for _, _, timeout in slow.sent] == [5.0, 5.0, 2.0]  # 5 s, then what is left
    assert len(files(tmp_path, "*-reservation.json")) - before == 2     # withheld: not reserved
    assert len(records(tmp_path).heartbeats(when.astimezone(UTC).date().isoformat())) == 1
    up = Pings()
    beat(tmp_path, cfg, when + timedelta(minutes=5), up)
    assert len(up.sent) <= cli.MAX_REQUESTS and len({k for k, _ in up.kinds()}) == len(up.sent)
    assert len(files(tmp_path, "*-reservation.json")) == len(files(tmp_path, "*-outcome.json"))


# ===========================================================================
# Fourth review B — delivered is not acknowledged
# ===========================================================================
def test_a_delivered_checkpoint_alert_without_acknowledgment_still_says_attention_yes(tmp_path):
    """Codex's finding: after the checkpoint /fail was delivered, the next request said
    attention=ACKNOWLEDGED with no acknowledgment on record."""
    cfg, pings = config(tmp_path, hc=True), Pings()
    days = weekdays(3)
    early_review_day(tmp_path, cfg, pings, days[0])
    checkpoint = records(tmp_path).checkpoint()
    assert all(n["delivered"] for n in intents(tmp_path, "review_attention"))   # delivered
    assert phase(tmp_path, cfg)["attention_required"] is True                    # not acked
    # The next outgoing after-close request: an independent failure, first lost, then retried.
    down = Pings(fail_first=10 ** 6)
    day_run(tmp_path, days[1], probe=FakeProbe(days[1], vix=None), cfg=cfg, post=down)
    _, up = beat(tmp_path, cfg, at("17:00", days[1]))
    body = up.sent[-1][1]
    assert up.kinds()[-1] == ("afterclose", "fail") and "outcome=INCOMPLETE" in body
    assert "attention=YES reason=EARLY_REVIEW" in body and "ACKNOWLEDGED" not in body
    assert f"checkpoint={checkpoint['checkpoint_id']}" in body
    for sent in pings.sent + down.sent + up.sent:
        assert "ACKNOWLEDGED" not in sent[1]
    shown = exposed(tmp_path)["checkpoint_attention"]
    assert shown == {"checkpoint_id": checkpoint["checkpoint_id"], "unacknowledged": True,
                     "acknowledged": False}
    assert "not an acknowledgment" in exposed(tmp_path)["meaning"]


def test_only_a_validated_acknowledgment_of_the_exact_checkpoint_says_acknowledged(tmp_path):
    cfg, down = config(tmp_path, hc=True), Pings(fail_first=10 ** 6)
    days = troubled_days(tmp_path, cfg, down)
    when = at("03:00", days[1] + timedelta(days=1))
    for forged in ({"checkpoint_sha256": "0" * 64}, {"checkpoint_id": "abcdefabcdef"},
                   {"scope": cli.COUNT_SCOPE}):
        path = acknowledge(tmp_path, **forged)
        context = cli.attention_context(records(tmp_path))
        assert context["open"] is True and context["acknowledged"] is False
        note = intents(tmp_path, "assessment_result", "afterclose")[-1]
        assert "attention=YES" in cli.current_body(note, context)
        path.unlink()
    acknowledge(tmp_path)
    _, up = beat(tmp_path, cfg, when)
    body = up.sent[-1][1]
    assert up.kinds()[-1] == ("afterclose", "fail") and "outcome=INCOMPLETE" in body
    assert "attention=ACKNOWLEDGED" in body and "attention=YES" not in body
    state = phase(tmp_path, cfg)
    assert state["phase"] == "REVIEW PENDING" and state["checkpoint"] is not None   # final
    assert state["qualifying_sessions"] == [days[0].isoformat()]      # no exception approved
    assert len(intents(tmp_path, "review_attention")[0]["attempts"]) >= 1   # history kept


def test_acknowledgment_does_not_resolve_an_independent_observer_fault(tmp_path, monkeypatch):
    cfg, pings = config(tmp_path, hc=True), Pings()
    early_review_day(tmp_path, cfg, pings)
    monkeypatch.setattr(cli, "settle_checkpoint", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("bug")))
    down = Pings(fail_first=10 ** 6)
    code, _ = beat(tmp_path, cfg, at("17:00"), down)
    assert code == 1
    acknowledge(tmp_path)
    (fault,) = records(tmp_path).faults()
    assert fault["resolved"] is False
    _, up = beat(tmp_path, cfg, at("17:10"))                 # housekeeping still failing
    assert up.kinds() == [("liveness", "success"), ("afterclose", "fail")]
    body = up.sent[-1][1]
    assert body == (f"check=afterclose attention=YES reason=observer_housekeeping_failure "
                    f"fault={fault['fault_id']} observer={VERSION}")
    assert "ACKNOWLEDGED" not in body and "checkpoint=" not in body
    context = cli.attention_context(records(tmp_path))
    assert context["acknowledged"] is True and context["open_faults"] == [fault["fault_id"]]
    assert records(tmp_path).faults()[0]["resolved"] is False


# ===========================================================================
# Fourth review C — the stored reports say which assessment is current
# ===========================================================================
def lost_premarket_failure(root, hc_cfg):
    run("premarket", root, FakeProbe(revision="f" * 40), cfg=hc_cfg,
        post=Pings(fail_first=10 ** 6))
    (note,) = intents(root, "assessment_result", "premarket")
    assert note["kind"] == "fail" and not note["delivered"]
    return note


def test_a_recovery_assessed_while_pings_were_off_supersedes_the_old_failure(tmp_path):
    """Codex's finding: fail (undelivered) -> pass with transport disabled -> transport
    enabled again -> the obsolete /fail was sent."""
    on = config(tmp_path, hc=True)
    off = {**on, "healthchecks": {"enabled": False}}
    old = lost_premarket_failure(tmp_path, on)
    nxt = DAY + timedelta(days=1)
    silent = Pings()
    passed, _ = run("premarket", tmp_path, FakeProbe(nxt), day=nxt, cfg=off, post=silent)
    assert passed["result"] == "PASS" and passed["invocation"] == "scheduled"
    assert silent.sent == [] and len(intents(tmp_path)) == 1      # no intent for the pass
    for hours in (0, 1, 30):                                      # transport back on
        code, seen = beat(tmp_path, on, at("09:00", nxt) + timedelta(hours=hours))
        assert seen.kinds() == [("liveness", "success")] and code == 0   # no /fail, no success
    states = cli.notification_states(records(tmp_path), at("12:00", nxt))
    assert "superseded by a newer assessment" in states[0]["delivery_state"]
    assert exposed(tmp_path)["outstanding"] == []
    (note,) = intents(tmp_path)                                    # history untouched
    assert note["notification_id"] == old["notification_id"] and len(note["attempts"]) == 1
    assert len(records(tmp_path).reports(check="premarket")) == 2


def test_what_is_not_an_assessment_cannot_establish_recovery(tmp_path):
    on = config(tmp_path, hc=True)
    off = {**on, "healthchecks": {"enabled": False}}
    nxt = DAY + timedelta(days=1)
    wanted = ("premarket", "fail")
    lost_premarket_failure(tmp_path, on)
    # An early invocation: it checked nothing.
    early, _ = run("premarket", tmp_path, FakeProbe(nxt), "08:00:00", nxt, off)
    assert early["catch_up"] and not cli.legitimate_assessment(early)
    assert wanted in beat(tmp_path, on, at("08:30", nxt))[1].kinds()
    # Another check passing says nothing about this one.
    root = tmp_path / "b"
    on_b = config(root, hc=True)
    off_b = {**on_b, "healthchecks": {"enabled": False}}
    lost_premarket_failure(root, on_b)
    mid, _ = run("midsession", root, midday(), "12:30:00", cfg=off_b)
    assert mid["result"] == "PASS"
    assert wanted in beat(root, on_b, at("13:00"))[1].kinds()
    # A report that no longer validates is evidence of nothing.
    root = tmp_path / "c"
    on_c = config(root, hc=True)
    off_c = {**on_c, "healthchecks": {"enabled": False}}
    lost_premarket_failure(root, on_c)
    run("premarket", root, FakeProbe(nxt), day=nxt, cfg=off_c)
    (good,) = (root / f"records/reports/{nxt}").glob("premarket-*.json")
    good.chmod(0o644)
    good.write_text(good.read_text().replace('"timely": true', '"timely": false'))
    code, seen = beat(root, on_c, at("09:00", nxt))
    assert wanted in seen.kinds() and code == 1                    # replayed; the damage reported
    assert ("afterclose", "fail") in seen.kinds()
    assert good.exists()


def test_a_reconstruction_cannot_establish_recovery(tmp_path):
    on = config(tmp_path, hc=True)
    off = {**on, "healthchecks": {"enabled": False}}
    day_run(tmp_path, probe=FakeProbe(vix=None), cfg=on, post=Pings(fail_first=10 ** 6))
    (note,) = intents(tmp_path, "assessment_result", "afterclose")
    assert note["kind"] == "fail" and "outcome=INCOMPLETE" in note["body"]
    rebuilt, _ = run("afterclose", tmp_path, FakeProbe(), "16:10:00", cfg=off,
                     session_date=DAY.isoformat())                 # the same day, by hand
    assert rebuilt["invocation"] == "reconstruction" and rebuilt["collection_gates_passed"]
    assert not cli.legitimate_assessment(rebuilt)
    _, seen = beat(tmp_path, on, at("16:15"))
    assert seen.kinds() == [("liveness", "success"), ("afterclose", "fail")]
    assert "outcome=INCOMPLETE" in seen.sent[-1][1]


def test_a_normal_pass_erases_neither_checkpoint_attention_nor_an_open_fault(tmp_path,
                                                                             monkeypatch):
    on = config(tmp_path, hc=True)
    off = {**on, "healthchecks": {"enabled": False}}
    days = weekdays(2)
    early_review_day(tmp_path, on, Pings(fail_first=10 ** 6), days[0])
    clean = day_run(tmp_path, days[1], cfg=off)                    # a qualified day, pings off
    assert clean["outcome"] == "QUALIFIED" and clean["result"] == "PASS"
    _, seen = beat(tmp_path, on, at("17:00", days[1]))
    assert seen.kinds() == [("liveness", "success"), ("afterclose", "fail")]
    assert "attention=YES reason=EARLY_REVIEW" in seen.sent[-1][1]
    states = {n["event_type"]: n["delivery_state"] for n in cli.notification_states(
        records(tmp_path), at("17:01", days[1]))}
    assert "superseded" in states["assessment_result"]             # day one's own failure is
    assert states["review_attention"] == cli.DELIVERED             # but the checkpoint is not
    # The same for an observer fault.
    root = tmp_path / "f"
    on_f = config(root, hc=True)
    off_f = {**on_f, "healthchecks": {"enabled": False}}
    real = cli.settle_checkpoint
    monkeypatch.setattr(cli, "settle_checkpoint", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("bug")))
    beat(root, on_f, at("07:00"), Pings(fail_first=10 ** 6))
    clean = day_run(root, cfg=off_f)                               # passes; housekeeping broken
    assert clean["outcome"] == "QUALIFIED"
    _, seen = beat(root, on_f, at("17:00"))
    assert ("afterclose", "fail") in seen.kinds()
    assert "reason=observer_housekeeping_failure" in seen.sent[-1][1]
    monkeypatch.setattr(cli, "settle_checkpoint", real)


# ===========================================================================
# Fourth review D — records written by schema /1 and /2 are read as they are
# ===========================================================================
T0 = at("08:36:00")


def old_intent(root, notification_id, schema=1, **over):
    record = {"schema": f"observer_notification_intent/{schema}",
              "notification_id": notification_id, "transport": "healthchecks.io",
              "event_type": "assessment_result", "check": "premarket", "kind": "fail",
              "body": "check=premarket session=2026-10-14 result=FAIL outcome=- timely=True "
                      "attention=NO reason=- report=aaaaaaaaaaaa observer=0.1.0",
              "created_at_utc": T0.astimezone(UTC).isoformat(), "checkpoint_id": None,
              "report_id": "aaaaaaaaaaaa", "session_date": DAY.isoformat(),
              "scheduled_at_ist": None, "deadline_at_ist": None, "started_at_utc": None,
              "completed_at_utc": None}
    if schema == 2:
        record["attention_only"] = False
    record.update(over)
    folder = Path(root) / NOTES
    folder.mkdir(parents=True, exist_ok=True)
    assert store.publish(folder / f"{notification_id}-intent.json", record)
    return record


def old_attempt(root, notification_id, number, began, schema=1, requests=3, delivered=False,
                **over):
    began = began.astimezone(UTC)
    record = {"schema": f"observer_notification_attempt/{schema}",
              "notification_id": notification_id, "attempt_number": number,
              "attempted_at_utc": began.isoformat(), "delivered": delivered,
              "notified_at_utc": (began + timedelta(seconds=1)).isoformat() if delivered
              else None, "failure_reason": None if delivered else "OSError: network unreachable"}
    if schema == 1:
        record["transport_attempts"] = [
            {"attempt": i + 1, "at_utc": (began + timedelta(seconds=2 * i)).isoformat(),
             "result": "OSError: network unreachable"} for i in range(requests)]
    else:
        record.update(result="OSError: network unreachable",
                      receipt=healthchecks.UNDELIVERED, request_timeout_seconds=5.0,
                      carried_by_notification_id=None)
    record.update(over)
    for key in [k for k, v in record.items() if v == "<absent>"]:
        del record[key]
    assert store.publish(Path(root) / NOTES / f"{notification_id}-attempt-{number:03d}.json",
                         record)
    return record


def snapshot(root):
    return {p.name: p.read_bytes() for p in (Path(root) / NOTES).iterdir()}


def test_schema_1_and_2_records_coexist_and_are_never_rewritten(tmp_path):
    cfg = config(tmp_path, hc=True)
    old_intent(tmp_path, "aaaaaaaaaaa1")
    one = old_attempt(tmp_path, "aaaaaaaaaaa1", 1, T0)
    old_intent(tmp_path, "bbbbbbbbbbb2", 2, check="midsession")
    two = old_attempt(tmp_path, "bbbbbbbbbbb2", 1, T0 + timedelta(seconds=30), schema=2)
    old_intent(tmp_path, "ccccccccccc3", 1, check="midsession", kind="success",
               created_at_utc=(T0 - timedelta(days=1)).astimezone(UTC).isoformat())
    old_attempt(tmp_path, "ccccccccccc3", 1, T0 - timedelta(days=1), requests=1, delivered=True)
    before = snapshot(tmp_path)
    notes = {n["notification_id"]: n for n in records(tmp_path).notifications()}
    assert [notes[k]["schema"][-1] for k in sorted(notes)] == ["1", "2", "1"]   # kept as stored
    first = notes["aaaaaaaaaaa1"]["attempts"][0]
    assert first["record"] == "observer_notification_attempt/1" and first["rounds"] == 3
    assert first["first_recorded_at_utc"] == one["attempted_at_utc"]
    assert first["attempted_at_utc"] == one["transport_attempts"][-1]["at_utc"]   # the latest
    assert notes["bbbbbbbbbbb2"]["attempts"][0]["attempted_at_utc"] == two["attempted_at_utc"]
    assert notes["bbbbbbbbbbb2"]["attempts"][0]["rounds"] == 1
    assert notes["ccccccccccc3"]["delivered"] and not notes["aaaaaaaaaaa1"]["delivered"]
    assert notes["aaaaaaaaaaa1"]["created_at_utc"] == T0.astimezone(UTC).isoformat()
    # Spacing runs from the last request the old attempt made, and its three requests count.
    last = T0 + timedelta(seconds=4)
    code, seen = beat(tmp_path, cfg, T0 + timedelta(minutes=5))        # 4 s too soon for /1
    assert seen.kinds() == [("liveness", "success")] and code == 0
    state = {n["notification_id"]: n for n in exposed(tmp_path)["outstanding"]}
    assert state["aaaaaaaaaaa1"]["rounds"] == 3 and state["aaaaaaaaaaa1"]["schema"].endswith("/1")
    assert datetime.fromisoformat(state["aaaaaaaaaaa1"]["next_eligible_at_utc"]) == \
        last + timedelta(minutes=5)
    _, seen = beat(tmp_path, cfg, last + timedelta(minutes=5))
    assert seen.kinds() == [("liveness", "success"), ("premarket", "fail")]
    assert files(tmp_path, "aaaaaaaaaaa1-round-004-reservation.json")    # round four, not two
    _, seen = beat(tmp_path, cfg, T0 + timedelta(minutes=11))
    assert seen.kinds() == [("liveness", "success"), ("midsession", "fail")]
    after = snapshot(tmp_path)
    assert {k: after[k] for k in before} == before                       # byte for byte
    notes = {n["notification_id"]: n for n in records(tmp_path).notifications()}
    assert notes["bbbbbbbbbbb2"]["delivered"] and notes["bbbbbbbbbbb2"]["schema"].endswith("/2")
    assert [a["record"] for a in notes["bbbbbbbbbbb2"]["attempts"]] == [
        "observer_notification_attempt/2", "observer_notification_reservation/1"]
    assert notes["ccccccccccc3"]["delivered"]                            # outcome unchanged
    assert not any(n["notification_id"] == "ccccccccccc3"
                   for n in exposed(tmp_path)["outstanding"])            # and not a new alert


def test_six_legacy_requests_put_the_next_round_an_hour_away(tmp_path):
    cfg = config(tmp_path, hc=True)
    old_intent(tmp_path, "aaaaaaaaaaa1")
    old_attempt(tmp_path, "aaaaaaaaaaa1", 1, T0)
    old_attempt(tmp_path, "aaaaaaaaaaa1", 2, T0 + timedelta(minutes=10))
    last = T0 + timedelta(minutes=10, seconds=4)
    for gap in ({"minutes": 5}, {"minutes": 59}):
        assert beat(tmp_path, cfg, last + timedelta(**gap))[1].kinds() == [
            ("liveness", "success")]
    assert ("premarket", "fail") in beat(tmp_path, cfg, last + timedelta(hours=1))[1].kinds()
    assert files(tmp_path, "aaaaaaaaaaa1-round-007-reservation.json")


def test_ambiguous_legacy_metadata_never_permits_a_retransmission(tmp_path, capsys):
    cfg = config(tmp_path, hc=True)
    old_intent(tmp_path, "aaaaaaaaaaa1")
    old_attempt(tmp_path, "aaaaaaaaaaa1", 1, T0, transport_attempts="<absent>")
    before = snapshot(tmp_path)
    for gap in ({"seconds": 1}, {"minutes": 5}, {"hours": 1}, {"days": 3}):
        code, seen = beat(tmp_path, cfg, T0 + timedelta(**gap))
        assert seen.kinds() == [("liveness", "success")]
    (state,) = exposed(tmp_path)["outstanding"]                          # explicit, and visible
    assert state["delivery_state"] == cli.UNRESOLVED
    assert state["unresolved"] == "a schema /1 attempt does not list the requests it made"
    assert state["next_eligible_at_utc"] is None
    assert "history unresolved" in capsys.readouterr().out
    assert snapshot(tmp_path) == before
    # A /1 after-close failure whose words do not say why it failed, once the checkpoint is
    # acknowledged: whether it was independent is unknown, so it is neither sent nor dropped.
    root = tmp_path / "b"
    cfg_b = config(root, hc=True)
    early_review_day(root, cfg_b, Pings())
    checkpoint = records(root).checkpoint()
    old_intent(root, "ddddddddddd4", check="afterclose", body="after-close failed",
               checkpoint_id=checkpoint["checkpoint_id"],
               created_at_utc=at("16:20").astimezone(UTC).isoformat())
    acknowledge(root)
    _, seen = beat(root, cfg_b, at("18:00"))
    assert seen.kinds() == [("liveness", "success")]
    states = {n["notification_id"]: n for n in exposed(root)["outstanding"]}
    assert states["ddddddddddd4"]["delivery_state"] == cli.UNRESOLVED


@pytest.mark.parametrize("damage, expected", [
    ("schema3", "accounts only for"), ("type", "missing or of the wrong type"),
    ("contradiction", "contradicts itself"), ("orphan", "for an intent that is not stored"),
    ("tampered", "does not match its own content hash"), ("gap", "contradicts itself"),
    ("after_delivery", "contradicts itself"), ("timestamp", "unusable timestamp"),
    ("stranger", "not a record this observer version knows"),
])
def test_unsupported_malformed_or_contradictory_records_fail_explicitly(tmp_path, damage,
                                                                         expected, capsys):
    cfg = config(tmp_path, hc=True)
    old_intent(tmp_path, "aaaaaaaaaaa1")
    folder = tmp_path / NOTES
    if damage == "schema3":
        old_intent(tmp_path, "eeeeeeeeeee5", schema=3)
    elif damage == "type":
        old_attempt(tmp_path, "aaaaaaaaaaa1", 1, T0, delivered="no")
    elif damage == "contradiction":
        old_attempt(tmp_path, "aaaaaaaaaaa1", 1, T0, delivered=True,
                    failure_reason="OSError: down")
    elif damage == "orphan":
        old_attempt(tmp_path, "fffffffffff6", 1, T0)
    elif damage == "tampered":
        old_attempt(tmp_path, "aaaaaaaaaaa1", 1, T0)
        path = folder / "aaaaaaaaaaa1-attempt-001.json"
        path.chmod(0o644)
        path.write_text(path.read_text().replace('"delivered": false', '"delivered": true'))
    elif damage == "gap":
        old_attempt(tmp_path, "aaaaaaaaaaa1", 2, T0)
    elif damage == "after_delivery":
        old_attempt(tmp_path, "aaaaaaaaaaa1", 1, T0, delivered=True)
        old_attempt(tmp_path, "aaaaaaaaaaa1", 2, T0 + timedelta(minutes=5))
    elif damage == "timestamp":
        old_attempt(tmp_path, "aaaaaaaaaaa1", 1, T0, attempted_at_utc="yesterday")
    elif damage == "stranger":
        assert store.publish(folder / "notes.json", {"schema": "observer_note/1"})
    before = snapshot(tmp_path)
    with pytest.raises(CorruptRecord, match=expected):
        records(tmp_path).notifications()
    # Through the entry point: reported as a fault, alerted, and nothing dropped or resent.
    code, seen = beat(tmp_path, cfg, T0 + timedelta(hours=3))
    assert code == 1 and seen.kinds()[0] == ("liveness", "success")
    assert ("afterclose", "fail") in seen.kinds()
    assert "reason=observer_housekeeping_failure" in seen.sent[-1][1]
    assert "HOUSEKEEPING FAULT" in capsys.readouterr().out
    assert exposed(tmp_path)["unreadable_notification_records"]
    if damage in ("type", "contradiction", "tampered", "gap", "after_delivery", "timestamp"):
        assert ("premarket", "fail") not in seen.kinds()        # damaged history: no resend
    assert {k: v for k, v in snapshot(tmp_path).items() if k in before} == before


def test_a_checkpoint_alert_is_one_alert_whatever_schema_recorded_it(tmp_path):
    cfg = config(tmp_path, hc=True)
    early_review_day(tmp_path, config(tmp_path), Pings())            # pings off: nothing sent
    checkpoint = records(tmp_path).checkpoint()
    (new,) = intents(tmp_path, "review_attention")
    path = tmp_path / NOTES / f"{new['notification_id']}-intent.json"
    path.chmod(0o644)
    path.unlink()                         # as the store looked under the first observer version
    old_intent(tmp_path, "aaaaaaaaaaa1", check="afterclose", event_type="review_attention",
               body="check=afterclose attention=YES reason=EARLY_REVIEW observer=0.1.0",
               checkpoint_id=checkpoint["checkpoint_id"],
               created_at_utc=at("16:01").astimezone(UTC).isoformat())
    old_attempt(tmp_path, "aaaaaaaaaaa1", 1, at("16:01"))
    before = snapshot(tmp_path)
    code, seen = beat(tmp_path, cfg, at("16:03"))                     # reconciliation runs
    assert seen.kinds() == [("liveness", "success")]                  # spacing from the /1 attempt
    (only,) = intents(tmp_path, "review_attention")                   # no /2 twin was created
    assert only["notification_id"] == "aaaaaaaaaaa1" and only["schema"].endswith("/1")
    _, seen = beat(tmp_path, cfg, at("16:07"))
    assert seen.kinds() == [("liveness", "success"), ("afterclose", "fail")]
    assert f"checkpoint={checkpoint['checkpoint_id']}" in seen.sent[-1][1]
    assert files(tmp_path, "aaaaaaaaaaa1-round-004-reservation.json")
    assert {k: v for k, v in snapshot(tmp_path).items() if k in before} == before
    # If both representations are already on disk they are still one alert, sent once.
    root = tmp_path / "b"
    cfg_b = config(root, hc=True)
    early_review_day(root, config(root), Pings())
    checkpoint = records(root).checkpoint()
    old_intent(root, "aaaaaaaaaaa1", check="afterclose", event_type="review_attention",
               body="check=afterclose attention=YES reason=EARLY_REVIEW observer=0.1.0",
               checkpoint_id=checkpoint["checkpoint_id"],
               created_at_utc=at("15:59").astimezone(UTC).isoformat())
    _, seen = beat(root, cfg_b, at("16:30"))
    assert seen.kinds().count(("afterclose", "fail")) == 1
    _, seen = beat(root, cfg_b, at("17:30"))
    assert seen.kinds() == [("liveness", "success")]                  # delivered for both
    assert len(exposed(root)["outstanding"]) == 0


# ===========================================================================
# Fourth review E — a housekeeping failure asks for the owner at once
# ===========================================================================
def break_housekeeping(monkeypatch, message="accounting bug"):
    real = cli.settle_checkpoint

    def broken(*_, **__):
        raise RuntimeError(message)
    monkeypatch.setattr(cli, "settle_checkpoint", broken)
    return lambda: monkeypatch.setattr(cli, "settle_checkpoint", real)


def test_a_housekeeping_failure_queues_and_sends_an_afterclose_fail_at_once(tmp_path,
                                                                            monkeypatch, capsys):
    cfg = config(tmp_path, hc=True)
    report = day_run(tmp_path, cfg=cfg, post=Pings())
    frozen = {p: p.read_bytes() for p in (tmp_path / "records/reports").rglob("*.json")}
    break_housekeeping(monkeypatch)
    seen = []

    def post(url, body, timeout):
        seen.append((url.rsplit("/", 2)[-2:], len(records(tmp_path).faults())))
        return 200
    assert cli.liveness(cfg, records(tmp_path), lambda: at("17:00"), post) == 1
    assert seen[0] == (["hc.invalid", PING_IDS["liveness"]], 0)       # heartbeat first
    assert seen[1] == ([PING_IDS["afterclose"], "fail"], 1) and len(seen) == 2
    (fault,) = records(tmp_path).faults()
    assert fault["reason_code"] == "observer_housekeeping_failure" and not fault["resolved"]
    assert fault["detected_by_check"] == "liveness" and "qualification" in fault["effect"]
    (note,) = intents(tmp_path, "observer_fault")
    assert note["notification_id"] == f"fault-{fault['fault_id']}" and note["delivered"]
    assert note["check"] == "afterclose" and note["kind"] == "fail"
    assert note["fault_ids"] == [fault["fault_id"]]
    assert "HOUSEKEEPING FAULT" in capsys.readouterr().out
    # Not a verdict on the session: its report, outcome and accounting are as they were.
    assert {p: p.read_bytes() for p in frozen} == frozen
    assert report["outcome"] == "QUALIFIED"
    monkeypatch.undo()
    assert phase(tmp_path, cfg)["qualifying_sessions"] == [DAY.isoformat()]
    assert phase(tmp_path, cfg)["sessions"][DAY.isoformat()]["outcome"] == "QUALIFIED"
    assert set(healthchecks.CHECKS) == {"liveness", "premarket", "midsession", "afterclose"}


def test_the_fault_alert_waits_when_no_request_or_time_remains(tmp_path, monkeypatch):
    cfg = config(tmp_path, hc=True)
    break_housekeeping(monkeypatch)
    slow = SlowPings(31.0)                                   # the heartbeat used the whole budget
    code, _ = beat(tmp_path, cfg, at("17:00"), slow, timer=slow.timer)
    assert slow.kinds() == [("liveness", "success")] and code == 1
    (fault,) = records(tmp_path).faults()                    # the fault is kept (accounting)
    # 0.4.6: its alert intent is notification work, and the deadline had passed: it is
    # not written by this invocation but by the next, from the fault record.
    assert intents(tmp_path, "observer_fault") == []
    assert files(tmp_path, "*-reservation.json") == []
    # 0.4.5: past the deadline nothing more is prepared, and the cache is not rebuilt.
    assert not (tmp_path / "records/notification-state.json").exists()
    shown = cli.notification_states(records(tmp_path), at("17:00"), tolerant=True)
    assert [n["delivery_state"] for n in shown if n["outstanding"]] == []
    assert cli.attention_context(records(tmp_path))["open_faults"] == [fault["fault_id"]]
    _, later = beat(tmp_path, cfg, at("17:05"))              # still failing: one fault, sent now
    assert later.kinds() == [("liveness", "success"), ("afterclose", "fail")]
    assert len(records(tmp_path).faults()) == 1 and len(intents(tmp_path, "observer_fault")) == 1


def test_an_afterclose_run_with_a_fault_makes_one_afterclose_request(tmp_path, monkeypatch):
    cfg, pings = config(tmp_path, hc=True), Pings()
    run("premarket", tmp_path, cfg=cfg, post=pings)
    run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg, post=pings)
    restore = break_housekeeping(monkeypatch)
    faults = []
    report, _ = run("afterclose", tmp_path, FakeProbe(), "16:00:00", cfg=cfg, post=pings,
                    faults=faults)
    assert report["outcome"] == "QUALIFIED" and report["result"] == "PASS"   # data untouched
    assert pings.kinds() == [("premarket", "success"), ("midsession", "success"),
                             ("afterclose", "fail")]                # once, and not a success
    body = pings.sent[-1][1]
    assert "outcome=QUALIFIED" in body
    assert "attention=YES reason=observer_housekeeping_failure" in body
    assert faults and "afterclose-housekeeping failed" in faults[0]
    (own,) = intents(tmp_path, "assessment_result", "afterclose")
    (alert,) = intents(tmp_path, "observer_fault")
    assert own["attention_only"] is True and own["fault_ids"] == alert["fault_ids"]
    assert own["attempts"][0]["reservation_id"] == alert["attempts"][0]["reservation_id"]
    assert len(files(tmp_path, "*-reservation.json")) == 3           # one per request made
    # The entry point reports it too: the assessment passes and the exit is still non-zero.
    assert restore is not None
    nxt = DAY + timedelta(days=1)
    path = cfg_file(tmp_path, cfg)
    code = cli.main(["premarket", "--config", path], probe=FakeProbe(nxt), post=Pings(),
                    clock=Clock(at("08:35:00", nxt)), sleep=never_sleep, alarm=False)
    assert code == 1


def test_fault_recovery_needs_a_later_successful_housekeeping_and_sends_no_success(
        tmp_path, monkeypatch):
    cfg, pings = config(tmp_path, hc=True), Pings()
    days = weekdays(3)
    day_run(tmp_path, days[0], cfg=cfg, post=pings)
    restore = break_housekeeping(monkeypatch)
    beat(tmp_path, cfg, at("17:00", days[0]))
    (fault,) = records(tmp_path).faults()
    for minute in (5, 10):                    # heartbeats delivered; housekeeping still failing
        code, seen = beat(tmp_path, cfg, at(f"17:{minute:02d}", days[0]))
        assert code == 1 and seen.kinds() == [("liveness", "success")]
        assert records(tmp_path).faults()[0]["resolved"] is False
    restore()
    code, seen = beat(tmp_path, cfg, at("17:15", days[0]))
    assert code == 0 and seen.kinds() == [("liveness", "success")]   # no synthetic success
    (fault,) = records(tmp_path).faults()
    assert fault["resolved"] and fault["resolution"]["resolved_by_check"] == "liveness"
    assert fault["summary"] and fault["detected_at_utc"]              # original history kept
    assert ("observer_fault_resolved", f"fault-{fault['fault_id']}") in events(tmp_path)
    assert exposed(tmp_path)["open_observer_faults"] == []
    for hours in (1, 2, 20):
        _, seen = beat(tmp_path, cfg, at("17:15", days[0]) + timedelta(hours=hours))
        assert seen.kinds() == [("liveness", "success")]
    assert ("afterclose", "success") not in pings.kinds()[3:]
    # Ordinary after-close success resumes with the next legitimate assessment.
    report = day_run(tmp_path, days[1], cfg=cfg, post=pings)
    assert report["outcome"] == "QUALIFIED" and pings.kinds()[-1] == ("afterclose", "success")


def test_an_undelivered_fault_alert_stops_at_resolution(tmp_path, monkeypatch):
    cfg, down = config(tmp_path, hc=True), Pings(fail_first=10 ** 6)
    restore = break_housekeeping(monkeypatch)
    beat(tmp_path, cfg, at("17:00"), down)
    restore()
    _, seen = beat(tmp_path, cfg, at("17:05"))
    assert seen.kinds() == [("liveness", "success")]         # obsolete: not retried
    (note,) = cli.notification_states(records(tmp_path), at("18:00"))
    assert note["delivery_state"].endswith("ended by resolution of the fault")
    assert len(note["attempts"]) == 1 and not note["delivered"]


def test_an_open_fault_blocks_ordinary_afterclose_recovery(tmp_path, monkeypatch):
    cfg, pings = config(tmp_path, hc=True), Pings()
    days = weekdays(2)
    restore = break_housekeeping(monkeypatch)
    report = day_run(tmp_path, days[0], cfg=cfg, post=pings)
    assert report["outcome"] == "QUALIFIED"
    assert pings.kinds()[-1] == ("afterclose", "fail")       # the fault, not the data
    closed = config(tmp_path, closed=[days[1]], hc=True)     # even a valid NO SESSION day
    empty = FakeProbe(days[1], signals=[], decisions=[])
    quiet, _ = run("afterclose", tmp_path, empty, "16:00:00", days[1], closed, post=pings)
    assert quiet["result"] == "NO SESSION" and pings.kinds()[-1] == ("afterclose", "fail")
    restore()
    root = tmp_path / "b"
    cfg_b = config(root, closed=[days[1]], hc=True)
    restore = break_housekeeping(monkeypatch)
    beat(root, cfg_b, at("07:00", days[1]))
    restore()
    seen = Pings()
    quiet, _ = run("afterclose", root, empty, "16:00:00", days[1], cfg_b, post=seen)
    assert quiet["result"] == "NO SESSION" and seen.kinds() == [("afterclose", "success")]
    assert records(root).faults()[0]["resolved"]             # resolved by that run's own work


# ===========================================================================
# Fourth review F — corrupt evidence and storage that cannot be written
# ===========================================================================
def test_a_corrupt_report_raises_a_durable_fault_without_the_normal_accounting(tmp_path):
    cfg = config(tmp_path, hc=True)
    run("premarket", tmp_path, cfg=cfg, post=Pings())
    (report,) = (tmp_path / "records/reports/2026-10-14").glob("*.json")
    original = report.read_bytes()
    report.chmod(0o644)
    report.write_text('{"schema": "observer_report/3"}')     # parses; holds nothing
    damaged = report.read_bytes()
    state_before = (tmp_path / "records/state.json").read_bytes()
    with pytest.raises(CorruptRecord):
        phase(tmp_path, cfg)                                 # the ordinary accounting is refused
    code, seen = beat(tmp_path, cfg, at("09:00"))
    assert code == 1 and seen.kinds() == [("liveness", "success"), ("afterclose", "fail")]
    (fault,) = records(tmp_path).faults()
    assert "CorruptRecord" in fault["summary"] and "does not match its own content hash" \
        in fault["summary"]                                  # the finding is preserved
    (note,) = intents(tmp_path, "observer_fault")
    assert note["delivered"]
    assert report.read_bytes() == damaged                    # not overwritten, not deleted
    assert (tmp_path / "records/state.json").read_bytes() == state_before   # nor accounted
    _, again = beat(tmp_path, cfg, at("09:05"))
    assert again.kinds() == [("liveness", "success")] and len(records(tmp_path).faults()) == 1
    assert report.exists()
    report.write_bytes(original)                             # the evidence is repaired by hand
    code, seen = beat(tmp_path, cfg, at("09:10"))
    assert code == 0 and records(tmp_path).faults()[0]["resolved"]


def test_fault_storage_that_cannot_be_written_exits_nonzero_and_claims_nothing(
        tmp_path, monkeypatch, capsys):
    cfg = config(tmp_path, hc=True)
    break_housekeeping(monkeypatch)
    monkeypatch.setattr(Store, "write_fault", lambda *a: (_ for _ in ()).throw(
        OSError(28, "No space left on device")))
    monkeypatch.setattr(Store, "write_event", lambda *a: (_ for _ in ()).throw(
        OSError(28, "No space left on device")))
    code, seen = beat(tmp_path, cfg, at("17:00"))
    printed = capsys.readouterr().out
    assert code == 1 and "STORAGE UNAVAILABLE" in printed
    assert "no alert can be guaranteed" in printed
    assert seen.kinds() == [("liveness", "success")]         # nothing sent for the fault
    assert intents(tmp_path) == [] and "delivered" not in printed.replace(
        "heartbeat delivered", "")


def test_no_request_is_made_when_its_reservation_cannot_be_written(tmp_path, monkeypatch,
                                                                  capsys):
    cfg, down = config(tmp_path, hc=True), Pings(fail_first=10 ** 6)
    run("premarket", tmp_path, FakeProbe(revision="f" * 40), cfg=cfg, post=down)
    note, t = last_attempt(tmp_path, "assessment_result", "premarket")

    def full(*_):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(Store, "write_reservation", full)
    code, seen = beat(tmp_path, cfg, t + timedelta(minutes=5))
    assert seen.kinds() == [("liveness", "success")] and code == 1   # the /fail did not leave
    printed = capsys.readouterr().out
    assert "the request was not made" in printed
    note, _ = last_attempt(tmp_path, "assessment_result", "premarket")
    assert len(note["attempts"]) == 1                        # and no attempt is claimed
    monkeypatch.setattr(Store, "write_heartbeat", full)      # not even the heartbeat
    silent = Pings()
    assert beat(tmp_path, cfg, t + timedelta(minutes=10), silent)[0] == 1
    assert silent.sent == []
    printed = capsys.readouterr().out
    assert "STORAGE UNAVAILABLE" in printed and "heartbeat: not sent" in printed
    path = cfg_file(tmp_path, cfg)
    nxt = DAY + timedelta(days=1)
    quiet = Pings()
    code = cli.main(["premarket", "--config", path], probe=FakeProbe(nxt), post=quiet,
                    clock=Clock(at("08:35:00", nxt)), sleep=never_sleep, alarm=False)
    assert quiet.sent == [] and code == 1                    # the assessment passed; exit says
    assert "HOUSEKEEPING FAULT" in capsys.readouterr().out   # the notification could not go


def test_fault_messages_carry_a_reason_code_and_nothing_else(tmp_path, monkeypatch):
    cfg, pings = config(tmp_path, hc=True), Pings()
    secret = PING_IDS["afterclose"]
    break_housekeeping(monkeypatch, f"password=hunter2 token {secret} at {tmp_path}/records "
                                    "position 22500CE qty 75")
    beat(tmp_path, cfg, at("17:00"), pings)
    (fault,) = records(tmp_path).faults()
    body = pings.sent[-1][1]
    assert body == (f"check=afterclose attention=YES reason=observer_housekeeping_failure "
                    f"fault={fault['fault_id']} observer={VERSION}")
    for leaked in ("hunter2", secret, str(tmp_path), "22500CE", "RuntimeError"):
        assert leaked not in body
    stored = "".join(p.read_text() for p in (tmp_path / "records").rglob("*") if p.is_file())
    assert secret not in stored and str(tmp_path) not in fault["summary"]
    assert all(uuid not in stored for uuid in PING_IDS.values())


def test_version_and_compatibility_are_documented():
    readme = " ".join((OBSERVER / "README.md").read_text().split())
    assert VERSION == "0.4.14"
    for phrase in ("read as they are and never rewritten", "After-close assessment and observer "
                   "evidence-accounting attention", "observer_housekeeping_failure",
                   "reliable remote notification cannot be guaranteed",
                   "Delivered is not acknowledged"):
        assert phrase in readme, phrase


# ===========================================================================
# Fifth review A — an assessment waits for the lock, but never past its deadline
# ===========================================================================
class LockHolder:
    """Holds the real observer lock, as liveness would, until `release_after` waits."""

    def __init__(self, root, release_after=None):
        self.handle = records(root).lock()
        self.release_after, self.waits = release_after, 0

    def sleep_hook(self, clock):
        def sleep(seconds):
            assert seconds == 1                       # at most one attempt a second
            self.waits += 1
            clock.sleep(seconds)
            if self.release_after is not None and self.waits >= self.release_after:
                self.release()
        return sleep

    def release(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


@pytest.mark.parametrize("check, start", [("premarket", "08:35:00"),
                                          ("midsession", "12:30:00"),
                                          ("afterclose", "16:00:00")])
def test_an_assessment_waits_for_liveness_to_release_the_lock(tmp_path, check, start):
    """Codex's finding: liveness took the lock at 08:35:00 and the assessment gave up."""
    cfg = config(tmp_path)
    if check == "afterclose":
        run("premarket", tmp_path, cfg=cfg)
        run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg)
    holder = LockHolder(tmp_path, release_after=4)
    clock = Clock(at(start) - timedelta(seconds=1))
    probe = midday() if check == "midsession" else FakeProbe()
    report = cli.run_check(check, cfg, records(tmp_path), probe, clock, holder.sleep_hook(clock),
                           post=Pings())
    assert report["ran_checks"] and report["timely"] and report["result"] == "PASS"
    assert report["lock_attempts"] == 5 and holder.waits == 4
    invoked = datetime.fromisoformat(report["invoked_at_ist"])
    acquired = datetime.fromisoformat(report["lock_acquired_at_ist"])
    began = datetime.fromisoformat(report["assessment_started_at_ist"])
    done = datetime.fromisoformat(report["assessment_completed_at_ist"])
    assert invoked == at(start) == datetime.fromisoformat(report["started_at_ist"])
    assert invoked < acquired <= began <= done <= at(TIMES[check][1])
    assert report["lock_wait_seconds"] == (acquired - invoked).total_seconds()
    assert cli.check_status(records(tmp_path), DAY.isoformat(), check) == "PASS"
    if check == "afterclose":
        assert report["outcome"] == "QUALIFIED"


def test_a_lock_held_through_the_deadline_leaves_the_assessment_missing(tmp_path):
    cfg = config(tmp_path)
    holder = LockHolder(tmp_path)                     # never released
    clock = Clock(at("08:44:50"))
    report = cli.run_check("premarket", cfg, records(tmp_path), FakeProbe(), clock,
                           holder.sleep_hook(clock), post=Pings())
    assert not report["ran_checks"] and report["result"] == "FAIL" and not report["timely"]
    assert report["assessment_status"] == "MISSING"
    contention = report["lock_contention"]
    assert datetime.fromisoformat(contention["limit_ist"]) == at("08:45:00")
    assert datetime.fromisoformat(contention["waited_until_ist"]) <= at("08:45:00")
    # One a second, and none at 08:45:00 itself: the last wait ended at the deadline.
    assert report["lock_attempts"] == holder.waits == 4
    assert "lock_acquired_at_ist" not in report and "assessment_started_at_ist" not in report
    holder.release()
    assert cli.check_status(records(tmp_path), DAY.isoformat(), "premarket") == "MISSING"
    assert ("operational_failure", "premarket-overlap") in events(tmp_path)
    run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg)
    later = run("afterclose", tmp_path, FakeProbe(), "16:00:00", cfg=cfg)[0]
    assert later["readiness_0845"] == "MISSING"
    assert later["outcome"] == "OPERATIONAL EXCEPTION REQUIRED"     # not QUALIFIED


def test_an_early_invocation_does_not_wait_its_way_into_the_window(tmp_path):
    cfg = config(tmp_path)
    holder = LockHolder(tmp_path)
    clock = Clock(at("08:34:50"))
    report = cli.run_check("premarket", cfg, records(tmp_path), FakeProbe(), clock,
                           holder.sleep_hook(clock), post=Pings())
    assert holder.waits == 0 and report["lock_attempts"] == 1 and not report["ran_checks"]
    holder.release()
    clock = Clock(at("08:34:55"))
    report = cli.run_check("premarket", cfg, records(tmp_path), FakeProbe(), clock,
                           never_sleep, post=Pings())
    assert report["catch_up"] and not report["ran_checks"] and not report["timely"]
    assert cli.check_status(records(tmp_path), DAY.isoformat(), "premarket") == "MISSING"


def test_a_late_invocation_spends_its_180_seconds_on_the_wait_and_the_work(tmp_path,
                                                                          monkeypatch):
    cfg = config(tmp_path)
    seen = []
    real = cli.time_limit

    def spy(seconds, enabled):
        seen.append(seconds)
        return real(seconds, False)
    monkeypatch.setattr(cli, "time_limit", spy)
    holder = LockHolder(tmp_path, release_after=50)   # 100 s of a 180 s allowance
    clock = Clock(at("09:10:00") - timedelta(seconds=1))
    report = cli.run_check("premarket", cfg, records(tmp_path), FakeProbe(), clock,
                           holder.sleep_hook(clock), post=Pings())
    waited = report["lock_wait_seconds"]
    # Measured to the lock's actual acquisition, one clock reading after the attempt.
    assert 100 <= waited <= 102 and report["ran_checks"] and not report["timely"]
    assert seen[-1] == pytest.approx(180 - waited - 1, abs=1)   # what was left, not 180 again
    holder = LockHolder(tmp_path / "b")                         # never released
    clock = Clock(at("09:10:00"))
    report = cli.run_check("premarket", config(tmp_path / "b"), records(tmp_path / "b"),
                           FakeProbe(), clock, holder.sleep_hook(clock), post=Pings())
    assert not report["ran_checks"] and report["assessment_status"] == "MISSING"
    assert datetime.fromisoformat(report["lock_contention"]["waited_until_ist"]) <= at(
        "09:13:00")


def test_the_lock_stays_exclusive_and_liveness_never_waits(tmp_path, monkeypatch):
    cfg = config(tmp_path, hc=True)
    seen = []

    class Probing(FakeProbe):
        def revision(self):
            try:                                        # another run, mid-assessment
                records(tmp_path).lock().close()
                seen.append("acquired")
            except Overlap:
                seen.append("refused")
            return super().revision()
    run("premarket", tmp_path, Probing(), cfg=cfg)
    assert seen and set(seen) == {"refused"}
    records(tmp_path).lock().close()                    # released afterwards
    monkeypatch.setattr(cli.time_module, "sleep", never_sleep)
    monkeypatch.setattr(store.time, "sleep", never_sleep)
    holder = LockHolder(tmp_path)
    code, pings = beat(tmp_path, cfg, at("08:35:00"))   # liveness: no waiting at all
    holder.release()
    assert pings.kinds() == [("liveness", "success")] and code == 0


# ===========================================================================
# Fifth review B — an unexpected error in the phase calculation
# ===========================================================================
def failing_phase(monkeypatch, times=1, message="bad value"):
    real, calls = cli.phase_state, {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= times:
            raise ValueError(message)
        return real(*args, **kwargs)
    monkeypatch.setattr(cli, "phase_state", flaky)
    return calls


def test_an_unexpected_phase_error_still_publishes_a_failure_report(tmp_path, monkeypatch):
    """Codex's finding: a ValueError in phase_state escaped with 0 reports, 0 faults and
    0 intents."""
    cfg = config(tmp_path, hc=True)
    path = cfg_file(tmp_path, cfg)
    run("premarket", tmp_path, cfg=cfg, post=Pings())
    run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg, post=Pings())
    secret = PING_IDS["afterclose"]
    failing_phase(monkeypatch, times=10 ** 6,
                  message=f"password=hunter2 {secret} position 22500CE qty 75")
    pings = Pings()
    code = cli.main(["afterclose", "--config", path], probe=trigger_probe(), post=pings,
                    clock=Clock(at("16:00:00")), sleep=never_sleep, alarm=False)
    assert code == 1
    (report,) = records(tmp_path).reports(check="afterclose")       # published
    assert report["bounded_phase"] == "UNKNOWN" and report["attention_required"] is True
    assert report["attention_reasons"] == ["OBSERVER_PHASE_ERROR"]
    assert report["outcome"] == "UNKNOWN" and report["result"] == "FAIL"   # withheld
    assert report["valid_early_review_candidates"] == 0
    assert report["qualification_withheld"]
    assert report["collection_gates_passed"] is True                 # the checks stand
    assert len(report["checks"]) == 17 and all(c["status"] == "PASS" for c in report["checks"])
    assert report["expiry_day_passed_candidates"][0]["valid_early_review_trigger"]  # kept
    assert records(tmp_path).checkpoint() is None                    # no new trigger accepted
    (fault,) = records(tmp_path).faults()                            # one fault, not two
    assert fault["reason_code"] == "observer_housekeeping_failure" and not fault["resolved"]
    assert intents(tmp_path, "observer_fault")[0]["fault_ids"] == [fault["fault_id"]]
    assert pings.kinds() == [("afterclose", "fail")]
    body = pings.sent[0][1]
    for leaked in ("hunter2", secret, "22500CE", "ValueError"):
        assert leaked not in body
    stored = "".join(p.read_text() for p in (tmp_path / "records").rglob("*") if p.is_file())
    assert "hunter2" not in stored and secret not in stored
    assert "password=<redacted>" in fault["summary"]
    records(tmp_path).lock().close()                                 # the lock was released
    monkeypatch.undo()
    assert phase(tmp_path, cfg)["sessions"] == {}                    # not counted


def test_a_phase_error_leaves_an_existing_checkpoint_as_it_is(tmp_path, monkeypatch):
    cfg = config(tmp_path, hc=True)
    days = weekdays(2)
    early_review_day(tmp_path, cfg, Pings(), days[0])
    path = tmp_path / "records/checkpoints/review-pending.json"
    frozen = path.read_bytes()
    run("premarket", tmp_path, FakeProbe(days[1]), day=days[1], cfg=cfg, post=Pings())
    run("midsession", tmp_path, midday(days[1]), "12:30:00", days[1], cfg, post=Pings())
    calls = failing_phase(monkeypatch, times=1)                      # once, then it works
    report, _ = run("afterclose", tmp_path, FakeProbe(days[1]), "16:00:00", days[1], cfg,
                    post=Pings())
    assert calls["n"] >= 2 and report["bounded_phase"] == "UNKNOWN"
    assert report["outcome"] == "UNKNOWN" and path.read_bytes() == frozen
    (fault,) = records(tmp_path).faults()
    assert not fault["resolved"]          # the same invocation's own housekeeping cannot


def test_a_phase_error_in_a_premarket_run_is_reported_and_exits_nonzero(tmp_path,
                                                                        monkeypatch):
    cfg = config(tmp_path, hc=True)
    path = cfg_file(tmp_path, cfg)
    failing_phase(monkeypatch, times=10 ** 6)
    pings = Pings()
    code = cli.main(["premarket", "--config", path], probe=FakeProbe(), post=pings,
                    clock=Clock(at("08:35:00")), sleep=never_sleep, alarm=False)
    (report,) = records(tmp_path).reports()
    assert code == 1 and report["result"] == "FAIL" and report["bounded_phase"] == "UNKNOWN"
    assert checks.all_pass(report["checks"])                         # its checks did pass
    assert sorted(pings.kinds()) == [("afterclose", "fail"), ("premarket", "fail")]
    assert len(records(tmp_path).faults()) == 1


# ===========================================================================
# Fifth review C — the notification-state cache cannot be written
# ===========================================================================
def failing_cache(monkeypatch):
    real, calls = Store.write_state, {"n": 0}

    def write_state(self, state, name="state.json"):
        if name == "notification-state.json":
            calls["n"] += 1
            raise OSError(28, "No space left on device")
        return real(self, state, name)
    monkeypatch.setattr(Store, "write_state", write_state)
    return calls


def test_a_cache_write_failure_is_a_fault_with_an_alert(tmp_path, monkeypatch, capsys):
    """Codex's finding: notification-state.json could not be written and the observer
    only printed a local message."""
    cfg = config(tmp_path, hc=True)
    calls = failing_cache(monkeypatch)
    code, pings = beat(tmp_path, cfg, at("17:00"))
    assert code == 1 and calls["n"] == 1                       # tried once: no loop
    assert pings.kinds() == [("liveness", "success"), ("afterclose", "fail")]
    (fault,) = records(tmp_path).faults()
    assert fault["kind"] == "notification-state" and fault["signature"].startswith(
        "notification-state:OSError")
    assert "No space left" in fault["summary"]
    (note,) = intents(tmp_path, "observer_fault")
    assert note["delivered"] and note["fault_ids"] == [fault["fault_id"]]
    assert "reason=observer_housekeeping_failure" in pings.sent[-1][1]
    assert "HOUSEKEEPING FAULT" in capsys.readouterr().out
    assert (tmp_path / "records/state.json").exists()          # other storage still writable
    for minute in (5, 10, 15):                                 # still failing: one fault
        code, seen = beat(tmp_path, cfg, at(f"17:{minute:02d}"))
        assert code == 1 and seen.kinds() == [("liveness", "success")]
    assert len(records(tmp_path).faults()) == 1 and len(intents(tmp_path, "observer_fault")) == 1
    assert calls["n"] == 4
    acknowledge_free = cli.attention_context(records(tmp_path))
    assert acknowledge_free["open_faults"] == [fault["fault_id"]]   # housekeeping succeeded
    monkeypatch.undo()                                              # and did not resolve it
    code, seen = beat(tmp_path, cfg, at("17:20"))
    assert code == 0 and seen.kinds() == [("liveness", "success")]  # no synthetic success
    (fault,) = records(tmp_path).faults()
    assert fault["resolved"] and fault["resolution"]["resolved_by_check"] == "liveness"
    assert (tmp_path / "records/notification-state.json").exists()


def test_a_cache_fault_waits_for_a_free_request_and_budget(tmp_path, monkeypatch):
    cfg = config(tmp_path, hc=True)
    failing_cache(monkeypatch)
    # 0.4.5: the cache is not even attempted once the deadline has passed, so the
    # heartbeat leaves half a second: the cache write is tried, fails, and its alert
    # then finds less than the one second a request needs.
    slow = SlowPings(29.5)
    code, _ = beat(tmp_path, cfg, at("17:00"), slow, timer=slow.timer)
    assert code == 1 and slow.kinds() == [("liveness", "success")]
    (note,) = intents(tmp_path, "observer_fault")
    assert note["attempts"] == []                               # kept for later
    monkeypatch.undo()
    _, later = beat(tmp_path, cfg, at("17:05"))
    assert later.kinds() == [("liveness", "success"), ("afterclose", "fail")]


def test_an_acknowledgment_does_not_resolve_a_cache_fault(tmp_path, monkeypatch):
    cfg = config(tmp_path, hc=True)
    early_review_day(tmp_path, cfg, Pings())
    failing_cache(monkeypatch)
    beat(tmp_path, cfg, at("17:00"))
    acknowledge(tmp_path)
    beat(tmp_path, cfg, at("17:05"))
    (fault,) = records(tmp_path).faults()
    assert not fault["resolved"]
    assert cli.attention_context(records(tmp_path))["acknowledged"] is True


def test_when_fault_storage_also_fails_nothing_unreserved_is_sent(tmp_path, monkeypatch,
                                                                  capsys):
    cfg = config(tmp_path, hc=True)
    failing_cache(monkeypatch)
    monkeypatch.setattr(Store, "write_fault", lambda *a: (_ for _ in ()).throw(
        OSError(28, "No space left on device")))
    code, pings = beat(tmp_path, cfg, at("17:00"))
    printed = capsys.readouterr().out
    assert code == 1 and "STORAGE UNAVAILABLE" in printed
    assert pings.kinds() == [("liveness", "success")] and intents(tmp_path) == []
    assert "delivered" not in printed.replace("heartbeat delivered", "")


# ===========================================================================
# Fifth review D — one absolute notification deadline
# ===========================================================================
def slow_store(monkeypatch, method, seconds, only=None):
    real = getattr(Store, method)

    def slow(self, *args):
        if only is None or args[0] == only:
            import time as real_time
            real_time.sleep(seconds)
        return real(self, *args)
    monkeypatch.setattr(Store, method, slow)


def test_a_slow_reservation_uses_the_budget_and_nothing_is_sent_after_it(tmp_path,
                                                                         monkeypatch):
    """Codex's finding: a delayed reservation let the request go out after the budget."""
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 2.0)
    cfg = config(tmp_path, hc=True)
    slow_store(monkeypatch, "write_heartbeat", 1.5, only="reservation")
    pings = Pings()
    out = cli.Outbound(records(tmp_path), cfg, lambda: at("17:00"), pings, alarm=True)
    outcome = out.send("liveness", "success", "x")
    assert pings.sent == []                                     # the budget was left < 1 s
    assert outcome["completion"] == "unknown" and outcome["delivered"] is False
    assert outcome["receipt"] == "Notification undelivered; remote receipt unknown."
    (hb,) = records(tmp_path).heartbeats(at("17:00").astimezone(UTC).date().isoformat())
    assert hb["outcome"] is None                                # reserved, never closed
    assert out.send("premarket", "fail", "y") is None           # the same deadline holds
    assert out.withheld[-1]["reason"].endswith("budget is spent")


def test_the_hard_limit_interrupts_storage_as_well_as_the_request(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 2.0)
    cfg = config(tmp_path, hc=True)
    slow_store(monkeypatch, "write_heartbeat", 10, only="reservation")
    pings = Pings()
    out = cli.Outbound(records(tmp_path), cfg, lambda: at("17:00"), pings, alarm=True)
    began = datetime.now()
    outcome = out.send("liveness", "success", "x")
    assert (datetime.now() - began).total_seconds() < 4          # cut off, not 10 s
    assert pings.sent == [] and outcome["completion"] == "unknown"
    assert outcome["reservation_id"] is None                     # it never got written
    assert out.withheld[-1]["reason"] == "stopped at the notification budget"


def test_a_slow_outcome_write_leaves_the_round_unknown_and_spacing_intact(tmp_path,
                                                                         monkeypatch):
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 2.0)
    cfg = config(tmp_path, hc=True)
    path = cfg_file(tmp_path, cfg)
    run("premarket", tmp_path, FakeProbe(revision="f" * 40), cfg=cfg,
        post=Pings(fail_first=10 ** 6))
    note, t = last_attempt(tmp_path, "assessment_result", "premarket")
    slow_store(monkeypatch, "write_outcome", 10)
    sent = Pings()
    when = t + timedelta(minutes=5)
    out = cli.Outbound(records(tmp_path), cfg, lambda: when, sent, alarm=True)
    began = datetime.now()
    cli.deliver_round(records(tmp_path), out, when)
    assert (datetime.now() - began).total_seconds() < 4
    assert sent.kinds() == [("premarket", "fail")]               # the request left in time
    note, _ = last_attempt(tmp_path, "assessment_result", "premarket")
    assert note["attempts"][-1]["completion"] == "unknown" and not note["delivered"]
    monkeypatch.undo()
    assert process(path, when + timedelta(minutes=4), Pings())[1] == [("liveness", "success")]
    assert ("premarket", "fail") in process(path, when + timedelta(minutes=5), Pings())[1]


def test_the_request_timeout_never_exceeds_what_is_left(tmp_path):
    cfg = config(tmp_path, hc=True)
    ticks = {"t": 0.0}
    slow = SlowPings(0.0)

    def timer():
        return ticks["t"]
    out = cli.Outbound(records(tmp_path), cfg, lambda: at("17:00"), slow, timer=timer)
    out.send("liveness", "success", "x")                         # starts the one deadline
    ticks["t"] = 27.5
    out.send("premarket", "fail", "y")
    ticks["t"] = 29.5
    assert out.send("midsession", "fail", "z") is None           # under a second left
    assert [timeout for _, _, timeout in slow.sent] == [5.0, 2.5]
    assert out.began == 0.0                                      # never reset


# ===========================================================================
# Sixth review — the exit paths, an unwritable report store, and the one deadline
# ===========================================================================
def boom(error):
    def raiser(*args, **kwargs):
        raise error
    return raiser


@pytest.mark.parametrize("where", ["checks-error", "checks-timeout", "housekeeping",
                                   "delivery", "publication", "contention"])
def test_the_lock_is_released_on_every_exit_path(tmp_path, monkeypatch, where):
    cfg = config(tmp_path, hc=True)
    clock = Clock(at("08:35:00") - timedelta(seconds=1))
    sleep = clock.sleep
    holder = None
    if where == "checks-error":
        monkeypatch.setitem(cli.RUNS, "premarket", boom(RuntimeError("broken")))
    elif where == "checks-timeout":
        monkeypatch.setitem(cli.RUNS, "premarket", boom(RunTimeout("limit reached")))
    elif where == "housekeeping":
        monkeypatch.setattr(cli, "housekeeping", boom(OSError("disk")))
    elif where == "delivery":
        monkeypatch.setattr(cli, "deliver_round", boom(RuntimeError("broken")))
    elif where == "publication":
        monkeypatch.setattr(Store, "write_report", boom(OSError(28, "No space left")))
    else:
        holder = LockHolder(tmp_path)                    # held throughout
        clock = Clock(at("08:44:57"))
        sleep = holder.sleep_hook(clock)
    outcome = {}
    report = cli.run_check("premarket", cfg, records(tmp_path), FakeProbe(), clock, sleep,
                           post=Pings(), publication=outcome)
    assert report["result"] == "FAIL" or where in ("housekeeping", "delivery", "publication")
    assert outcome["state"] == ("ABSENT" if where == "publication" else "CONFIRMED")
    if holder is not None:
        holder.release()
    records(tmp_path).lock().close()                     # free: nothing kept it


def test_a_phase_error_with_no_report_storage_claims_nothing(tmp_path, monkeypatch, capsys):
    cfg = config(tmp_path, hc=True)
    path = cfg_file(tmp_path, cfg)
    failing_phase(monkeypatch, times=10 ** 6)
    real = Store.write_report
    monkeypatch.setattr(Store, "write_report", boom(OSError(28, "No space left on device")))
    pings = Pings()
    code = cli.main(["premarket", "--config", path], probe=FakeProbe(), post=pings,
                    clock=Clock(at("08:35:00")), sleep=never_sleep, alarm=False)
    printed = capsys.readouterr().out
    assert code == 1 and "REPORT ABSENT" in printed and "PASS" not in printed
    assert records(tmp_path).reports() == []                     # no report is fabricated
    by_kind = {f["kind"]: f for f in records(tmp_path).faults()}  # one each, both open
    phase_fault, published = by_kind["housekeeping"], by_kind["report-publication"]
    assert not phase_fault["resolved"] and not published["resolved"]
    assert published["report"]["state"] == "ABSENT"
    assert len(intents(tmp_path, "observer_fault")) == 2
    assert pings.kinds() == [("afterclose", "fail")]             # one request speaks for both
    records(tmp_path).lock().close()
    monkeypatch.setattr(Store, "write_report", real)
    code, later = beat(tmp_path, cfg, at("08:41"))               # the phase still fails
    assert code == 1 and later.kinds() == [("liveness", "success")]   # delivered: not resent
    assert len(records(tmp_path).faults()) == 2                  # the same faults, no third
    monkeypatch.undo()
    code, later = beat(tmp_path, cfg, at("08:47"))               # the phase works again
    by_kind = {f["kind"]: f for f in records(tmp_path).faults()}
    phase_fault, published = by_kind["housekeeping"], by_kind["report-publication"]
    assert phase_fault["resolved"]                               # its operation succeeded
    assert not published["resolved"]       # an ABSENT report has nothing to verify: still open
    assert later.kinds() == [("liveness", "success")] and code == 0    # no synthetic success

    # Nothing writable at all: still a non-zero exit, and nothing is sent or claimed.
    root = tmp_path / "b"
    cfg = config(root, hc=True)
    path = cfg_file(root, cfg)
    failing_phase(monkeypatch, times=10 ** 6)
    monkeypatch.setattr(Store, "write_report", boom(OSError(28, "No space left on device")))
    monkeypatch.setattr(Store, "write_fault", boom(OSError(28, "No space left on device")))
    pings = Pings()
    code = cli.main(["premarket", "--config", path], probe=FakeProbe(), post=pings,
                    clock=Clock(at("08:35:00")), sleep=never_sleep, alarm=False)
    printed = capsys.readouterr().out
    assert code == 1 and printed.count("STORAGE UNAVAILABLE") == 2
    assert records(root).reports() == [] and records(root).faults() == []
    assert intents(root) == [] and pings.sent == []
    records(root).lock().close()


def test_an_exception_does_not_restart_the_notification_deadline(tmp_path, monkeypatch):
    import time as real_time
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 3.0)
    cfg = config(tmp_path, hc=True)

    class SlowFirst(Pings):
        def __call__(self, url, body, timeout):
            if not self.sent:
                real_time.sleep(0.8)
            return super().__call__(url, body, timeout)
    pings = SlowFirst(fail_first=1)                              # the transport raises
    out = cli.Outbound(records(tmp_path), cfg, lambda: at("17:00"), pings, alarm=True)
    assert out.send("liveness", "success", "x")["delivered"] is False
    began = out.began
    real = Store.write_heartbeat

    def unwritable(self, *args):
        real_time.sleep(0.5)
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(Store, "write_heartbeat", unwritable)
    with pytest.raises(cli.StorageUnavailable):                  # a storage exception
        out.send("premarket", "fail", "y")
    monkeypatch.setattr(Store, "write_heartbeat", real)
    out.send("midsession", "fail", "z")
    assert out.began == began                                    # never restarted
    timeouts = [timeout for _, _, timeout in pings.sent]
    assert timeouts[0] == pytest.approx(3.0, abs=0.05)
    assert 1.3 <= timeouts[1] <= 3.0 - 1.3 + 0.05               # 0.8 s + 0.5 s already gone


def test_requests_in_one_invocation_share_one_real_deadline(tmp_path, monkeypatch):
    import time as real_time
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 3.5)
    cfg = config(tmp_path, hc=True)

    class OneSecond(Pings):
        def __call__(self, url, body, timeout):
            real_time.sleep(1.0)
            return super().__call__(url, body, timeout)
    pings = OneSecond()
    out = cli.Outbound(records(tmp_path), cfg, lambda: at("17:00"), pings, alarm=True)
    started = real_time.monotonic()
    for check in ("liveness", "premarket", "midsession", "afterclose"):
        out.send(check, "success" if check == "liveness" else "fail", "x")
    elapsed = real_time.monotonic() - started
    timeouts = [timeout for _, _, timeout in pings.sent]
    assert len(timeouts) == 3                                    # the fourth: under 1 s left
    assert timeouts[0] == pytest.approx(3.5, abs=0.05)
    assert timeouts[1] == pytest.approx(2.5, abs=0.15)
    assert timeouts[2] == pytest.approx(1.5, abs=0.25)
    assert out.withheld[-1]["reason"].endswith("budget is spent")
    assert elapsed < 3.5 + 0.5                                   # inside the one budget


# ===========================================================================
# Seventh review A — an exhausted deadline starts nothing
# ===========================================================================
def limit_spy(monkeypatch):
    seen, real = [], cli.time_limit

    def spy(seconds, enabled):
        seen.append(seconds)
        return real(seconds, False)
    monkeypatch.setattr(cli, "time_limit", spy)
    return seen


def test_codex_lock_at_the_deadline_no_longer_buys_180_seconds(tmp_path, monkeypatch):
    """Codex's reproduction: invoked 08:44:59, lock 08:45:00, start 08:45:01, 180 s."""
    seen = limit_spy(monkeypatch)
    clock = Clock(at("08:44:58"))                        # readings 08:44:59, 08:45:00, ...
    report = cli.run_check("premarket", config(tmp_path), records(tmp_path), FakeProbe(), clock,
                           clock.sleep, post=Pings())
    assert report["invoked_at_ist"].endswith("08:44:59+05:30")
    assert not report["ran_checks"] and report["assessment_status"] == "MISSING"
    assert report["lock_attempts"] == 0 and "lock_acquired_at_ist" not in report
    assert report["deadline_exhausted"]["stage"] == "before the lock"
    assert seen == []                                    # no allowance of any size
    assert cli.check_status(records(tmp_path), DAY.isoformat(), "premarket") == "MISSING"


@pytest.mark.parametrize("began, case", [("08:45:00", "zero"), ("08:45:01", "negative")])
def test_a_start_at_or_after_the_deadline_does_no_work(tmp_path, monkeypatch, began, case):
    seen = limit_spy(monkeypatch)
    clock = Scripted(at("08:44:58"), at("08:44:59"), at(began))   # invoke, attempt, then
    report = cli.run_check("premarket", config(tmp_path), records(tmp_path), FakeProbe(), clock,
                           never_sleep, post=Pings())             # acquired and start
    assert report["lock_attempted_at_ist"].endswith("08:44:59+05:30")  # tried in time,
    assert report["lock_acquired_at_ist"].endswith(f"{began}+05:30")   # held only then
    assert not report["ran_checks"] and report["checks"] == [] and not report["timely"]
    assert report["assessment_status"] == "MISSING" and report["result"] == "FAIL"
    assert report["deadline_exhausted"]["stage"] == "after the lock"
    assert "assessment_started_at_ist" not in report
    assert "assessment_completed_at_ist" not in report
    assert seen == []                                    # 0 or less never becomes 180
    records(tmp_path).lock().close()                     # released


@pytest.mark.parametrize("check", list(TIMES))
def test_the_lock_is_never_taken_at_or_after_the_deadline(tmp_path, check):
    cfg = config(tmp_path)
    if check == "afterclose":
        run("premarket", tmp_path, cfg=cfg)
        run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg)
    deadline = at(TIMES[check][1])
    holder = LockHolder(tmp_path, release_after=3)       # free from deadline - 1 s on
    clock = Clock(deadline - timedelta(seconds=7))       # attempts at -6, -4, -2 s
    probe = midday() if check == "midsession" else FakeProbe()
    report = cli.run_check(check, cfg, records(tmp_path), probe, clock,
                           holder.sleep_hook(clock), post=Pings())
    assert report["lock_attempts"] == 3 and "lock_acquired_at_ist" not in report
    assert not report["ran_checks"] and report["assessment_status"] == "MISSING"
    assert datetime.fromisoformat(report["lock_contention"]["waited_until_ist"]) <= deadline
    assert cli.check_status(records(tmp_path), DAY.isoformat(), check) == "MISSING"


def test_a_late_run_has_one_180_second_allowance_and_no_second(tmp_path, monkeypatch):
    seen = limit_spy(monkeypatch)
    # Invoked 09:10:00; the lock at 09:12:29; the work starts 09:12:30: 30 s are left.
    clock = Scripted(at("09:10:00"), at("09:12:29"), at("09:12:30"))
    report = cli.run_check("premarket", config(tmp_path), records(tmp_path), FakeProbe(), clock,
                           never_sleep, post=Pings())
    assert report["ran_checks"] and not report["timely"] and seen == [30.0]
    # The lock at 09:12:59 and a start at 09:13:00: the 180 s are spent; nothing starts.
    seen.clear()
    clock = Scripted(at("09:10:00"), at("09:12:59"), at("09:13:00"))
    report = cli.run_check("premarket", config(tmp_path / "b"), records(tmp_path / "b"),
                           FakeProbe(), clock, never_sleep, post=Pings())
    assert not report["ran_checks"] and report["assessment_status"] == "MISSING"
    assert seen == []


def test_an_assessment_that_never_started_stays_missing_for_the_session(tmp_path):
    cfg = config(tmp_path)
    run("premarket", tmp_path, clock=Scripted(at("08:44:58"), at("08:44:59"), at("08:45:00")),
        cfg=cfg)
    run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg)
    report = run("afterclose", tmp_path, FakeProbe(), "16:00:00", cfg=cfg)[0]
    assert report["readiness_0845"] == "MISSING"
    assert report["outcome"] == "OPERATIONAL EXCEPTION REQUIRED" and report["result"] == "FAIL"
    assert phase(tmp_path, cfg)["qualifying_sessions"] == []


def test_interrupted_work_is_not_a_completed_assessment(tmp_path, monkeypatch):
    monkeypatch.setitem(cli.RUNS, "premarket", boom(RunTimeout("limit reached")))
    clock = Scripted(at("08:35:00"), at("08:35:00"), at("08:35:01"), at("08:40:00"))
    report = cli.run_check("premarket", config(tmp_path), records(tmp_path), FakeProbe(), clock,
                           never_sleep, post=Pings())
    assert report["ran_checks"] and report["timed_out"] and report["result"] == "FAIL"
    assert not report["timely"]                          # though it stopped before 08:45
    assert "assessment_completed_at_ist" not in report
    assert report["assessment_interrupted_at_ist"].endswith("08:40:00+05:30")
    assert cli.check_status(records(tmp_path), DAY.isoformat(), "premarket") == "FAIL"


# ===========================================================================
# Seventh review B — a report that cannot be published is a fault of its own
# ===========================================================================
def test_codex_a_report_only_write_failure_is_recorded_and_alerted(tmp_path, monkeypatch,
                                                                   capsys):
    """Codex's reproduction: only write_report fails; exit 1 with 0 reports, 0 faults and
    0 intents."""
    cfg = config(tmp_path, hc=True)
    path = cfg_file(tmp_path, cfg)
    calls = {"n": 0}

    def write_report(self, report):
        calls["n"] += 1
        raise OSError(28, f"No space left on device: {self.root} password=hunter2")
    monkeypatch.setattr(Store, "write_report", write_report)
    pings = Pings()
    code = cli.main(["premarket", "--config", path], probe=FakeProbe(), post=pings,
                    clock=Clock(at("08:35:00")), sleep=never_sleep, alarm=False)
    printed = capsys.readouterr().out
    assert code == 1 and calls["n"] == 1                 # tried once; no loop
    assert "REPORT ABSENT" in printed and ": PASS" not in printed
    assert records(tmp_path).reports() == []             # nothing is made up
    (fault,) = records(tmp_path).faults()
    assert fault["kind"] == "report-publication" and not fault["resolved"]
    assert fault["reason_code"] == "observer_report_publication_failure"
    assert fault["report"]["state"] == "ABSENT" and fault["report"]["file_sha256"] is None
    (note,) = intents(tmp_path, "observer_fault")
    assert note["fault_ids"] == [fault["fault_id"]] and note["delivered"]
    assert pings.kinds() == [("afterclose", "fail")]     # no premarket success is claimed
    body = pings.sent[0][1]
    assert "reason=observer_report_publication_failure" in body
    stored = "".join(p.read_text() for p in (tmp_path / "records").rglob("*") if p.is_file())
    for leaked in ("hunter2", str(tmp_path)):
        assert leaked not in body and leaked not in stored
    (reservation,) = list((tmp_path / "records").rglob("*-reservation.json"))
    assert reservation.exists()                          # reserved before it was sent
    records(tmp_path).lock().close()


def test_a_report_and_its_fault_unwritable_together_claim_and_send_nothing(tmp_path,
                                                                          monkeypatch, capsys):
    cfg = config(tmp_path, hc=True)
    path = cfg_file(tmp_path, cfg)
    monkeypatch.setattr(Store, "write_report", boom(OSError(28, "No space left on device")))
    monkeypatch.setattr(Store, "write_fault", boom(OSError(28, "No space left on device")))
    pings = Pings()
    code = cli.main(["premarket", "--config", path], probe=FakeProbe(), post=pings,
                    clock=Clock(at("08:35:00")), sleep=never_sleep, alarm=False)
    printed = capsys.readouterr().out
    assert code == 1 and "REPORT ABSENT" in printed and "STORAGE UNAVAILABLE" in printed
    assert records(tmp_path).reports() == [] and records(tmp_path).faults() == []
    assert intents(tmp_path) == [] and pings.sent == []  # no unreserved request
    assert not list((tmp_path / "records").rglob("*-reservation.json"))
    assert "delivered" not in printed
    records(tmp_path).lock().close()


def test_a_publication_fault_waits_for_a_free_request_and_budget(tmp_path, monkeypatch):
    cfg = config(tmp_path, hc=True)
    monkeypatch.setattr(Store, "write_report", boom(OSError(28, "No space left on device")))
    down = Pings(fail_first=10 ** 6)
    run("premarket", tmp_path, cfg=cfg, post=down)
    assert down.kinds() == [("afterclose", "fail")]           # one request, and it failed
    monkeypatch.undo()
    note, first = last_attempt(tmp_path, "observer_fault", "afterclose")
    slow = SlowPings(31.0)                                     # the heartbeat spends it all
    _, spent = beat(tmp_path, cfg, first + timedelta(minutes=5), slow, timer=slow.timer)
    assert spent.kinds() == [("liveness", "success")]
    assert len(last_attempt(tmp_path, "observer_fault", "afterclose")[0]["attempts"]) == 1
    _, later = beat(tmp_path, cfg, first + timedelta(minutes=6))
    assert later.kinds() == [("liveness", "success"), ("afterclose", "fail")]
    assert len(intents(tmp_path, "observer_fault")) == 1      # still the one intent


# ===========================================================================
# Seventh review C — published, absent, or unknown
# ===========================================================================
def unsyncable_reports(monkeypatch, prefix=""):
    """Reports named `prefix`... cannot be synced: not their directory after the final
    link, and not on any later attempt. Every other record syncs."""
    real_publish, real_chain, real_dir = store.publish, store.sync_chain, store.sync_directory
    linking = {"on": False}

    def mine(path):
        return "reports" in Path(path).parts and Path(path).name.startswith(prefix)

    def sync_directory(directory):
        if linking["on"]:
            raise OSError(5, "Input/output error")
        return real_dir(directory)

    def publish(path, record, mode=0o440):
        linking["on"] = mine(path)
        try:
            return real_publish(path, record, mode)
        finally:
            linking["on"] = False

    def sync_chain(path, root):
        if mine(path):
            raise OSError(5, "Input/output error")
        return real_chain(path, root)
    monkeypatch.setattr(store, "sync_directory", sync_directory)
    monkeypatch.setattr(store, "publish", publish)
    monkeypatch.setattr(store, "sync_chain", sync_chain)


def premarket_unconfirmed(tmp_path, monkeypatch, capsys, cfg):
    unsyncable_reports(monkeypatch, "premarket-")
    pings = Pings()
    code = cli.main(["premarket", "--config", cfg_file(tmp_path, cfg)], probe=FakeProbe(),
                    post=pings, clock=Clock(at("08:35:00")), sleep=never_sleep, alarm=False)
    return code, pings, capsys.readouterr().out


def test_codex_a_failed_sync_after_the_link_is_present_unconfirmed_not_absent(
        tmp_path, monkeypatch, capsys):
    """Codex's reproduction: the link succeeded, the directory sync failed, the observer
    said "not published" while a readable PASS report existed."""
    cfg = config(tmp_path, hc=True)
    code, pings, printed = premarket_unconfirmed(tmp_path, monkeypatch, capsys, cfg)
    assert records(tmp_path).reports() == []                 # not evidence
    (report,) = records(tmp_path).all_reports()              # but there, and readable
    (path,) = (tmp_path / "records/reports/2026-10-14").glob("premarket-*.json")
    assert code == 1 and report["result"] == "PASS"
    assert "REPORT PRESENT_UNCONFIRMED" in printed and "not published" not in printed
    (fault,) = records(tmp_path).faults()
    found = fault["report"]
    assert found["state"] == "PRESENT_UNCONFIRMED" and found["failure"] == "DurabilityUnconfirmed"
    assert found["report_id"] == report["report_id"]
    assert found["content_sha256"] == report["integrity"]["content_sha256"]
    assert found["file_sha256"] == store.file_sha256(path)
    assert found["report_path"] == str(path.relative_to(tmp_path / "records"))
    assert fault["signature"] == f"report-publication:{report['report_id']}"
    assert pings.kinds() == [("afterclose", "fail")]          # no premarket success
    assert cli.check_status(records(tmp_path), DAY.isoformat(), "premarket") == "MISSING"


def test_the_three_publication_states_come_from_the_disk(tmp_path):
    shelf = records(tmp_path)
    report, _ = run("premarket", tmp_path)
    (path,) = (tmp_path / "records/reports/2026-10-14").glob("premarket-*.json")
    stored = shelf.reports()[0]
    assert cli.publication_state(shelf, stored, store.DurabilityUnconfirmed(5, "x"))[
        "state"] == "PRESENT_UNCONFIRMED"
    other = {**stored, "report_id": "000000000000"}
    assert cli.publication_state(shelf, other, OSError("before the link"))["state"] == "ABSENT"
    assert cli.publication_state(shelf, other, store.DurabilityUnconfirmed(5, "x"))[
        "state"] == "UNKNOWN"                               # linked, yet nothing there
    altered = {**stored, "result": "FAIL"}                  # same name, different content
    assert cli.publication_state(shelf, altered, FileExistsError())["state"] == "UNKNOWN"
    path.chmod(0o640)
    path.write_text("{not json")
    assert cli.publication_state(shelf, stored, store.DurabilityUnconfirmed(5, "x"))[
        "state"] == "UNKNOWN"


def test_a_report_before_its_final_link_is_absent_and_leaves_nothing(tmp_path, monkeypatch):
    real = store.publish

    def publish(path, record, mode=0o440):
        if "reports" in path.parts:
            raise OSError("power lost before the record was linked")
        return real(path, record, mode)
    monkeypatch.setattr(store, "publish", publish)
    outcome = {}
    run("premarket", tmp_path, cfg=config(tmp_path, hc=True), publication=outcome)
    assert outcome["state"] == "ABSENT"
    assert not [p for p in (tmp_path / "records/reports").rglob("*") if p.is_file()]
    (fault,) = records(tmp_path).faults()
    assert fault["report"]["state"] == "ABSENT"


def test_an_unconfirmed_afterclose_report_establishes_no_qualification(tmp_path, monkeypatch,
                                                                       capsys):
    cfg = config(tmp_path, hc=True)
    run("premarket", tmp_path, cfg=cfg, post=Pings())
    run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg, post=Pings())
    unsyncable_reports(monkeypatch, "afterclose-")
    code = cli.main(["afterclose", "--config", cfg_file(tmp_path, cfg)],
                    probe=trigger_probe(), post=Pings(), clock=Clock(at("16:00:00")),
                    sleep=never_sleep, alarm=False)
    assert code == 1
    assert records(tmp_path).reports(check="afterclose") == []
    (report,) = records(tmp_path).all_reports(check="afterclose")
    assert report["outcome"] == "QUALIFIED"                  # what it says is left as it is
    assert report["expiry_day_passed_candidates"][0]["valid_early_review_trigger"]
    (fault,) = records(tmp_path).faults()
    state = phase(tmp_path, cfg, at("16:10"))
    assert state["qualifying_sessions"] == []
    assert DAY.isoformat() not in state["sessions"]          # not evidence at all
    assert fault["report"]["state"] == "PRESENT_UNCONFIRMED"
    assert state["computed_reasons"] == [] and records(tmp_path).checkpoint() is None
    assert cli.trusted_baseline(records(tmp_path), cfg, "2026-10-15")["from_session"] == \
        "configuration"                                      # not from the unconfirmed report


def test_verification_and_a_successful_sync_resolve_the_fault_durably(tmp_path, monkeypatch,
                                                                      capsys):
    cfg = config(tmp_path, hc=True)
    run("premarket", tmp_path, cfg=cfg, post=Pings())
    run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg, post=Pings())
    unsyncable_reports(monkeypatch, "afterclose-")
    cli.main(["afterclose", "--config", cfg_file(tmp_path, cfg)], probe=trigger_probe(),
             post=Pings(), clock=Clock(at("16:00:00")), sleep=never_sleep, alarm=False)
    (path,) = (tmp_path / "records/reports/2026-10-14").glob("afterclose-*.json")
    frozen, (report,) = path.read_bytes(), records(tmp_path).all_reports(check="afterclose")
    monkeypatch.undo()
    code, pings = beat(tmp_path, cfg, at("16:20"))
    # No recovery signal. What does go out is the attention of the checkpoint that the
    # re-accounting has now recorded, from the verified report.
    assert code == 0 and pings.kinds() == [("liveness", "success"), ("afterclose", "fail")]
    assert "reason=EARLY_REVIEW" in pings.sent[-1][1]
    (fault,) = records(tmp_path).faults()
    assert fault["resolved"] and fault["resolution"]["resolved_by_check"] == "liveness"
    closing = fault["resolution"]
    assert closing["report_id"] == report["report_id"]
    assert closing["content_sha256"] == report["integrity"]["content_sha256"]
    assert closing["verified_file_sha256"] == store.file_sha256(path)
    assert (tmp_path / f"records/faults/{fault['fault_id']}-resolution.json").exists()
    assert ("observer_fault_resolved", f"fault-{fault['fault_id']}") in events(tmp_path)
    assert path.read_bytes() == frozen                         # the evidence is untouched
    state = phase(tmp_path, cfg, at("16:25"))
    assert state["qualifying_sessions"] == [DAY.isoformat()]  # it counts as what it says
    assert records(tmp_path).checkpoint()["reasons"] == ["EARLY_REVIEW"]   # reconciled


def test_nothing_but_verifying_that_report_resolves_its_fault(tmp_path, monkeypatch, capsys):
    cfg = config(tmp_path, hc=True)
    premarket_unconfirmed(tmp_path, monkeypatch, capsys, cfg)
    (path,) = (tmp_path / "records/reports/2026-10-14").glob("premarket-*.json")
    frozen = path.read_bytes()
    monkeypatch.undo()
    real = store.sync_file

    def sync_file(target):                                     # that one report only
        if target.name.startswith("premarket-"):
            raise OSError(5, "Input/output error")
        return real(target)
    monkeypatch.setattr(store, "sync_file", sync_file)
    # Unrelated work succeeds while that report still cannot be made durable.
    run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg, post=Pings())
    code, _ = beat(tmp_path, cfg, at("12:50"))
    assert code == 0
    (fault,) = records(tmp_path).faults()
    assert not fault["resolved"]                               # housekeeping is not enough
    monkeypatch.undo()
    # The file is there, readable and validly sealed, but it is not the recorded report.
    path.chmod(0o640)
    altered = json.loads(frozen)
    altered["failure_reasons"] = ["changed afterwards"]
    path.write_text(json.dumps(store.seal({k: v for k, v in altered.items()
                                           if k != "integrity"}), indent=2, sort_keys=True))
    beat(tmp_path, cfg, at("12:55"))
    (fault,) = records(tmp_path).faults()
    assert not fault["resolved"]                               # existence is not enough
    path.write_bytes(frozen)                                   # the recorded bytes again
    beat(tmp_path, cfg, at("13:00"))
    (fault,) = records(tmp_path).faults()
    assert fault["resolved"] and path.read_bytes() == frozen


def test_an_acknowledgment_does_not_resolve_a_publication_fault(tmp_path, monkeypatch, capsys):
    cfg = config(tmp_path, hc=True)
    early_review_day(tmp_path, cfg, Pings())
    days = weekdays(2)
    unsyncable_reports(monkeypatch)
    run("premarket", tmp_path, FakeProbe(days[1]), day=days[1], cfg=cfg, post=Pings())
    acknowledge(tmp_path)
    beat(tmp_path, cfg, at("08:50", days[1]))
    (fault,) = records(tmp_path).faults()
    assert not fault["resolved"]
    assert cli.attention_context(records(tmp_path))["acknowledged"] is True


def test_recovery_supplies_no_readiness_and_approves_no_exception(tmp_path, monkeypatch,
                                                                  capsys):
    cfg = config(tmp_path, hc=True)
    premarket_unconfirmed(tmp_path, monkeypatch, capsys, cfg)
    run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg, post=Pings())
    later = run("afterclose", tmp_path, FakeProbe(), "16:00:00", cfg=cfg, post=Pings())[0]
    assert later["readiness_0845"] == "MISSING"               # unconfirmed: not readiness
    assert later["outcome"] == "OPERATIONAL EXCEPTION REQUIRED"
    monkeypatch.undo()
    beat(tmp_path, cfg, at("16:40"))
    premarket_fault = next(f for f in records(tmp_path).faults()
                           if f["report"]["check"] == "premarket")
    assert premarket_fault["resolved"]
    (stored,) = records(tmp_path).reports(check="afterclose")
    assert stored["readiness_0845"] == "MISSING"              # the record is not rewritten
    entry = phase(tmp_path, cfg)["sessions"][DAY.isoformat()]
    assert entry["outcome"] == "OPERATIONAL EXCEPTION REQUIRED"
    assert entry["qualifies"] is False and entry["exception_approval"] is None


def test_an_unconfirmed_report_supersedes_no_earlier_failure_alert(tmp_path, monkeypatch,
                                                                  capsys):
    cfg = config(tmp_path, hc=True)
    run("premarket", tmp_path, FakeProbe(revision="f" * 40), cfg=cfg,
        post=Pings(fail_first=10 ** 6))                         # a failure, undelivered
    unsyncable_reports(monkeypatch)
    run("premarket", tmp_path, start="08:40:00", cfg=cfg, post=Pings(fail_first=10 ** 6))
    states = cli.notification_states(records(tmp_path), at("08:46"), tolerant=True)
    failure = next(n for n in states if n["event_type"] == "assessment_result")
    assert "superseded" not in failure["delivery_state"]       # still current


# ===========================================================================
# 0.4.3 — positive confirmation, durable resolution, exact lock times
# ===========================================================================
class Activity:
    """A probe that reports observations on `day`, for unreported-activity checks."""

    def __init__(self, day=DAY):
        self.day = day

    def signal_days(self, first, last):
        return [{"day": self.day.isoformat(), "observations": 39}]


def authority(root, day=DAY, shelf=None):
    """Everything an authoritative reader concludes from the records, as one dict.

    `shelf` is the Store to ask (one already used, say); a new one by default.
    """
    cfg = json.loads((Path(root) / "observer.json").read_text())
    shelf = shelf or records(root)
    calendar = cli.load_calendar(cfg)
    state = cli.phase_state(shelf, cfg, calendar, at("23:00", day))
    notes = cli.notification_states(shelf, at("23:00", day), tolerant=True)
    return {
        "readiness": cli.check_status(shelf, day.isoformat(), "premarket"),
        "midsession": cli.check_status(shelf, day.isoformat(), "midsession"),
        "authoritative": sorted(r["report_id"] for r in shelf.reports()),
        "authoritative_readable": sorted(r["report_id"]
                                         for r in shelf.confirmed_readable_reports()[0]),
        "diagnostic": sorted(r["report_id"] for r in shelf.all_reports()),
        "sessions": sorted(state["sessions"]), "qualifying": state["qualifying_sessions"],
        "reasons": state["computed_reasons"], "checkpoint": shelf.checkpoint() is not None,
        "baseline_from": (cli.trusted_baseline(
            shelf, cfg, (day + timedelta(days=1)).isoformat()) or {}).get("from_session"),
        "superseded": sorted(n["notification_id"] for n in notes
                             if "superseded" in n["delivery_state"]),
        "open_faults": sorted(f["kind"] for f in shelf.faults() if not f["resolved"]),
        "resolved_faults": sorted(f["kind"] for f in shelf.faults() if f["resolved"]),
        "unreported": [row["day"] for row in cli.unreported_activity(
            Activity(day), shelf, cfg, calendar, day + timedelta(days=1))]}


def fresh(root, code, expect_json=True):
    """Run `code` in a brand-new Python process; return (last JSON it printed, exit code)."""
    script = textwrap.dedent(f"""
        import json, sys
        sys.path[:0] = [{str(Path(__file__).parent)!r}, {str(OBSERVER)!r}]
        import test_observer as T
        from quantdesk_observer import cli, store
        ROOT = T.Path({str(root)!r})
        CONFIG = str(ROOT / "observer.json")
    """) + textwrap.dedent(code)
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          timeout=180, cwd=str(root))
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    if expect_json:
        assert lines, done.stdout + done.stderr
        return json.loads(lines[-1]), done.returncode
    return None, done.returncode


def fd_path(fd):
    try:
        import fcntl
        return fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0].decode()
    except (AttributeError, OSError):
        return os.readlink(f"/proc/self/fd/{fd}")


PUBLISH_STEPS = ("write-before", "write-after", "file-sync-after", "link-before", "link-after",
                 "dir-sync-before", "dir-sync-after")


def inject(target, step, patch, terminate=False):
    """Fail — or end the process at — one step of publishing a record under `target/`.

    Works on the operating-system calls themselves, so it hits the observer's real
    code path. `patch(obj, name, value)` installs a replacement. Fires once.
    """
    real_open, real_fsync, real_link = os.open, os.fsync, os.link
    op, when = step.rsplit("-", 1)
    fired = {"done": False}

    def mine(path):
        return not fired["done"] and target in Path(path).parts

    def fail():
        fired["done"] = True
        if terminate:
            os._exit(70)
        raise OSError(5, f"injected failure: {step}")

    def fake_open(path, flags, *args, **kwargs):
        if op == "write" and when == "before" and flags & os.O_CREAT and mine(path):
            fail()
        return real_open(path, flags, *args, **kwargs)

    def fake_fsync(fd):
        path = fd_path(fd)
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if mine(path):
            scratch = not directory and Path(path).name.startswith(".tmp-")
            if op == "dir-sync" and directory and when == "before":
                fail()
            if op == "write" and when == "after" and scratch:
                fail()                       # written, not yet synced
            if (op == "file-sync" and scratch) or (op == "dir-sync" and directory):
                real_fsync(fd)
                fail()
        return real_fsync(fd)

    def fake_link(src, dst, *args, **kwargs):
        if op == "link" and mine(dst):
            if when == "before":
                fail()
            real_link(src, dst, *args, **kwargs)
            fail()
        return real_link(src, dst, *args, **kwargs)
    patch(os, "open", fake_open)
    patch(os, "fsync", fake_fsync)
    patch(os, "link", fake_link)


def failing_dir_sync(name, patch):
    """Every sync of a directory whose name is `name`, or which lies under it, fails."""
    real = store.sync_directory

    def sync(directory):
        if name in Path(directory).parts:
            raise OSError(5, "Input/output error")
        return real(directory)
    patch(store, "sync_directory", sync)


def resealed(path, change):
    """Rewrite a record with `change` applied and a valid seal: a substitution."""
    record = json.loads(Path(path).read_text())
    record.pop("integrity")
    change(record)
    Path(path).chmod(0o640)
    Path(path).write_text(json.dumps(store.seal(record), indent=2, sort_keys=True) + "\n")


def only(root, kind, check="premarket"):
    (path,) = (Path(root) / f"records/{kind}").rglob(f"{check}-*.json")
    return path


# --- A. report confirmation ------------------------------------------------------
def test_a_published_report_is_confirmed_and_bound_to_its_exact_bytes(tmp_path):
    cfg = config(tmp_path)
    cfg_file(tmp_path, cfg)
    report, _ = run("premarket", tmp_path, cfg=cfg)
    path, confirmation = only(tmp_path, "reports"), only(tmp_path, "confirmations")
    record = json.loads(confirmation.read_text())
    assert record["schema"] == "observer_report_confirmation/1" and record["basis"] == "published"
    assert record["report_id"] == report["report_id"]
    assert record["report_path"] == str(path.relative_to(tmp_path / "records"))
    assert record["file_sha256"] == store.file_sha256(path)
    assert record["content_sha256"] == json.loads(path.read_text())["integrity"][
        "content_sha256"]
    seen, _ = fresh(tmp_path, "print(json.dumps(T.authority(ROOT)))")      # a new process
    assert seen["authoritative"] == [report["report_id"]] and seen["readiness"] == "PASS"


@pytest.mark.parametrize("damage", ["missing", "corrupt", "report_id", "report_path",
                                    "content_sha256", "file_sha256", "report_bytes",
                                    "other_confirmation"])
def test_a_confirmation_counts_only_if_it_is_exactly_this_reports(tmp_path, damage):
    cfg = config(tmp_path)
    cfg_file(tmp_path, cfg)
    report, _ = run("premarket", tmp_path, cfg=cfg)
    path, confirmation = only(tmp_path, "reports"), only(tmp_path, "confirmations")
    if damage == "missing":
        confirmation.unlink()
    elif damage == "corrupt":
        confirmation.chmod(0o640)
        confirmation.write_text("{not json")
    elif damage == "report_bytes":
        resealed(path, lambda r: r["failure_reasons"].append("changed afterwards"))
    elif damage == "other_confirmation":
        other, _ = run("premarket", tmp_path / "b", cfg=config(tmp_path / "b"))
        confirmation.chmod(0o640)
        confirmation.write_bytes(only(tmp_path / "b", "confirmations").read_bytes())
    else:
        resealed(confirmation, lambda r: r.update({damage: "0" * len(str(r[damage]))}))
    seen, _ = fresh(tmp_path, "print(json.dumps(T.authority(ROOT)))")
    assert seen["authoritative"] == [] and seen["authoritative_readable"] == []
    assert seen["diagnostic"] == [report["report_id"]]          # kept, for diagnosis only
    assert seen["readiness"] == "MISSING" and seen["open_faults"] == []
    assert records(tmp_path).reports() == []


@pytest.mark.parametrize("name, stage", [("reports", "report-sync"),
                                         ("confirmations", "confirmation-sync")])
def test_a_new_directory_entry_must_be_synced_too(tmp_path, monkeypatch, name, stage):
    """The date folder made for a record is a name in its parent: that parent is synced."""
    real = store.sync_directory

    def sync(directory):
        if Path(directory).name == name:          # only the parent of the new date folder
            raise OSError(5, "Input/output error")
        return real(directory)
    monkeypatch.setattr(store, "sync_directory", sync)
    outcome = {}
    run("premarket", tmp_path, cfg=config(tmp_path, hc=True), publication=outcome)
    assert outcome["state"] == "PRESENT_UNCONFIRMED" and outcome["stage"] == stage
    assert records(tmp_path).reports() == []


def test_a_report_from_before_confirmations_is_diagnostic_only(tmp_path):
    """Historical reports are not grandfathered, and nothing certifies them from their
    own contents."""
    run("premarket", tmp_path / "old")
    (tmp_path / "records").mkdir()
    import shutil
    shutil.copytree(tmp_path / "old/records/reports", tmp_path / "records/reports")
    cfg = config(tmp_path, hc=True)
    cfg_file(tmp_path, cfg)
    before = only(tmp_path, "reports").read_bytes()
    assert authority(tmp_path)["readiness"] == "MISSING"
    beat(tmp_path, cfg, at("09:00"))                      # no fault: nothing to reconcile
    seen = authority(tmp_path)
    assert seen["authoritative"] == [] and len(seen["diagnostic"]) == 1
    assert not (tmp_path / "records/confirmations").exists()
    assert only(tmp_path, "reports").read_bytes() == before


# --- B. fail closed across a restart ----------------------------------------------
def test_an_unconfirmed_report_establishes_nothing_in_a_new_process(tmp_path):
    """Codex's HIGH-1: the report's sync failed and so did fault storage."""
    cfg = config(tmp_path, hc=True)
    cfg_file(tmp_path, cfg)
    _, code = fresh(tmp_path, """
        c = json.loads(open(CONFIG).read())
        down = T.Pings(fail_first=10 ** 6)
        # A confirmed premarket FAIL whose alert is still undelivered ...
        T.run("premarket", ROOT, T.FakeProbe(revision="f" * 40), cfg=c, post=down)
        # ... then a PASS at 08:40, the 12:30 run and a triggering after-close, none of
        # them confirmable, with no fault recorded for any of them.
        T.failing_dir_sync("reports", setattr)
        store.Store.write_fault = lambda *a: (_ for _ in ()).throw(OSError(28, "full"))
        T.run("premarket", ROOT, T.FakeProbe(), "08:40:00", cfg=c, post=down)
        T.run("midsession", ROOT, T.midday(), "12:30:00", cfg=c, post=T.Pings())
        code = cli.main(["afterclose", "--config", CONFIG], probe=T.trigger_probe(),
                        post=T.Pings(), clock=T.Clock(T.at("16:00:00")), sleep=T.never_sleep,
                        alarm=False)
        print(json.dumps({"code": code}))
    """)
    assert code == 0
    seen, _ = fresh(tmp_path, """
        cli.main(["liveness", "--config", CONFIG], post=T.Pings(), clock=lambda: T.at("16:40"),
                 alarm=False)                           # housekeeping in a new process
        print(json.dumps(T.authority(ROOT)))
    """)
    assert len(seen["diagnostic"]) == 4 and len(seen["authoritative"]) == 1   # survivors
    assert seen["readiness"] == "FAIL"                  # the 08:40 PASS is not readiness
    assert seen["midsession"] == "MISSING"              # nor is the 12:30 run completion
    assert seen["qualifying"] == [] and DAY.isoformat() not in seen["sessions"]
    assert seen["reasons"] == [] and seen["checkpoint"] is False   # no trigger, no checkpoint
    assert seen["baseline_from"] == "configuration"
    assert seen["superseded"] == []                    # the 08:35 failure is still current
    assert seen["open_faults"] == []                   # no fault, and still not confirmed


def test_every_authoritative_reader_requires_confirmation(tmp_path, monkeypatch):
    cfg = config(tmp_path, hc=True)
    cfg_file(tmp_path, cfg)
    run("premarket", tmp_path, cfg=cfg)
    run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg)
    failing_dir_sync("reports", monkeypatch.setattr)
    monkeypatch.setattr(Store, "write_fault", boom(OSError(28, "full")))
    run("afterclose", tmp_path, trigger_probe(), "16:00:00", cfg=cfg)
    monkeypatch.undo()
    shelf, calendar = records(tmp_path), cli.load_calendar(cfg)
    day = DAY.isoformat()
    assert [r["check"] for r in shelf.all_reports()] == ["afterclose", "midsession",
                                                         "premarket"]
    assert shelf.reports(check="afterclose") == []
    assert shelf.confirmed_readable_reports()[0] and all(
        r["check"] != "afterclose" for r in shelf.confirmed_readable_reports()[0])
    assert cli.accounting(shelf)[0] == {}
    assert cli.phase_state(shelf, cfg, calendar, at("23:00"))["computed_reasons"] == []
    assert cli.trusted_baseline(shelf, cfg, "2026-10-15")["from_session"] == "configuration"

    class Activity:
        def signal_days(self, first, last):
            return [{"day": day, "observations": 39}]
    unreported = cli.unreported_activity(Activity(), shelf, cfg, calendar, DAY + timedelta(1))
    assert [row["day"] for row in unreported] == [day]   # an unconfirmed report is no report
    assert cli.check_status(shelf, day, "premarket") == "PASS"   # confirmed ones still count


# --- C. durable resolution -------------------------------------------------------
def unconfirmed_afterclose(tmp_path, monkeypatch):
    cfg = config(tmp_path, hc=True)
    cfg_file(tmp_path, cfg)
    run("premarket", tmp_path, cfg=cfg, post=Pings())
    run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg, post=Pings())
    unsyncable_reports(monkeypatch, "afterclose-")
    run("afterclose", tmp_path, trigger_probe(), "16:00:00", cfg=cfg, post=Pings())
    monkeypatch.undo()
    return cfg


def test_codex_an_undurable_resolution_is_not_a_resolution_in_any_process(tmp_path,
                                                                          monkeypatch):
    """Codex's HIGH-2: the resolution's link succeeded and its directory sync failed."""
    unconfirmed_afterclose(tmp_path, monkeypatch)
    during, _ = fresh(tmp_path, """
        T.failing_dir_sync("faults", setattr)
        cli.main(["liveness", "--config", CONFIG], post=T.Pings(), clock=lambda: T.at("16:20"),
                 alarm=False)
        print(json.dumps(T.authority(ROOT)))           # the same process, afterwards
    """)
    # The resolution file does exist (its link succeeded) — and still resolves nothing.
    (fault,) = records(tmp_path).faults()
    closings = list((tmp_path / "records/faults").glob("*-resolution.json"))
    assert len(closings) == 1 and closings[0].name == f"{fault['fault_id']}-resolution.json"
    closing = json.loads(closings[0].read_text())
    assert closing["fault_id"] == fault["fault_id"]
    assert closing["report_id"] == fault["report"]["report_id"]
    assert during["open_faults"] == ["report-publication"] and during["qualifying"] == []
    assert during["reasons"] == [] and during["checkpoint"] is False
    later, _ = fresh(tmp_path, """
        T.failing_dir_sync("faults", setattr)           # still cannot be made durable
        print(json.dumps(T.authority(ROOT)))
    """)
    assert later["open_faults"] == ["report-publication"] and later["qualifying"] == []
    assert later["baseline_from"] == "configuration"
    recovered, _ = fresh(tmp_path, """
        cli.main(["liveness", "--config", CONFIG], post=T.Pings(), clock=lambda: T.at("16:30"),
                 alarm=False)                           # verified, synced, then accounted
        print(json.dumps(T.authority(ROOT)))
    """)
    assert recovered["open_faults"] == [] and recovered["resolved_faults"] == [
        "report-publication"]
    assert recovered["qualifying"] == [DAY.isoformat()] and recovered["checkpoint"] is True


def test_a_resolution_requires_confirmation_and_a_durable_record(tmp_path, monkeypatch):
    cfg = unconfirmed_afterclose(tmp_path, monkeypatch)
    path = only(tmp_path, "reports", "afterclose")
    frozen = path.read_bytes()
    code, pings = beat(tmp_path, cfg, at("16:20"))
    (fault,) = records(tmp_path).faults()
    assert fault["resolved"] and code == 0
    confirmation = json.loads(only(tmp_path, "confirmations", "afterclose").read_text())
    assert confirmation["basis"] == "reconciled"
    assert confirmation["evidence"]["fault_id"] == fault["fault_id"]
    closing = fault["resolution"]
    assert closing["report_id"] == confirmation["report_id"]
    assert closing["verified_file_sha256"] == confirmation["file_sha256"]
    assert path.read_bytes() == frozen                       # nothing rewritten
    assert ("afterclose", "success") not in pings.kinds()     # no synthetic recovery
    assert not (tmp_path / "operator/acknowledgments").exists()   # nothing acknowledged
    seen, _ = fresh(tmp_path, "print(json.dumps(T.authority(ROOT)))")
    assert seen["resolved_faults"] == ["report-publication"]  # durable: a new process agrees


@pytest.mark.parametrize("damage", ["corrupt", "fault_id", "report_id", "content_sha256",
                                    "another_report"])
def test_a_damaged_or_substituted_resolution_is_rejected(tmp_path, monkeypatch, damage):
    cfg = unconfirmed_afterclose(tmp_path, monkeypatch)
    beat(tmp_path, cfg, at("16:20"))
    (closing,) = (tmp_path / "records/faults").glob("*-resolution.json")
    if damage == "corrupt":
        closing.chmod(0o640)
        closing.write_text("{not json")
    elif damage == "another_report":                      # a real, confirmed report
        premarket = json.loads(only(tmp_path, "reports").read_text())
        resealed(closing, lambda r: r.update(
            report_id=premarket["report_id"],
            content_sha256=premarket["integrity"]["content_sha256"]))
    else:
        resealed(closing, lambda r: r.update({damage: "0" * 12}))
    seen, _ = fresh(tmp_path, "print(json.dumps(T.authority(ROOT)))")
    assert seen["open_faults"] == ["report-publication"]
    assert seen["qualifying"] == [] and seen["baseline_from"] == "configuration"
    (fault,) = records(tmp_path).faults()
    assert fault["resolution_rejected"]


def test_failed_reconciliation_resolves_nothing(tmp_path, monkeypatch):
    cfg = unconfirmed_afterclose(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "housekeeping", boom(RuntimeError("reconciliation failed")))
    beat(tmp_path, cfg, at("16:20"))
    monkeypatch.undo()
    assert not list((tmp_path / "records/faults").glob("*-resolution.json"))
    assert not list((tmp_path / "records/confirmations").rglob("afterclose-*"))
    assert authority(tmp_path)["qualifying"] == []


# --- D. provenance survives recovery -------------------------------------------------
@pytest.mark.parametrize("kind", ["timely", "late", "interrupted", "reconstruction"])
def test_recovery_restores_exactly_what_the_assessment_was(tmp_path, monkeypatch, kind):
    def assess(root, broken):
        cfg = config(root, hc=True)
        cfg_file(root, cfg)
        check = "afterclose" if kind == "reconstruction" else "premarket"
        if kind == "reconstruction":
            run("premarket", root, cfg=cfg)
            run("midsession", root, midday(), "12:30:00", cfg=cfg)
        if broken:
            unsyncable_reports(monkeypatch, f"{check}-")
        if kind == "interrupted":
            monkeypatch.setitem(cli.RUNS, "premarket", boom(RunTimeout("limit reached")))
        if kind == "reconstruction":
            run("afterclose", root, FakeProbe(), "07:30:00", DAY + timedelta(days=1), cfg=cfg,
                session_date=DAY.isoformat())
        else:
            run("premarket", root, start="09:10:00" if kind == "late" else "08:35:00",
                cfg=cfg)
        monkeypatch.undo()
        return cfg, check
    assess(tmp_path / "control", broken=False)
    cfg, check = assess(tmp_path / "broken", broken=True)
    path = only(tmp_path / "broken", "reports", check)
    frozen = path.read_bytes()
    beat(tmp_path / "broken", cfg, at("23:30", DAY + timedelta(days=1)))
    control, recovered = authority(tmp_path / "control"), authority(tmp_path / "broken")
    for key in ("readiness", "midsession", "sessions", "qualifying", "reasons",
                "baseline_from"):
        assert recovered[key] == control[key], key        # no more, no less than it was
    assert path.read_bytes() == frozen                    # timestamps and all, unchanged
    if kind == "late":                                    # started outside its window
        assert recovered["readiness"] == "MISSING"        # not turned into readiness
    if kind == "interrupted":
        assert recovered["readiness"] == "FAIL"           # not turned into a pass


def test_recovery_rewrites_no_exception_and_reopens_no_checkpoint(tmp_path, monkeypatch):
    cfg = config(tmp_path, hc=True)
    cfg_file(tmp_path, cfg)
    days = weekdays(2)
    early_review_day(tmp_path, cfg, Pings(), days[0])       # a final checkpoint
    checkpoint = tmp_path / "records/checkpoints/review-pending.json"
    final = checkpoint.read_bytes()
    run("midsession", tmp_path, midday(days[1]), "12:30:00", days[1], cfg)   # no premarket
    unsyncable_reports(monkeypatch, "afterclose-")
    run("afterclose", tmp_path, trigger_probe(days[1]), "16:00:00", days[1], cfg)
    monkeypatch.undo()
    path = next((tmp_path / f"records/reports/{days[1]}").glob("afterclose-*.json"))
    frozen = path.read_bytes()
    beat(tmp_path, cfg, at("16:40", days[1]))
    entry = authority(tmp_path, days[1])
    state = phase(tmp_path, cfg, at("23:00", days[1]))["sessions"][days[1].isoformat()]
    assert state["outcome"] == "OPERATIONAL EXCEPTION REQUIRED" and not state["qualifies"]
    assert state["exception_approval"] is None and entry["resolved_faults"]
    assert checkpoint.read_bytes() == final and path.read_bytes() == frozen


# --- E. lock times --------------------------------------------------------------------
class Manual:
    """A clock that moves only when time passes, not when it is read."""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


def slow_lock(monkeypatch, clock, seconds=1):
    real = Store.lock

    def lock(self, *args, **kwargs):
        held = real(self, *args, **kwargs)
        clock.now += timedelta(seconds=seconds)          # held only this much later
        return held
    monkeypatch.setattr(Store, "lock", lock)


def test_codex_the_acquisition_time_is_read_after_the_lock_is_held(tmp_path, monkeypatch):
    """Codex's finding: actually acquired 08:45:00, recorded 08:44:59."""
    seen = limit_spy(monkeypatch)
    clock = Manual(at("08:44:59"))
    slow_lock(monkeypatch, clock)
    report = cli.run_check("premarket", config(tmp_path), records(tmp_path), FakeProbe(), clock,
                           clock.sleep, post=Pings())
    assert report["invoked_at_ist"].endswith("08:44:59+05:30")
    assert report["lock_attempted_at_ist"].endswith("08:44:59+05:30")
    assert report["lock_acquired_at_ist"].endswith("08:45:00+05:30")      # as it happened
    assert not report["ran_checks"] and report["assessment_status"] == "MISSING"
    assert "assessment_started_at_ist" not in report and seen == []
    monkeypatch.undo()
    records(tmp_path).lock().close()                                       # released


def test_an_acquisition_inside_the_window_starts_work_with_what_is_left(tmp_path,
                                                                        monkeypatch):
    seen = limit_spy(monkeypatch)
    clock = Manual(at("08:44:00"))
    slow_lock(monkeypatch, clock, 20)                    # held at 08:44:20
    report = cli.run_check("premarket", config(tmp_path), records(tmp_path), FakeProbe(), clock,
                           clock.sleep, post=Pings())
    assert report["lock_attempted_at_ist"].endswith("08:44:00+05:30")
    assert report["lock_acquired_at_ist"].endswith("08:44:20+05:30")
    assert report["assessment_started_at_ist"].endswith("08:44:20+05:30")
    assert report["lock_wait_seconds"] == 20 and seen == [40.0] and report["timely"]


def test_a_late_acquisition_spends_the_late_allowance_and_gets_no_more(tmp_path, monkeypatch):
    seen = limit_spy(monkeypatch)
    clock = Manual(at("09:10:00"))                       # late; its 180 s end at 09:13:00
    slow_lock(monkeypatch, clock, 180)                   # held exactly at 09:13:00
    report = cli.run_check("premarket", config(tmp_path), records(tmp_path), FakeProbe(), clock,
                           clock.sleep, post=Pings())
    assert report["lock_acquired_at_ist"].endswith("09:13:00+05:30")
    assert not report["ran_checks"] and report["assessment_status"] == "MISSING"
    assert seen == []
    monkeypatch.undo()
    records(tmp_path).lock().close()


# --- F. a missing report's fault -------------------------------------------------------
def test_an_absent_reports_fault_ends_only_with_a_confirmed_assessment_of_the_same_check(
        tmp_path, monkeypatch):
    cfg = config(tmp_path, hc=True)
    cfg_file(tmp_path, cfg)
    days = weekdays(3)
    real = Store.write_report
    monkeypatch.setattr(Store, "write_report", boom(OSError(28, "No space left on device")))
    run("premarket", tmp_path, FakeProbe(days[0]), day=days[0], cfg=cfg)
    monkeypatch.setattr(Store, "write_report", real)
    (fault,) = records(tmp_path).faults()
    missing = fault["report"]
    assert missing["state"] == "ABSENT"
    effect = fault["effect"]
    assert "MISSING" in effect["missing_evidence"] and "none" in effect["publication_barrier"]
    assert "same check" in effect["monitoring_attention"]

    def still_open():
        return [f["fault_id"] for f in records(tmp_path).faults()
                if not f["resolved"] and f["fault_id"] == fault["fault_id"]]
    run("midsession", tmp_path, midday(days[0]), "12:30:00", days[0], cfg)   # another check
    assert still_open()
    beat(tmp_path, cfg, at("13:00", days[0]))                                  # liveness
    assert still_open()
    run("afterclose", tmp_path, FakeProbe(days[0]), "16:00:00", days[0], cfg)
    assert still_open()
    unsyncable_reports(monkeypatch, "premarket-")                              # unconfirmed
    run("premarket", tmp_path, FakeProbe(days[1]), day=days[1], cfg=cfg)
    monkeypatch.undo()
    assert still_open()
    outbox = {p: p.read_bytes() for p in (tmp_path / "records/outbox").rglob("*") if p.is_file()}
    pings = Pings()
    replacement, _ = run("premarket", tmp_path, FakeProbe(days[2], revision="f" * 40),
                         day=days[2], cfg=cfg, post=pings)    # real, confirmed, and a FAIL
    assert replacement["result"] == "FAIL" and not still_open()
    (closing,) = [f["resolution"] for f in records(tmp_path).faults()
                  if f["fault_id"] == fault["fault_id"]]
    assert closing["original_report"]["report_id"] == missing["report_id"]
    assert closing["original_report"]["state"] == "ABSENT"
    assert closing["replacement_report"]["report_id"] == replacement["report_id"]
    assert closing["reconciliation"]["original_still_absent"] is True
    assert not (tmp_path / "records" / missing["report_path"]).exists()
    assert cli.check_status(records(tmp_path), days[0].isoformat(), "premarket") == "MISSING"
    entry = phase(tmp_path, cfg)["sessions"][days[0].isoformat()]
    assert not entry["qualifies"] and entry["exception_approval"] is None
    assert pings.kinds() == [("premarket", "fail")]          # its own signal; nothing synthetic
    assert all(p.read_bytes() == b for p, b in outbox.items())   # history unchanged
    seen, _ = fresh(tmp_path, """
        s = store.Store(ROOT / "records")
        print(json.dumps({f["fault_id"]: f["resolved"] for f in s.faults()}))
    """)
    assert seen[fault["fault_id"]] is True                    # durable for a new process


def test_an_acknowledgment_or_another_check_cannot_end_an_absent_fault(tmp_path, monkeypatch):
    cfg = config(tmp_path, hc=True)
    days = weekdays(2)
    early_review_day(tmp_path, cfg, Pings(), days[0])
    monkeypatch.setattr(Store, "write_report", boom(OSError(28, "No space left on device")))
    run("premarket", tmp_path, FakeProbe(days[1]), day=days[1], cfg=cfg)
    monkeypatch.undo()
    acknowledge(tmp_path)
    beat(tmp_path, cfg, at("09:00", days[1]))
    run("midsession", tmp_path, midday(days[1]), "12:30:00", days[1], cfg)
    (fault,) = records(tmp_path).faults()
    assert fault["report"]["state"] == "ABSENT" and not fault["resolved"]


# --- G. metadata and immutability -------------------------------------------------------
def test_publication_fault_metadata_says_what_it_blocks(tmp_path, monkeypatch, capsys):
    cfg = config(tmp_path, hc=True)
    premarket_unconfirmed(tmp_path, monkeypatch, capsys, cfg)
    (fault,) = records(tmp_path).faults()
    effect = fault["effect"]
    assert fault["report"]["state"] == "PRESENT_UNCONFIRMED"
    assert "counts for nothing" in effect["publication_barrier"]
    assert "readiness" in effect["publication_barrier"]
    assert "until confirmed" in effect["missing_evidence"]
    assert "cannot use this report" in effect["qualification_impact"]
    assert "none on any session" not in json.dumps(effect)


def test_older_records_are_read_and_never_changed(tmp_path, monkeypatch):
    cfg = config(tmp_path, hc=True)
    cfg_file(tmp_path, cfg)
    monkeypatch.setattr(cli, "housekeeping", boom(RuntimeError("bug")))
    beat(tmp_path, cfg, at("08:00"))                          # a housekeeping fault, 0.4.2-style
    monkeypatch.undo()
    old = {p: p.read_bytes() for p in (tmp_path / "records").rglob("*")
           if p.is_file() and "faults" in p.parts}
    unsyncable_reports(monkeypatch, "premarket-")
    run("premarket", tmp_path, cfg=cfg)
    monkeypatch.undo()
    beat(tmp_path, cfg, at("08:50"))
    assert all(p.read_bytes() == b for p, b in old.items())
    assert {f["kind"]: f["resolved"] for f in records(tmp_path).faults()} == {
        "housekeeping": True, "report-publication": True}


# --- H. every step of publishing, with and without the process surviving -----------------
@pytest.mark.parametrize("target", ["reports", "confirmations"])
@pytest.mark.parametrize("step", PUBLISH_STEPS)
def test_a_failure_at_any_publishing_step_leaves_the_report_unconfirmed(tmp_path, monkeypatch,
                                                                      target, step):
    cfg = config(tmp_path, hc=True)
    cfg_file(tmp_path, cfg)
    inject(target, step, monkeypatch.setattr)
    outcome = {}
    report, _ = run("premarket", tmp_path, cfg=cfg, publication=outcome)
    monkeypatch.undo()
    absent = target == "reports" and step in ("write-before", "write-after", "file-sync-after",
                                              "link-before")
    assert outcome["state"] == ("ABSENT" if absent else "PRESENT_UNCONFIRMED")
    if target == "reports":                  # the confirmation comes only after the report
        assert not (tmp_path / "records/confirmations").exists() or not list(
            (tmp_path / "records/confirmations").rglob("*.json"))
    (fault,) = records(tmp_path).faults()
    assert fault["report"]["state"] == outcome["state"]
    seen, _ = fresh(tmp_path, "print(json.dumps(T.authority(ROOT)))")      # restart
    assert seen["authoritative"] == [] and seen["readiness"] == "MISSING"
    assert seen["open_faults"] == ["report-publication"]
    beat(tmp_path, cfg, at("08:50"))                                        # a later run
    after = authority(tmp_path)
    if absent:
        assert after["open_faults"] == ["report-publication"]
        assert after["readiness"] == "MISSING" and after["diagnostic"] == []
    else:
        assert after["open_faults"] == [] and after["readiness"] == "PASS"
        assert after["authoritative"] == [report["report_id"]]


@pytest.mark.parametrize("target, step, survives", [
    ("reports", "link-after", False), ("reports", "dir-sync-before", False),
    ("confirmations", "link-after", True), ("confirmations", "dir-sync-before", True)])
def test_a_process_that_dies_mid_publication(tmp_path, target, step, survives):
    cfg = config(tmp_path, hc=True)
    cfg_file(tmp_path, cfg)
    _, code = fresh(tmp_path, f"""
        T.inject({target!r}, {step!r}, setattr, terminate=True)
        cli.main(["premarket", "--config", CONFIG], probe=T.FakeProbe(), post=T.Pings(),
                 clock=T.Clock(T.at("08:35:00")), sleep=T.never_sleep, alarm=False)
    """, expect_json=False)
    assert code == 70                                   # ended at that very step
    blocked, _ = fresh(tmp_path, """
        T.failing_dir_sync("confirmations", setattr)    # the new process cannot sync it
        print(json.dumps(T.authority(ROOT)))
    """)
    assert blocked["authoritative"] == [] and blocked["open_faults"] == []
    seen, _ = fresh(tmp_path, "print(json.dumps(T.authority(ROOT)))")
    assert len(seen["diagnostic"]) == 1 and seen["open_faults"] == []
    if survives:      # the report was synced and its confirmation linked: verified, synced
        assert seen["authoritative"] == seen["diagnostic"] and seen["readiness"] == "PASS"
    else:             # an orphan: no confirmation, no fault, never certified
        assert seen["authoritative"] == [] and seen["readiness"] == "MISSING"
        beat(tmp_path, cfg, at("08:50"))
        assert authority(tmp_path)["authoritative"] == []


# ===========================================================================
# 0.4.4 — no reusable positive verdicts; eligible replacements; exact hashes
# ===========================================================================
def resolved_afterclose(tmp_path, monkeypatch):
    """A day whose after-close report was unconfirmed, then properly recovered."""
    cfg = unconfirmed_afterclose(tmp_path, monkeypatch)
    beat(tmp_path, cfg, at("16:20"))
    (fault,) = records(tmp_path).faults()
    assert fault["resolved"]
    return cfg, fault


def resolution_file(root, fault_id="*"):
    (path,) = list((Path(root) / "records/faults").glob(f"{fault_id}-resolution.json"))
    return path


OTHER_HASH = "ab" * 32


def damage_evidence(root, damage):
    """Change, remove or substitute one piece of the evidence a decision rests on."""
    records_root = Path(root) / "records"
    premarket = only(root, "reports")
    confirmation = only(root, "confirmations")
    if damage == "confirmation-corrupt":
        confirmation.chmod(0o640)
        confirmation.write_text("{not json")
    elif damage == "confirmation-removed":
        confirmation.unlink()
    elif damage == "confirmation-substituted":
        confirmation.chmod(0o640)
        confirmation.write_bytes(only(root, "confirmations", "midsession").read_bytes())
    elif damage == "report-bytes":
        resealed(premarket, lambda r: r["failure_reasons"].append("changed afterwards"))
    elif damage == "confirmation-content-hash":
        resealed(confirmation, lambda r: r.update(content_sha256=OTHER_HASH))
    elif damage == "confirmation-file-hash":
        resealed(confirmation, lambda r: r.update(file_sha256=OTHER_HASH))
    elif damage == "confirmation-path":
        resealed(confirmation, lambda r: r.update(
            report_path=str(only(root, "reports", "midsession").relative_to(records_root))))
    elif damage == "confirmation-report-id":
        resealed(confirmation, lambda r: r.update(report_id="000000000000"))
    elif damage == "fault-evidence":
        (fault_path,) = list((records_root / "faults").glob("*-fault.json"))
        resealed(fault_path, lambda r: r["report"].update(content_sha256=OTHER_HASH))
    elif damage == "resolution-evidence":
        resealed(resolution_file(root), lambda r: r.update(verified_file_sha256="0" * 64))
    elif damage == "dependent-removed":
        only(root, "confirmations", "afterclose").unlink()
    else:
        raise AssertionError(damage)


STALE = ["confirmation-corrupt", "confirmation-removed", "confirmation-substituted",
         "report-bytes", "confirmation-content-hash", "confirmation-file-hash",
         "confirmation-path", "confirmation-report-id", "fault-evidence",
         "resolution-evidence", "dependent-removed"]


@pytest.mark.parametrize("damage", STALE)
def test_a_store_already_used_sees_changed_evidence_as_a_new_one_does(tmp_path, monkeypatch,
                                                                     damage):
    """Codex's finding: a positive verdict cached by report id outlived its evidence."""
    resolved_afterclose(tmp_path, monkeypatch)
    shelf = records(tmp_path)
    warm = authority(tmp_path, shelf=shelf)               # every reader, through one Store
    assert warm["readiness"] == "PASS" and warm["qualifying"] == [DAY.isoformat()]
    damage_evidence(tmp_path, damage)
    same = authority(tmp_path, shelf=shelf)               # the same Store, afterwards
    new = authority(tmp_path)                             # a new Store
    process, _ = fresh(tmp_path, "print(json.dumps(T.authority(ROOT)))")   # a new process
    assert same == new == process
    assert same != warm
    if damage.startswith(("confirmation", "report")):
        assert same["readiness"] == "MISSING"              # premarket readiness withdrawn
    else:
        assert same["qualifying"] == [] and same["open_faults"] == ["report-publication"]
        assert same["baseline_from"] == "configuration"


def test_a_failed_confirmation_stays_failed_in_its_own_process(tmp_path, monkeypatch):
    """The intended exception: what this process failed to do is not later taken as done."""
    cfg = config(tmp_path, hc=True)
    cfg_file(tmp_path, cfg)
    inject("confirmations", "dir-sync-before", monkeypatch.setattr)    # fires once
    monkeypatch.setattr(Store, "write_fault", boom(OSError(28, "full")))
    shelf = records(tmp_path)
    cli.run_check("premarket", cfg, shelf, FakeProbe(), Clock(at("08:35:00")), never_sleep,
                  post=Pings())
    monkeypatch.undo()                                    # the disk works again
    assert only(tmp_path, "confirmations").exists()
    assert authority(tmp_path, shelf=shelf)["authoritative"] == []   # still failed here
    assert "failed in this process" in shelf.unconfirmed[json.loads(
        only(tmp_path, "reports").read_text())["report_id"]]
    # A later process verifies the surviving confirmation and syncs it itself.
    assert len(authority(tmp_path)["authoritative"]) == 1


def test_a_failed_resolution_stays_failed_in_its_own_process(tmp_path, monkeypatch):
    cfg = unconfirmed_afterclose(tmp_path, monkeypatch)
    shelf = records(tmp_path)
    failing_dir_sync("faults", monkeypatch.setattr)
    cli.liveness(cfg, shelf, lambda: at("16:20"), Pings())
    monkeypatch.undo()
    assert resolution_file(tmp_path).exists()
    assert authority(tmp_path, shelf=shelf)["open_faults"] == ["report-publication"]
    assert authority(tmp_path)["resolved_faults"] == ["report-publication"]   # later


def test_checking_the_evidence_again_does_not_spend_the_notification_budget(tmp_path,
                                                                           monkeypatch):
    import time as real_time
    cfg = config(tmp_path, hc=True)
    run("premarket", tmp_path, FakeProbe(revision="f" * 40), cfg=cfg,
        post=Pings(fail_first=10 ** 6))                  # a failure to deliver later
    run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg,
        post=Pings(fail_first=10 ** 6))                  # still undelivered after this run
    note, t = last_attempt(tmp_path, "assessment_result", "premarket")
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 2.0)
    real = store.sync_chain

    def slow(path, root):
        real_time.sleep(0.7)                              # every verification is slow
        return real(path, root)
    monkeypatch.setattr(store, "sync_chain", slow)
    pings = Pings()
    when = t + timedelta(minutes=5)
    reservations = files(tmp_path, "*-reservation.json")
    out = cli.Outbound(records(tmp_path), cfg, lambda: when, pings, alarm=True)
    began = real_time.monotonic()
    cli.deliver_round(records(tmp_path), out, when)
    elapsed = real_time.monotonic() - began
    # 0.4.5 (Astra): checking the evidence is notification work. It spends the one
    # shared deadline and is cut off by the real timer at it; nothing is sent after
    # it, and no fresh allowance is granted for sending.
    assert out.began is not None and out.began <= began + 0.05   # started before preparing
    assert 1.9 <= elapsed < 2.6                                   # interrupted at the deadline
    assert out.expiry["during"] == "the delivery round"
    assert pings.sent == []                                       # no request after expiry
    assert files(tmp_path, "*-reservation.json") == reservations  # and no reservation
    assert out.send("midsession", "fail", "x") is None            # nor any later identity
    note_after, t_after = last_attempt(tmp_path, "assessment_result", "premarket")
    assert len(note_after["attempts"]) == len(note["attempts"]) and t_after == t
    monkeypatch.undo()
    due = cli.notification_states(records(tmp_path), when, tolerant=True)
    assert [n["delivery_state"] for n in due if n["check"] == "premarket"] == [cli.DUE]


# --- B. what may end a missing report's fault -------------------------------------
NEXT = weekdays(2)[1]


def absent_afterclose(tmp_path, monkeypatch):
    cfg = config(tmp_path, hc=True)
    cfg_file(tmp_path, cfg)
    run("premarket", tmp_path, cfg=cfg)
    run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg)
    monkeypatch.setattr(Store, "write_report", boom(OSError(28, "No space left on device")))
    run("afterclose", tmp_path, FakeProbe(), "16:00:00", cfg=cfg)
    monkeypatch.undo()
    (fault,) = records(tmp_path).faults()
    assert fault["report"]["state"] == "ABSENT"
    return cfg, fault


def fault_now(root, fault_id):
    return next(f for f in records(root).faults() if f["fault_id"] == fault_id)


@pytest.mark.parametrize("kind, resolves", [
    ("reconstruction", False), ("early", False), ("another-check", False),
    ("interrupted", False), ("liveness", False), ("unconfirmed", False),
    ("genuine", True), ("genuine-fail", True), ("genuine-late", True)])
def test_only_a_genuine_later_scheduled_assessment_ends_an_absent_fault(tmp_path, monkeypatch,
                                                                       kind, resolves):
    cfg, fault = absent_afterclose(tmp_path, monkeypatch)
    outbox = {p: p.read_bytes() for p in (tmp_path / "records/outbox").rglob("*") if p.is_file()}
    if kind == "reconstruction":                          # ran its checks, of the same day
        rebuilt, _ = run("afterclose", tmp_path, FakeProbe(), "07:30:00", NEXT, cfg=cfg,
                         session_date=DAY.isoformat())
        assert rebuilt["ran_checks"] and rebuilt["invocation"] == "reconstruction"
    elif kind == "early":
        run("afterclose", tmp_path, FakeProbe(NEXT), "15:50:00", NEXT, cfg)
    elif kind == "another-check":
        run("midsession", tmp_path, midday(NEXT), "12:30:00", NEXT, cfg)
    elif kind == "interrupted":
        monkeypatch.setitem(cli.RUNS, "afterclose", boom(RunTimeout("limit reached")))
        run("afterclose", tmp_path, FakeProbe(NEXT), "16:00:00", NEXT, cfg)
        monkeypatch.undo()
    elif kind == "liveness":
        beat(tmp_path, cfg, at("16:10", NEXT))
    elif kind == "unconfirmed":
        unsyncable_reports(monkeypatch, "afterclose-")
        run("afterclose", tmp_path, FakeProbe(NEXT), "16:00:00", NEXT, cfg)
        monkeypatch.undo()
    else:
        if kind == "genuine":
            run("premarket", tmp_path, FakeProbe(NEXT), day=NEXT, cfg=cfg)
            run("midsession", tmp_path, midday(NEXT), "12:30:00", NEXT, cfg)
        replacement, _ = run("afterclose", tmp_path, FakeProbe(NEXT),
                             "18:00:00" if kind == "genuine-late" else "16:00:00", NEXT, cfg)
        assert replacement["ran_checks"] and replacement["invocation"] == "scheduled"
        assert (replacement["result"] == "FAIL") is (kind != "genuine")
    after = fault_now(tmp_path, fault["fault_id"])
    assert after["resolved"] is resolves
    assert not (tmp_path / "records" / fault["report"]["report_path"]).exists()
    sessions = phase(tmp_path, cfg)["sessions"]
    if kind == "reconstruction":         # its own evidence, under the existing policy
        assert sessions[DAY.isoformat()]["report_id"] == rebuilt["report_id"]
        assert not sessions[DAY.isoformat()]["qualifies"]
    else:
        assert DAY.isoformat() not in sessions                      # still missing
    assert all(p.read_bytes() == b for p, b in outbox.items())       # history unchanged
    assert len([n for n in intents(tmp_path, "observer_fault")       # nothing new on its account
                if fault["fault_id"] in n["fault_ids"]]) == 1
    if resolves:
        closing = after["resolution"]
        assert closing["original_report"]["report_id"] == fault["report"]["report_id"]
        assert closing["replacement_report"]["report_id"] == replacement["report_id"]
        assert closing["resolved_by_invocation"] == replacement["report_id"]
        seen, _ = fresh(tmp_path, "print(json.dumps(T.authority(ROOT)))")
        assert seen["open_faults"] == []                             # durable after restart


def substitute_replacement(root, fault, path, **over):
    """Point the persisted resolution at the report at `path`, consistently re-sealed."""
    report = json.loads(path.read_text())

    def change(record):
        record["replacement_report"] = {
            "report_id": report["report_id"], "check": report["check"],
            "session_date": report["session_date"],
            "report_path": str(path.relative_to(Path(root) / "records")),
            "content_sha256": report["integrity"]["content_sha256"],
            "file_sha256": store.file_sha256(path)}
        record["resolved_by_invocation"] = report["report_id"]
        for key, value in over.items():
            if isinstance(value, dict):
                record[key].update(value)
            else:
                record[key] = value
    resealed(resolution_file(root, fault["fault_id"]), change)


@pytest.mark.parametrize("substitution", [
    "reconstruction", "earlier", "own-publication-fault", "same-invocation", "resolved-early",
    "replacement-hash", "replacement-path", "original-report"])
def test_a_persisted_absent_resolution_is_checked_again_after_a_restart(tmp_path, monkeypatch,
                                                                       substitution):
    if substitution == "earlier":                         # a confirmed after-close of 10-13
        before = date(2026, 10, 13)
        run("afterclose", tmp_path, FakeProbe(before), "16:00:00", before,
            config(tmp_path, hc=True))
    cfg, fault = absent_afterclose(tmp_path, monkeypatch)
    replacement, _ = run("afterclose", tmp_path, FakeProbe(NEXT), "16:00:00", NEXT, cfg)
    assert fault_now(tmp_path, fault["fault_id"])["resolved"]
    shelf = records(tmp_path)
    assert authority(tmp_path, shelf=shelf)["open_faults"] == []     # warm
    reports = tmp_path / "records/reports"
    if substitution == "reconstruction":
        run("afterclose", tmp_path, FakeProbe(), "07:30:00", NEXT, cfg=cfg,
            session_date=DAY.isoformat())
        rebuilt = max((reports / DAY.isoformat()).glob("afterclose-*.json"))
        substitute_replacement(tmp_path, fault, rebuilt)
    elif substitution == "earlier":
        substitute_replacement(tmp_path, fault,
                               next((reports / "2026-10-13").glob("afterclose-*.json")))
    elif substitution == "own-publication-fault":         # confirmed only after a fault
        unsyncable_reports(monkeypatch, "afterclose-")
        run("afterclose", tmp_path, FakeProbe(NEXT), "16:05:00", NEXT, cfg)
        monkeypatch.undo()
        beat(tmp_path, cfg, at("16:20", NEXT))
        latest = max((reports / NEXT.isoformat()).glob("afterclose-*.json"),
                     key=lambda p: json.loads(p.read_text())["started_at_utc"])
        substitute_replacement(tmp_path, fault, latest)
    elif substitution == "same-invocation":
        resealed(resolution_file(tmp_path, fault["fault_id"]), lambda r: r.update(
            resolved_by_invocation=fault["detected_by_invocation"]))
    elif substitution == "resolved-early":
        resealed(resolution_file(tmp_path, fault["fault_id"]), lambda r: r.update(
            resolved_at_utc=replacement["assessment_started_at_utc"]))
    elif substitution == "replacement-hash":
        resealed(resolution_file(tmp_path, fault["fault_id"]),
                 lambda r: r["replacement_report"].update(file_sha256=OTHER_HASH))
    elif substitution == "replacement-path":
        resealed(resolution_file(tmp_path, fault["fault_id"]),
                 lambda r: r["replacement_report"].update(
                     report_path=fault["report"]["report_path"]))
    else:
        resealed(resolution_file(tmp_path, fault["fault_id"]),
                 lambda r: r["original_report"].update(report_id="000000000000"))
    assert not fault_now(tmp_path, fault["fault_id"])["resolved"]
    absent_open = [f for f in shelf.faults()
                   if f["fault_id"] == fault["fault_id"] and not f["resolved"]]
    assert absent_open                                    # the same Store agrees
    seen, _ = fresh(tmp_path, """
        s = store.Store(ROOT / "records")
        print(json.dumps({f["fault_id"]: f["resolved"] for f in s.faults()}))
    """)
    assert seen[fault["fault_id"]] is False               # and so does a new process


# --- C. times are compared as instants and fail closed -----------------------------
def test_replacement_times_are_instants_and_anything_doubtful_fails(tmp_path, monkeypatch):
    cfg, fault = absent_afterclose(tmp_path, monkeypatch)
    run("afterclose", tmp_path, FakeProbe(NEXT), "16:00:00", NEXT, cfg)
    closing = fault_now(tmp_path, fault["fault_id"])["resolution"]
    path = tmp_path / "records" / closing["replacement_report"]["report_path"]
    report = json.loads(path.read_text())
    resolved_at = closing["resolved_at_utc"]
    assert store.replacement_problem(report, fault, resolved_at) is None

    def problem(report_over=None, fault_over=None, resolved=resolved_at, drop=None):
        changed = {**report, **(report_over or {})}
        if drop:
            changed.pop(drop)
        return store.replacement_problem(changed, {**fault, **(fault_over or {})}, resolved)
    started = datetime.fromisoformat(report["assessment_started_at_utc"])
    completed = datetime.fromisoformat(report["assessment_completed_at_utc"])
    iso = datetime.isoformat
    assert problem(fault_over={"detected_at_utc": iso(started)})            # not strictly after
    assert problem(fault_over={"detected_at_utc": iso(started + timedelta(seconds=1))})
    # The same instants written in another zone: compared as instants, not as text.
    assert problem(fault_over={"detected_at_utc": iso(
        (started - timedelta(seconds=1)).astimezone(IST))}) is None
    later_in_utc_text = iso(started + timedelta(seconds=1))                 # "…T10:30:…+00:00"
    assert later_in_utc_text < iso(started.astimezone(IST))                 # text says earlier
    assert problem(fault_over={"detected_at_utc": later_in_utc_text})       # instant: later
    assert problem({"assessment_completed_at_utc": iso(started - timedelta(seconds=1))})
    assert problem(resolved=iso(completed - timedelta(seconds=1)))
    assert problem({"assessment_started_at_utc": iso(started.replace(tzinfo=None))})  # naive
    assert problem({"assessment_completed_at_utc": "not a time"})
    assert problem(drop="assessment_started_at_utc")
    assert problem(resolved=None)
    assert problem(fault_over={"detected_at_utc": "2026-10-15"})            # no zone: naive
    assert problem({"session_date": DAY.isoformat()})                       # a replayed day
    assert problem({"invocation": "reconstruction"})
    assert problem({"catch_up": True})
    assert problem({"timed_out": True})
    assert problem({"check": "midsession"})
    assert problem({"report_id": fault["detected_by_invocation"]})          # same invocation


# --- D. a present report's resolution: exact identity, path and hashes -------------
HASH_DAMAGE = ["verified-zero", "verified-missing", "verified-malformed", "verified-other",
               "report-id", "fault-id", "report-path", "report-path-missing", "content-hash",
               "substituted", "confirmation-mismatch"]


@pytest.mark.parametrize("damage", HASH_DAMAGE)
def test_a_resolution_must_agree_exactly_with_its_fault_report_and_confirmation(
        tmp_path, monkeypatch, damage):
    """Codex's finding: a zeroed verified_file_sha256 was accepted after a restart."""
    resolved_afterclose(tmp_path, monkeypatch)
    shelf = records(tmp_path)
    assert authority(tmp_path, shelf=shelf)["qualifying"] == [DAY.isoformat()]   # warm
    closing = resolution_file(tmp_path)
    verified = json.loads(closing.read_text())["verified_file_sha256"]
    change = {
        "verified-zero": lambda r: r.update(verified_file_sha256="0" * 64),
        "verified-missing": lambda r: r.pop("verified_file_sha256"),
        "verified-malformed": lambda r: r.update(verified_file_sha256=verified.upper()),
        "verified-other": lambda r: r.update(verified_file_sha256=OTHER_HASH),
        "report-id": lambda r: r.update(report_id="000000000000"),
        "fault-id": lambda r: r.update(fault_id="000000000000"),
        "report-path": lambda r: r.update(report_path=str(only(
            tmp_path, "reports").relative_to(tmp_path / "records"))),
        "report-path-missing": lambda r: r.pop("report_path"),
        "content-hash": lambda r: r.update(content_sha256=OTHER_HASH),
    }.get(damage)
    if change:
        resealed(closing, change)
    elif damage == "substituted":
        closing.chmod(0o640)
        closing.write_bytes(only(tmp_path, "confirmations", "afterclose").read_bytes())
    else:
        resealed(only(tmp_path, "confirmations", "afterclose"),
                 lambda r: r.update(file_sha256=OTHER_HASH))
    same = authority(tmp_path, shelf=shelf)
    assert same["open_faults"] == ["report-publication"] and same["qualifying"] == []
    assert same == authority(tmp_path)
    seen, _ = fresh(tmp_path, "print(json.dumps(T.authority(ROOT)))")
    assert seen == same


def test_an_exact_resolution_is_accepted_and_a_fault_without_a_file_hash_is_not_resolved(
        tmp_path, monkeypatch):
    cfg, fault = resolved_afterclose(tmp_path, monkeypatch)
    closing = fault["resolution"]
    path = tmp_path / "records" / fault["report"]["report_path"]
    assert closing["verified_file_sha256"] == store.file_sha256(path) == \
        fault["report"]["file_sha256"]
    assert closing["report_path"] == fault["report"]["report_path"]
    assert store.is_sha256(closing["verified_file_sha256"])
    # A fault that recorded no file hash has nothing to verify the bytes by.
    root = tmp_path / "b"
    cfg = unconfirmed_afterclose(root, monkeypatch)
    (fault_path,) = list((root / "records/faults").glob("*-fault.json"))
    resealed(fault_path, lambda r: r["report"].update(file_sha256=None))
    beat(root, cfg, at("16:20"))
    (fault,) = records(root).faults()
    assert not fault["resolved"] and not list((root / "records/faults").glob("*-resolution*"))


# ===========================================================================
# 0.4.5 — one notification deadline, from the first notification work
#
# Tests named `..._in_real_time` use real elapsed time and the real interval
# timer (alarm=True); tests named `..._on_a_simulated_clock` use a counting timer.
# ===========================================================================
def due_alerts(tmp_path):
    """Undelivered premarket and midsession failure alerts, both due at `when`."""
    cfg = config(tmp_path, hc=True)
    down = Pings(fail_first=10 ** 6)
    run("premarket", tmp_path, FakeProbe(revision="f" * 40), cfg=cfg, post=down)
    run("midsession", tmp_path, midday(revision="f" * 40), "12:30:00", cfg=cfg, post=down)
    _, first = last_attempt(tmp_path, "assessment_result", "premarket")
    _, second = last_attempt(tmp_path, "assessment_result", "midsession")
    return cfg, max(first, second) + timedelta(minutes=5)


class Counting:
    """A simulated monotonic timer: each reading is `step` seconds after the last."""

    def __init__(self, step=0.0):
        self.now, self.step = 0.0, step

    def __call__(self):
        self.now += self.step
        return self.now


def order_spy(monkeypatch, events, timer):
    """Record, in order, the start of the deadline and the notification work that follows."""
    real_start = cli.Outbound.start

    def start(self):
        if self.began is None:
            events.append("deadline")
        return real_start(self)
    monkeypatch.setattr(cli.Outbound, "start", start)
    for name, label in (("notify", "intent"), ("notification_states", "state"),
                        ("attention_context", "attention"), ("expose", "cache")):
        real = getattr(cli, name)

        def spy(*args, _real=real, _label=label, **kwargs):
            events.append(_label)
            return _real(*args, **kwargs)
        monkeypatch.setattr(cli, name, spy)
    real_send = cli.Outbound.send

    def send(self, check, *args, **kwargs):
        events.append(f"send:{check}")
        return real_send(self, check, *args, **kwargs)
    monkeypatch.setattr(cli.Outbound, "send", send)


# --- deadline origin ----------------------------------------------------------------
def test_an_assessment_starts_the_deadline_before_its_intent_on_a_simulated_clock(
        tmp_path, monkeypatch):
    events, timer = [], Counting()
    order_spy(monkeypatch, events, timer)
    run("premarket", tmp_path, FakeProbe(revision="f" * 40), cfg=config(tmp_path, hc=True),
        timer=timer)
    assert events.index("deadline") < events.index("intent") < events.index("state")
    assert events.index("deadline") < events.index("send:premarket")
    assert events.count("deadline") == 1                     # once, never restarted


def test_liveness_starts_the_deadline_before_the_heartbeat_on_a_simulated_clock(
        tmp_path, monkeypatch):
    cfg, when = due_alerts(tmp_path)
    events = []
    order_spy(monkeypatch, events, Counting())
    beat(tmp_path, cfg, when)
    assert events[0] == "deadline" and events[1] == "send:liveness"
    assert events.count("deadline") == 1
    assert events.index("state") < events.index("send:premarket")


@pytest.mark.parametrize("entry", ["delivery round", "notification-state cache"])
def test_whichever_notification_work_comes_first_starts_the_deadline_on_a_simulated_clock(
        tmp_path, monkeypatch, entry):
    cfg, when = due_alerts(tmp_path)
    events = []
    order_spy(monkeypatch, events, Counting())
    out = cli.Outbound(records(tmp_path), cfg, lambda: when, Pings(), timer=Counting(1.0))
    if entry == "delivery round":                            # attention and retry evaluation
        cli.deliver_round(records(tmp_path), out, when)
        assert events[:2] == ["deadline", "state"]
    else:                                                    # notification-state preparation
        cli.expose_or_fault(records(tmp_path), out, when, "liveness", [], "x", True)
        assert events[:2] == ["deadline", "cache"]
    assert out.began == 1.0                                  # read once, before the work


def test_the_deadline_is_never_moved_on_a_simulated_clock(tmp_path):
    cfg, when = due_alerts(tmp_path)
    timer = Counting(1.0)
    out = cli.Outbound(records(tmp_path), cfg, lambda: when, Pings(), timer=timer)
    out.start()
    began = out.began
    timer.now += 10.0                                        # lock waiting, accounting, ...
    out.start()
    with out.guard("outer"), out.guard("inner"):             # nested work: one deadline
        pass
    cli.deliver_round(records(tmp_path), out, when)          # retry eligibility, requests
    cli.expose_or_fault(records(tmp_path), out, when, "liveness", [], "x", True)
    assert out.began == began


# --- preparation spends the deadline, and is cut off by it --------------------------------
SLOW_PREPARATION = {
    "notification-state preparation": (cli, "notification_states"),
    "attention and retry evaluation": (cli, "attention_context"),
    "report validation": (store, "validate_report"),
    "confirmation validation": (Store, "_confirmation_valid"),
    "fault validation": (Store, "faults"),
    "file hashing": (store, "file_sha256"),
    "directory synchronization": (store, "sync_directory"),
    "outbox preparation": (cli, "current_body"),
}


def slow_down(monkeypatch, owner, name, seconds):
    """The first call of `owner.name` takes `seconds` of real time; the rest are normal."""
    import time as real_time
    real, calls = getattr(owner, name), {"n": 0}

    def slow(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            real_time.sleep(seconds)                         # interruptible by the timer
        return real(*args, **kwargs)
    monkeypatch.setattr(owner, name, slow)
    return calls


def test_fast_preparation_delivers_normally_in_real_time(tmp_path):
    cfg, when = due_alerts(tmp_path)
    pings = Pings()
    out = cli.Outbound(records(tmp_path), cfg, lambda: when, pings, alarm=True)
    cli.deliver_round(records(tmp_path), out, when)
    assert pings.kinds() == [("premarket", "fail"), ("midsession", "fail")]
    assert out.expiry is None and [t for _, _, t in pings.sent] == [5.0, 5.0]


@pytest.mark.parametrize("stage", list(SLOW_PREPARATION))
def test_slow_preparation_is_cut_off_at_the_deadline_in_real_time(tmp_path, monkeypatch,
                                                                 stage):
    import time as real_time
    cfg, when = due_alerts(tmp_path)
    if stage == "fault validation":                          # make sure there is one
        restore = break_housekeeping(monkeypatch)
        beat(tmp_path, cfg, when - timedelta(minutes=1), Pings(fail_first=10 ** 6))
        restore()
    reservations = files(tmp_path, "*-reservation.json")
    history = {p: p.read_bytes() for p in (tmp_path / "records/outbox").rglob("*")
               if p.is_file()}
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 2.0)
    owner, name = SLOW_PREPARATION[stage]
    calls = slow_down(monkeypatch, owner, name, 5.0)
    pings = Pings()
    out = cli.Outbound(records(tmp_path), cfg, lambda: when, pings, alarm=True)
    began = real_time.monotonic()
    cli.deliver_round(records(tmp_path), out, when)
    elapsed = real_time.monotonic() - began
    assert calls["n"] >= 1                                   # the slow step was reached
    assert elapsed < 2.6                                     # cut off, not 5 s later
    assert out.expiry is not None and pings.sent == []        # nothing after the deadline
    assert files(tmp_path, "*-reservation.json") == reservations
    assert all(p.read_bytes() == b for p, b in history.items())


def test_a_slow_reservation_write_sends_nothing_in_real_time(tmp_path, monkeypatch):
    cfg, when = due_alerts(tmp_path)
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 2.0)
    slow_down(monkeypatch, Store, "write_reservation", 5.0)
    pings = Pings()
    out = cli.Outbound(records(tmp_path), cfg, lambda: when, pings, alarm=True)
    cli.deliver_round(records(tmp_path), out, when)
    assert pings.sent == [] and out.expiry["during"] == "the premarket request"
    assert out.send("midsession", "fail", "x") is None       # no second identity


# --- after the deadline ------------------------------------------------------------------
class SlowTransport(Pings):
    """A transport whose first request takes `seconds` of real time."""

    def __init__(self, seconds, **kw):
        super().__init__(**kw)
        self.seconds = seconds

    def __call__(self, url, body, timeout):
        import time as real_time
        if not self.sent:
            self.sent.append((url, body.decode(), timeout))
            real_time.sleep(self.seconds)
            return self.status
        return super().__call__(url, body, timeout)


def test_a_request_cut_off_by_the_deadline_stays_unknown_and_reserved_in_real_time(
        tmp_path, monkeypatch, capsys):
    cfg, when = due_alerts(tmp_path)
    history = {p: p.read_bytes() for p in (tmp_path / "records/outbox").rglob("*")
               if p.is_file()}
    before = len(files(tmp_path, "*-reservation.json"))
    outcomes_before = files(tmp_path, "*-outcome.json")
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 2.0)
    pings = SlowTransport(5.0)
    out = cli.Outbound(records(tmp_path), cfg, lambda: when, pings, alarm=True)
    outcomes = cli.deliver_round(records(tmp_path), out, when)
    assert [c for c, _ in pings.kinds()] == ["premarket"]    # one attempt; none after it
    assert len(files(tmp_path, "*-reservation.json")) == before + 1   # reserved first
    (outcome,) = {o["reservation_id"]: o for o in outcomes.values()}.values()
    assert outcome["completion"] == "unknown" and outcome["delivered"] is False
    assert files(tmp_path, "*-outcome.json") == outcomes_before        # no outcome claimed
    assert all(p.read_bytes() == b for p, b in history.items())       # history unchanged
    note, attempted = last_attempt(tmp_path, "assessment_result", "premarket")
    assert note["attempts"][-1]["completion"] == "unknown"            # counted, conservatively
    state = {n["check"]: n for n in cli.notification_states(records(tmp_path), when,
                                                             tolerant=True)}
    assert state["premarket"]["delivery_state"] == cli.WAITING        # spacing from it
    monkeypatch.undo()
    _, again = beat(tmp_path, cfg, when + timedelta(minutes=4))
    assert ("premarket", "fail") not in again.kinds()                   # no duplicate
    _, later = beat(tmp_path, cfg, attempted + timedelta(minutes=5))
    assert ("premarket", "fail") in later.kinds()


def test_after_the_deadline_nothing_more_is_prepared_or_written_on_a_simulated_clock(
        tmp_path, monkeypatch):
    cfg, when = due_alerts(tmp_path)
    timer = Counting()
    out = cli.Outbound(records(tmp_path), cfg, lambda: when, Pings(), timer=timer)
    out.start()
    timer.now = 31.0                                         # the deadline has passed
    events = []
    order_spy(monkeypatch, events, timer)
    cache = tmp_path / "records/notification-state.json"
    before = cache.read_bytes() if cache.exists() else None
    reservations = files(tmp_path, "*-reservation.json")
    faults = []
    assert cli.deliver_round(records(tmp_path), out, when) == {}
    cli.expose_or_fault(records(tmp_path), out, when, "liveness", faults, "x", True)
    assert events == []                                      # no state, attention, cache
    assert files(tmp_path, "*-reservation.json") == reservations       # no reservation
    assert (cache.read_bytes() if cache.exists() else None) == before  # no rebuild
    assert faults == [] and records(tmp_path).faults() == []          # not a fault
    assert intents(tmp_path, "observer_fault") == []                  # no recursive round


def test_an_expired_invocation_says_so_locally_and_only_locally_in_real_time(
        tmp_path, monkeypatch, capsys):
    cfg, when = due_alerts(tmp_path)
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 2.0)
    slow_down(monkeypatch, cli, "notification_states", 5.0)
    capsys.readouterr()
    code = cli.liveness(cfg, records(tmp_path), lambda: when, Pings(), alarm=True)
    printed = capsys.readouterr().out
    assert "NOTIFICATION DEADLINE" in printed and "the delivery round" in printed
    for leaked in [str(tmp_path), *PING_IDS.values()]:
        assert leaked not in printed                         # sanitized
    assert records(tmp_path).faults() == [] and code == 0    # not a fault; heartbeat went
    assert intents(tmp_path, "observer_fault") == []


def test_an_interrupted_reconciliation_resolves_nothing_in_real_time(tmp_path, monkeypatch):
    cfg, when = due_alerts(tmp_path)
    real = cli.deliver_round
    monkeypatch.setattr(cli, "deliver_round", boom(RuntimeError("round broke")))
    beat(tmp_path, cfg, when - timedelta(minutes=2))         # a notification-round fault
    monkeypatch.setattr(cli, "deliver_round", real)
    (fault,) = records(tmp_path).faults()
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 2.0)
    slow_down(monkeypatch, cli, "resolve_faults", 5.0)
    cli.liveness(cfg, records(tmp_path), lambda: when, Pings(), alarm=True)
    monkeypatch.undo()
    (fault,) = records(tmp_path).faults()
    assert not fault["resolved"]                             # no success from a partial round
    assert not list((tmp_path / "records/faults").glob("*-resolution.json"))


def test_an_interrupted_validation_leaves_nothing_verified_in_real_time(tmp_path, monkeypatch):
    cfg, when = due_alerts(tmp_path)
    shelf = records(tmp_path)
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 2.0)
    slow_down(monkeypatch, store, "sync_chain", 5.0)         # inside the first verification
    out = cli.Outbound(shelf, cfg, lambda: when, Pings(), alarm=True)
    cli.deliver_round(shelf, out, when)
    assert out.expiry is not None
    assert shelf._failed_confirmations == set()             # no verdict of any kind kept
    assert not hasattr(shelf, "_confirmed")                  # and no positive cache exists
    monkeypatch.undo()
    assert len(shelf.reports()) == 2                         # verified afresh, from the files


# --- what is unchanged -----------------------------------------------------------------
def test_the_limits_and_the_other_deadlines_are_unchanged():
    assert cli.NOTIFY_BUDGET_SECONDS == 30.0 and cli.MAX_REQUESTS == 4
    assert healthchecks.REQUEST_TIMEOUT == 5
    assert cli.INITIAL_ROUNDS == 6 and cli.INITIAL_GAP == timedelta(minutes=5)
    assert cli.LATER_GAP == timedelta(hours=1)
    assert cli.DEFAULTS["late_run_seconds"] == 180


def test_the_assessment_is_recorded_whatever_the_notification_deadline_on_a_simulated_clock(
        tmp_path, monkeypatch):
    """Expiry stops notification work only: the report, its confirmation and the accounting
    are the same as an undisturbed run's."""
    control, _ = run("premarket", tmp_path / "a", cfg=config(tmp_path / "a", hc=True))
    timer = Counting()

    class Expired(cli.Outbound):
        def start(self):
            super().start()
            timer.now = self.began + 31.0                    # spent the moment it starts
    monkeypatch.setattr(cli, "Outbound", Expired)
    pings = Pings()
    report, _ = run("premarket", tmp_path / "b", cfg=config(tmp_path / "b", hc=True),
                    post=pings, timer=timer)
    assert pings.sent == []                                  # no notification work at all
    assert cli.check_status(records(tmp_path / "b"), DAY.isoformat(), "premarket") == "PASS"
    assert len(records(tmp_path / "b").reports()) == 1       # published and confirmed
    for key in ("result", "timely", "ran_checks", "checks", "failure_reasons"):
        assert report[key] == control[key], key
    assert records(tmp_path / "b").faults() == []


# --- Codex's two reproductions, as tests ------------------------------------------------
def codex_round(tmp_path, monkeypatch, started_first):
    """Two seconds of budget; every evidence check takes 0.7 s of real time."""
    import time as real_time
    cfg, when = due_alerts(tmp_path)
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 2.0)
    pings = Pings()
    out = cli.Outbound(records(tmp_path), cfg, lambda: when, pings, alarm=True)
    began = real_time.monotonic()
    if started_first:
        out.send("liveness", "success", "check=liveness")   # the deadline has started
    real = store.sync_chain

    def slow(path, root):
        real_time.sleep(0.7)
        return real(path, root)
    monkeypatch.setattr(store, "sync_chain", slow)
    cli.deliver_round(records(tmp_path), out, when)
    return real_time.monotonic() - began, out, pings


def test_codex_preparation_before_the_first_send_is_cut_off_in_real_time(tmp_path,
                                                                        monkeypatch):
    """0.4.4: 2.8 s of preparation, then a request with a fresh 2-second timeout."""
    elapsed, out, pings = codex_round(tmp_path, monkeypatch, started_first=False)
    assert elapsed < 2.6 and out.expiry["during"] == "the delivery round"
    assert pings.sent == []


def test_codex_preparation_after_the_deadline_started_is_cut_off_in_real_time(tmp_path,
                                                                             monkeypatch):
    """0.4.4: the heartbeat started the deadline; preparation then ran on, uninterrupted."""
    elapsed, out, pings = codex_round(tmp_path, monkeypatch, started_first=True)
    assert elapsed < 2.6 and out.expiry["during"] == "the delivery round"
    assert pings.kinds() == [("liveness", "success")]


# ===========================================================================
# 0.4.6 — durable outcomes; one notification context; expiry stops all of it;
# no timer is put over another
# ===========================================================================
def undurable_outcomes(monkeypatch, stage):
    """Make publishing an outcome (notification or heartbeat) fail at `stage`.

    "before the final link": nothing is linked. "directory synchronization": the
    final name is linked and syncing its directory fails. "after the final link":
    the publish completes, and syncing it and the directories above it fails.
    Reservations and every other record are written normally.
    """
    real_publish, real_sync, real_chain = store.publish, store.sync_directory, store.sync_chain
    real_link = store.os.link
    state = {"outcome": False}

    def publish(path, record, mode=0o440):
        state["outcome"] = path.name.endswith("-outcome.json")
        try:
            return real_publish(path, record, mode)
        finally:
            state["outcome"] = False

    def link(src, dst, *a, **k):
        if stage == "before the final link" and state["outcome"]:
            raise OSError(5, "Input/output error")
        return real_link(src, dst, *a, **k)

    def sync_directory(directory):
        if stage == "directory synchronization" and state["outcome"]:
            raise OSError(5, "Input/output error")
        return real_sync(directory)

    def sync_chain(path, root):
        if stage == "after the final link" and path.name.endswith("-outcome.json"):
            raise OSError(5, "Input/output error")
        return real_chain(path, root)
    monkeypatch.setattr(store, "publish", publish)
    monkeypatch.setattr(store.os, "link", link)
    monkeypatch.setattr(store, "sync_directory", sync_directory)
    monkeypatch.setattr(store, "sync_chain", sync_chain)


def no_sync(monkeypatch):
    """Synchronization unavailable: every fsync fails."""
    def fsync(fd):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(store.os, "fsync", fsync)


def premarket_outcome(root):
    return sorted((Path(root) / NOTES).glob("*-outcome.json"))


OUTCOME_STAGES = ["before the final link", "directory synchronization", "after the final link"]


@pytest.mark.parametrize("stage", OUTCOME_STAGES)
def test_an_undurable_notification_outcome_is_never_taken_as_delivered(tmp_path, monkeypatch,
                                                                        stage):
    cfg, when = due_alerts(tmp_path)
    before = premarket_outcome(tmp_path)
    reservations = files(tmp_path, "*-reservation.json")
    undurable_outcomes(monkeypatch, stage)
    shelf, pings = records(tmp_path), Pings()
    out = cli.Outbound(shelf, cfg, lambda: when, pings)
    with pytest.raises(cli.StorageUnavailable) as stopped:
        cli.deliver_round(shelf, out, when)
    assert pings.kinds() == [("premarket", "fail")]                   # the request was made
    attempt = stopped.value.attempt
    assert attempt["completion"] == "unknown" and attempt["delivered"] is False
    assert attempt["receipt"] == healthchecks.UNDELIVERED
    linked = premarket_outcome(tmp_path)
    assert len(linked) == len(before) + (stage != "before the final link")
    assert len(files(tmp_path, "*-reservation.json")) == len(reservations) + 1   # retained
    monkeypatch.undo()                                       # synchronization works again
    for reader in (shelf, ):                                 # the same Store: never trusted
        (note,) = [n for n in reader.notifications() if n["check"] == "premarket"]
        assert note["delivered"] is False and note["attempts"][-1]["completion"] == "unknown"
        assert note["attempts"][-1]["receipt"] == healthchecks.UNDELIVERED
    if stage != "before the final link":
        assert "failed in this process" in note["attempts"][-1]["outcome_unverified"]
        assert "completion unknown" in note["attempts"][-1]["result"]
    state = {n["check"]: n for n in cli.notification_states(shelf, when, tolerant=True)}
    assert state["premarket"]["outstanding"] and state["premarket"]["last_completion"] == "unknown"


@pytest.mark.parametrize("stage", OUTCOME_STAGES[1:])
def test_a_new_store_trusts_a_surviving_outcome_only_once_it_syncs(tmp_path, monkeypatch, stage):
    cfg, when = due_alerts(tmp_path)
    undurable_outcomes(monkeypatch, stage)
    shelf = records(tmp_path)
    with pytest.raises(cli.StorageUnavailable):
        cli.deliver_round(shelf, cli.Outbound(shelf, cfg, lambda: when, Pings()), when)
    monkeypatch.undo()
    with monkeypatch.context() as m:                         # a new reader; sync unavailable
        no_sync(m)
        fresh = records(tmp_path)
        (note,) = [n for n in fresh.notifications() if n["check"] == "premarket"]
        assert note["delivered"] is False and note["attempts"][-1]["completion"] == "unknown"
        assert "could not be synced" in note["attempts"][-1]["outcome_unverified"]
        assert any("could not be synced" in why for why in fresh.unverified_outcomes.values())
    recovered = records(tmp_path)                            # a later reader that can sync
    (note,) = [n for n in recovered.notifications() if n["check"] == "premarket"]
    assert note["delivered"] is True and note["attempts"][-1]["completion"] == "recorded"
    assert recovered.unverified_outcomes == {}
    (note,) = [n for n in shelf.notifications() if n["check"] == "premarket"]
    assert note["delivered"] is False                        # the writer's own verdict stands


FRESH_READER = """
import json, os, sys
sys.path.insert(0, {observer!r})
from quantdesk_observer import store
if {broken}:
    def fsync(fd):
        raise OSError(5, "Input/output error")
    os.fsync = fsync
shelf = store.Store({root!r})
if {heartbeat}:
    print(json.dumps([[h["outcome"] is not None and h["outcome"]["delivered"],
                       h.get("outcome_unverified")] for h in shelf.heartbeats({day!r})]))
else:
    print(json.dumps([[n["delivered"], n["attempts"][-1]["completion"]]
                      for n in shelf.notifications() if n["check"] == "premarket"]))
"""


def fresh_process(root, broken, heartbeat=False, day=""):
    """Read the records in a new Python process; nothing but the files carries over."""
    source = FRESH_READER.format(observer=str(OBSERVER), root=str(Path(root) / "records"),
                                 broken=broken, heartbeat=heartbeat, day=day)
    done = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_a_restarted_process_does_not_trust_an_undurable_notification_outcome(tmp_path,
                                                                               monkeypatch):
    cfg, when = due_alerts(tmp_path)
    undurable_outcomes(monkeypatch, "directory synchronization")
    shelf = records(tmp_path)
    with pytest.raises(cli.StorageUnavailable):
        cli.deliver_round(shelf, cli.Outbound(shelf, cfg, lambda: when, Pings()), when)
    monkeypatch.undo()
    assert fresh_process(tmp_path, broken=True) == [[False, "unknown"]]    # cannot sync: no
    assert fresh_process(tmp_path, broken=False) == [[True, "recorded"]]   # verified and synced


def test_a_restarted_process_does_not_trust_an_undurable_heartbeat_outcome(tmp_path,
                                                                            monkeypatch, capsys):
    cfg = config(tmp_path, hc=True)
    when = at("17:00")
    day = when.astimezone(UTC).date().isoformat()
    undurable_outcomes(monkeypatch, "directory synchronization")
    shelf = records(tmp_path)
    code = cli.liveness(cfg, shelf, lambda: when, Pings())
    printed = capsys.readouterr().out
    assert code == 1 and "STORAGE UNAVAILABLE" in printed
    assert f"heartbeat: {healthchecks.UNDELIVERED}" in printed      # not "not sent"
    assert "heartbeat delivered" not in printed
    monkeypatch.undo()
    (beat_,) = shelf.heartbeats(day)                         # the same Store: never trusted
    assert beat_["outcome"] is None and "failed in this process" in beat_["outcome_unverified"]
    (reloaded,) = fresh_process(tmp_path, broken=True, heartbeat=True, day=day)
    assert reloaded[0] is False and "could not be synced" in reloaded[1]
    assert fresh_process(tmp_path, broken=False, heartbeat=True, day=day) == [[True, None]]


def test_an_outcome_interrupted_by_the_deadline_in_real_time_is_not_trusted(tmp_path,
                                                                             monkeypatch):
    """Codex's reproduction: the outcome's final link exists, its directory sync is cut off."""
    import time as real_time
    cfg, when = due_alerts(tmp_path)
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 2.0)
    real_publish, real_sync = store.publish, store.sync_directory
    state = {"outcome": False}

    def publish(path, record, mode=0o440):
        state["outcome"] = path.name.endswith("-outcome.json")
        try:
            return real_publish(path, record, mode)
        finally:
            state["outcome"] = False

    def sync_directory(directory):
        if state["outcome"]:
            real_time.sleep(5.0)                             # interrupted here by the timer
        return real_sync(directory)
    monkeypatch.setattr(store, "publish", publish)
    monkeypatch.setattr(store, "sync_directory", sync_directory)
    before = len(premarket_outcome(tmp_path))
    shelf, pings = records(tmp_path), Pings()
    out = cli.Outbound(shelf, cfg, lambda: when, pings, alarm=True)
    outcomes = cli.deliver_round(shelf, out, when)
    assert pings.kinds() == [("premarket", "fail")] and out.expiry is not None
    assert len(premarket_outcome(tmp_path)) == before + 1    # readable, linked ...
    assert {o["completion"] for o in outcomes.values()} == {"unknown"}
    monkeypatch.undo()
    (note,) = [n for n in shelf.notifications() if n["check"] == "premarket"]
    assert note["delivered"] is False                        # ... and not trusted by its writer
    assert fresh_process(tmp_path, broken=True) == [[False, "unknown"]]


MUTATIONS = {
    "missing": None,
    "corrupt": lambda r: {**r, "result": "altered after sealing"},
    "another reservation": lambda r: store.seal({**r, "reservation_id": "f" * 12}),
    "another identity": lambda r: store.seal({**r, "check": "midsession"}),
    "another kind": lambda r: store.seal({**r, "kind": "success"}),
    "another reservation time": lambda r: store.seal(
        {**r, "reserved_at_utc": "2026-10-14T00:00:00+00:00"}),
    "contradictory delivery evidence": lambda r: store.seal(
        {**r, "delivery_state": "undelivered"}),
}


@pytest.mark.parametrize("mutation", list(MUTATIONS))
def test_a_missing_corrupt_or_substituted_outcome_claims_no_delivery(tmp_path, mutation):
    cfg, when = due_alerts(tmp_path)
    shelf = records(tmp_path)
    cli.deliver_round(shelf, cli.Outbound(shelf, cfg, lambda: when, Pings()), when)
    (note,) = [n for n in records(tmp_path).notifications() if n["check"] == "premarket"]
    assert note["delivered"]                                 # the genuine outcome counts
    path = Path(tmp_path) / NOTES / note["attempts"][-1]["record_file"].replace(
        "-reservation.json", "-outcome.json")
    record = json.loads(path.read_text())
    path.chmod(0o640)
    if MUTATIONS[mutation] is None:
        path.unlink()
    else:
        path.write_text(json.dumps(MUTATIONS[mutation](record)))
    notes, problems = records(tmp_path).notifications(tolerant=True)
    (note,) = [n for n in notes if n["check"] == "premarket"]
    assert note["delivered"] is False                        # never a completed delivery
    if mutation == "missing":
        assert problems == [] and note["attempts"][-1]["completion"] == "unknown"
    else:
        assert problems and note["unresolved"]               # damaged: shown, never resent
        with pytest.raises(CorruptRecord):
            records(tmp_path).notifications()
    assert files(tmp_path, "*-reservation.json")             # the reservation stays


def test_a_valid_outcome_for_its_own_reservation_is_trusted(tmp_path):
    cfg, when = due_alerts(tmp_path)
    shelf = records(tmp_path)
    cli.deliver_round(shelf, cli.Outbound(shelf, cfg, lambda: when, Pings()), when)
    for reader in (shelf, records(tmp_path)):
        (note,) = [n for n in reader.notifications() if n["check"] == "premarket"]
        assert note["delivered"] and note["attempts"][-1]["completion"] == "recorded"
        assert note["attempts"][-1]["outcome_unverified"] is None
    assert shelf.unverified_outcomes == {} and not hasattr(shelf, "_trusted_outcomes")


def test_an_undurable_outcome_keeps_the_retry_spacing_and_sends_no_early_duplicate(
        tmp_path, monkeypatch):
    cfg, when = due_alerts(tmp_path)
    path = cfg_file(tmp_path, cfg)
    undurable_outcomes(monkeypatch, "after the final link")
    shelf = records(tmp_path)
    with pytest.raises(cli.StorageUnavailable):
        cli.deliver_round(shelf, cli.Outbound(shelf, cfg, lambda: when, Pings()), when)
    with monkeypatch.context() as m:                         # restarted, sync still failing
        no_sync(m)
        state = {n["check"]: n for n in cli.notification_states(records(tmp_path),
                                                                 when + timedelta(minutes=4),
                                                                 tolerant=True)}
        assert state["premarket"]["delivery_state"] == cli.WAITING   # spaced from the round
    monkeypatch.undo()
    # Once it syncs, the surviving outcome is the record: delivered, nothing more is sent.
    assert ("premarket", "fail") not in process(path, when + timedelta(minutes=5), Pings())[1]


# --- one context, one deadline, from the first notification work --------------------------
class ExpiredAtOnce(cli.Outbound):
    """A notification context whose deadline has passed the moment it starts."""

    def start(self):
        super().start()
        self.timer = lambda: self.began + 31.0


def context_spy(monkeypatch):
    """Every Outbound made, and every deadline reading taken at a guard's entry."""
    made, seen = [], []
    real_init, real_guard = cli.Outbound.__init__, cli.Outbound.guard

    def init(self, *a, **k):
        made.append(self)
        real_init(self, *a, **k)

    def guard(self, during):
        seen.append((id(self), during))
        return real_guard(self, during)
    monkeypatch.setattr(cli.Outbound, "__init__", init)
    monkeypatch.setattr(cli.Outbound, "guard", guard)
    return made, seen


def first_work_spy(monkeypatch, names):
    """Record, in order, the deadline's start and the named notification functions."""
    events = []
    real_start = cli.Outbound.start

    def start(self):
        if self.began is None:
            events.append("deadline")
        return real_start(self)
    monkeypatch.setattr(cli.Outbound, "start", start)
    for name in names:
        real = getattr(cli, name)

        def spy(*args, _real=real, _name=name, **kwargs):
            events.append(_name)
            return _real(*args, **kwargs)
        monkeypatch.setattr(cli, name, spy)
    return events


def test_a_checkpoint_alert_intent_is_the_first_notification_work_on_a_simulated_clock(
        tmp_path, monkeypatch):
    cfg = config(tmp_path, hc=True)
    run("premarket", tmp_path, cfg=cfg)
    run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg)
    events = first_work_spy(monkeypatch, ["ensure_attention_intent", "fault_intent", "notify",
                                          "deliver_round"])
    made, _ = context_spy(monkeypatch)
    run("afterclose", tmp_path, trigger_probe(), "16:00:00", cfg=cfg)
    assert events[:2] == ["deadline", "ensure_attention_intent"]       # immediately before it
    assert events.count("deadline") == 1 and len(made) == 1
    assert records(tmp_path).checkpoint() and intents(tmp_path, "review_attention")


def test_a_fault_alert_intent_is_the_first_notification_work_on_a_simulated_clock(
        tmp_path, monkeypatch):
    cfg = config(tmp_path, hc=True)
    break_housekeeping(monkeypatch)
    events = first_work_spy(monkeypatch, ["ensure_attention_intent", "fault_intent", "notify",
                                          "deliver_round"])
    made, _ = context_spy(monkeypatch)
    run("premarket", tmp_path, cfg=cfg)
    assert events[:2] == ["deadline", "fault_intent"]
    assert events.count("deadline") == 1 and len(made) == 1
    (fault,) = records(tmp_path).faults()
    assert intents(tmp_path, "observer_fault")[0]["fault_ids"] == [fault["fault_id"]]


def test_slow_housekeeping_notification_work_spends_the_assessments_deadline_in_real_time(
        tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 2.0)
    slow_down(monkeypatch, cli, "prepare_notifications", 1.5)
    pings = Pings()
    report, _ = run("premarket", tmp_path, FakeProbe(revision="f" * 40),
                    cfg=config(tmp_path, hc=True), post=pings, alarm=True)
    assert pings.sent == []                                  # under a second was left
    assert report["result"] == "FAIL" and len(records(tmp_path).reports()) == 1
    (note,) = intents(tmp_path, "assessment_result")         # stored, kept for a later round
    assert note["attempts"] == []


def test_slow_checkpoint_intent_work_is_cut_off_and_the_accounting_still_completes_in_real_time(
        tmp_path, monkeypatch):
    import time as real_time
    cfg = config(tmp_path, hc=True)
    run("premarket", tmp_path, cfg=cfg)
    run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg)
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 2.0)
    slow_down(monkeypatch, cli, "ensure_attention_intent", 5.0)
    pings = Pings()
    began = real_time.monotonic()
    report, _ = run("afterclose", tmp_path, trigger_probe(), "16:00:00", cfg=cfg, post=pings,
                    alarm=True)
    assert real_time.monotonic() - began < 4.5               # cut off, not 5 s later
    assert pings.sent == [] and intents(tmp_path, "review_attention") == []
    # The accounting is complete and exactly as an undisturbed run's.
    assert report["outcome"] == "QUALIFIED" and report["attention_required"] is True
    assert len(records(tmp_path).reports(check="afterclose")) == 1
    assert records(tmp_path).checkpoint()["reasons"] == ["EARLY_REVIEW"]
    assert json.loads((tmp_path / "records/state.json").read_text())["phase"] == "REVIEW PENDING"
    assert records(tmp_path).faults() == []
    monkeypatch.undo()
    code, later = beat(tmp_path, cfg, at("16:05:00"))         # recovered from the records
    assert code == 0 and ("afterclose", "fail") in later.kinds()
    (note,) = intents(tmp_path, "review_attention")
    assert note["delivered"]


def test_one_context_and_one_deadline_serve_every_path_on_a_simulated_clock(tmp_path,
                                                                            monkeypatch):
    cfg = config(tmp_path, hc=True)
    failing_cache(monkeypatch)                               # the failure path, too
    break_housekeeping(monkeypatch)
    made, seen = context_spy(monkeypatch)
    timer = Counting(0.01)
    run("premarket", tmp_path, FakeProbe(revision="f" * 40), cfg=cfg,
        post=Pings(fail_first=10 ** 6), timer=timer)
    assert len(made) == 1 and {who for who, _ in seen} == {id(made[0])}
    during = [d for _, d in seen]
    assert "the fault alert intent" in during and "the notification-state cache" in during
    assert made[0].began is not None
    began = made[0].began
    beats = []
    real_check = cli.Outbound.check

    def check(self, during):
        beats.append(self.began)
        return real_check(self, during)
    monkeypatch.setattr(cli.Outbound, "check", check)
    made.clear()
    cli.liveness(cfg, records(tmp_path), lambda: at("08:45"), Pings(), timer=timer)
    assert len(made) == 1 and len(set(beats)) == 1           # started once, never reset
    assert began != beats[0]                                 # each invocation its own


def test_the_deadline_runs_on_through_accounting_on_a_simulated_clock(tmp_path, monkeypatch):
    """Started at the first notification work, it is not paused for the accounting after it."""
    cfg = config(tmp_path, hc=True)
    timer = Counting()
    real = cli.settle_publication

    def slow_accounting(*args, **kwargs):
        timer.now += 29.5                                    # accounting takes 29.5 s
        return real(*args, **kwargs)
    monkeypatch.setattr(cli, "settle_publication", slow_accounting)
    pings = Pings()
    report, _ = run("premarket", tmp_path, FakeProbe(revision="f" * 40), cfg=cfg, post=pings,
                    timer=timer)
    assert pings.sent == []                                  # half a second left: no request
    assert len(records(tmp_path).reports()) == 1 and report["result"] == "FAIL"


def test_accounting_continues_after_notification_expiry_and_nothing_is_fabricated(
        tmp_path, monkeypatch):
    control = early_review_day(tmp_path / "a", config(tmp_path / "a", hc=True), Pings())
    monkeypatch.setattr(cli, "Outbound", ExpiredAtOnce)
    root = tmp_path / "b"
    cfg = config(root, hc=True)
    pings = Pings()
    report = early_review_day(root, cfg, pings)
    assert pings.sent == []
    for key in ("result", "outcome", "timely", "ran_checks", "collection_gates_passed",
                "valid_early_review_candidates", "attention_required", "bounded_phase"):
        assert report[key] == control[key], key
    assert records(root).checkpoint()["reasons"] == ["EARLY_REVIEW"]
    assert len(records(root).reports()) == 3 and records(root).faults() == []
    assert intents(root) == []                               # nothing prepared past it
    assert phase(root, cfg)["qualifying_sessions"] == phase(tmp_path / "a")["qualifying_sessions"]


def test_a_deferred_assessment_failure_alert_is_stored_and_sent_later(tmp_path, monkeypatch):
    cfg = config(tmp_path, hc=True)
    with monkeypatch.context() as m:
        m.setattr(cli, "Outbound", ExpiredAtOnce)
        report, _ = run("premarket", tmp_path, FakeProbe(revision="f" * 40), cfg=cfg)
    assert report["result"] == "FAIL" and report["notification_planned"] is True
    assert intents(tmp_path) == []
    code, later = beat(tmp_path, cfg, at("08:50"))
    assert ("premarket", "fail") in later.kinds()
    (note,) = intents(tmp_path, "assessment_result")
    assert note["notification_id"] == f"assessment-{report['report_id']}" and note["delivered"]
    beat(tmp_path, cfg, at("08:55"))
    assert len(intents(tmp_path, "assessment_result")) == 1  # stored once


def test_a_deferred_pass_is_not_sent_late(tmp_path, monkeypatch):
    cfg = config(tmp_path, hc=True)
    with monkeypatch.context() as m:
        m.setattr(cli, "Outbound", ExpiredAtOnce)
        run("premarket", tmp_path, cfg=cfg)
    _, later = beat(tmp_path, cfg, at("08:50"))
    assert later.kinds() == [("liveness", "success")] and intents(tmp_path) == []


def test_a_deferred_fault_alert_is_stored_and_sent_by_a_later_invocation(tmp_path, monkeypatch):
    cfg = config(tmp_path, hc=True)
    with monkeypatch.context() as m:
        m.setattr(cli, "Outbound", ExpiredAtOnce)
        m.setattr(Store, "write_report", boom(OSError(28, "No space left")))
        run("premarket", tmp_path, cfg=cfg)
    (fault,) = records(tmp_path).faults()
    assert fault["report"]["state"] == "ABSENT" and intents(tmp_path) == []
    code, later = beat(tmp_path, cfg, at("08:50"))
    assert ("afterclose", "fail") in later.kinds()
    (note,) = intents(tmp_path, "observer_fault")
    assert note["notification_id"] == f"fault-{fault['fault_id']}" and note["delivered"]


# --- notification-failure handling is notification work ------------------------------------
NOTIFICATION_FAILURE_WORK = {
    "fault recording": (Store, "write_fault"),
    "fault event": (cli, "raise_event"),
    "notification-state fault resolution": (cli, "resolve_faults"),
}


@pytest.mark.parametrize("work", list(NOTIFICATION_FAILURE_WORK))
def test_slow_notification_failure_handling_stops_at_the_deadline_in_real_time(
        tmp_path, monkeypatch, work):
    import time as real_time
    cfg, when = due_alerts(tmp_path)
    if work == "notification-state fault resolution":
        with monkeypatch.context() as m:                     # an earlier cache fault
            failing_cache(m)
            beat(tmp_path, cfg, when - timedelta(minutes=1))
        assert len(records(tmp_path).faults()) == 1
    else:
        failing_cache(monkeypatch)
    faults_before = [f["fault_id"] for f in records(tmp_path).faults()]
    intents_before = len(intents(tmp_path))
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 2.0)
    owner, name = NOTIFICATION_FAILURE_WORK[work]
    slow_down(monkeypatch, owner, name, 5.0)
    out = cli.Outbound(records(tmp_path), cfg, lambda: when, Pings(), alarm=True)
    began = real_time.monotonic()
    cli.expose_or_fault(records(tmp_path), out, when, "liveness", [], "later", True)
    assert real_time.monotonic() - began < 2.6
    assert out.expiry is not None
    assert [f["fault_id"] for f in records(tmp_path).faults()] == faults_before
    assert not any(f["resolved"] for f in records(tmp_path).faults())
    assert len(intents(tmp_path)) == intents_before          # no new intent, no new round


def test_a_slow_notification_round_resolution_stops_at_the_deadline_in_real_time(
        tmp_path, monkeypatch):
    import time as real_time
    cfg, when = due_alerts(tmp_path)
    with monkeypatch.context() as m:                         # an earlier notification-round fault
        m.setattr(cli, "deliver_round", boom(RuntimeError("round broke")))
        beat(tmp_path, cfg, when - timedelta(minutes=2))
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 2.0)
    real = cli.resolve_faults
    seen = []

    def slow(store_, now, check, kind, invocation):
        seen.append(kind)
        if kind == "notification-round":                     # the notification work only
            real_time.sleep(5.0)
        return real(store_, now, check, kind, invocation)
    monkeypatch.setattr(cli, "resolve_faults", slow)
    began = real_time.monotonic()
    cli.liveness(cfg, records(tmp_path), lambda: when, Pings(), alarm=True)
    assert real_time.monotonic() - began < 2.6
    assert "notification-round" in seen
    (fault,) = records(tmp_path).faults()
    assert not fault["resolved"]


def test_no_notification_failure_work_is_done_after_expiry_on_a_simulated_clock(
        tmp_path, monkeypatch, capsys):
    cfg, when = due_alerts(tmp_path)
    failing_cache(monkeypatch)
    timer = Counting()
    out = cli.Outbound(records(tmp_path), cfg, lambda: when, Pings(), timer=timer)
    out.start()
    timer.now = 31.0
    events = first_work_spy(monkeypatch, ["fault_intent", "resolve_faults", "expose",
                                          "notification_states", "deliver_round", "raise_event"])
    faults = []
    cli.expose_or_fault(records(tmp_path), out, when, "liveness", faults, "x", True)
    cli.notification_failure(records(tmp_path), out, when, "liveness",
                             RuntimeError(f"token=abc {tmp_path}"), faults, "x")
    assert [e for e in events if e != "deadline"] == []      # nothing at all
    assert records(tmp_path).faults() == [] and intents(tmp_path, "observer_fault") == []
    (line,) = faults
    assert "not recorded" in line and str(tmp_path) not in line and "abc" not in line
    cli.report_expiry(out, "liveness", faults)
    printed = capsys.readouterr().out
    assert len(printed.splitlines()) <= 1 + len(out.withheld)
    assert str(tmp_path) not in printed and not any(u in printed for u in PING_IDS.values())


# --- expiry stops all enclosing notification work ------------------------------------------
def test_expiry_during_a_request_stops_the_enclosing_round_in_real_time(tmp_path, monkeypatch):
    cfg, when = due_alerts(tmp_path)
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 2.0)
    before = len(files(tmp_path, "*-reservation.json"))
    bodies = []
    real_body = cli.current_body

    def body(note, context):
        bodies.append(note["check"])
        return real_body(note, context)
    monkeypatch.setattr(cli, "current_body", body)
    pings = SlowTransport(5.0)
    shelf = records(tmp_path)
    out = cli.Outbound(shelf, cfg, lambda: when, pings, alarm=True)
    with pytest.raises(cli.NotificationExpired) as stopped:
        with out.guard("an enclosing invocation's notification work"):
            cli.deliver_round(shelf, out, when)
            raise AssertionError("the enclosing work went on after the deadline")
    assert stopped.value.attempt["completion"] == "unknown"
    assert stopped.value.attempt["receipt"] == healthchecks.UNDELIVERED
    assert bodies == ["premarket"]                           # the midsession alert: not prepared
    assert [c for c, _ in pings.kinds()] == ["premarket"]    # no request after it
    assert len(files(tmp_path, "*-reservation.json")) == before + 1    # reserved, kept
    note, _ = last_attempt(tmp_path, "assessment_result", "premarket")
    assert note["attempts"][-1]["completion"] == "unknown"
    assert out.send("midsession", "fail", "x") is None       # outermost: nothing more


def test_an_assessment_whose_request_is_cut_off_records_no_failure_event_in_real_time(
        tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 2.0)
    bodies = []
    real_emit = cli.emit

    def emit(store_, report, kind, subject, summary):
        bodies.append(subject)
        return real_emit(store_, report, kind, subject, summary)
    monkeypatch.setattr(cli, "emit", emit)
    pings = SlowTransport(5.0)
    report, _ = run("premarket", tmp_path, FakeProbe(revision="f" * 40),
                    cfg=config(tmp_path, hc=True), post=pings, alarm=True)
    assert "premarket-notification" not in bodies           # no post-expiry failure event
    assert ("operational_failure", "premarket-notification") not in events(tmp_path)
    assert len(pings.sent) == 1 and len(records(tmp_path).reports()) == 1
    (note,) = intents(tmp_path, "assessment_result")
    assert note["attempts"][-1]["completion"] == "unknown"
    assert records(tmp_path).faults() == []


def test_the_outermost_request_still_returns_its_unknown_attempt_in_real_time(tmp_path,
                                                                              monkeypatch):
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 2.0)
    out = cli.Outbound(records(tmp_path), config(tmp_path, hc=True), lambda: at("17:00"),
                       SlowTransport(5.0), alarm=True)
    attempt = out.send("liveness", "success", "x")
    assert attempt["completion"] == "unknown" and attempt["reservation_id"]


# --- timers: one per context; another one already running is left alone ---------------------
@pytest.fixture
def clean_timer():
    previous = signal.getsignal(signal.SIGALRM)
    yield
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, previous)


def test_nested_guards_of_one_context_reuse_its_timer(tmp_path, monkeypatch, clean_timer):
    calls = []
    real = signal.setitimer

    def setitimer(which, seconds, *a):
        calls.append(seconds)
        return real(which, seconds, *a)
    monkeypatch.setattr(cli.signal, "setitimer", setitimer)
    out = cli.Outbound(records(tmp_path), config(tmp_path, hc=True), lambda: at("17:00"),
                       Pings(), alarm=True)
    with out.guard("outer"):
        armed = signal.getitimer(signal.ITIMER_REAL)[0]
        handler = signal.getsignal(signal.SIGALRM)
        with out.guard("inner"), out.guard("innermost"):
            assert signal.getsignal(signal.SIGALRM) is handler
            assert 0 < signal.getitimer(signal.ITIMER_REAL)[0] <= armed
    assert len(calls) == 2 and calls[-1] == 0                # armed once, cleared once
    assert 0 < armed <= 30.0


def test_an_unrelated_running_timer_is_left_alone_and_notification_work_is_refused(
        tmp_path, clean_timer):
    fired = []

    def unrelated(signum, frame):
        fired.append(signum)
    signal.signal(signal.SIGALRM, unrelated)
    signal.setitimer(signal.ITIMER_REAL, 100.0)
    pings = Pings()
    out = cli.Outbound(records(tmp_path), config(tmp_path, hc=True), lambda: at("17:00"),
                       pings, alarm=True)
    with pytest.raises(cli.NotificationExpired):
        with out.guard("probe"):
            raise AssertionError("work ran under a timer it could not install")
    assert signal.getsignal(signal.SIGALRM) is unrelated     # its handler, unchanged
    assert 99.0 < signal.getitimer(signal.ITIMER_REAL)[0] <= 100.0   # not cancelled or moved
    assert out.conflict and out.expired()                    # no later notification work
    assert out.send("liveness", "success", "x") is None
    assert pings.sent == [] and fired == []


def test_liveness_with_an_unrelated_timer_sends_nothing_keeps_its_accounting_and_fails(
        tmp_path, capsys, clean_timer):

    def unrelated(signum, frame):
        pass
    signal.signal(signal.SIGALRM, unrelated)
    signal.setitimer(signal.ITIMER_REAL, 100.0)
    cfg = config(tmp_path, hc=True)
    pings = Pings()
    code = cli.liveness(cfg, records(tmp_path), lambda: at("17:00"), pings, alarm=True)
    printed = capsys.readouterr().out
    assert code == 1 and pings.sent == []
    assert "NOTIFICATION DEADLINE NOT ENFORCEABLE" in printed
    assert (tmp_path / "records/state.json").exists()        # the accounting was done
    assert signal.getsignal(signal.SIGALRM) is unrelated
    assert signal.getitimer(signal.ITIMER_REAL)[0] > 99.0


def test_the_timer_and_handler_are_restored_after_an_exception(tmp_path, clean_timer):
    previous = signal.getsignal(signal.SIGALRM)
    out = cli.Outbound(records(tmp_path), config(tmp_path, hc=True), lambda: at("17:00"),
                       Pings(), alarm=True)
    with pytest.raises(ValueError):
        with out.guard("work that breaks"):
            raise ValueError("broken")
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0
    assert signal.getsignal(signal.SIGALRM) is previous
    assert out.expiry is None and out.began is not None      # not expired, not restarted
    began = out.began
    with out.guard("more"):
        pass
    assert out.began == began


def test_an_assessment_timer_cannot_replace_a_running_notification_timer(tmp_path, clean_timer):
    previous = signal.getsignal(signal.SIGALRM)
    out = cli.Outbound(records(tmp_path), config(tmp_path, hc=True), lambda: at("17:00"),
                       Pings(), alarm=True)
    with pytest.raises(cli.NotificationExpired) as stopped:
        with out.guard("notification work"):
            handler, armed = signal.getsignal(signal.SIGALRM), signal.getitimer(
                signal.ITIMER_REAL)[0]
            try:
                with cli.time_limit(100, True):
                    raise AssertionError("the assessment timer replaced the notification one")
            finally:
                assert signal.getsignal(signal.SIGALRM) is handler
                assert signal.getitimer(signal.ITIMER_REAL)[0] <= armed
    assert isinstance(stopped.value.__cause__, cli.TimerConflict)
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0
    assert signal.getsignal(signal.SIGALRM) is previous


def test_an_assessment_timer_is_not_put_over_another_one(clean_timer):

    def unrelated(signum, frame):
        pass
    signal.signal(signal.SIGALRM, unrelated)
    signal.setitimer(signal.ITIMER_REAL, 100.0)
    with pytest.raises(cli.TimerConflict):
        with cli.time_limit(10, True):
            pass
    assert signal.getsignal(signal.SIGALRM) is unrelated
    assert signal.getitimer(signal.ITIMER_REAL)[0] > 99.0


def test_timers_are_refused_off_the_main_thread(tmp_path, clean_timer):
    previous = signal.getsignal(signal.SIGALRM)
    seen = {}
    out = cli.Outbound(records(tmp_path), config(tmp_path, hc=True), lambda: at("17:00"),
                       Pings(), alarm=True)

    def work():
        try:
            with out.guard("off the main thread"):
                seen["ran"] = True
        except cli.NotificationExpired as exc:
            seen["stopped"] = exc.__cause__
        try:
            with cli.time_limit(10, True):
                pass
        except cli.TimerConflict as exc:
            seen["assessment"] = exc
    worker = threading.Thread(target=work)
    worker.start()
    worker.join()
    assert "ran" not in seen and isinstance(seen["stopped"], cli.TimerConflict)
    assert "main thread" in str(seen["stopped"]) and "main thread" in str(seen["assessment"])
    assert signal.getsignal(signal.SIGALRM) is previous
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


# ===========================================================================
# 0.4.7 — a notification-preparation failure is notification work: bounded,
# classified by the operation that failed, and never stops the accounting
# ===========================================================================
ENTRIES = ["run_check", "liveness"]


def tamper_intent(root):
    """Alter one stored intent after it was sealed: genuinely damaged notification history."""
    path = sorted((Path(root) / NOTES).glob("*-intent.json"))[0]
    path.chmod(0o640)
    text = path.read_text()
    altered = text.replace("observer=", "observer=x", 1)
    assert altered != text
    path.write_text(altered)
    return path, path.read_bytes()


def damaged_setup(root):
    """A passed pre-market run whose intent is then damaged; the state cache removed."""
    cfg = config(root, hc=True)
    run("premarket", root, cfg=cfg, post=Pings())
    damaged = tamper_intent(root)
    (Path(root) / "records/state.json").unlink()
    return cfg, damaged


def accounting_spy(monkeypatch):
    seen = {"settle_publication": 0, "state.json": 0}
    real_settle, real_state = cli.settle_publication, Store.write_state

    def settle(*a, **k):
        seen["settle_publication"] += 1
        return real_settle(*a, **k)

    def write_state(self, state, name="state.json"):
        if name == "state.json":
            seen["state.json"] += 1
        return real_state(self, state, name)
    monkeypatch.setattr(cli, "settle_publication", settle)
    monkeypatch.setattr(Store, "write_state", write_state)
    return seen


def invoke(entry, root, cfg, capsys, post=None, alarm=False, when=None, probe=None):
    """One invocation through `main`, as the service runs it: its exit code and output."""
    path = cfg_file(root, cfg)
    post = Pings() if post is None else post
    capsys.readouterr()
    if entry == "run_check":
        clock = Clock(at("12:30:00") - timedelta(seconds=1), 1.0)
        code = cli.main(["midsession", "--config", path], probe=probe or midday(), clock=clock,
                        sleep=clock.sleep, alarm=alarm, post=post)
    else:
        code = cli.main(["liveness", "--config", path], clock=lambda: when or at("12:40:00"),
                        sleep=never_sleep, alarm=alarm, post=post)
    return code, capsys.readouterr().out, post


def kinds_of_faults(root):
    return sorted(f.get("kind") for f in records(root).faults())


def midsession_report(root):
    (report,) = [r for r in records(root).reports() if r["check"] == "midsession"]
    return report


# --- A. damaged history --------------------------------------------------------------------
@pytest.mark.parametrize("entry", ENTRIES)
def test_damaged_notification_history_is_a_notification_preparation_failure(
        tmp_path, monkeypatch, capsys, entry):
    cfg, (path, damaged) = damaged_setup(tmp_path)
    seen = accounting_spy(monkeypatch)
    made, _ = context_spy(monkeypatch)
    code, printed, pings = invoke(entry, tmp_path, cfg, capsys)
    assert code == 1                                         # operationally visible
    (fault,) = records(tmp_path).faults()
    assert fault["kind"] == "notification-preparation"
    assert fault["signature"] == "notification-preparation:CorruptRecord"
    assert "housekeeping" not in kinds_of_faults(tmp_path)
    assert f"{entry if entry == 'liveness' else 'midsession'}-notification-preparation failed" \
        in printed and "-housekeeping failed" not in printed
    assert str(tmp_path) not in printed and not any(u in printed for u in PING_IDS.values())
    assert made[0].preparation == "failed"
    # Bounded failure handling, inside the same deadline: the fault and its alert.
    (note,) = [n for n in records(tmp_path).notifications(tolerant=True)[0]   # history damaged
               if n["event_type"] == "observer_fault"]
    assert note["fault_ids"] == [fault["fault_id"]]
    assert ("afterclose", "fail") in pings.kinds()
    # The accounting went on.
    assert seen["state.json"] >= 1 and (tmp_path / "records/state.json").exists()
    assert seen["settle_publication"] >= 1
    if entry == "run_check":
        report = midsession_report(tmp_path)
        assert report["result"] == "PASS" and "PASS" in printed
    assert path.read_bytes() == damaged                      # the damage is left as it is


# --- B. other notification-only errors -----------------------------------------------------
PREPARATION_ERRORS = {
    "CorruptRecord": CorruptRecord("a notification record does not match its own hash"),
    "OSError": OSError(5, "Input/output error"),
    "ValueError": ValueError("unexpected"),
}
PREPARATION_STAGES = {"history and checkpoint intent": "prepare_notifications",
                      "deferred intents": "ensure_deferred_intents"}


@pytest.mark.parametrize("error", list(PREPARATION_ERRORS))
@pytest.mark.parametrize("stage", list(PREPARATION_STAGES))
@pytest.mark.parametrize("entry", ENTRIES)
def test_a_notification_only_error_fails_preparation_and_the_accounting_goes_on(
        tmp_path, monkeypatch, capsys, entry, stage, error):
    cfg = config(tmp_path, hc=True)
    run("premarket", tmp_path, cfg=cfg, post=Pings())
    monkeypatch.setattr(cli, PREPARATION_STAGES[stage], boom(PREPARATION_ERRORS[error]))
    seen = accounting_spy(monkeypatch)
    made, _ = context_spy(monkeypatch)
    code, printed, _ = invoke(entry, tmp_path, cfg, capsys)
    assert code == 1 and made[0].preparation == "failed"     # explicitly unsuccessful
    (fault,) = records(tmp_path).faults()
    assert fault["kind"] == "notification-preparation"
    assert fault["signature"] == f"notification-preparation:{error}"
    assert not fault["resolved"]                             # nothing resolves its own failure
    assert seen["state.json"] >= 1 and seen["settle_publication"] >= 1
    assert "notification-preparation failed" in printed
    if entry == "run_check":
        assert midsession_report(tmp_path)["result"] == "PASS"   # not changed by it


# --- C. bounded failure persistence, on the original deadline ------------------------------
def deadline_armings(monkeypatch):
    """When each notification timer was armed, and for how long."""
    import time as real_time
    armings, real = [], cli.hard_limit

    def hard_limit(seconds, enabled):
        armings.append((real_time.monotonic(), seconds))
        return real(seconds, enabled)
    monkeypatch.setattr(cli, "hard_limit", hard_limit)
    return armings


@pytest.mark.parametrize("persistence", ["failure event", "fault record"])
@pytest.mark.parametrize("entry", ENTRIES)
def test_slow_failure_recording_stops_at_the_original_deadline_in_real_time(
        tmp_path, monkeypatch, capsys, entry, persistence):
    import time as real_time
    cfg, _ = damaged_setup(tmp_path)
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 0.5)
    if persistence == "failure event":
        real_event = cli.raise_event

        def raise_event(store_, day, check, report_id, moment, kind, subject, summary):
            if subject.endswith("-notification-preparation"):
                real_time.sleep(2.0)                         # interruptible
            return real_event(store_, day, check, report_id, moment, kind, subject, summary)
        monkeypatch.setattr(cli, "raise_event", raise_event)
    else:
        slow_store(monkeypatch, "write_fault", 2.0)
    armings = deadline_armings(monkeypatch)
    opened = []
    real_open = cli.open_fault

    def open_fault(*a, **k):
        opened.append(a[3])
        return real_open(*a, **k)
    monkeypatch.setattr(cli, "open_fault", open_fault)
    seen = accounting_spy(monkeypatch)
    began = real_time.monotonic()
    code, printed, pings = invoke(entry, tmp_path, cfg, capsys, alarm=True)
    elapsed = real_time.monotonic() - began
    assert elapsed < 1.5                                     # not 2 s: cut off at 0.5 s
    assert code == 1 and "may not have been stored" in printed
    assert "notification-preparation" not in kinds_of_faults(tmp_path)   # no fallback write
    assert [n for n in records(tmp_path).notifications(tolerant=True)[0]
            if n["event_type"] == "observer_fault"] == []
    assert opened == ["notification-preparation"]            # one recording, no recursion
    ends = [at_ + seconds for at_, seconds in armings]
    assert armings and armings[0][1] <= 0.5
    assert max(ends) - min(ends) < 0.1                       # every guard: the one deadline
    assert [k for k in pings.kinds() if k[0] != "liveness"] == []
    assert seen["state.json"] >= 1 and seen["settle_publication"] >= 1
    if entry == "run_check":
        assert midsession_report(tmp_path)["result"] == "PASS"


# --- D. after expiry, nothing notification-related begins ----------------------------------
def post_expiry_spies(monkeypatch, flag):
    """Every notification operation attempted once `flag["expired"]` is set."""
    after = []

    def watch(owner, name, label=None, when=lambda *a, **k: True):
        real = getattr(owner, name)

        def spy(*a, **k):
            if flag["expired"] and when(*a, **k):
                after.append(label or name)
            return real(*a, **k)
        monkeypatch.setattr(owner, name, spy)
    watch(cli, "raise_event", "notification event",
          lambda *a, **k: "notification" in a[6])
    watch(Store, "write_fault", "fault record")
    watch(cli, "queue", "intent")
    watch(Store, "write_state", "cache write", lambda self, state, name="state.json":
          name == "notification-state.json")
    watch(cli, "expose", "cache rebuild")
    watch(cli, "resolve_faults", "notification-fault resolution",
          lambda *a, **k: a[3].startswith("notification"))
    watch(Store, "write_reservation", "reservation")
    watch(Store, "write_heartbeat", "heartbeat record")
    watch(cli, "notification_states", "notification preparation")
    watch(cli, "ensure_deferred_intents", "notification preparation")
    watch(cli, "prepare_notifications", "notification preparation")
    watch(cli, "current_body", "notification preparation")
    return after


@pytest.mark.parametrize("entry", ENTRIES)
def test_nothing_notification_related_begins_after_expiry_following_a_preparation_error(
        tmp_path, monkeypatch, entry):
    cfg, _ = damaged_setup(tmp_path)
    timer, flag = Counting(), {"expired": False}
    after = post_expiry_spies(monkeypatch, flag)
    real_prepare = cli.prepare_notifications

    def expiring(*a, **k):
        timer.now += 31.0                                    # the deadline passes here ...
        try:
            return real_prepare(*a, **k)                     # ... and the history is damaged
        finally:
            flag["expired"] = True                           # everything after: watched
    monkeypatch.setattr(cli, "prepare_notifications", expiring)
    seen = accounting_spy(monkeypatch)
    pings, faults = Pings(), []
    if entry == "run_check":
        report, _ = run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg, post=pings,
                        timer=timer, faults=faults)
        assert report["result"] == "PASS"
        sent = pings.sent
    else:
        cli.liveness(cfg, records(tmp_path), lambda: at("12:40"), pings, timer=timer)
        sent = pings.sent[1:]                                # after the heartbeat
    assert flag["expired"] and after == []                   # no notification work at all
    assert sent == [] and records(tmp_path).faults() == []
    assert seen["state.json"] >= 1 and seen["settle_publication"] >= 1
    if entry == "run_check":                                 # liveness: the next test
        assert any("not recorded, because the notification deadline had passed" in f
                   for f in faults)


def test_liveness_says_so_and_exits_non_zero_when_expiry_stopped_the_recording(
        tmp_path, monkeypatch, capsys):
    cfg, _ = damaged_setup(tmp_path)
    timer = Counting()
    real_prepare = cli.prepare_notifications

    def expiring(*a, **k):
        timer.now += 31.0
        return real_prepare(*a, **k)
    monkeypatch.setattr(cli, "prepare_notifications", expiring)
    capsys.readouterr()
    code = cli.liveness(cfg, records(tmp_path), lambda: at("12:40"), Pings(), timer=timer)
    printed = capsys.readouterr().out
    assert code == 1
    assert "notification-preparation failed: CorruptRecord; not recorded, because the " \
           "notification deadline had passed; nothing was stored" in printed
    assert records(tmp_path).faults() == []                  # nothing invented afterwards


# --- E. the accounting is complete -----------------------------------------------------------
def test_damaged_history_does_not_stop_publication_reconciliation_or_the_checkpoint(
        tmp_path, monkeypatch, capsys):
    results = {}
    for name in ("control", "damaged"):
        root = tmp_path / name
        cfg = unconfirmed_afterclose(root, monkeypatch)
        (fault,) = records(root).faults()
        assert fault["kind"] == "report-publication" and not fault["resolved"]
        if name == "damaged":
            path, damaged = tamper_intent(root)
        code, printed, _ = invoke("liveness", root, cfg, capsys, when=at("16:20"))
        checkpoint = records(root).checkpoint()
        results[name] = {
            "code": code, "publication_resolved": next(
                f for f in records(root).faults() if f["kind"] == "report-publication")["resolved"],
            "reasons": checkpoint and checkpoint["reasons"],
            "qualifying": phase(root, cfg)["qualifying_sessions"],
            "state_phase": json.loads((root / "records/state.json").read_text())["phase"],
            "afterclose": [r["outcome"] for r in records(root).reports(check="afterclose")]}
        if name == "damaged":
            frozen = records(root).checkpoint_path.read_bytes()
            invoke("liveness", root, cfg, capsys, when=at("16:25"))
            assert records(root).checkpoint_path.read_bytes() == frozen   # final
            assert path.read_bytes() == damaged
            assert kinds_of_faults(root) == ["notification-preparation", "report-publication"]
    control, damaged = results["control"], results["damaged"]
    assert control["publication_resolved"] and damaged["publication_resolved"]
    assert damaged["reasons"] == control["reasons"] == ["EARLY_REVIEW"]
    assert damaged["qualifying"] == control["qualifying"]
    assert damaged["state_phase"] == control["state_phase"] == "REVIEW PENDING"
    assert damaged["afterclose"] == control["afterclose"]
    assert control["code"] == 0 and damaged["code"] == 1     # the failure stays visible


@pytest.mark.parametrize("probe,result", [(None, "PASS"), ("stale", "FAIL")])
def test_damaged_history_changes_no_assessment_result(tmp_path, monkeypatch, capsys, probe,
                                                      result):
    cfg, _ = damaged_setup(tmp_path)
    code, printed, _ = invoke("run_check", tmp_path, cfg, capsys,
                              probe=midday(revision="f" * 40) if probe else None)
    report = midsession_report(tmp_path)
    assert report["result"] == result and code == 1
    assert cli.check_status(records(tmp_path), DAY.isoformat(), "midsession") == result
    assert cli.check_status(records(tmp_path), DAY.isoformat(), "premarket") == "PASS"


# --- F. a real accounting failure is still one -----------------------------------------------
ACCOUNTING_FAILURES = {
    "state persistence": "state",
    "checkpoint accounting": "checkpoint",
}


@pytest.mark.parametrize("failure", list(ACCOUNTING_FAILURES))
@pytest.mark.parametrize("entry", ENTRIES)
def test_a_real_accounting_failure_stays_a_housekeeping_failure_and_fails_closed(
        tmp_path, monkeypatch, capsys, entry, failure):
    cfg = config(tmp_path, hc=True)
    run("premarket", tmp_path, cfg=cfg, post=Pings())
    if ACCOUNTING_FAILURES[failure] == "state":
        real = Store.write_state

        def write_state(self, state, name="state.json"):
            if name == "state.json":
                raise OSError(28, "No space left on device")
            return real(self, state, name)
        monkeypatch.setattr(Store, "write_state", write_state)
    else:
        monkeypatch.setattr(cli, "settle_checkpoint",
                            boom(CorruptRecord("a checkpoint record is damaged")))
    settled = []
    real_settle = cli.settle_publication
    monkeypatch.setattr(cli, "settle_publication",
                        lambda *a, **k: settled.append(1) or real_settle(*a, **k))
    code, printed, _ = invoke(entry, tmp_path, cfg, capsys)
    assert code == 1
    assert kinds_of_faults(tmp_path) == ["housekeeping"]     # not a notification failure
    assert settled == []                                     # fail-closed, as before
    assert "-housekeeping failed" in printed
    assert "notification-preparation" not in printed


# --- G. recovery: re-detected, once, conservatively ------------------------------------------
@pytest.mark.parametrize("entry", ENTRIES)
def test_a_lost_preparation_failure_is_detected_again_once_and_sent_once(
        tmp_path, monkeypatch, capsys, entry):
    cfg, (path, damaged) = damaged_setup(tmp_path)
    timer = Counting()
    with monkeypatch.context() as m:                         # its recording is cut off
        real_prepare = cli.prepare_notifications

        def expiring(*a, **k):
            timer.now += 31.0
            return real_prepare(*a, **k)
        m.setattr(cli, "prepare_notifications", expiring)
        if entry == "run_check":
            run("midsession", tmp_path, midday(), "12:30:00", cfg=cfg, timer=timer)
        else:
            cli.liveness(cfg, records(tmp_path), lambda: at("12:40"), Pings(), timer=timer)
    assert records(tmp_path).faults() == []
    before = {p: p.read_bytes() for p in (tmp_path / "records").rglob("*.json")
              if "outbox" in p.parts or "reports" in p.parts}
    code, _, first = invoke("liveness", tmp_path, cfg, capsys, when=at("12:45"))
    (fault,) = records(tmp_path).faults()
    assert fault["kind"] == "notification-preparation" and code == 1
    assert first.kinds().count(("afterclose", "fail")) == 1
    reservations = files(tmp_path, "*-reservation.json")
    code, _, second = invoke("liveness", tmp_path, cfg, capsys, when=at("12:47"))
    assert ("afterclose", "fail") not in second.kinds()      # no premature retry
    assert files(tmp_path, "*-reservation.json") == reservations   # no new reservation
    assert len(records(tmp_path).faults()) == 1               # no duplicate fault
    assert len([n for n in records(tmp_path).notifications(tolerant=True)[0]
                if n["event_type"] == "observer_fault"]) == 1      # no duplicate intent
    assert all(p.read_bytes() == b for p, b in before.items())     # history unchanged
    assert path.read_bytes() == damaged


def test_a_preparation_fault_is_resolved_only_by_a_later_clean_invocation(tmp_path, monkeypatch,
                                                                          capsys):
    cfg = config(tmp_path, hc=True)
    with monkeypatch.context() as m:                         # an earlier notification-round fault
        m.setattr(cli, "deliver_round", boom(RuntimeError("round broke")))
        beat(tmp_path, cfg, at("12:30"))
    with monkeypatch.context() as m:                         # preparation fails once
        m.setattr(cli, "prepare_notifications", boom(OSError(5, "Input/output error")))
        code, _, _ = invoke("liveness", tmp_path, cfg, capsys, when=at("12:35"))
    assert code == 1
    by_kind = {f["kind"]: f for f in records(tmp_path).faults()}
    assert set(by_kind) == {"notification-round", "notification-preparation"}
    assert not any(f["resolved"] for f in by_kind.values())  # nothing resolved after failure
    code, _, _ = invoke("liveness", tmp_path, cfg, capsys, when=at("12:40"))
    by_kind = {f["kind"]: f for f in records(tmp_path).faults()}
    assert by_kind["notification-preparation"]["resolved"]
    assert by_kind["notification-round"]["resolved"]
    resolution = by_kind["notification-preparation"]["resolution"]
    assert resolution["resolved_by_invocation"] != \
        by_kind["notification-preparation"]["detected_by_invocation"]
    assert code == 0


# ===========================================================================
# 0.4.8 — a preparation fault is resolved only by an invocation whose every
# preparation pass succeeded, and only after the last of them
# ===========================================================================
def two_open_faults(root, monkeypatch):
    """An unresolved notification-preparation fault and an unresolved report-publication fault.

    The after-close report cannot be synced (PRESENT_UNCONFIRMED); a liveness run
    then fails its preparation while the report still cannot be synced, so the
    publication fault stays open as well. Returns (cfg, preparation fault id,
    publication fault id).
    """
    cfg = config(root, hc=True)
    run("premarket", root, cfg=cfg, post=Pings())
    run("midsession", root, midday(), "12:30:00", cfg=cfg, post=Pings())
    with monkeypatch.context() as m:
        unsyncable_reports(m, "afterclose-")
        run("afterclose", root, trigger_probe(), "16:00:00", cfg=cfg, post=Pings())
        m.setattr(cli, "prepare_notifications", boom(OSError(5, "Input/output error")))
        beat(root, cfg, at("16:05"))
    by_kind = {f["kind"]: f for f in records(root).faults()}
    assert set(by_kind) == {"notification-preparation", "report-publication"}
    assert not any(f["resolved"] for f in by_kind.values())
    return (cfg, by_kind["notification-preparation"]["fault_id"],
            by_kind["report-publication"]["fault_id"])


def transient_preparation(monkeypatch, fail_on, error=None):
    """Preparation fails on the given passes (1-based) of the next invocation, else works."""
    real, calls = cli.prepare_notifications, {"n": 0}

    def prepare_notifications(*a, **k):
        calls["n"] += 1
        if calls["n"] in fail_on:
            raise error or OSError(5, "Input/output error (transient)")
        return real(*a, **k)
    monkeypatch.setattr(cli, "prepare_notifications", prepare_notifications)
    return calls


def preparation_resolutions(monkeypatch):
    """Every attempt to resolve notification-preparation faults, as it starts."""
    attempts, real = [], cli.resolve_faults

    def resolve_faults(store_, now, check, kind, invocation):
        if kind == "notification-preparation":
            attempts.append(invocation)
        return real(store_, now, check, kind, invocation)
    monkeypatch.setattr(cli, "resolve_faults", resolve_faults)
    return attempts


def reentrant_invocation(entry, root, cfg, capsys, when="16:20"):
    """An invocation that reconciles the publication fault, and so runs housekeeping twice."""
    path = cfg_file(root, cfg)
    capsys.readouterr()
    if entry == "run_check":
        clock = Clock(at(f"{when}:00") - timedelta(seconds=1), 1.0)
        code = cli.main(["afterclose", "--config", path], probe=trigger_probe(), clock=clock,
                        sleep=clock.sleep, alarm=False, post=Pings())
    else:
        code = cli.main(["liveness", "--config", path], clock=lambda: at(when),
                        sleep=never_sleep, alarm=False, post=Pings())
    return code, capsys.readouterr().out


def history_of(root):
    return {p: p.read_bytes() for p in (Path(root) / "records").rglob("*.json")
            if p.name not in ("state.json", "notification-state.json")}


def prep_resolution_file(root, fault_id):
    return Path(root) / "records/faults" / f"{fault_id}-resolution.json"


@pytest.mark.parametrize("order", ["failure then success", "success then failure"])
@pytest.mark.parametrize("entry", ENTRIES)
def test_a_failed_preparation_anywhere_in_an_invocation_resolves_no_preparation_fault(
        tmp_path, monkeypatch, capsys, entry, order):
    cfg, prep_id, publication_id = two_open_faults(tmp_path, monkeypatch)
    history = history_of(tmp_path)
    calls = transient_preparation(monkeypatch, {1} if order == "failure then success" else {2})
    attempts = preparation_resolutions(monkeypatch)
    made, _ = context_spy(monkeypatch)
    code, printed = reentrant_invocation(entry, tmp_path, cfg, capsys)
    assert calls["n"] == 2                                   # the reconciliation re-entered
    assert made[0].preparation == "failed"                   # worst result kept
    assert attempts == []                                    # never even attempted
    faults = {f["fault_id"]: f for f in records(tmp_path).faults()}
    assert not faults[prep_id]["resolved"]
    assert not prep_resolution_file(tmp_path, prep_id).exists()  # nothing persisted for it
    assert faults[publication_id]["resolved"]                # resolved on its own terms
    assert [f["kind"] for f in faults.values()].count("notification-preparation") == 1
    assert code == 1 and "notification-preparation failed" in printed
    assert records(tmp_path).checkpoint()["reasons"] == ["EARLY_REVIEW"]
    assert (tmp_path / "records/state.json").exists()
    assert all(p.read_bytes() == b for p, b in history.items())     # nothing overwritten


@pytest.mark.parametrize("entry", ENTRIES)
def test_a_reentrant_invocation_with_failed_preparation_keeps_the_accounting_of_a_clean_one(
        tmp_path, monkeypatch, capsys, entry):
    results = {}
    for name in ("clean", "failed"):
        root = tmp_path / name
        cfg, _, publication_id = two_open_faults(root, monkeypatch)
        with monkeypatch.context() as m:
            if name == "failed":
                transient_preparation(m, {1})
            code, _ = reentrant_invocation(entry, root, cfg, capsys)
        reports = records(root).reports(check="afterclose")
        results[name] = {
            "code": code,
            "publication": next(f for f in records(root).faults()
                                if f["fault_id"] == publication_id)["resolved"],
            "checkpoint": records(root).checkpoint()["reasons"],
            "qualifying": phase(root, cfg)["qualifying_sessions"],
            "state": json.loads((root / "records/state.json").read_text())["phase"],
            "reports": [(r["result"], r["outcome"]) for r in reports]}
    clean, failed = results["clean"], results["failed"]
    for key in ("publication", "checkpoint", "qualifying", "state", "reports"):
        assert clean[key] == failed[key], key
    assert clean["publication"] is True
    assert failed["code"] == 1


@pytest.mark.parametrize("entry", ENTRIES)
def test_a_genuinely_later_clean_invocation_resolves_the_preparation_fault(
        tmp_path, monkeypatch, capsys, entry):
    cfg, prep_id, _ = two_open_faults(tmp_path, monkeypatch)
    with monkeypatch.context() as m:                         # the failed invocation
        transient_preparation(m, {1})
        reentrant_invocation(entry, tmp_path, cfg, capsys)
    failed_invocations = {f["detected_by_invocation"] for f in records(tmp_path).faults()}
    history = history_of(tmp_path)
    attempts = preparation_resolutions(monkeypatch)
    code, _ = reentrant_invocation("liveness", tmp_path, cfg, capsys, when="16:30")
    (fault,) = [f for f in records(tmp_path).faults() if f["fault_id"] == prep_id]
    assert fault["resolved"] and len(attempts) == 1
    resolution = fault["resolution"]
    assert resolution["resolved_by_invocation"] == attempts[0]
    assert resolution["resolved_by_invocation"] not in failed_invocations
    assert prep_resolution_file(tmp_path, prep_id).exists()
    assert ("observer_fault_resolved", f"fault-{prep_id}") in events(tmp_path)
    assert all(p.read_bytes() == b for p, b in history.items())     # history immutable
    assert code == 0


def test_repeated_preparation_failures_resolve_nothing_and_open_nothing_new(
        tmp_path, monkeypatch, capsys):
    cfg, prep_id, publication_id = two_open_faults(tmp_path, monkeypatch)
    transient_preparation(monkeypatch, {1, 2})
    attempts = preparation_resolutions(monkeypatch)
    code, _ = reentrant_invocation("liveness", tmp_path, cfg, capsys)
    faults = {f["fault_id"]: f for f in records(tmp_path).faults()}
    assert code == 1 and attempts == [] and not faults[prep_id]["resolved"]
    assert [f["kind"] for f in faults.values()].count("notification-preparation") == 1
    assert faults[publication_id]["resolved"]


@pytest.mark.parametrize("entry", ENTRIES)
def test_expiry_then_successful_accounting_resolves_nothing_and_starts_nothing(
        tmp_path, monkeypatch, capsys, entry):
    cfg, prep_id, publication_id = two_open_faults(tmp_path, monkeypatch)
    timer, flag = Counting(), {"expired": False}
    after = post_expiry_spies(monkeypatch, flag)
    real = cli.prepare_notifications
    calls = {"n": 0}

    def expiring(*a, **k):
        calls["n"] += 1
        timer.now += 31.0                                    # the deadline passes here
        try:
            return real(*a, **k)                             # this pass itself succeeds
        finally:
            flag["expired"] = True
    monkeypatch.setattr(cli, "prepare_notifications", expiring)
    attempts = preparation_resolutions(monkeypatch)
    made, _ = context_spy(monkeypatch)
    pings, faults = Pings(), []
    if entry == "run_check":
        run("afterclose", tmp_path, trigger_probe(), "16:20:00", cfg=cfg, post=pings,
            timer=timer, faults=faults)
        sent = pings.sent
    else:
        cli.liveness(cfg, records(tmp_path), lambda: at("16:20"), pings, timer=timer)
        sent = pings.sent[1:]
    assert calls["n"] == 1                                   # no second preparation pass
    assert made[0].preparation == "expired" and attempts == [] and after == []
    assert sent == []
    by_id = {f["fault_id"]: f for f in records(tmp_path).faults()}
    assert not by_id[prep_id]["resolved"] and by_id[publication_id]["resolved"]
    assert records(tmp_path).checkpoint()["reasons"] == ["EARLY_REVIEW"]


def test_reentry_does_not_restart_the_deadline_in_real_time(tmp_path, monkeypatch, capsys):
    import time as real_time
    cfg, prep_id, _ = two_open_faults(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "NOTIFY_BUDGET_SECONDS", 1.5)
    real = cli.prepare_notifications

    def slow(*a, **k):
        real_time.sleep(0.3)
        return real(*a, **k)
    monkeypatch.setattr(cli, "prepare_notifications", slow)
    armings = deadline_armings(monkeypatch)
    calls = transient_preparation(monkeypatch, set())
    path = cfg_file(tmp_path, cfg)
    cli.main(["liveness", "--config", path], clock=lambda: at("16:20"), sleep=never_sleep,
             alarm=True, post=Pings())
    assert calls["n"] == 2 and len(armings) >= 4
    ends = [a + s for a, s in armings]
    assert max(ends) - min(ends) < 0.1                       # one deadline throughout
    assert all(s <= 1.5 for _, s in armings)


def test_a_failed_resolution_write_is_not_reported_as_a_resolution(tmp_path, monkeypatch,
                                                                   capsys):
    # A preparation fault whose resolution, attempted by a clean later invocation, is
    # linked but cannot be made durable.
    cfg2 = config(tmp_path / "b", hc=True)
    with monkeypatch.context() as m:
        m.setattr(cli, "prepare_notifications", boom(OSError(5, "Input/output error")))
        beat(tmp_path / "b", cfg2, at("12:00"))
    (fault,) = records(tmp_path / "b").faults()
    real_chain = store.sync_chain

    def sync_chain(path, root):
        if path.name == f"{fault['fault_id']}-resolution.json":
            raise OSError(5, "Input/output error")
        return real_chain(path, root)
    with monkeypatch.context() as m:
        m.setattr(store, "sync_chain", sync_chain)
        code, printed = reentrant_invocation("liveness", tmp_path / "b", cfg2, capsys,
                                             when="12:10")
        assert not records(tmp_path / "b").faults()[0]["resolved"]   # not verified: open
    assert code == 1 and "notification-round failed" in printed
    assert ("observer_fault_resolved", f"fault-{fault['fault_id']}") \
        not in events(tmp_path / "b")
    assert prep_resolution_file(tmp_path / "b", fault["fault_id"]).exists()   # linked, unverified


def test_an_accounting_failure_in_the_reentrant_pass_stays_a_housekeeping_failure(
        tmp_path, monkeypatch, capsys):
    cfg, prep_id, publication_id = two_open_faults(tmp_path, monkeypatch)
    real, calls = Store.write_state, {"n": 0}

    def write_state(self, state, name="state.json"):
        if name == "state.json":
            calls["n"] += 1
            if calls["n"] == 2:                              # the reconciliation's pass
                raise OSError(28, "No space left on device")
        return real(self, state, name)
    monkeypatch.setattr(Store, "write_state", write_state)
    attempts = preparation_resolutions(monkeypatch)
    made, _ = context_spy(monkeypatch)
    code, printed = reentrant_invocation("liveness", tmp_path, cfg, capsys)
    faults = {f["fault_id"]: f for f in records(tmp_path).faults()}
    open_kinds = sorted(f["kind"] for f in faults.values() if not f["resolved"])
    assert code == 1 and "liveness-housekeeping failed" in printed
    assert open_kinds == ["housekeeping", "notification-preparation"]   # not relabelled
    assert faults[publication_id]["resolved"]                # its own terms were met
    # The reentrant pass stopped before its deferred preparation stage: incomplete.
    assert made[0].preparation == "incomplete" and attempts == []
    assert not faults[prep_id]["resolved"]
    assert not prep_resolution_file(tmp_path, prep_id).exists()


# ---------------------------------------------------------------------------
# v0.4.14 — the read-only shutdown-evidence interface
# ---------------------------------------------------------------------------
from quantdesk_observer import evidence  # noqa: E402

TXN = "0123456789abcdef0123456789abcdef"


class EvidenceProbe:
    """systemd's answer for the API unit, and the boot: what the evidence binds to."""

    def __init__(self, running=True, mono=MONOTONIC, boot=BOOT, error=None):
        self.running, self.mono, self.boot, self.error = running, mono, boot, error

    def boot_id(self):
        return self.boot

    def unit(self, name):
        return {"ActiveState": "active" if self.running else "inactive",
                "MainPID": "943" if self.running else "0",
                "ExecMainStartTimestampMonotonic": self.mono}

    def _api_identity(self):
        if self.error:
            raise self.error
        if not self.running:
            raise ProbeError("systemd does not report a running API process with a start time")
        return ({"boot_id": self.boot, "unit": "quantdesk-api.service", "main_pid": 943,
                 "main_started": at("08:20:00").isoformat(),
                 "main_started_monotonic_usec": int(self.mono)}, [943])


def evidence_setup(tmp_path, **cfg_over):
    cfg = config(tmp_path, units={"api": "quantdesk-api.service"}, **cfg_over)
    path = tmp_path / "observer.json"
    path.write_text(json.dumps(cfg))
    records(tmp_path).lock().close()          # the lock file exists once the observer has run
    return cfg, str(path)


def collect(tmp_path, cfg, path, now="17:30:00", day=DAY, probe=None, **kw):
    return evidence.collect(cfg, records(tmp_path), probe or EvidenceProbe(),
                            lambda: at(now, day), TXN, path, **kw)


def tree(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}


def test_evidence_is_clear_after_a_recorded_session_and_binds_its_inputs(tmp_path):
    cfg, path = evidence_setup(tmp_path)
    day_run(tmp_path, cfg=cfg)
    record = collect(tmp_path, cfg, path)
    assert record["verdict"] == "CLEAR", record["reasons"]
    assert record["schema"] == "observer_shutdown_evidence/1" and record["txn_id"] == TXN
    assert record["boot_id"] == BOOT and record["api"]["main_pid"] == 943
    assert record["api"]["main_started_monotonic_usec"] == int(MONOTONIC)
    assert record["api"]["state"] == "running" and record["api"]["pids"] == [943]
    assert record["config_sha256"] == hashlib.sha256(Path(path).read_bytes()).hexdigest()
    assert record["calendar_sha256"] == hashlib.sha256(
        Path(cfg["calendar_file"]).read_bytes()).hexdigest()
    assert record["observer_version"] == VERSION and record["lock"]["mode"] == "own"
    [found] = record["afterclose"]["confirmed_reports"]
    assert found["file_sha256"] and found["confirmation_file_sha256"]
    assert record["integrity"]["content_sha256"] == store.content_hash(record)


def test_evidence_blocks_without_a_confirmed_after_close_report(tmp_path):
    cfg, path = evidence_setup(tmp_path)
    run("premarket", tmp_path, cfg=cfg)
    record = collect(tmp_path, cfg, path)
    assert record["verdict"] == "BLOCKED" and "afterclose_missing" in record["reasons"]["blocked"]


def test_an_unconfirmed_after_close_report_does_not_count(tmp_path):
    cfg, path = evidence_setup(tmp_path)
    day_run(tmp_path, cfg=cfg)
    for confirmation in (tmp_path / "records" / "confirmations").rglob("afterclose-*.json"):
        confirmation.unlink()
    record = collect(tmp_path, cfg, path)
    assert record["verdict"] == "BLOCKED" and "afterclose_missing" in record["reasons"]["blocked"]


def test_a_reviewed_closed_day_needs_no_after_close_report_and_an_unreviewed_one_does(tmp_path):
    cfg, path = evidence_setup(tmp_path / "closed", closed=(DAY,))
    record = collect(tmp_path / "closed", cfg, path)
    assert record["verdict"] == "CLEAR" and record["afterclose"]["required"] is False
    cfg, path = evidence_setup(tmp_path / "unreviewed", reviewed=False)
    record = collect(tmp_path / "unreviewed", cfg, path)
    assert record["afterclose"]["required"] is True
    assert "afterclose_missing" in record["reasons"]["blocked"]


@pytest.mark.parametrize("special", [DAY, DAY + timedelta(days=1)])
def test_a_reviewed_special_session_today_or_tomorrow_blocks(tmp_path, special):
    cfg, path = evidence_setup(tmp_path, special=(special,))
    record = collect(tmp_path, cfg, path)
    assert record["verdict"] == "BLOCKED"
    assert f"special_session: {special.isoformat()}" in record["reasons"]["blocked"]


def test_an_undelivered_current_failure_blocks_shutdown(tmp_path):
    cfg, path = evidence_setup(tmp_path, hc=True)
    probe = FakeProbe(ntp=False)                         # pre-market fails
    run("premarket", tmp_path, probe, cfg=cfg, post=Pings(fail_first=99))
    record = collect(tmp_path, cfg, path, now="08:50:00")
    assert record["verdict"] == "BLOCKED"
    assert any(r.startswith("notification_outstanding") for r in record["reasons"]["blocked"])
    assert record["notifications_outstanding"][0]["kind"] == "fail"


def test_a_delivered_failure_does_not_block(tmp_path):
    cfg, path = evidence_setup(tmp_path, hc=True, closed=(DAY,))
    run("premarket", tmp_path, FakeProbe(ntp=False), cfg=cfg, post=Pings())
    record = collect(tmp_path, cfg, path, now="17:30:00")
    assert record["notifications_outstanding"] == []
    assert record["verdict"] == "CLEAR", record["reasons"]


def test_an_open_observer_fault_blocks_and_an_unverifiable_resolution_is_unknown(tmp_path):
    cfg, path = evidence_setup(tmp_path, closed=(DAY,))
    store_ = records(tmp_path)
    faults: list[str] = []
    cli.open_fault(store_, at("17:00"), "liveness", "housekeeping", RuntimeError("x"), faults)
    record = collect(tmp_path, cfg, path)
    assert record["verdict"] == "BLOCKED"
    assert any(r.startswith("fault_open") for r in record["reasons"]["blocked"])
    [fault] = records(tmp_path).faults()
    bogus = tmp_path / "records" / "faults" / f"{fault['fault_id']}-resolution.json"
    bogus.write_text(json.dumps({"schema": "nonsense"}))
    record = collect(tmp_path, cfg, path)
    assert record["verdict"] == "UNKNOWN"


def test_a_due_checkpoint_that_is_not_recorded_blocks(tmp_path):
    cfg, path = evidence_setup(tmp_path)
    window = cli.window(cli.load_calendar(cfg), cfg)
    due = checks.ist(window["due_at_ist"])
    record = collect(tmp_path, cfg, path, now=(due + timedelta(hours=1)).strftime("%H:%M:%S"),
                     day=due.date())
    assert "checkpoint_due" in record["reasons"]["blocked"]
    assert record["phase"]["computed_reasons"] == ["BOUNDED_WINDOW_ENDED_WITH_UNRESOLVED_EVIDENCE"]


def test_damaged_records_make_the_evidence_unknown(tmp_path):
    cfg, path = evidence_setup(tmp_path)
    day_run(tmp_path, cfg=cfg)
    report = next((tmp_path / "records" / "reports").rglob("afterclose-*.json"))
    report.chmod(0o600)
    report.write_text("{ damaged")
    record = collect(tmp_path, cfg, path)
    assert record["verdict"] == "UNKNOWN"


def test_an_unresolved_delivery_history_is_unknown(tmp_path, monkeypatch):
    cfg, path = evidence_setup(tmp_path, closed=(DAY,))
    note = {"outstanding": True, "delivery_state": cli.UNRESOLVED, "notification_id": "n1",
            "check": "premarket", "event_type": "assessment_result", "kind": "fail"}
    monkeypatch.setattr(evidence, "notification_states", lambda store_, now: [note])
    assert collect(tmp_path, cfg, path)["verdict"] == "UNKNOWN"


def test_without_the_api_identity_the_evidence_is_unknown(tmp_path):
    cfg, path = evidence_setup(tmp_path, closed=(DAY,))
    record = collect(tmp_path, cfg, path, probe=EvidenceProbe(error=ProbeError("2 processes")))
    assert record["verdict"] == "UNKNOWN" and record["api"] is None


def test_after_the_drain_the_evidence_binds_the_stopped_api_units_last_start(tmp_path):
    cfg, path = evidence_setup(tmp_path, closed=(DAY,))
    record = collect(tmp_path, cfg, path, probe=EvidenceProbe(running=False), api_stopped=True)
    assert record["verdict"] == "CLEAR"
    assert record["api"] == {"unit": "quantdesk-api.service", "state": "stopped",
                             "active_state": "inactive",
                             "main_started_monotonic_usec": int(MONOTONIC)}
    still_running = collect(tmp_path, cfg, path, probe=EvidenceProbe(running=True),
                            api_stopped=True)
    assert still_running["verdict"] == "UNKNOWN"


def test_evidence_reads_under_an_inherited_lock_and_refuses_another_descriptor(tmp_path):
    cfg, path = evidence_setup(tmp_path, closed=(DAY,))
    lock_path = tmp_path / "records" / "lock"
    fd = os.open(lock_path, os.O_RDWR)
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)        # the power controller holds it
    try:
        record = collect(tmp_path, cfg, path, lock_fd=fd)
        assert record["verdict"] == "CLEAR" and record["lock"]["mode"] == "inherited"
        assert (record["lock"]["device"], record["lock"]["inode"]) == (
            os.fstat(fd).st_dev, os.fstat(fd).st_ino)
        with pytest.raises(Overlap):                       # no observer run can start meanwhile
            records(tmp_path).lock()
        other = os.open(tmp_path / "observer.json", os.O_RDONLY)
        try:
            refused = collect(tmp_path, cfg, path, lock_fd=other)
            assert refused["verdict"] == "UNKNOWN" and refused["lock"] is None
        finally:
            os.close(other)
    finally:
        os.close(fd)


def test_evidence_cannot_be_read_while_another_process_holds_the_lock(tmp_path):
    cfg, path = evidence_setup(tmp_path, closed=(DAY,))
    holder = subprocess.Popen(
        [sys.executable, "-c", "import fcntl,sys,time; f=open(sys.argv[1],'a'); "
         "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(30)",
         str(tmp_path / "records" / "lock")], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        record = collect(tmp_path, cfg, path)
        assert record["verdict"] == "UNKNOWN"
        assert any("lock" in r for r in record["reasons"]["unknown"])
    finally:
        holder.kill()
        holder.wait()


def test_evidence_mode_writes_nothing_and_sends_nothing(tmp_path, capsys):
    cfg, path = evidence_setup(tmp_path, hc=True)
    run("premarket", tmp_path, FakeProbe(ntp=False), cfg=cfg, post=Pings(fail_first=99))
    before = tree(tmp_path)
    sent = Pings()
    code = cli.main(["shutdown-evidence", "--config", path, "--txn", TXN],
                    probe=EvidenceProbe(), clock=lambda: at("17:30:00"), post=sent)
    printed = json.loads(capsys.readouterr().out)
    assert code == 0 and printed["verdict"] == "BLOCKED"       # the failure is still undelivered
    assert sent.sent == [] and tree(tmp_path) == before


def test_evidence_mode_refuses_a_malformed_transaction_and_misplaced_options(tmp_path, capsys):
    cfg, path = evidence_setup(tmp_path)
    assert cli.main(["shutdown-evidence", "--config", path, "--txn", "../x"],
                    probe=EvidenceProbe()) == 2
    with pytest.raises(SystemExit):
        cli.main(["status", "--config", path, "--txn", TXN])
    with pytest.raises(SystemExit):
        cli.main(["liveness", "--config", path, "--api-stopped"])


def test_evidence_through_the_real_entry_point_with_an_inherited_descriptor(tmp_path):
    cfg, path = evidence_setup(tmp_path, closed=(DAY,))
    fd = os.open(tmp_path / "records" / "lock", os.O_RDWR)
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        out = subprocess.run([sys.executable, "-I", str(OBSERVER / "observer.py"),
                              "shutdown-evidence", "--config", path, "--txn", TXN,
                              "--lock-fd", str(fd)], pass_fds=(fd,), capture_output=True,
                             text=True, timeout=60)
    finally:
        os.close(fd)
    record = json.loads(out.stdout)
    assert out.returncode == 0 and record["lock"]["mode"] == "inherited"
    # This machine has no QuantDesk API unit, so the real probe cannot bind one: UNKNOWN.
    assert record["verdict"] == "UNKNOWN" and record["api"] is None


def test_evidence_never_creates_the_observer_lock(tmp_path):
    cfg = config(tmp_path, units={"api": "quantdesk-api.service"}, closed=(DAY,))
    path = tmp_path / "observer.json"
    path.write_text(json.dumps(cfg))
    record = collect(tmp_path, cfg, str(path))
    assert record["verdict"] == "UNKNOWN" and not (tmp_path / "records" / "lock").exists()
