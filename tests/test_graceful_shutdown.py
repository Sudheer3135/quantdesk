"""stop.sh gives the API its whole drain, and stops nothing it depends on
until it is gone (Pass 2E-B).

The reproduction: stop.sh sent SIGTERM, waited 10 s and sent SIGKILL, while
the API's verified shutdown may drain writers for up to 120 s — and it
stopped Redis and PostgreSQL next regardless. Worse, uvicorn waited without
limit for the dashboard's WebSocket to finish before it even began the
lifespan shutdown, so the drain rarely started at all.

These tests run the real scripts/stop.sh and the real `start_api` from
scripts/native/env.sh, copied into a scratch tree whose env.sh points at a
stand-in API (tests/shutdown_harness.py, the real drain) and at stub
`pg_ctl` / `redis-cli` that record whether the API was still alive when they
were asked to stop. Deadlines are seconds, not minutes: the policy comes
from the same Settings fields, set through the environment.
"""
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell scripts")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


STUB = """#!/bin/sh
alive=0; kill -0 "$(cat '{api_pid}' 2>/dev/null)" 2>/dev/null && alive=1
echo "{name} $* api_alive=$alive" >> '{deps}'
{extra}
exit 0
"""
# pg_ctl stop can be made slow (QD_TEST_DEP_DELAY seconds), holding stop.sh
# in the window after the API is gone and before its database is.
PG_CTL_EXTRA = """case "$*" in *" stop"*) sleep "${QD_TEST_DEP_DELAY:-0}";
  echo "pg_ctl stop finished" >> '%s';; esac"""
# A listener on a port, for the dashboard and Redis stand-ins.
LISTEN = ("import socket,sys,time;s=socket.socket();"
          "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
          "s.bind(('127.0.0.1',int(sys.argv[1])));s.listen();time.sleep(3600)")


class Desk:
    """A scratch copy of the scripts, pointed at the stand-in API.

    Shutdown settings come either from the environment (grace/drain/margin)
    or from a `.env` in the scratch project root (`dotenv`), which is where
    the API reads it from."""

    def __init__(self, tmp: Path, *, grace: float | None = None, drain: float | None = None,
                 margin: float | None = None, dotenv: dict | None = None,
                 writer_seconds: float = 0.2, stuck: bool = False):
        self.tmp = tmp
        (tmp / "scripts" / "native").mkdir(parents=True)
        for rel in ("scripts/start.sh", "scripts/stop.sh", "scripts/native/env.sh"):
            shutil.copy(ROOT / rel, tmp / rel)
        for d in ("bin", "run", "logs", ".venv/bin", "backend/app",
                  "frontend/node_modules/.bin"):
            (tmp / d).mkdir(parents=True)
        self.marks, self.deps, self.api_pid = tmp / "marks", tmp / "deps", tmp / "api.pid"
        self.api_port, self.redis_port, self.web_port = _free_port(), _free_port(), _free_port()
        (tmp / ".env").write_text("".join(f"{k}={v}\n" for k, v in (dotenv or {}).items()))

        # A listener standing in for Redis, closed by the redis-cli stub.
        self.redis = subprocess.Popen([sys.executable, "-c", LISTEN, str(self.redis_port)])
        stubs = {
            "pg_ctl": PG_CTL_EXTRA % self.deps,
            "redis-cli": f"kill {self.redis.pid}",
            "redis-server": "",                       # start.sh finds Redis already up
            "docker": "",                             # no Docker QuantDesk running
        }
        for name, extra in stubs.items():
            path = tmp / "bin" / name
            path.write_text(STUB.format(name=name, api_pid=self.api_pid, deps=self.deps,
                                        extra=extra))
            path.chmod(0o755)
        # start.sh's schema check and dashboard, as stand-ins: the schema
        # check passes unless QD_TEST_SCHEMA_FAIL=1; the dashboard listens.
        (tmp / ".venv" / "bin" / "python").symlink_to(sys.executable)
        (tmp / "backend" / "app" / "__init__.py").write_text("")
        (tmp / "backend" / "app" / "schema_check.py").write_text(
            "import os, sys\nprint('schema stand-in: ok')\n"
            "sys.exit(1 if os.environ.get('QD_TEST_SCHEMA_FAIL') == '1' else 0)\n")
        (tmp / "listen.py").write_text(LISTEN)
        vite = tmp / "frontend" / "node_modules" / ".bin" / "vite"
        vite.write_text(f"#!/bin/sh\necho $$ > '{tmp}/vite.pid'\n"
                        f"exec '{sys.executable}' '{tmp}/listen.py' {self.web_port}\n")
        vite.chmod(0o755)

        with (tmp / "scripts" / "native" / "env.sh").open("a") as f:
            f.write(f"""
# --- test overrides ---
PG_BIN='{tmp}/bin'
PG_DATA='{tmp}/pgdata'
REDIS_PORT={self.redis_port}
API_PORT={self.api_port}
WEB_PORT={self.web_port}
RUN_DIR='{tmp}/run'
LOG_DIR='{tmp}/logs'
PYTHON='{sys.executable}'
UVICORN='{Path(sys.executable).parent / "uvicorn"}'
BACKEND_DIR='{BACKEND}'
API_APP='shutdown_harness:app'
PATH='{tmp}/bin':"$PATH"
export DATABASE_URL='sqlite:///{tmp}/unused.db'
export REDIS_URL='redis://127.0.0.1:{self.redis_port}/0'
""")
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("SHUTDOWN_", "QUANTDESK_STOP_"))}
        for key, value in (("SHUTDOWN_CONNECTION_GRACE_SECONDS", grace),
                           ("SHUTDOWN_DRAIN_SECONDS", drain),
                           ("SHUTDOWN_EXIT_MARGIN_SECONDS", margin)):
            if value is not None:
                self.env[key] = str(value)
        self.env.update({
                    "QD_TEST_MARKS": str(self.marks),
                    "QD_TEST_WRITER_SECONDS": str(writer_seconds),
                    "QD_TEST_STUCK": "1" if stuck else "0",
                    "PYTHONPATH": os.pathsep.join([str(BACKEND), str(ROOT / "tests")]),
                    "PYTHONDONTWRITEBYTECODE": "1"})

    def script(self, name: str, **env) -> subprocess.Popen:
        """Run the real scripts/<name> in the background."""
        return subprocess.Popen(["bash", str(self.tmp / "scripts" / name)],
                                env={**self.env, **env}, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)

    def lock_free(self, within: float = 3.0) -> bool:
        """Whether the lifecycle lock can be taken within `within` seconds.
        A script's short-lived children (a `docker ps`, a `sleep`) inherit
        the lock's fd and may take a moment to exit after the script does;
        a leaked long-lived process would hold it indefinitely."""
        deadline = time.monotonic() + within
        while True:
            free = subprocess.run(
                [sys.executable, "-c",
                 "import fcntl,sys\nf=open(sys.argv[1],'a')\n"
                 "try: fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)\nexcept OSError: sys.exit(1)",
                 str(self.tmp / "run" / "lifecycle.lock")]).returncode == 0
            if free or time.monotonic() >= deadline:
                return free
            time.sleep(0.1)

    def policy_marks(self) -> dict:
        """What the running stand-in API reported about its own policy."""
        line = next(e for _, e in self.events() if e.startswith("policy "))
        return dict(kv.split("=") for kv in line.split()[1:])

    def recorded_deadline(self) -> str:
        return (self.tmp / "run" / "backend.stop_deadline").read_text().strip()

    def bash(self, script: str, timeout: float = 120) -> subprocess.CompletedProcess:
        source = f"source '{self.tmp}/scripts/native/env.sh'; {script}"
        return subprocess.run(["bash", "-c", source], env=self.env,
                              capture_output=True, text=True, timeout=timeout)

    def start(self) -> int:
        """Launch the stand-in API through the real start_api."""
        run = self.bash("start_api")
        assert run.returncode == 0, run.stdout + run.stderr
        for _ in range(150):
            try:
                if httpx.get(f"http://127.0.0.1:{self.api_port}/health", timeout=1).is_success:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            pytest.fail("stand-in API did not start: " + self.log())
        pids = self.bash("api_pids").stdout.split()
        assert len(pids) == 1, pids
        self.api_pid.write_text(pids[0])
        (self.tmp / "run" / "backend.pid").write_text(pids[0])
        return int(pids[0])

    def hold_socket(self) -> None:
        """Open a dashboard-like WebSocket and keep it open."""
        from websockets.sync.client import connect

        ready = threading.Event()

        def run():
            try:
                with connect(f"ws://127.0.0.1:{self.api_port}/ws/hold", open_timeout=5) as ws:
                    ready.set()
                    ws.recv(timeout=600)
            except Exception:  # noqa: BLE001, S110 — closed by the shutdown
                ready.set()

        threading.Thread(target=run, daemon=True).start()
        assert ready.wait(10)

    def stop(self, timeout: float = 120) -> tuple[subprocess.CompletedProcess, float]:
        t0 = time.monotonic()
        run = subprocess.run(["bash", str(self.tmp / "scripts" / "stop.sh")], env=self.env,
                             capture_output=True, text=True, timeout=timeout)
        return run, time.monotonic() - t0

    def events(self) -> list[tuple[float, str]]:
        out = []
        for line in self.marks.read_text().splitlines():
            stamp, event = line.split(" ", 1)
            out.append((float(stamp), event))
        return out

    def names(self) -> list[str]:
        return [e.split(" ")[0] for _, e in self.events()]

    def deps_lines(self) -> list[str]:
        return self.deps.read_text().splitlines() if self.deps.exists() else []

    def log(self) -> str:
        path = self.tmp / "logs" / "backend.log"
        return path.read_text() if path.exists() else ""

    def diag(self, out: str = "") -> str:
        """Everything needed to read a failure: stop.sh, the stand-in's
        steps, its log and the dependency stubs."""
        marks = self.marks.read_text() if self.marks.exists() else ""
        return (f"\n--- stop.sh\n{out}\n--- marks\n{marks}\n--- deps\n"
                + "\n".join(self.deps_lines()) + f"\n--- backend.log\n{self.log()[-3000:]}")

    def alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def close(self) -> None:
        pids = self.bash("api_pids").stdout.split()
        vite = self.tmp / "vite.pid"
        if vite.exists():
            pids.append(vite.read_text().strip())
        for pid in pids:
            try:
                os.kill(int(pid), 9)
            except (ProcessLookupError, ValueError):
                pass
        self.redis.kill()
        self.redis.wait()


@pytest.fixture
def desk(tmp_path):
    made = []

    def make(**kw) -> Desk:
        d = Desk(tmp_path, **kw)
        made.append(d)
        return d

    yield make
    for d in made:
        d.close()


def _dependencies_stopped_after_the_api(d: Desk) -> None:
    deps = d.deps_lines()
    assert any(line.startswith("redis-cli") and "shutdown" in line for line in deps), deps
    assert any(line.startswith("pg_ctl") and " stop" in line for line in deps), deps
    stops = [line for line in deps if ("shutdown" in line or " stop" in line)
             and "finished" not in line]
    assert all(line.endswith("api_alive=0") for line in stops), deps


# --- The four shutdowns ------------------------------------------------------

def test_a_quick_drain_exits_cleanly_without_sigkill(desk):
    d = desk(grace=1, drain=5, margin=2, writer_seconds=0.3)
    pid = d.start()
    run, took = d.stop()
    out = run.stdout + run.stderr
    assert run.returncode == 0, d.diag(out)
    assert "stopped api" in out and "EMERGENCY" not in out, d.diag(out)
    assert not d.alive(pid)
    assert d.names()[-3:] == ["writer-stopped", "lease-released", "drained"], d.names()
    assert took < 8
    _dependencies_stopped_after_the_api(d)


def test_a_slow_valid_drain_past_the_old_ten_seconds_is_waited_for(desk):
    """The writer needs 12 s — the old stop.sh SIGKILLed at 10 — and a
    dashboard WebSocket is open, which without the connection grace would
    have held uvicorn's shutdown open before the drain ever began."""
    d = desk(grace=1, drain=20, margin=3, writer_seconds=12)
    pid = d.start()
    d.hold_socket()
    run, took = d.stop()
    out = run.stdout + run.stderr
    assert run.returncode == 0, d.diag(out)
    assert "EMERGENCY" not in out and "stopped api" in out, d.diag(out)
    assert not d.alive(pid)
    ev = dict((e.split(" ")[0], t) for t, e in d.events())
    assert ev["writer-stopped"] - ev["writer-stop-called"] >= 11.9   # longer than the old kill
    assert d.names()[-2:] == ["lease-released", "drained"]
    assert took >= 12
    _dependencies_stopped_after_the_api(d)


def test_a_hung_writer_ends_in_the_fail_safe_and_stop_sh_observes_it(desk):
    """The writer never stops: the drain gives up at its deadline, the API
    ends itself (exit 70) without releasing the lease, and stop.sh sees that
    exit — well inside its own deadline, with no SIGKILL."""
    d = desk(grace=1, drain=3, margin=4, writer_seconds=-1)
    pid = d.start()
    run, took = d.stop()
    out = run.stdout + run.stderr
    assert run.returncode == 1, d.diag(out)                   # stopped, but flagged
    assert "fail-safe" in out and "EMERGENCY" not in out, d.diag(out)
    assert not d.alive(pid)
    assert "lease-released" not in d.names() and "drained" not in d.names()
    assert "writers not confirmed drained" in d.log()
    assert 3 <= took < 3 + 1 + 4
    _dependencies_stopped_after_the_api(d)


def test_a_truly_stuck_process_is_killed_only_after_the_whole_deadline(desk):
    d = desk(grace=1, drain=2, margin=2, stuck=True)       # deadline 5 s
    pid = d.start()
    run, took = d.stop()
    out = run.stdout + run.stderr
    assert run.returncode == 1, d.diag(out)
    assert "EMERGENCY" in out and "SIGKILL" in out, d.diag(out)
    assert "api killed after 5s" in out, d.diag(out)
    assert not d.alive(pid)
    assert took >= 5
    assert "shutdown-begins" in d.names() and "drained" not in d.names()
    _dependencies_stopped_after_the_api(d)


def test_a_mac_sleep_mid_shutdown_does_not_cut_the_drain_short(desk):
    """The reproduction for the one failure the full suite showed: the Mac
    slept while stop.sh waited. The API's drain runs on a monotonic clock
    that stands still in sleep; a wall clock does not, so stop.sh woke up
    past its deadline and SIGKILLed a drain that had barely run.

    Sleep is imitated by freezing stop.sh and the API together (SIGSTOP)
    for longer than stop.sh's deadline. A frozen process's monotonic clock
    keeps running, unlike in real sleep, so the API's own drain is given
    room (30 s) and stop.sh's recorded deadline is set below the freeze
    (4 s): what is under test is stop.sh's clock alone. The writer still has
    real work left when both resume; stop.sh must wait for it, not kill on
    waking."""
    d = desk(grace=1, drain=30, margin=2, writer_seconds=3)
    pid = d.start()
    (d.tmp / "run" / "backend.stop_deadline").write_text("4\n")
    proc = subprocess.Popen(["bash", str(d.tmp / "scripts" / "stop.sh")], env=d.env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(200):
        if "writer-stop-called" in d.names():
            break
        time.sleep(0.05)
    time.sleep(0.5)
    os.kill(proc.pid, signal.SIGSTOP)
    os.kill(pid, signal.SIGSTOP)
    time.sleep(8)                                      # longer than its deadline
    os.kill(pid, signal.SIGCONT)
    os.kill(proc.pid, signal.SIGCONT)
    out = proc.communicate(timeout=60)[0]
    assert "EMERGENCY" not in out, d.diag(out)
    assert proc.returncode == 0, d.diag(out)
    assert d.names()[-3:] == ["writer-stopped", "lease-released", "drained"], d.diag(out)
    _dependencies_stopped_after_the_api(d)


def test_dependencies_are_not_touched_while_the_api_drains(desk):
    """Sampled during a drain: stop.sh has not called redis-cli or pg_ctl
    while the API process is alive."""
    d = desk(grace=1, drain=10, margin=2, writer_seconds=6)
    pid = d.start()
    proc = subprocess.Popen(["bash", str(d.tmp / "scripts" / "stop.sh")], env=d.env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    seen_draining = False
    while proc.poll() is None:
        if d.alive(pid):
            seen_draining = seen_draining or "writer-stop-called" in d.names()
            assert not any("shutdown" in x or " stop" in x for x in d.deps_lines()), \
                "a dependency was stopped while the API was still running"
        time.sleep(0.1)
    assert proc.returncode == 0, d.diag(proc.stdout.read())
    assert seen_draining
    _dependencies_stopped_after_the_api(d)


def test_a_draining_api_is_still_found_after_its_socket_closes(desk):
    d = desk(grace=1, drain=10, margin=2, writer_seconds=5)
    d.start()
    proc = d.script("stop.sh")
    _wait_for(d, "writer-stop-called")
    # The port is closed, the process is not: it must still be found.
    assert d.bash("api_pids").stdout.split(), "a draining API was invisible to api_pids"
    proc.wait(timeout=60)


# --- One lifecycle operation at a time ---------------------------------------

def _wait_for(d: Desk, event: str, timeout: float = 20) -> None:
    for _ in range(int(timeout * 20)):
        if event in d.names():
            return
        time.sleep(0.05)
    pytest.fail(f"never saw {event}: " + d.diag())


def _apis(d: Desk) -> list[str]:
    return d.bash("api_pids").stdout.split()


REFUSED = "another QuantDesk lifecycle operation is in progress"


def test_a_start_during_a_stops_drain_is_refused_and_launches_nothing(desk):
    d = desk(grace=1, drain=10, margin=2, writer_seconds=4)
    old = d.start()
    stop = d.script("stop.sh")
    _wait_for(d, "writer-stop-called")
    start = subprocess.run(["bash", str(d.tmp / "scripts" / "start.sh")], env=d.env,
                           capture_output=True, text=True, timeout=60)
    assert start.returncode == 1 and REFUSED in start.stdout, d.diag(start.stdout)
    assert "./scripts/stop.sh" in start.stdout                   # names the holder
    assert stop.wait(timeout=60) == 0, d.diag(stop.stdout.read())
    assert _apis(d) == [] and not d.alive(old)
    # The one API ever launched is the one that was stopped.
    assert sum(1 for _, e in d.events() if e.startswith("started")) == 1


def test_a_start_after_the_api_is_gone_but_before_its_database_is_is_refused(desk):
    """The window Codex found: stop.sh has seen the API exit and is stopping
    PostgreSQL (held here for 4 s). A start then must not launch an API
    whose database is about to be stopped underneath it."""
    d = desk(grace=1, drain=5, margin=2, writer_seconds=0.2)
    old = d.start()
    stop = d.script("stop.sh", QD_TEST_DEP_DELAY="4")
    for _ in range(400):
        if any(" stop " in x or x.endswith(" stop") for x in d.deps_lines()
               if x.startswith("pg_ctl")):
            break
        time.sleep(0.02)
    else:
        pytest.fail("stop.sh never reached the database: " + d.diag())
    assert not d.alive(old)                                       # inside the window
    assert "pg_ctl stop finished" not in d.deps_lines()
    start = subprocess.run(["bash", str(d.tmp / "scripts" / "start.sh")], env=d.env,
                           capture_output=True, text=True, timeout=60)
    assert start.returncode == 1 and REFUSED in start.stdout, d.diag(start.stdout)
    assert stop.wait(timeout=60) == 0, d.diag(stop.stdout.read())
    assert _apis(d) == []
    assert sum(1 for _, e in d.events() if e.startswith("started")) == 1


def test_two_concurrent_starts_launch_one_api(desk):
    d = desk(grace=1, drain=5, margin=2)
    first, second = d.script("start.sh"), d.script("start.sh")
    outs = [first.communicate(timeout=90)[0], second.communicate(timeout=90)[0]]
    codes = sorted([first.returncode, second.returncode])
    assert codes == [0, 1], d.diag("\n=====\n".join(outs))
    assert any(REFUSED in o for o in outs)
    assert len(_apis(d)) == 1
    assert sum(1 for _, e in d.events() if e.startswith("started")) == 1
    d.api_pid.write_text(_apis(d)[0])


def test_two_concurrent_stops_stop_each_dependency_once(desk):
    d = desk(grace=1, drain=5, margin=2, writer_seconds=2)
    old = d.start()
    first, second = d.script("stop.sh"), d.script("stop.sh")
    outs = [first.communicate(timeout=90)[0], second.communicate(timeout=90)[0]]
    codes = sorted([first.returncode, second.returncode])
    assert codes == [0, 1], d.diag("\n=====\n".join(outs))
    assert any(REFUSED in o for o in outs)
    assert not d.alive(old)
    deps = d.deps_lines()
    assert sum(1 for x in deps if x.startswith("redis-cli")) == 1, deps
    assert sum(1 for x in deps if x.startswith("pg_ctl") and " stop" in x
               and "finished" not in x) == 1, deps
    assert d.names().count("lease-released") == 1
    _dependencies_stopped_after_the_api(d)


def test_the_lock_is_released_after_a_normal_start_and_stop(desk):
    """Also that nothing start.sh leaves running — API, dashboard, Redis,
    PostgreSQL — inherited the lock: it is free while they all run."""
    d = desk(grace=1, drain=5, margin=2)
    start = d.script("start.sh")
    out = start.communicate(timeout=90)[0]
    assert start.returncode == 0, d.diag(out)
    d.api_pid.write_text(_apis(d)[0])
    assert d.lock_free(), "the lifecycle lock outlived start.sh: " + d.diag(out)
    stop = d.script("stop.sh")
    out = stop.communicate(timeout=90)[0]
    assert stop.returncode == 0, d.diag(out)
    assert d.lock_free()
    assert _apis(d) == []


def test_the_lock_is_released_when_a_lifecycle_command_fails_or_dies(desk):
    d = desk(grace=1, drain=10, margin=2, writer_seconds=6)
    failed = d.script("start.sh", QD_TEST_SCHEMA_FAIL="1")
    out = failed.communicate(timeout=60)[0]
    assert failed.returncode == 1 and "API not started" in out, d.diag(out)
    assert d.lock_free() and _apis(d) == []
    # A stop killed outright mid-drain: the kernel drops its lock with it.
    d.start()
    stop = d.script("stop.sh")
    _wait_for(d, "writer-stop-called")
    assert not d.lock_free(within=0)
    stop.kill()
    stop.wait()
    assert d.lock_free()


# --- One policy --------------------------------------------------------------

def test_the_policy_is_one_deadline_from_settings():
    from app import migration_guard
    from app.config import Settings
    from app.shutdown_policy import ShutdownPolicyError, policy

    s = Settings(_env_file=None)
    p = policy(s)
    assert s.shutdown_drain_seconds == migration_guard.DRAIN_SECONDS
    assert p.deadline == p.grace + int(s.shutdown_drain_seconds) + p.margin == 145
    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ShutdownPolicyError):
            policy(Settings(_env_file=None, shutdown_drain_seconds=bad))


def test_the_api_refuses_a_policy_longer_than_its_supervisor_waits():
    from app.config import Settings
    from app.shutdown_policy import SUPERVISOR_ENV, ShutdownPolicyError, check_supervisor

    long = Settings(_env_file=None, shutdown_drain_seconds=200)          # deadline 225
    assert check_supervisor(long, {}).deadline == 225                   # no supervisor: ok
    assert check_supervisor(long, {SUPERVISOR_ENV: "225"}).deadline == 225
    for waits in ("224", "150", "abc", ""):
        with pytest.raises(ShutdownPolicyError):
            check_supervisor(long, {SUPERVISOR_ENV: waits})
    with pytest.raises(ShutdownPolicyError):
        check_supervisor(Settings(_env_file=None, shutdown_drain_seconds=0), {})
    main = (BACKEND / "app" / "main.py").read_text()
    assert main.index("check_supervisor(settings)") < main.index("writer_lease(\"api\"")


@pytest.mark.parametrize("dotenv,expected", [
    ({}, 145),                                                           # defaults
    ({"SHUTDOWN_DRAIN_SECONDS": "200"}, 225),                            # drain > 120
    ({"SHUTDOWN_DRAIN_SECONDS": "130", "SHUTDOWN_CONNECTION_GRACE_SECONDS": "3",
      "SHUTDOWN_EXIT_MARGIN_SECONDS": "7"}, 140),
])
def test_the_native_scripts_and_the_api_read_one_policy_from_the_root_env(desk, dotenv,
                                                                         expected):
    """Codex's reproduction: the settings in the project root's .env. The
    deadline start_api records, the one it hands the API, and the API's own
    effective policy must all be that one."""
    d = desk(dotenv=dotenv)
    d.start()
    seen = d.policy_marks()
    assert d.recorded_deadline() == str(expected), d.diag()
    assert seen["deadline"] == str(expected) and seen["supervisor"] == str(expected), seen
    # stop.sh, even without the recorded file, derives the same deadline.
    (d.tmp / "run" / "backend.stop_deadline").unlink()
    run, _ = d.stop()
    assert f"allowing up to {expected}s" in run.stdout, d.diag(run.stdout)


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "nan"])
def test_an_invalid_root_env_policy_launches_nothing(desk, bad):
    d = desk(dotenv={"SHUTDOWN_DRAIN_SECONDS": bad})
    run = d.bash("start_api")
    assert run.returncode == 1 and "cannot read the API shutdown policy" in run.stdout
    assert not (d.tmp / "run" / "backend.stop_deadline").exists()
    time.sleep(1)
    assert _apis(d) == []


def test_the_policy_command_prints_numbers_only():
    run = subprocess.run([sys.executable, "-m", "app.shutdown_policy"], cwd=BACKEND,
                         env={**os.environ, "SHUTDOWN_DRAIN_SECONDS": "7"},
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0
    assert re.fullmatch(r"grace=\d+\ndrain=7\nmargin=\d+\ndeadline=\d+\n", run.stdout)


def test_the_api_drains_for_the_policys_time_and_the_scripts_use_it():
    main = (BACKEND / "app" / "main.py").read_text()
    assert "timeout=settings.shutdown_drain_seconds" in main
    start = (ROOT / "scripts" / "start.sh").read_text()
    assert "start_api" in start and "exec nohup" not in start.split("dashboard")[0]
    env = (ROOT / "scripts" / "native" / "env.sh").read_text()
    assert '--timeout-graceful-shutdown "$API_SHUTDOWN_GRACE"' in env
    stop = (ROOT / "scripts" / "stop.sh").read_text()
    assert "sleep 0.25; done\n    kill -0" not in stop.split("stop_api()")[1]   # no fixed kill


def test_docker_waits_at_least_the_policys_deadline():
    import yaml
    from app.config import Settings
    from app.shutdown_policy import SUPERVISOR_ENV, ShutdownPolicyError, check_supervisor, policy

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    backend = compose["services"]["backend"]
    grace = backend["stop_grace_period"]
    assert grace.endswith("s") and int(grace[:-1]) >= policy(Settings(_env_file=None)).deadline
    assert "--timeout-graceful-shutdown" in backend["command"]
    # Compose cannot compute stop_grace_period from .env; the API is told
    # the same number and refuses a longer policy instead.
    told = backend["environment"][SUPERVISOR_ENV]
    assert told == grace[:-1], "stop_grace_period and the deadline the API is told differ"
    assert check_supervisor(Settings(_env_file=None), {SUPERVISOR_ENV: told}).deadline == 145
    with pytest.raises(ShutdownPolicyError):                  # a .env drain of 200
        check_supervisor(Settings(_env_file=None, shutdown_drain_seconds=200),
                         {SUPERVISOR_ENV: told})
