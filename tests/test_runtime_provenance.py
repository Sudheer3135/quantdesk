"""API-initialization provenance, kept apart from row-time `code_id`.

Measured 01-Oct-2026: one API process, started at 09:16 and never restarted,
wrote signal rows labelled `51c1dc0`, five `51c1dc0+dirty.*` and `12c2935`,
because `code_id` reads the repository each time a row is written and the
repository was edited and committed around the running process. That value
is right for what it says — the disk at write time — and is kept as it is.

These tests hold the second, separate value to its own contract: sampled
once as the API initializes, unchanged for the run whatever the disk does,
carried by each observation from the process that produced it, and copied
into Phase 3B evidence from that observation, never from whoever reads it.
"""
# ruff: noqa: F811
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app import runtime_provenance as rp
from app.analytics.signal_engine import Signal
from app.api import signals as signals_api
from app.api import strategy_v2 as api
from app.api.signals import Analysis, new_observation
from app.backtest import measurement
from app.db import get_db
from app.models import PaperDecision, SignalRecord
from app.strategy_v2 import evidence
from test_v2_evidence import last_evidence, last_row, observed
from test_v2_paper import desk  # noqa: F401  (the fixture)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

STATE_A = {"git_commit": "a" * 40, "dirty_worktree": False, "dirty_diff_sha256": None}
STATE_B = {"git_commit": "b" * 40, "dirty_worktree": False, "dirty_diff_sha256": None}


@pytest.fixture(autouse=True)
def no_inherited_snapshot():
    rp._reset_for_tests()
    yield
    rp._reset_for_tests()


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "backend" / "app").mkdir(parents=True)
    (root / "frontend" / "src").mkdir(parents=True)
    (root / "backend" / "app" / "m.py").write_text("x = 1\n")
    (root / "frontend" / "src" / "App.jsx").write_text("a\n")
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.invalid")
    git(root, "config", "user.name", "t")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "A")
    return root


def row_code_id(repo):
    """The row-time value, exactly as `provenance_columns` computes it."""
    return measurement.code_id(measurement.git_state(repo))


# ---- A. one snapshot per run, while row-time code_id moves -----------------------

def test_the_initialization_snapshot_holds_while_row_time_code_id_moves(repo):
    snapshot = rp.initialize(state_fn=lambda: measurement.git_state(repo))
    rows = [row_code_id(repo)]

    (repo / "frontend" / "src" / "App.jsx").write_text("a\nedited\n")       # frontend edit
    assert rp.current() == snapshot
    rows.append(row_code_id(repo))

    (repo / "frontend" / "src" / "new.test.jsx").write_text("t\n")           # untracked file
    assert rp.current() == snapshot
    rows.append(row_code_id(repo))

    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "B")                                          # later commit
    assert rp.current() == snapshot
    rows.append(row_code_id(repo))

    assert snapshot["status"] == rp.CAPTURED
    assert snapshot["code_id_at_init"] == rows[0]
    assert len(set(rows)) == 4, rows                 # row-time code_id moved every time
    assert "+dirty." in rows[1] and "+dirty." in rows[2] and "+dirty." not in rows[3]


def test_a_returned_snapshot_cannot_change_the_held_one():
    rp.initialize(state_fn=lambda: STATE_A, pid=4242)
    taken = rp.current()
    taken["repo_state"]["git_commit"] = "tampered"
    taken["status"] = "tampered"
    assert rp.current()["repo_state"]["git_commit"] == "a" * 40
    assert rp.current()["status"] == rp.CAPTURED


def test_the_snapshot_states_its_bounded_meaning():
    snap = rp.initialize(state_fn=lambda: STATE_A, pid=4242,
                         now=datetime(2026, 10, 5, 3, 30, tzinfo=UTC))
    assert snap["schema"] == "api_init_provenance/1"
    assert snap["pid"] == 4242
    assert snap["initialized_at"] == "2026-10-05T03:30:00+00:00"
    assert snap["repo_state"] == {**STATE_A, "note": None}
    basis = snap["basis"]
    assert "when this API application initialized" in basis
    assert "not proof of the Python code image" in basis
    assert "not a unique runtime identity" in basis
    assert list(snap) == list(rp.FIELDS), "a fixed, declared field order"


# ---- B. a new initialization reads the repository as it is then ------------------

def test_a_new_initialization_captures_the_current_repository(repo):
    first = rp.initialize(state_fn=lambda: measurement.git_state(repo),
                          now=datetime(2026, 10, 5, 3, 30, tzinfo=UTC))
    (repo / "frontend" / "src" / "App.jsx").write_text("b\n")
    git(repo, "commit", "-qam", "B")

    second = rp.initialize(state_fn=lambda: measurement.git_state(repo),
                           now=datetime(2026, 10, 6, 3, 30, tzinfo=UTC))

    assert second["code_id_at_init"] == row_code_id(repo) != first["code_id_at_init"]
    assert second["initialized_at"] > first["initialized_at"]
    assert rp.current() == second
    # Nothing here assumes the OS gave a second run a different pid.


def test_the_lifespan_takes_the_snapshot_before_any_writer_starts(monkeypatch):
    from app import main

    order: list[str] = []

    def note(name, result=None):
        def call(*a, **k):
            order.append(name)
            return result
        return call

    class Lease:
        def acquire(self):
            order.append("lease")
            return self

    monkeypatch.setattr(main.runtime_provenance, "initialize", note("provenance"))
    monkeypatch.setattr(main, "verify_startup", note("verify"))
    monkeypatch.setattr(main, "check_supervisor", note("supervisor"))
    monkeypatch.setattr(main, "writer_lease", lambda *a, **k: Lease())
    monkeypatch.setattr(main, "init_db", note("init_db"))
    monkeypatch.setattr(main.agent, "start", note("scheduler", object()))
    for module, name in ((main.ticker, "start"), (main.option_collector, "start"),
                         (main.watchdog, "attach"), (main.angel_feed, "start"),
                         (main.chain_publisher, "start"), (main.v2_paper, "start")):
        monkeypatch.setattr(module, name, note(f"{module.__name__}.{name}"))
    monkeypatch.setattr(main, "release_after_drain", note("drain"))

    async def run_twice():
        for _ in range(2):
            async with main.lifespan(main.app):
                pass

    asyncio.run(run_twice())

    first_run = order[:order.index("drain") + 1]
    assert first_run[0] == "provenance"
    assert first_run.index("provenance") < first_run.index("scheduler")
    assert order.count("provenance") == 2, "each initialization takes its own sample"


# ---- C. a dirty start stays dirty -------------------------------------------------

def test_a_dirty_start_is_recorded_as_dirty(repo):
    (repo / "frontend" / "src" / "App.jsx").write_text("uncommitted\n")
    state = measurement.git_state(repo)

    snap = rp.initialize(state_fn=lambda: measurement.git_state(repo))

    assert snap["status"] == rp.CAPTURED
    assert snap["repo_state"]["dirty_worktree"] is True
    assert snap["repo_state"]["dirty_diff_sha256"] == state["dirty_diff_sha256"]
    assert snap["code_id_at_init"] == measurement.code_id(state)
    assert "+dirty." in snap["code_id_at_init"], "never a bare commit"


# ---- D. unavailable is said, never filled in later ---------------------------------

def test_git_failing_at_initialization_is_recorded_as_unavailable():
    def broken():
        raise OSError("no git here")

    snap = rp.initialize(state_fn=broken, pid=7)
    assert snap["status"] == rp.GIT_UNAVAILABLE
    assert snap["code_id_at_init"] is None
    assert snap["repo_state"]["git_commit"] == measurement.GIT_UNAVAILABLE
    assert snap["pid"] == 7 and snap["initialized_at"]


def test_a_directory_that_is_not_a_repository_is_unavailable(tmp_path):
    snap = rp.initialize(state_fn=lambda: measurement.git_state(tmp_path))
    assert snap["status"] == rp.GIT_UNAVAILABLE
    assert snap["code_id_at_init"] is None


def test_unavailable_or_uncaptured_provenance_is_never_resampled(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("the repository must not be read after initialization")

    # Never initialized: nothing is taken, and the repository is never read.
    monkeypatch.setattr(measurement, "git_state", forbidden)
    for _ in range(3):
        snap = rp.current()
        new_observation({})
    assert snap["status"] == rp.NOT_CAPTURED
    assert snap["repo_state"] is None and snap["pid"] is None
    assert snap["initialized_at"] is None and snap["code_id_at_init"] is None

    # Initialized while git was down: it stays down for the run.
    calls = []

    def down():
        calls.append(1)
        raise OSError("down")

    rp.initialize(state_fn=down)
    for _ in range(3):
        assert rp.current()["status"] == rp.GIT_UNAVAILABLE
        assert new_observation({})["api_init_provenance"]["status"] == rp.GIT_UNAVAILABLE
    assert calls == [1]


@pytest.mark.parametrize("variant", ["v1_absent", "captured", "git_unavailable",
                                     "not_captured"])
def test_provenance_cannot_change_a_trading_decision(desk, variant):
    """Same signal, any producer provenance — the same full decision.

    Each variant on its own desk, so an entry by one cannot block another.
    The /1 observation is the baseline the desk decided on before this
    change; every other variant must reach exactly the same place."""
    if variant == "captured":
        extra = {"api_init_provenance": rp.initialize(state_fn=lambda: STATE_A)}
    elif variant == "git_unavailable":
        extra = {"api_init_provenance": rp.initialize(
            state_fn=lambda: {"git_commit": measurement.GIT_UNAVAILABLE})}
    elif variant == "not_captured":
        extra = {"api_init_provenance": rp.current()}
    else:
        extra = {}
    sig = observed()
    sig["observation"].update(extra)
    desk.signal = sig
    trader = desk.trader()
    trader.step()

    row = last_row(desk)
    assert (row.action, row.outcome, row.code) == ("BUY", "entered", "entered")
    recorded = last_evidence(desk)["vector"]["versions"]["api_init_provenance"]
    expected = {"v1_absent": "absent", "captured": rp.CAPTURED,
                "git_unavailable": rp.GIT_UNAVAILABLE, "not_captured": rp.NOT_CAPTURED}
    assert recorded["status"] == expected[variant]


# ---- E. both producer paths persist and publish the same snapshot -----------------

def _signal_with_provenance():
    now = datetime.now(UTC).isoformat()
    return Signal(symbol="NIFTY", timeframe="5m", timestamp=now, action="BUY",
                  confidence=0.6, price=24200.0, entry=24200.0, stop_loss=24190.0,
                  target=24225.0, risk_reward=2.5, checks=[],
                  context={"timing": {"decision_at": now, "bar_open_time": now},
                           "provenance": {"strategy_version": "s", "parameter_hash": "p",
                                          "input_fingerprint": "f"}})


def test_the_agent_path_persists_and_publishes_the_producer_snapshot(db, monkeypatch):
    from app.config import get_settings
    from app.workers import agent
    from test_risk_on_live_path import SessionFactory

    snapshot = rp.initialize(state_fn=lambda: STATE_A, pid=1111)
    sent = {}
    monkeypatch.setenv("ARCHIVE_CANDLES", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(agent, "SessionLocal", SessionFactory(db))
    monkeypatch.setattr(agent, "publish",
                        lambda ch, blob, **k: sent.setdefault("p", json.loads(blob)) or True)
    sig = _signal_with_provenance()
    monkeypatch.setattr(agent, "build_analysis", lambda *a, **k: Analysis(signal=sig, plan=None))

    agent.tick(force=True)

    obs = sent["p"]["observation"]
    row = db.get(SignalRecord, obs["signal_id"])
    assert obs["schema"] == "signal_observation/2"
    assert obs["api_init_provenance"] == snapshot
    assert row.provenance["api_init_provenance"] == snapshot
    # Row-time code_id is still its own value, computed as before.
    assert obs["code_id"] == row.code_id
    assert obs["code_id_basis"] == "git state on disk when the row was written"


def test_the_endpoint_path_persists_and_returns_the_producer_snapshot(db, monkeypatch):
    snapshot = rp.initialize(state_fn=lambda: STATE_B, pid=2222)
    sig = _signal_with_provenance()
    monkeypatch.setattr(signals_api, "build_analysis",
                        lambda *a, **k: Analysis(signal=sig, plan=None))
    app = FastAPI()
    app.include_router(signals_api.router)
    app.dependency_overrides[get_db] = lambda: db

    body = TestClient(app).get("/signals/live?persist=true").json()
    row = db.scalars(select(SignalRecord)).one()

    assert body["observation"]["schema"] == "signal_observation/2"
    assert body["observation"]["api_init_provenance"] == snapshot
    assert row.provenance["api_init_provenance"] == snapshot
    assert body["observation"]["observation_id"] == row.provenance["observation_id"]


def _served_evidence(desk):
    app = FastAPI()
    app.include_router(api.router)

    def session():
        with desk.session() as db:
            yield db
    app.dependency_overrides[get_db] = session
    client = TestClient(app)
    decision = last_row(desk)
    return client.get(f"{api.router.prefix}/decisions/{decision.id}/evidence").json()


def _published(observation: dict, signal_id: int) -> dict:
    """A signal as the agent publishes it, carrying a real observation."""
    sig = observed(observation_id=observation["observation_id"], signal_id=signal_id)
    sig["observation"] = {**json.loads(json.dumps(observation)), "signal_id": signal_id,
                          "published_at": sig["observation"]["published_at"]}
    return sig


def test_evidence_carries_the_producer_snapshot_through_the_api(desk):
    snapshot = rp.initialize(state_fn=lambda: STATE_A, pid=3333)
    desk.signal = _published(new_observation({"provenance": {}}), 77)
    desk.trader().step()

    stored = last_evidence(desk)
    served = _served_evidence(desk)["evidence"]
    assert stored["schema"] == "v2_decision_evidence/2"
    assert stored["vector"]["versions"]["evidence_schema"] == "v2_decision_evidence/2"
    assert stored["vector"]["identity"]["observation_schema"] == "signal_observation/2"
    assert stored["vector"]["versions"]["api_init_provenance"] == snapshot
    assert served["vector"]["versions"]["api_init_provenance"] == snapshot
    assert stored["status"] == evidence.COMPLETE
    assert stored["diagnostics"]["rejected_predictors"] == []


# ---- F. an older observation keeps its own producer's snapshot ---------------------

def test_an_observation_from_before_a_restart_keeps_its_producer(desk):
    producer = rp.initialize(state_fn=lambda: STATE_A, pid=1)
    old = new_observation({"provenance": {}})

    consumer = rp.initialize(state_fn=lambda: STATE_B, pid=2)        # "after a restart"
    assert consumer != producer

    desk.signal = _published(old, 5)
    desk.trader().step()

    recorded = last_evidence(desk)["vector"]["versions"]["api_init_provenance"]
    assert recorded == producer
    assert recorded["code_id_at_init"] == "a" * 12
    assert new_observation({})["api_init_provenance"] == consumer     # new ones get B


# ---- G. structured, validated, and not reachable afterwards ------------------------

def test_the_structure_survives_and_later_mutation_cannot_reach_it(desk):
    snapshot = rp.initialize(state_fn=lambda: {**STATE_A, "dirty_worktree": True,
                                               "dirty_diff_sha256": "d" * 64}, pid=9)
    sig = _published(new_observation({"provenance": {}}), 6)
    desk.signal = sig
    desk.trader().step()
    before = json.dumps(last_evidence(desk), sort_keys=True)

    sig["observation"]["api_init_provenance"]["repo_state"]["git_commit"] = "changed"
    sig["observation"]["api_init_provenance"]["status"] = "changed"

    after = last_evidence(desk)
    assert json.dumps(after, sort_keys=True) == before
    recorded = after["vector"]["versions"]["api_init_provenance"]
    assert recorded == snapshot
    assert isinstance(recorded["repo_state"], dict), "nested, not flattened or dropped"
    assert recorded["repo_state"]["dirty_diff_sha256"] == "d" * 64


def test_undeclared_or_nested_values_make_the_block_invalid():
    rp.initialize(state_fn=lambda: STATE_A, pid=9)
    src = rp.current()
    src["extra"] = "x"
    src["repo_state"]["git_commit"] = {"nested": True}
    src["repo_state"]["surprise"] = 1
    rejected: list[dict] = []

    out = evidence.producer_provenance({"api_init_provenance": src}, rejected)

    assert out["status"] == "invalid" and "set aside" in out["reason"]
    assert set(out) == {"status", "reason"}, "nothing of the malformed block is copied"
    paths = {r["path"] for r in rejected}
    assert {"versions.api_init_provenance",
            "versions.api_init_provenance.repo_state",
            "versions.api_init_provenance.repo_state.git_commit"} <= paths


def test_extraction_is_its_own_copy_before_anything_is_serialised():
    """Copy isolation on its own: no persistence, no JSON round trip."""
    rp.initialize(state_fn=lambda: {**STATE_A, "dirty_worktree": True,
                                    "dirty_diff_sha256": "d" * 64}, pid=9)
    obs = {"api_init_provenance": rp.current()}
    out = evidence.producer_provenance(obs, [])
    expected = rp.current()
    assert out == expected

    src = obs["api_init_provenance"]
    assert out is not src and out["repo_state"] is not src["repo_state"]
    src["status"] = "changed"
    src["repo_state"]["git_commit"] = "changed"
    src["repo_state"]["dirty_diff_sha256"] = "e" * 64
    obs["api_init_provenance"] = None

    assert out == expected


# ---- adversarial blocks through the real Recorder.record() path -------------------

def _record(block, *, present=True):
    """One envelope from Recorder.record(), with `block` as the observation's
    provenance — a cheap kill-switch refusal, so the decision is fixed."""
    from app.strategy_v2 import rules
    from app.strategy_v2.config import V2Config

    sig = observed()
    if present:
        sig["observation"]["schema"] = "signal_observation/2"
        sig["observation"]["api_init_provenance"] = block
    cap = evidence.Capture(consumed_at=datetime(2026, 9, 17, 5, 0, tzinfo=UTC),
                           killed=True, failed_gate="kill_switch")
    rec = evidence.Recorder(V2Config(), strategy="v2", version="t")
    return rec.record(sig, cap, outcome="rejected", code=rules.KILL_SWITCH)


def _valid(**over):
    rp.initialize(state_fn=lambda: STATE_A, pid=4242,
                  now=datetime(2026, 10, 5, 3, 30, tzinfo=UTC))
    block = rp.current()
    for key, value in over.items():
        if key.startswith("repo_"):
            block["repo_state"][key[5:]] = value
        else:
            block[key] = value
    return block


def _dropped(key):
    block = _valid()
    if key.startswith("repo_"):
        del block["repo_state"][key[5:]]
    else:
        del block[key]
    return block


@pytest.mark.parametrize("case", [
    "empty_captured", "unsupported_schema", "numeric_commit", "short_commit",
    "string_dirty_flag", "nonnumeric_pid", "bool_pid", "zero_pid",
    "invalid_timestamp", "naive_timestamp", "numeric_timestamp",
    "repo_state_not_object", "repo_state_missing_field", "repo_state_extra_field",
    "nested_repo_value", "missing_top_field", "unknown_status",
    "code_id_unrelated", "code_id_missing", "dirty_without_fingerprint",
    "clean_with_fingerprint", "bad_fingerprint", "captured_but_unavailable_commit",
    "git_unavailable_with_commit", "git_unavailable_with_code_id",
    "git_unavailable_without_pid", "git_unavailable_string_dirty",
    "not_captured_with_pid", "not_captured_with_repo",
    "not_captured_without_reason", "missing_basis",
])
def test_malformed_provenance_is_invalid_through_recorder(case):
    unavailable = {**_valid(), "status": rp.GIT_UNAVAILABLE, "code_id_at_init": None,
                   "reason": "git state was unavailable when the API initialized",
                   "repo_state": {"git_commit": "unavailable", "dirty_worktree": None,
                                  "dirty_diff_sha256": None, "note": "x"}}
    never = {**{k: None for k in rp.FIELDS}, "schema": rp.SCHEMA,
             "status": rp.NOT_CAPTURED, "basis": rp.BASIS, "reason": "never taken"}
    block = {
        "empty_captured": {"status": "captured"},
        "unsupported_schema": _valid(schema="api_init_provenance/9"),
        "numeric_commit": _valid(repo_git_commit=1234567),
        "short_commit": _valid(repo_git_commit="a" * 12, code_id_at_init="a" * 12),
        "string_dirty_flag": _valid(repo_dirty_worktree="false"),
        "nonnumeric_pid": _valid(pid="4242"),
        "bool_pid": _valid(pid=True),
        "zero_pid": _valid(pid=0),
        "invalid_timestamp": _valid(initialized_at="yesterday morning"),
        "naive_timestamp": _valid(initialized_at="2026-10-05T03:30:00"),
        "numeric_timestamp": _valid(initialized_at=1759635000),
        "repo_state_not_object": _valid(repo_state="a" * 40),
        "repo_state_missing_field": _dropped("repo_note"),
        "repo_state_extra_field": _valid(repo_branch="main"),
        "nested_repo_value": _valid(repo_note={"x": 1}),
        "missing_top_field": _dropped("initialized_at"),
        "unknown_status": _valid(status="verified"),
        "code_id_unrelated": _valid(code_id_at_init="b" * 12),
        "code_id_missing": _valid(code_id_at_init=None),
        "dirty_without_fingerprint": _valid(repo_dirty_worktree=True),
        "clean_with_fingerprint": _valid(repo_dirty_diff_sha256="d" * 64),
        "bad_fingerprint": _valid(repo_dirty_worktree=True, repo_dirty_diff_sha256="xyz",
                                  code_id_at_init="a" * 12 + "+dirty.xyz"),
        "captured_but_unavailable_commit": _valid(repo_git_commit="unavailable"),
        "git_unavailable_with_commit": {**unavailable,
                                        "repo_state": {**unavailable["repo_state"],
                                                       "git_commit": "a" * 40}},
        "git_unavailable_with_code_id": {**unavailable, "code_id_at_init": "a" * 12},
        "git_unavailable_without_pid": {**unavailable, "pid": None},
        "git_unavailable_string_dirty": {**unavailable,
                                         "repo_state": {**unavailable["repo_state"],
                                                        "dirty_worktree": "no"}},
        "not_captured_with_pid": {**never, "pid": 4242},
        "not_captured_with_repo": {**never, "repo_state": dict(STATE_A, note=None)},
        "not_captured_without_reason": {**never, "reason": None},
        "missing_basis": _valid(basis=""),
    }[case]

    env = _record(block)

    recorded = env["vector"]["versions"]["api_init_provenance"]
    assert recorded["status"] == "invalid", (case, recorded)
    assert recorded["reason"]
    assert set(recorded) == {"status", "reason"}
    reasons = [r for r in env["diagnostics"]["rejected_predictors"]
               if r["path"].startswith("versions.api_init_provenance")]
    assert reasons, case
    # Readable, and the decision it describes is untouched.
    assert env["schema"] == "v2_decision_evidence/2"
    from app.strategy_v2 import rules
    assert env["vector"]["decision_context"]["code"] == rules.KILL_SWITCH
    assert env["status"] == evidence.COMPLETE
    json.dumps(env, allow_nan=False)


@pytest.mark.parametrize("value", [0, 1, 0.0, 1.0])
@pytest.mark.parametrize("status", [rp.CAPTURED, rp.GIT_UNAVAILABLE])
def test_a_number_is_not_a_dirty_flag_through_recorder(status, value):
    """1 == True and 0.0 == False in Python; neither is a boolean here."""
    if status == rp.CAPTURED:
        # Fingerprint chosen to agree with the value's truthiness, so the only
        # thing wrong is the flag's type.
        block = _valid(repo_dirty_worktree=value,
                       repo_dirty_diff_sha256="d" * 64 if value else None)
        if value:
            block["code_id_at_init"] = "a" * 12 + "+dirty." + "d" * 12
    else:
        block = {**_valid(), "status": rp.GIT_UNAVAILABLE, "code_id_at_init": None,
                 "reason": "git state was unavailable when the API initialized",
                 "repo_state": {"git_commit": "unavailable", "dirty_worktree": value,
                                "dirty_diff_sha256": None, "note": "x"}}

    env = _record(block)

    recorded = env["vector"]["versions"]["api_init_provenance"]
    assert recorded["status"] == "invalid", (status, value, recorded)
    assert "dirty_worktree" in recorded["reason"]
    assert [r for r in env["diagnostics"]["rejected_predictors"]
            if r["path"] == "versions.api_init_provenance"
            and "dirty_worktree" in r["reason"]]


@pytest.mark.parametrize("status", ["captured", "captured_dirty", "git_unavailable",
                                    "git_raised", "not_captured", "absent"])
def test_legitimate_provenance_stays_valid_through_recorder(status, tmp_path):
    if status == "captured":
        block = rp.initialize(state_fn=lambda: STATE_A)
    elif status == "captured_dirty":
        block = rp.initialize(state_fn=lambda: {**STATE_A, "dirty_worktree": True,
                                                "dirty_diff_sha256": "d" * 64})
    elif status == "git_unavailable":
        block = rp.initialize(state_fn=lambda: measurement.git_state(tmp_path))
    elif status == "git_raised":
        def broken():
            raise OSError("no git")
        block = rp.initialize(state_fn=broken)
    else:
        block = rp.current()                                  # never initialized

    env = _record(block, present=status != "absent")

    recorded = env["vector"]["versions"]["api_init_provenance"]
    expected = {"captured": rp.CAPTURED, "captured_dirty": rp.CAPTURED,
                "git_unavailable": rp.GIT_UNAVAILABLE, "git_raised": rp.GIT_UNAVAILABLE,
                "not_captured": rp.NOT_CAPTURED, "absent": "absent"}[status]
    assert recorded["status"] == expected
    if status != "absent":
        assert recorded == block
    assert not [r for r in env["diagnostics"]["rejected_predictors"]
                if r["path"].startswith("versions.api_init_provenance")]


# ---- H. what was written before stays as it was ------------------------------------

def test_a_v1_observation_is_read_and_recorded_as_absent(desk):
    rp.initialize(state_fn=lambda: STATE_B, pid=2)          # the consumer has one; unused
    desk.signal = observed()                                 # signal_observation/1
    desk.trader().step()

    ev = last_evidence(desk)
    recorded = ev["vector"]["versions"]["api_init_provenance"]
    assert recorded["status"] == "absent"
    assert "signal_observation/1" in recorded["reason"]
    assert ev["vector"]["identity"]["observation_schema"] == "signal_observation/1"
    assert ev["vector"]["identity"]["linkage"] == "linked"
    assert ev["status"] == evidence.COMPLETE


def test_a_v1_evidence_envelope_is_served_unchanged(desk):
    v1 = {"schema": "v2_decision_evidence/1", "status": "complete", "issues": [],
          "vector": {"versions": {"evidence_schema": "v2_decision_evidence/1",
                                  "code_id": "51c1dc0a3ae1",
                                  "code_id_basis": "git state on disk when the row was "
                                                   "written"}}}
    with desk.session() as db:
        db.add(PaperDecision(strategy=api.NAME, decided_at=datetime(2026, 10, 1, 5, 41, tzinfo=UTC),
                             session_date=datetime(2026, 10, 1).date(), action="SELL",
                             outcome="rejected", code="no_two_sided_quote",
                             detail={"evidence": v1}))
        db.commit()

    assert _served_evidence(desk)["evidence"] == v1


def test_a_v1_signal_row_is_read_unchanged(db):
    db.add(SignalRecord(symbol="NIFTY", timeframe="5m", action="SELL", confidence=0.7,
                        price=1.0, code_id="51c1dc0a3ae1+dirty.c6e3381bdfa2",
                        provenance={"code_id": "51c1dc0a3ae1+dirty.c6e3381bdfa2",
                                    "observation_id": "obs-old"}))
    db.commit()
    row = db.scalars(select(SignalRecord)).one()
    assert row.code_id == "51c1dc0a3ae1+dirty.c6e3381bdfa2"
    assert "api_init_provenance" not in row.provenance
