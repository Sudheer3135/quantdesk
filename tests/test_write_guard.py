"""Transaction-level write protection and same-connection migrations (Pass 2E-A.2).

Codex showed the process lease alone is not a safety boundary: kill the
lease connection, let a migration take the key, and the writer's own
Session could still write until a heartbeat noticed. These tests hold the
new invariant to real PostgreSQL:

    every write transaction takes the shared key on its own connection
    before its first write, and keeps it to commit or rollback;
    the connection holding the exclusive key is the one Alembic runs on.

They check the database, not only exceptions. `trades.id` is drawn from a
sequence, and nextval is not rolled back — so an INSERT that reached the
server at all, even one later rolled back, moves the sequence. "Refused
before DML" is asserted as: the sequence did not move and the row is absent.

Needs TEST_MIGRATION_DATABASE_URL (a scratch PostgreSQL these tests wipe);
the parts that need no database always run.
"""
import ast
import json
import os
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
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from app import migrate, migration_guard, schema_check  # noqa: E402
from app.migration_guard import (  # noqa: E402
    CoordinationError,
    MigrationInProgress,
    MigrationLock,
    Writer,
    WritersActive,
    is_write,
    protect_writes,
    release_after_drain,
    writer_lease,
)
from app.models import TradeRecord  # noqa: E402

PG = os.getenv("TEST_MIGRATION_DATABASE_URL")
needs_pg = pytest.mark.skipif(not PG, reason="TEST_MIGRATION_DATABASE_URL not set")
SECRET = "Sup3rS3cretQD"


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


def admin(url):
    return create_engine(url, isolation_level="AUTOCOMMIT")


def scalar(url, sql, **params):
    engine = admin(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()
    finally:
        engine.dispose()


def trades_state(url):
    """(row count, sequence last_value, is_called) — the sequence moves for
    any INSERT that reached the server, committed or not."""
    engine = admin(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT count(*) FROM trades")).scalar()
            last, called = conn.execute(text(
                "SELECT last_value, is_called FROM trades_id_seq")).one()
            return rows, last, called
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


@pytest.fixture
def app_engine(at_head):
    """An engine built exactly as app.db builds the application engine."""
    engine = create_engine(at_head, pool_pre_ping=True, future=True, pool_size=1,
                           max_overflow=0)
    assert protect_writes(engine)
    yield engine
    engine.dispose()


def new_trade(symbol="NIFTY-TEST"):
    return TradeRecord(symbol=symbol, side="BUY", quantity=1, entry=100.0,
                       stop_loss=95.0, status="open")


def exclusive_holder(url):
    return scalar(url, "SELECT pid FROM pg_locks WHERE locktype = 'advisory' "
                  "AND classid = :c AND objid = :o AND objsubid = 2 "
                  "AND mode = 'ExclusiveLock' AND granted",
                  c=migration_guard.LOCK_CLASS, o=migration_guard.LOCK_SCHEMA)


def refused(exc_info) -> bool:
    chain, seen = exc_info.value, set()
    while chain is not None and id(chain) not in seen:
        if isinstance(chain, MigrationInProgress):
            return True
        seen.add(id(chain))
        chain = chain.__cause__ or chain.__context__
    return False


# --- What counts as a write ------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "INSERT INTO t VALUES (1)", "  update t set a = 1", "DELETE FROM t",
    "/* c */ INSERT INTO t VALUES (1)",
    "-- c\nMERGE INTO t USING s ON true WHEN MATCHED THEN DELETE",
    "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x", "CREATE TABLE t (a int)",
    "ALTER TABLE t ADD b int", "DROP TABLE t", "TRUNCATE t", "COPY t FROM STDIN",
    "LOCK TABLE t", "SELECT * INTO t2 FROM t", "EXPLAIN ANALYZE DELETE FROM t",
    "CALL p()", "DO $$ BEGIN END $$", "(INSERT INTO t VALUES (1))"])
def test_writes_are_recognised(sql):
    assert is_write(sql)


@pytest.mark.parametrize("sql", [
    "SELECT 1", "  select a from t where b = 'INSERT'", "SHOW timezone",
    "SET TIME ZONE 'UTC'", "(SELECT 1)", "VALUES (1)"])
def test_reads_are_not_locked(sql):
    assert not is_write(sql)


def test_the_application_engine_is_protected(tmp_path):
    """app.db guards its engine when the app runs on PostgreSQL. Checked in a
    fresh interpreter with a PostgreSQL URL (create_engine never connects), so
    it holds however this test run's own DATABASE_URL is set — CI's is SQLite,
    where the guard is deliberately a no-op."""
    probe = ("from sqlalchemy import event\n"
             "from app import db, migration_guard\n"
             "assert db.engine.dialect.name == 'postgresql'\n"
             "assert event.contains(db.engine, 'before_cursor_execute',"
             " migration_guard._guard_write)\n")
    env = {**os.environ, "PYTHONPATH": str(BACKEND),
           "DATABASE_URL": "postgresql+psycopg://quant:quant@127.0.0.1:1/unused"}
    # Run outside the project so no .env is read.
    run = subprocess.run([sys.executable, "-c", probe], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-2000:]


def test_the_application_engine_matches_its_dialect():
    """In this process: guarded on PostgreSQL, left alone on SQLite."""
    from app import db
    guarded = event.contains(db.engine, "before_cursor_execute", migration_guard._guard_write)
    assert guarded == (db.engine.dialect.name == "postgresql")


# --- A: lost lease + migration + attempted write -------------------------------

@needs_pg
@pytest.mark.parametrize("heartbeat", [0, 5.0], ids=["heartbeat-off", "heartbeat-5s"])
def test_a_codex_sequence_the_write_is_refused_before_dml(app_engine, heartbeat):
    """T0 lease held · T1 lease connection killed · T2 migration takes the key ·
    T3 heartbeat has not run (or is off) · T4 a Session write → refused."""
    url = str(app_engine.url.render_as_string(hide_password=False))
    lost = threading.Event()
    lease = writer_lease("api", url, heartbeat=heartbeat, on_lost=lost.set).acquire()
    Session = sessionmaker(bind=app_engine, expire_on_commit=False)
    with Session() as s:                                   # warm the pool
        s.add(new_trade("BEFORE"))
        s.commit()
    before = trades_state(url)
    try:
        engine = admin(url)
        with engine.connect() as a:                         # T1
            pid = [h["pid"] for h in migration_guard.holders(a)][0]
            a.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
        engine.dispose()
        with MigrationLock(url):                           # T2 (T3: no heartbeat yet)
            assert not lost.is_set()
            with Session() as s, pytest.raises(Exception) as orm:   # T4, ORM flush
                s.add(new_trade("DURING-ORM"))
                s.commit()
            assert refused(orm)
            with Session() as s, pytest.raises(Exception) as raw:   # T4, raw SQL
                s.execute(text("INSERT INTO trades (created_at, symbol, side, quantity, "
                               "entry, stop_loss, status) VALUES (now(), 'DURING-RAW', "
                               "'BUY', 1, 1, 1, 'open')"))
                s.commit()
            assert refused(raw)
            assert trades_state(url) == before              # nothing reached the server
    finally:
        lease.release()
    assert scalar(url, "SELECT count(*) FROM trades WHERE symbol LIKE 'DURING%'") == 0


# --- B/C: an open write transaction holds off the migration --------------------

@needs_pg
@pytest.mark.parametrize("ending", ["commit", "rollback"])
def test_b_c_an_open_write_transaction_blocks_the_migration_until_it_ends(app_engine, ending):
    url = app_engine.url.render_as_string(hide_password=False)
    Session = sessionmaker(bind=app_engine)
    s = Session()
    s.add(new_trade("OPEN"))
    s.flush()                                   # written, not committed, key held
    try:
        with pytest.raises(WritersActive) as blocked:
            with MigrationLock(url):
                pass
        assert blocked.value.holders[0]["mode"] == "ShareLock"
        getattr(s, ending)()
    finally:
        s.close()
    with MigrationLock(url) as lock:           # the xact lock went with the transaction
        assert lock.still_held()
    expected = 1 if ending == "commit" else 0
    assert scalar(url, "SELECT count(*) FROM trades WHERE symbol = 'OPEN'") == expected


@needs_pg
def test_a_pooled_connection_takes_the_lock_again_in_every_transaction(app_engine):
    """pool_size=1: the second transaction reuses the first one's connection.
    A stale 'already locked' flag would let it write during a migration."""
    url = app_engine.url.render_as_string(hide_password=False)
    Session = sessionmaker(bind=app_engine)
    with Session() as s:
        s.add(new_trade("FIRST"))
        s.commit()
    before = trades_state(url)
    with MigrationLock(url):
        with Session() as s, pytest.raises(Exception) as second:
            s.add(new_trade("SECOND"))
            s.commit()
        assert refused(second)
    assert trades_state(url) == before


@needs_pg
def test_a_rolled_back_savepoint_does_not_leave_the_transaction_unlocked(app_engine):
    held_sql = text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND classid = :c AND objid = :o AND pid = pg_backend_pid()")
    key = {"c": migration_guard.LOCK_CLASS, "o": migration_guard.LOCK_SCHEMA}
    with sessionmaker(bind=app_engine)() as s:
        nested = s.begin_nested()
        s.add(new_trade("IN-SAVEPOINT"))
        s.flush()
        assert s.execute(held_sql, key).scalar() >= 1
        nested.rollback()                           # PostgreSQL drops locks taken inside
        s.add(new_trade("AFTER-SAVEPOINT"))
        s.flush()                                   # must take the key again
        assert s.execute(held_sql, key).scalar() >= 1
        s.commit()


@needs_pg
def test_reads_do_not_hold_off_a_migration(app_engine):
    url = app_engine.url.render_as_string(hide_password=False)
    with sessionmaker(bind=app_engine)() as s:
        s.execute(text("SELECT count(*) FROM trades")).scalar()    # read txn open
        with MigrationLock(url) as lock:
            assert lock.still_held()


@needs_pg
def test_an_autocommit_write_is_refused(app_engine):
    url = app_engine.url.render_as_string(hide_password=False)
    before = trades_state(url)
    with app_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        with pytest.raises(CoordinationError):
            conn.execute(text("INSERT INTO trades (created_at, symbol, side, quantity, "
                              "entry, stop_loss, status) VALUES (now(), 'AUTO', 'BUY', "
                              "1, 1, 1, 'open')"))
    assert trades_state(url) == before


@needs_pg
def test_f_writers_share_the_key(app_engine):
    url = app_engine.url.render_as_string(hide_password=False)
    other = create_engine(url, pool_size=1, max_overflow=0)
    protect_writes(other)
    a, b = sessionmaker(bind=app_engine)(), sessionmaker(bind=other)()
    try:
        a.add(new_trade("W1"))
        b.add(new_trade("W2"))
        a.flush()
        b.flush()                                    # both hold it shared, neither waits
        with pytest.raises(WritersActive) as blocked:
            with MigrationLock(url):
                pass
        assert len(blocked.value.holders) == 2
        a.commit()
        b.commit()
    finally:
        a.close()
        b.close()
        other.dispose()
    assert scalar(url, "SELECT count(*) FROM trades WHERE symbol IN ('W1', 'W2')") == 2


# --- I: a crashed writer mid-transaction -----------------------------------------

@needs_pg
def test_i_a_process_killed_inside_a_write_transaction_releases_everything(at_head):
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(BACKEND)!r})
        from sqlalchemy import create_engine, text
        from app.migration_guard import protect_writes
        e = create_engine({at_head!r}); protect_writes(e)
        c = e.connect()
        c.execute(text("INSERT INTO trades (created_at, symbol, side, quantity, entry, "
                       "stop_loss, status) VALUES (now(), 'CRASH', 'BUY', 1, 1, 1, 'open')"))
        print("WRITING", flush=True)
        time.sleep(600)
    """)
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                            text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        assert proc.stdout.readline().strip() == "WRITING"
        with pytest.raises(WritersActive):
            with MigrationLock(at_head):
                pass
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with MigrationLock(at_head):
                    break
            except WritersActive:
                time.sleep(0.1)
        else:
            pytest.fail("the crashed writer's lock outlived it")
    finally:
        proc.kill()
    assert scalar(at_head, "SELECT count(*) FROM trades WHERE symbol = 'CRASH'") == 0


# --- C/D: the migration runs on the connection that owns the key -----------------

@needs_pg
def test_c_alembic_runs_on_the_lock_owning_connection_and_d_dies_with_it(behind):
    """A blocker holds alembic_version's row, so the migration's own UPDATE of
    it waits. While it waits: the backend running Alembic *is* the one holding
    the exclusive key, and it is the only migration connection. Then that
    connection is killed — the migration fails, and nothing continues it."""
    engine = admin(behind)
    blocker = engine.connect().execution_options(isolation_level="READ COMMITTED")
    tx = blocker.begin()
    blocker.execute(text("SELECT version_num FROM alembic_version FOR UPDATE"))
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("code", migrate.apply(behind)))
    worker.start()
    try:
        deadline, waiting = time.time() + 30, None
        while time.time() < deadline and waiting is None:
            waiting = scalar(behind, "SELECT pid FROM pg_stat_activity WHERE application_name "
                             "LIKE 'quantdesk-migration:%' AND wait_event_type = 'Lock' "
                             "AND query ILIKE 'UPDATE alembic_version%'")
            time.sleep(0.05)
        assert waiting, "the migration never reached its version update"
        assert exclusive_holder(behind) == waiting                     # C
        assert scalar(behind, "SELECT count(*) FROM pg_stat_activity WHERE "
                      "application_name LIKE 'quantdesk-migration:%'") == 1
        assert scalar(behind, "SELECT pg_terminate_backend(:p)", p=waiting)   # D
        worker.join(30)
        assert result["code"] == migrate.EXIT_FAILED
    finally:
        tx.rollback()
        blocker.close()
        engine.dispose()
        worker.join(30)
    assert scalar(behind, "SELECT count(*) FROM pg_stat_activity WHERE "
                  "application_name LIKE 'quantdesk-migration:%'") == 0
    assert exclusive_holder(behind) is None
    assert scalar(behind, "SELECT version_num FROM alembic_version") == "0009"
    assert scalar(behind, "SELECT to_regclass('public.research_events')") is None


@needs_pg
def test_a_writer_is_refused_during_a_real_migration(behind):
    """Inside `alembic upgrade` itself, on the lock-owning connection, a write
    transaction elsewhere cannot start."""
    engine = create_engine(behind)
    protect_writes(engine)
    during = {}

    def upgrade(conn):
        during["pid_is_holder"] = (conn.execute(text("SELECT pg_backend_pid()")).scalar()
                                   == exclusive_holder(behind))
        conn.rollback()
        with engine.connect() as w, pytest.raises(Exception) as exc:
            w.execute(text("INSERT INTO trades (created_at, symbol, side, quantity, entry, "
                           "stop_loss, status) VALUES (now(), 'MID', 'BUY', 1, 1, 1, 'open')"))
        during["refused"] = refused(exc)
        migrate.upgrade_on(conn)

    before = trades_state(behind)
    assert migrate.apply(behind, upgrade=upgrade) == 0
    engine.dispose()
    assert during == {"pid_is_holder": True, "refused": True}
    assert trades_state(behind) == before
    assert scalar(behind, "SELECT version_num FROM alembic_version") == schema_check.expected_head()


# --- 18: a failing migration, with a secret in its error --------------------------

def _url_with_secret(url, where):
    u = make_url(url)
    if where == "authority":
        return u.set(password=SECRET).render_as_string(hide_password=False)
    return u.update_query_dict({"password": SECRET}).render_as_string(hide_password=False)


@needs_pg
@pytest.mark.parametrize("where", ["authority", "query"])
def test_a_failing_migration_reports_safely_and_leaves_the_truth(behind, where, tmp_path):
    """A trigger fails the migration's version update with a message quoting a
    DSN that carries the secret. The transactional DDL rolls back; the
    command, its stdout/stderr and the migration log carry no secret."""
    secret_url = _url_with_secret(behind, where)             # trust auth: still connects
    engine = admin(behind)
    with engine.connect() as c:
        c.execute(text(f"""
            CREATE FUNCTION qd_fail() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'boom: could not reach postgresql://quant:{SECRET}@db/qd?password={SECRET}';
            END $$;
            CREATE TRIGGER qd_fail BEFORE UPDATE ON alembic_version
                FOR EACH ROW EXECUTE FUNCTION qd_fail();"""))
    engine.dispose()
    log = tmp_path / "migrations.log"
    run = subprocess.run(
        ["bash", "-c", 'set -o pipefail; "$PY" -m app.migrate --apply 2>&1 | tee -a "$LOG"'],
        cwd=BACKEND, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PY": sys.executable, "LOG": str(log), "DATABASE_URL": secret_url,
             "PYTHONDONTWRITEBYTECODE": "1"})
    output = run.stdout + run.stderr + log.read_text()
    assert run.returncode == migrate.EXIT_FAILED, output
    assert "migration failed" in output and "boom" in output
    assert SECRET not in output, output
    assert scalar(behind, "SELECT version_num FROM alembic_version") == "0009"
    assert scalar(behind, "SELECT to_regclass('public.research_events')") is None
    assert exclusive_holder(behind) is None                 # closed with the connection


# --- 15: Codex's reproduction — an exception inside MigrationLock.__enter__ --------

INJECT = textwrap.dedent("""
    import sys
    sys.path.insert(0, {backend!r})
    from sqlalchemy import exc as sa_exc
    from sqlalchemy.engine import Connection
    from app import migrate, schema_check
    from app.redact import run_cli
    DSN = "postgresql://quant:{secret}@db.internal/qd?password={secret}"
    AT = {at!r}

    class Driver(Exception):
        pass

    def boom():
        raise sa_exc.OperationalError("SELECT ...", {{}},
                                      Driver("connection to " + DSN + " failed"))

    real = Connection.execute
    def execute(self, statement, *a, **k):
        sql = str(statement)
        if AT == "lock" and "pg_try_advisory_lock(" in sql: boom()
        if AT == "unlock" and "pg_advisory_unlock(" in sql: boom()
        return real(self, statement, *a, **k)
    Connection.execute = execute
    if AT == "precheck":
        schema_check.check_connection = lambda *a, **k: boom()
    if AT == "escape":
        migrate.apply = lambda *a, **k: boom()
    if AT == "log":
        import logging
        logging.basicConfig()
        real_apply = migrate.apply
        def logged(url, **k):
            logging.getLogger("x").error("connecting to %s", DSN)
            try:
                boom()
            except Exception:
                logging.getLogger("x").exception("with traceback")
            return real_apply(url, **k)
        migrate.apply = logged
    sys.exit(run_cli(lambda: migrate.main(["--apply"])))
""")


@needs_pg
@pytest.mark.parametrize("at", ["lock", "precheck", "unlock", "escape", "log"])
def test_15_no_secret_escapes_any_migration_boundary(behind, at, tmp_path):
    log = tmp_path / "migrations.log"
    script = INJECT.format(backend=str(BACKEND), secret=SECRET, at=at)
    run = subprocess.run(
        ["bash", "-c", 'set -o pipefail; "$PY" -c "$SCRIPT" 2>&1 | tee -a "$LOG"'],
        cwd=BACKEND, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PY": sys.executable, "SCRIPT": script, "LOG": str(log),
             "DATABASE_URL": behind, "PYTHONDONTWRITEBYTECODE": "1"})
    output = run.stdout + run.stderr + log.read_text()
    assert SECRET not in output, output
    assert "Traceback" not in output or at == "log", output
    if at in ("lock", "precheck", "escape"):
        assert run.returncode != 0, output
    if at == "lock":
        assert "cannot take the migration lock" in output
        assert scalar(behind, "SELECT version_num FROM alembic_version") == "0009"
    assert exclusive_holder(behind) is None


# --- E: a real API on another port, lease lost, migration running -----------------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@needs_pg
def test_e_a_real_api_on_another_port_cannot_write_during_a_migration(at_head, tmp_path):
    port = _free_port()
    assert port != 8000
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", ""),
           "PYTHONDONTWRITEBYTECODE": "1", "DATABASE_URL": at_head, "BROKER": "mock",
           "ANGEL_ENABLED": "false", "ANGEL_OPTIONS_ENABLED": "false",
           "V2_PAPER_ENABLED": "false", "ARCHIVE_CANDLES": "false",
           "ARCHIVE_OPTION_CHAIN": "false", "REDIS_URL": "redis://127.0.0.1:1/0",
           "API_KEY": "test-only-key", "WRITER_LEASE_HEARTBEAT_SECONDS": "0"}
    log = open(tmp_path / "api.log", "w+")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", str(BACKEND),
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=tmp_path, env=env, stdout=log, stderr=subprocess.STDOUT)

    def post_trade(symbol):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/journal", method="POST",
            data=json.dumps({"symbol": symbol, "side": "BUY", "quantity": 1,
                             "entry": 100, "stop_loss": 95}).encode(),
            headers={"Content-Type": "application/json", "X-API-Key": "test-only-key"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
                break
            except OSError:
                time.sleep(0.3)
        assert post_trade("API-BEFORE") == 200
        engine = admin(at_head)
        with engine.connect() as a:        # the API's lease connection dies
            a.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                           "WHERE application_name LIKE 'quantdesk-writer:api:%'"))
        engine.dispose()
        before = trades_state(at_head)
        with MigrationLock(at_head):
            assert post_trade("API-DURING") >= 500
            assert trades_state(at_head) == before
        assert post_trade("API-AFTER") == 200          # no migration: writes resume
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=60)
    assert scalar(at_head, "SELECT count(*) FROM trades WHERE symbol = 'API-DURING'") == 0


# --- G/H: shutdown drains writers before the lease is released ---------------------

@needs_pg
@pytest.mark.parametrize("kind", ["scheduler", "paper-thread"])
def test_g_h_shutdown_keeps_protection_until_the_in_flight_write_ends(app_engine, kind):
    from apscheduler.schedulers.background import BackgroundScheduler
    url = app_engine.url.render_as_string(hide_password=False)
    lease = writer_lease("api", url, heartbeat=0, on_lost=lambda: None).acquire()
    Session = sessionmaker(bind=app_engine)
    writing, finish = threading.Event(), threading.Event()

    def job():
        with Session() as s:
            s.add(new_trade(f"INFLIGHT-{kind}"))
            s.flush()                        # key held by this transaction
            writing.set()
            finish.wait(60)                  # shutdown arrives mid-transaction
            s.commit()

    if kind == "scheduler":
        sched = BackgroundScheduler()
        sched.add_job(job)
        sched.start()
        writers = [Writer("scheduler", lambda: sched.shutdown(wait=True),
                          lambda: migration_guard.scheduler_drained(sched))]
    else:
        stopping = threading.Event()
        thread = threading.Thread(target=job, daemon=True)
        thread.start()

        def stop(timeout=3.0):               # the paper trader's own stop: a timed join
            stopping.set()
            thread.join(timeout=timeout)
        writers = [Writer("v2-paper", stop, lambda: not thread.is_alive())]

    assert writing.wait(10)
    result = {}
    shutdown = threading.Thread(target=lambda: result.setdefault(
        "released", release_after_drain(lease, writers, timeout=60)))
    shutdown.start()
    time.sleep(4.0 if kind == "paper-thread" else 0.5)   # past the paper trader's 3 s join
    assert shutdown.is_alive() and lease.active           # still draining, still protected
    with pytest.raises(WritersActive) as blocked:
        with MigrationLock(url):
            pass
    names = {h["application_name"] for h in blocked.value.holders}
    assert any(n.startswith("quantdesk-writer:api:") for n in names)
    finish.set()
    shutdown.join(30)
    assert result == {"released": True} and not lease.active
    with MigrationLock(url) as lock:
        assert lock.still_held()
    assert scalar(url, f"SELECT count(*) FROM trades WHERE symbol = 'INFLIGHT-{kind}'") == 1


@needs_pg
def test_a_writer_that_will_not_drain_keeps_the_lease_and_ends_the_process(app_engine):
    url = app_engine.url.render_as_string(hide_password=False)
    lease = writer_lease("api", url, heartbeat=0, on_lost=lambda: None).acquire()
    hang = threading.Event()
    stuck_with = {}
    try:
        ok = release_after_drain(lease, [Writer("hung", lambda: hang.wait(30),
                                                lambda: hang.is_set())],
                                 timeout=0.5, on_stuck=stuck_with.update)
        assert not ok and stuck_with == {"hung": "stop did not return"}
        assert lease.active                               # never voluntarily released
        with pytest.raises(WritersActive):
            with MigrationLock(url):
                pass
    finally:
        hang.set()
        lease.release()


def test_the_api_shutdown_drains_before_releasing():
    tree = ast.parse((BACKEND / "app" / "main.py").read_text())
    source = ast.unparse(tree)
    assert "scheduler.shutdown(wait=True)" in source
    assert "scheduler.shutdown(wait=False)" not in source
    assert "release_after_drain(lease" in source
    assert "lease.release()" not in source            # only via release_after_drain
    assert "v2_paper.TRADER.running" in source


# --- Every supported writer goes through the protected engine ----------------------

def test_every_supported_writer_uses_the_protected_engine():
    """The application's one Session factory is bound to the protected engine,
    and nothing else in the app or the supported scripts makes a writable
    engine or Session of its own."""
    allowed_engines = {"app/db.py",                 # the protected application engine
                       "app/migration_guard.py",    # lease / migration-lock connections
                       "app/schema_check.py"}       # read-only revision check
    found_engines, found_sessionmakers = set(), set()
    files = [*(BACKEND / "app").rglob("*.py"), *(ROOT / "scripts").glob("*.py")]
    for path in files:
        rel = str(path.relative_to(BACKEND if path.is_relative_to(BACKEND) else ROOT))
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                if name == "create_engine":
                    found_engines.add(rel)
                if name == "sessionmaker":
                    found_sessionmakers.add(rel)
    assert found_engines == allowed_engines, found_engines
    assert found_sessionmakers == {"app/db.py"}, found_sessionmakers
    db_src = (BACKEND / "app" / "db.py").read_text()
    assert db_src.index("protect_writes(engine)") < db_src.index("SessionLocal = sessionmaker")
    for script in ("angel_backfill.py", "doctor.py"):
        assert "SessionLocal" in (ROOT / "scripts" / script).read_text()
    paper = (BACKEND / "app" / "strategy_v2" / "paper.py").read_text()
    assert "session_factory=SessionLocal" in paper
