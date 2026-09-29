"""Writers and migrations exclude each other in PostgreSQL (Pass 2E-A.1).

Races, not static conditions: real connections, real processes, a real API
on a port that is not 8000. Needs TEST_MIGRATION_DATABASE_URL — a scratch
PostgreSQL database these tests wipe — and is skipped without one. The
Compose and startup-path audits at the bottom always run.
"""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from app import migrate, migration_guard, schema_check  # noqa: E402
from app.migration_guard import (  # noqa: E402
    MigrationInProgress,
    MigrationLock,
    SchemaNotReady,
    WritersActive,
    writer_lease,
)

PG = os.getenv("TEST_MIGRATION_DATABASE_URL")
needs_pg = pytest.mark.skipif(not PG, reason="TEST_MIGRATION_DATABASE_URL not set")


def config(url):
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def wipe(url):
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


def revision(url):
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()


@pytest.fixture
def at_head():
    wipe(PG)
    command.upgrade(config(PG), "head")
    yield PG
    wipe(PG)


@pytest.fixture
def behind():
    wipe(PG)
    command.upgrade(config(PG), "0009")
    yield PG
    wipe(PG)


def acquired_eventually(url, seconds=10.0):
    """Poll for the exclusive lock (a dying backend takes a moment to go)."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with MigrationLock(url):
                return True
        except WritersActive:
            time.sleep(0.1)
    return False


# --- A–D: the protocol itself -------------------------------------------------

@needs_pg
def test_a_writer_first_refuses_the_migration(at_head):
    with writer_lease("writer-a", at_head) as lease:
        assert lease.active
        with pytest.raises(WritersActive) as refused:
            with MigrationLock(at_head):
                pass
        names = [h["application_name"] for h in refused.value.holders]
        assert any(n.startswith("quantdesk-writer:writer-a:") for n in names)


@needs_pg
def test_b_migration_first_refuses_the_writer(at_head):
    with MigrationLock(at_head):
        with pytest.raises(MigrationInProgress, match="migration holds the schema lock"):
            writer_lease("writer-b", at_head).acquire()


@needs_pg
def test_c_writers_share_and_together_refuse_the_migration(at_head):
    with writer_lease("writer-1", at_head), writer_lease("writer-2", at_head):
        with pytest.raises(WritersActive) as refused:
            with MigrationLock(at_head):
                pass
        assert len(refused.value.holders) == 2


@needs_pg
def test_d_a_writer_that_exits_frees_the_migration(at_head):
    lease = writer_lease("writer-d", at_head).acquire()
    with pytest.raises(WritersActive):
        with MigrationLock(at_head):
            pass
    lease.release()
    with MigrationLock(at_head) as lock:
        assert lock.still_held()


@needs_pg
def test_the_schema_is_checked_under_the_lock_and_a_wrong_one_is_refused(behind):
    with pytest.raises(SchemaNotReady) as refused:
        writer_lease("writer", behind).acquire()
    assert refused.value.status.state == schema_check.BEHIND
    # Refusing released the shared lock: nothing is left holding it.
    with MigrationLock(behind) as lock:
        assert lock.still_held()


@needs_pg
def test_the_writer_checks_the_schema_only_after_it_holds_the_lock(at_head, monkeypatch):
    """Lock, then verify — never verify, then lock."""
    seen = {}
    real = schema_check.check

    def spying(url, **kw):
        with pytest.raises(WritersActive):
            with MigrationLock(url):
                pass
        seen["locked_during_check"] = True
        return real(url, **kw)

    monkeypatch.setattr(schema_check, "check", spying)
    with writer_lease("writer", at_head):
        pass
    assert seen == {"locked_during_check": True}


# --- E: crash and disconnect ---------------------------------------------------

WRITER_PROCESS = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, {backend!r})
    from app.migration_guard import writer_lease
    lease = writer_lease({role!r}, {url!r}).acquire()
    print("HELD", flush=True)
    time.sleep(600)
""")


def spawn_writer(url, role="crash-writer"):
    proc = subprocess.Popen(
        [sys.executable, "-c", WRITER_PROCESS.format(backend=str(BACKEND), role=role, url=url)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    line = proc.stdout.readline()
    assert line.strip() == "HELD", proc.stderr.read()
    return proc


@needs_pg
def test_e_a_killed_writer_process_releases_its_lock(at_head):
    proc = spawn_writer(at_head)
    try:
        with pytest.raises(WritersActive):
            with MigrationLock(at_head):
                pass
        proc.send_signal(signal.SIGKILL)          # no cleanup code runs
        proc.wait(timeout=10)
        assert acquired_eventually(at_head)
    finally:
        proc.kill()


@needs_pg
def test_e_a_dropped_lease_connection_releases_the_lock_and_the_writer_stops(at_head):
    stopped = threading.Event()
    lease = writer_lease("dropped", at_head, heartbeat=0.2, on_lost=stopped.set).acquire()
    with pytest.raises(WritersActive):
        with MigrationLock(at_head):
            pass
    engine = create_engine(at_head, isolation_level="AUTOCOMMIT")
    with engine.connect() as admin:
        pids = [h["pid"] for h in migration_guard.holders(admin)]
        assert len(pids) == 1
        # The migration is ready and waiting; the writer's connection dies.
        admin.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pids[0]})
    with MigrationLock(at_head) as lock:     # the dead session's lock is gone
        assert lock.still_held()
        # The writer cannot re-take its lease during the migration, so it stops.
        assert stopped.wait(10), "a writer that lost its lease kept running"
    assert not lease.active
    lease.release()
    engine.dispose()


@needs_pg
def test_e_a_dropped_lease_is_retaken_when_no_migration_is_running(at_head):
    stopped = threading.Event()
    lease = writer_lease("retake", at_head, heartbeat=0.2, on_lost=stopped.set).acquire()
    engine = create_engine(at_head, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as admin:
            first = [h["pid"] for h in migration_guard.holders(admin)]
            admin.execute(text("SELECT pg_terminate_backend(:p)"), {"p": first[0]})
            deadline = time.time() + 10
            while time.time() < deadline:
                now = [h["pid"] for h in migration_guard.holders(admin)]
                if now and now != first:
                    break
                time.sleep(0.1)
            assert now and now != first, "the lease was not re-taken"
        assert not stopped.is_set() and lease.active
        with pytest.raises(WritersActive):
            with MigrationLock(at_head):
                pass
    finally:
        lease.release()
        engine.dispose()


# --- F: a real API on a port that is not 8000 ------------------------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_api(url, tmp_path):
    port = free_port()
    assert port != 8000
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", ""),
           "PYTHONDONTWRITEBYTECODE": "1", "DATABASE_URL": url, "BROKER": "mock",
           "ANGEL_ENABLED": "false", "ANGEL_OPTIONS_ENABLED": "false",
           "V2_PAPER_ENABLED": "false", "ARCHIVE_CANDLES": "false",
           "ARCHIVE_OPTION_CHAIN": "false", "REDIS_URL": "redis://127.0.0.1:1/0",
           "API_KEY": "test-only-key"}
    log = open(tmp_path / f"api-{port}.log", "w+")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", str(BACKEND),
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=tmp_path, env=env, stdout=log, stderr=subprocess.STDOUT)
    return proc, port, log


def wait_healthy(proc, port, seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline and proc.poll() is None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                return json.loads(r.read())
        except OSError:
            time.sleep(0.3)
    return None


def stop(proc):
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


@needs_pg
def test_f_an_api_on_another_port_refuses_the_migration(at_head, tmp_path):
    proc, port, log = start_api(at_head, tmp_path)
    try:
        assert wait_healthy(proc, port), log.seek(0) or log.read()
        with pytest.raises(WritersActive) as refused:
            with MigrationLock(at_head):
                pass
        api_writer = f"quantdesk-writer:api:{socket.gethostname()}:{proc.pid}"
        assert any(h["application_name"] == api_writer for h in refused.value.holders)
        code = migrate.apply(at_head)
        assert code == migrate.EXIT_WRITERS_ACTIVE
    finally:
        stop(proc)
    assert acquired_eventually(at_head)       # a clean shutdown released it


@needs_pg
def test_f_the_api_refuses_to_start_during_a_migration(at_head, tmp_path):
    with MigrationLock(at_head):
        proc, port, log = start_api(at_head, tmp_path)
        try:
            assert proc.wait(timeout=60) != 0
        finally:
            stop(proc)
    log.seek(0)
    out = log.read()
    assert "migration holds the schema lock" in out and "Application startup failed" in out


@needs_pg
def test_f_the_api_refuses_to_start_on_a_schema_behind_head(behind, tmp_path):
    proc, port, log = start_api(behind, tmp_path)
    try:
        assert proc.wait(timeout=60) != 0
    finally:
        stop(proc)
    log.seek(0)
    out = log.read()
    assert "is at revision 0009; this code expects" in out
    assert revision(behind) == "0009"


# --- G: the primitive, from any client anywhere ---------------------------------

@needs_pg
@pytest.mark.skipif(not shutil.which("psql"), reason="psql not on PATH")
def test_g_any_client_holding_the_shared_key_blocks_the_migration(at_head):
    """What a container, a remote host or another language would do: take the
    same key over its own connection. The migration sees it all the same.
    The session then sits idle, as a writer's lease connection does — which
    is what lets PostgreSQL notice at once when the client is killed."""
    from sqlalchemy.engine import make_url
    u = make_url(at_head)
    host = "localhost" if u.host in ("127.0.0.1", None) else u.host
    env = {**os.environ, "PGAPPNAME": "docker-equivalent-writer"}
    if u.password:
        env["PGPASSWORD"] = u.password
    proc = subprocess.Popen(
        ["psql", "-X", "-A", "-t", "-q", "-h", host, "-p", str(u.port or 5432),
         "-U", u.username or "postgres", "-d", u.database],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env)
    try:
        proc.stdin.write(f"SELECT pg_advisory_lock_shared({migration_guard.LOCK_CLASS}, "
                         f"{migration_guard.LOCK_SCHEMA});\nSELECT 'HELD';\n")
        proc.stdin.flush()
        lines = [proc.stdout.readline().strip() for _ in range(2)]
        assert "HELD" in lines, (lines, proc.stderr.read())
        with pytest.raises(WritersActive) as refused:
            with MigrationLock(at_head):
                pass
        assert any(h["application_name"] == "docker-equivalent-writer"
                   for h in refused.value.holders)
    finally:
        proc.kill()                              # the client dies; its idle session goes
        proc.wait(timeout=10)
    assert acquired_eventually(at_head)


# --- The migration command -----------------------------------------------------

@needs_pg
def test_apply_with_a_writer_alive_changes_nothing(behind):
    # A writer cannot lease a 0009 schema, so hold the raw shared key as a
    # still-running old-version writer would.
    engine = create_engine(behind, isolation_level="AUTOCOMMIT")
    with engine.connect() as old_writer:
        old_writer.execute(text("SELECT pg_advisory_lock_shared(:c, :o)"),
                           {"c": migration_guard.LOCK_CLASS, "o": migration_guard.LOCK_SCHEMA})
        assert migrate.apply(behind) == migrate.EXIT_WRITERS_ACTIVE
        assert revision(behind) == "0009"
    engine.dispose()


@needs_pg
def test_apply_holds_the_lock_through_the_whole_upgrade(behind):
    """No check-then-release: during `alembic upgrade` itself a writer is refused."""
    during = {}

    def upgrade(conn):
        with pytest.raises(MigrationInProgress):
            writer_lease("late-writer", behind).acquire()
        during["writer_refused"] = True
        migrate.upgrade_on(conn)

    assert migrate.apply(behind, upgrade=upgrade) == 0
    assert during == {"writer_refused": True}
    assert revision(behind) == schema_check.expected_head()
    with writer_lease("after", behind) as lease:
        assert lease.active


@needs_pg
def test_apply_at_head_is_a_no_op_and_dry_run_lists_writers(at_head, capsys):
    assert migrate.apply(at_head) == 0
    with writer_lease("listed", at_head):
        assert migrate.dry_run(at_head) == 0
    out = capsys.readouterr().out
    assert "quantdesk-writer:listed:" in out


def test_apply_refuses_an_uncoordinated_database(tmp_path):
    url = f"sqlite:///{tmp_path / 'x.db'}"
    command.upgrade(config(url), "0009")
    assert migrate.apply(url) == migrate.EXIT_NOT_COORDINATED
    assert revision(url) == "0009"


def test_only_sqlite_may_skip_coordination():
    assert migration_guard.scratch("sqlite://")
    for url in ("mysql://u@h/db", "postgres://u@h/db", "::garbage::"):
        with pytest.raises(migration_guard.CoordinationError):
            writer_lease("x", url).acquire()


# --- Startup paths: none of them migrates ----------------------------------------

def compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_normal_docker_startup_checks_and_never_migrates():
    services = compose()["services"]
    command_ = " ".join(services["backend"]["command"]
                        if isinstance(services["backend"]["command"], list)
                        else [services["backend"]["command"]])
    assert "alembic" not in command_ and "app.migrate" not in command_
    assert command_.index("app.schema_check") < command_.index("uvicorn")
    assert "profiles" not in services["backend"]
    dockerfile = (BACKEND / "Dockerfile").read_text()
    assert "alembic upgrade" not in dockerfile and "app.migrate" not in dockerfile


def test_the_docker_migration_path_is_explicit_and_separate():
    migrate_svc = compose()["services"]["migrate"]
    assert migrate_svc["profiles"] == ["migrate"]       # `up` never starts it
    assert migrate_svc["entrypoint"] == ["python", "-m", "app.migrate"]
    assert "--apply" not in json.dumps(migrate_svc)      # dry run unless asked


@pytest.mark.skipif(not shutil.which("docker"), reason="docker CLI not installed")
def test_compose_expansion_agrees():
    def cfg(*extra):
        run = subprocess.run(["docker", "compose", *extra, "config", "--format", "json"],
                             cwd=ROOT, capture_output=True, text=True, timeout=60)
        if run.returncode != 0:
            pytest.skip(f"compose config unavailable: {run.stderr.strip()[:200]}")
        return json.loads(run.stdout)["services"]

    normal = cfg()
    assert "migrate" not in normal
    backend = " ".join(normal["backend"]["command"])
    assert "alembic" not in backend and "app.schema_check" in backend
    explicit = cfg("--profile", "migrate")
    assert explicit["migrate"]["entrypoint"] == ["python", "-m", "app.migrate"]


def _migrating_calls(path):
    """Calls that change a schema: alembic `command.upgrade`, `create_all`,
    or a subprocess/os call whose arguments run `alembic upgrade`."""
    import ast
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", getattr(node.func, "id", ""))
        args = " ".join(ast.unparse(a) for a in node.args)
        launches = name in ("run", "Popen", "call", "check_call", "check_output", "system")
        if name in ("upgrade", "create_all") or (launches and "alembic" in args
                                                 and "upgrade" in args):
            found.append(f"{path.relative_to(ROOT)}:{node.lineno}: {ast.unparse(node)[:80]}")
    return found


def test_no_startup_path_in_the_repository_runs_a_migration():
    """Every file that starts QuantDesk. The one place a migration may run is
    app/migrate.py, reached only by an explicit operator command."""
    offenders = []
    for path in [ROOT / "docker-compose.yml", BACKEND / "Dockerfile",
                 ROOT / "frontend" / "Dockerfile", *sorted((ROOT / "scripts").rglob("*.sh"))]:
        if not path.exists():
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if "alembic" in code and "upgrade" in code:
                offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    python = [*sorted((ROOT / "scripts").glob("*.py")), *sorted((BACKEND / "app").rglob("*.py"))]
    for path in python:
        if path == BACKEND / "app" / "migrate.py":
            continue
        offenders += _migrating_calls(path)
    # init_db's create_all: reachable only after the writer lease has verified
    # a PostgreSQL schema at head (init_db then returns before it), or on the
    # SQLite scratch/test backend.
    where = [re.sub(r":\d+:", ":", o) for o in offenders]      # line numbers move
    assert where == ["backend/app/db.py: Base.metadata.create_all(engine)"], offenders
    assert _migrating_calls(BACKEND / "app" / "migrate.py"), "the explicit path must exist"
    migrate_sh = (ROOT / "scripts" / "migrate.sh").read_text()
    assert "app.migrate" in migrate_sh and "alembic" not in migrate_sh.split("set -euo")[1]
