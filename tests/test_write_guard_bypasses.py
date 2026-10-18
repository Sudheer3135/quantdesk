"""Codex's 2E-A.2 bypasses, closed (Pass 2E-A.3).

1. Cached authority. A per-transaction "already locked" flag let a write
   skip the PostgreSQL check after a raw COMMIT or ROLLBACK TO SAVEPOINT had
   released the lock. Now every write statement asks PostgreSQL, every time,
   and raw transaction control is refused.
2. SQL batching. `SELECT 1; UPDATE ...` was classified by its first word as
   a read. Now a string holding more than one top-level statement is refused
   before classification, by a PostgreSQL-aware scanner.
3. Shutdown. A stop that raised, or returned while its worker lived on,
   counted as drained. Now only a positively confirmed stop counts, and
   anything else keeps the lease held and ends the process.

Database state is asserted, not only exceptions: `trades.id` comes from a
sequence, which no rollback rewinds, so an INSERT that reached the server
at all moves it. Needs TEST_MIGRATION_DATABASE_URL (a scratch PostgreSQL
these tests wipe); the parts that need no database always run.
"""
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert, text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from app import migration_guard  # noqa: E402
from app.migration_guard import (  # noqa: E402
    MigrationInProgress,
    MigrationLock,
    Step,
    UnsupportedStatement,
    Writer,
    WritersActive,
    drain,
    protect_writes,
    release_after_drain,
    scheduler_drained,
    statements,
    writer_lease,
)
from app.models import TradeRecord  # noqa: E402

PG = os.getenv("TEST_MIGRATION_DATABASE_URL")
needs_pg = pytest.mark.skipif(not PG, reason="TEST_MIGRATION_DATABASE_URL not set")
INSERT_SQL = ("INSERT INTO trades (created_at, symbol, side, quantity, entry, stop_loss, "
              "status) VALUES (now(), '{s}', 'BUY', 1, 1, 1, 'open')")


# --- fixtures and database-state helpers --------------------------------------------

def _config(url):
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _wipe(url):
    engine = create_engine(url)
    with engine.begin() as conn:
        # A test that failed part-way can leave a session idle in transaction;
        # end it, or DROP SCHEMA waits on it forever and a failure becomes a hang.
        conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                          "WHERE datname = current_database() AND pid <> pg_backend_pid()"))
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


def q(url, sql, **params):
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()
    finally:
        engine.dispose()


def state(url):
    """(rows, sequence last_value, is_called, symbols) — the sequence moves for
    any INSERT that reached the server, committed or not."""
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            last, called = conn.execute(text(
                "SELECT last_value, is_called FROM trades_id_seq")).one()
            symbols = tuple(conn.execute(text(
                "SELECT symbol FROM trades ORDER BY id")).scalars())
            return len(symbols), last, called, symbols
    finally:
        engine.dispose()


@pytest.fixture
def url():
    _wipe(PG)
    command.upgrade(_config(PG), "head")
    yield PG
    _wipe(PG)


@pytest.fixture
def engine(url):
    """Built exactly as app.db builds the application engine (pool of one, so
    the pooled-connection reuse path is always exercised)."""
    eng = create_engine(url, pool_pre_ping=True, future=True, pool_size=1, max_overflow=0)
    assert protect_writes(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def Session(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def trade(symbol):
    return TradeRecord(symbol=symbol, side="BUY", quantity=1, entry=100.0,
                       stop_loss=95.0, status="open")


def migration_refused(url):
    try:
        with MigrationLock(url):
            return False
    except WritersActive:
        return True


def held_here(conn):
    return conn.execute(text(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND classid = :c "
        "AND objid = :o AND granted AND pid = pg_backend_pid()"),
        {"c": migration_guard.LOCK_CLASS, "o": migration_guard.LOCK_SCHEMA}).scalar()


def chain_has(exc, kind):
    seen, e = set(), exc
    while e is not None and id(e) not in seen:
        if isinstance(e, kind):
            return True
        seen.add(id(e))
        e = e.__cause__ or e.__context__
    return False


# --- A3: one statement per execute, found by a PostgreSQL-aware scanner --------------

@pytest.mark.parametrize("sql", [
    "SELECT ';'", "SELECT 'a'';b'", "SELECT E'\\';'", 'SELECT "a;b" FROM t',
    "SELECT $$a;b$$", "SELECT $fn$ x; y $fn$", "SELECT 1 -- ; not a statement",
    "/* ; /* nested ; */ ; */ SELECT 1", "SELECT 1;", "SELECT 1;  -- trailing",
    "SELECT $1", "select a$b$c from t", "INSERT INTO t VALUES ('x;y')"])
def test_a_single_statement_is_one_statement(sql):
    assert len(statements(sql)) == 1


@pytest.mark.parametrize("sql", [
    "SELECT 1; UPDATE trades SET symbol = 'x'", "SELECT 1; DELETE FROM trades",
    "SELECT 1; INSERT INTO trades DEFAULT VALUES", "UPDATE trades SET a = 1; SELECT 1",
    "SELECT ';'; DELETE FROM t", "SELECT $$;$$; DELETE FROM t",
    "SELECT 1 /* c */; DELETE FROM t", "select a$b$c from t; delete from t",
    "SELECT 1;DELETE FROM t"])
def test_a_batch_is_several_statements(sql):
    assert len(statements(sql)) > 1


@pytest.mark.parametrize("sql", ["SELECT 'x", "SELECT $$x", "SELECT \"x", "/* x",
                                 "SELECT E'\\'", "SELECT E'\\\\';'"])
def test_unterminated_quoting_fails_closed(sql):
    with pytest.raises(UnsupportedStatement):
        statements(sql)


@needs_pg
@pytest.mark.parametrize("batch", [
    "SELECT 1; UPDATE trades SET symbol = 'HIJACKED'",
    "SELECT 1; DELETE FROM trades",
    "SELECT 1; " + INSERT_SQL.format(s="SMUGGLED"),
    "UPDATE trades SET symbol = 'HIJACKED'; SELECT 1"])
@pytest.mark.parametrize("migrating", [False, True], ids=["idle", "migrating"])
def test_codex_batch_is_refused_whole_before_it_reaches_the_server(Session, url, batch,
                                                                   migrating):
    with Session() as s:
        s.add(trade("KEEP"))
        s.commit()
    before = state(url)
    lock = MigrationLock(url) if migrating else None
    if lock:
        lock.__enter__()
    try:
        with Session() as s, pytest.raises(UnsupportedStatement):
            s.execute(text(batch))
        with Session() as s, pytest.raises(UnsupportedStatement):
            s.connection().exec_driver_sql(batch)
    finally:
        if lock:
            lock.__exit__(None, None, None)
    assert state(url) == before


@needs_pg
def test_quoted_and_commented_semicolons_still_run(Session):
    with Session() as s:
        assert s.execute(text("SELECT ';'")).scalar() == ";"
        assert s.execute(text("SELECT $$a;b$$ /* ; */ -- ;")).scalar() == "a;b"
        s.execute(text("INSERT INTO trades (created_at, symbol, side, quantity, entry, "
                       "stop_loss, status) VALUES (now(), 'semi;colon', 'BUY', 1, 1, 1, "
                       "'open')"))
        s.commit()
        assert s.execute(text("SELECT symbol FROM trades")).scalar() == "semi;colon"


# --- A2: raw transaction control is refused; SQLAlchemy's own is not ---------------

CONTROL = ["COMMIT", "commit", "  /* x */ COMMIT", "END", "ROLLBACK", "ABORT", "BEGIN",
           "START TRANSACTION", "SAVEPOINT p", "RELEASE SAVEPOINT p", "RELEASE p",
           "ROLLBACK TO SAVEPOINT p", "ROLLBACK TO p", "COMMIT;"]


@needs_pg
@pytest.mark.parametrize("sql", CONTROL)
def test_raw_transaction_control_is_refused(Session, url, sql):
    with Session() as s:
        s.add(trade("OPEN"))
        s.flush()                                     # write in flight, key held
        with pytest.raises(UnsupportedStatement):
            s.execute(text(sql))
        with pytest.raises(UnsupportedStatement):
            s.connection().exec_driver_sql(sql)
        # Refused before it was sent: the transaction is intact and still
        # holds the key, so a migration is still shut out.
        assert held_here(s.connection()) >= 1
        assert migration_refused(url)
        s.rollback()
    assert state(url)[0] == 0


@needs_pg
def test_sqlalchemy_commit_rollback_and_savepoints_still_work(Session, url):
    with Session() as s:
        s.add(trade("COMMITTED"))
        s.commit()
        s.add(trade("ROLLED-BACK"))
        s.flush()
        s.rollback()
        with s.begin_nested():                         # SAVEPOINT / RELEASE
            s.add(trade("NESTED-KEPT"))
        nested = s.begin_nested()
        s.add(trade("NESTED-DROPPED"))
        s.flush()
        nested.rollback()                              # ROLLBACK TO SAVEPOINT
        s.add(trade("AFTER-SAVEPOINT"))
        s.commit()
    assert state(url)[3] == ("COMMITTED", "NESTED-KEPT", "AFTER-SAVEPOINT")


# --- A1/A4: no cached authority — the exact stale-state bypasses ---------------------

@needs_pg
def test_every_write_asks_postgresql_again(Session):
    with Session() as s:
        before = migration_guard.AUTHORITY_CHECKS
        for n in range(3):
            s.add(trade(f"W{n}"))
            s.flush()
        s.execute(text(INSERT_SQL.format(s="W3")))
        s.execute(text("SELECT count(*) FROM trades"))            # a read asks nothing
        assert migration_guard.AUTHORITY_CHECKS - before == 4
        s.commit()


@needs_pg
def test_a4_codex_raw_commit_cannot_release_the_key(Session, url):
    """T0 write · T1 raw COMMIT → refused, so the key is never released under
    the writer · T2 migration still refused · writer ends · T3 migration
    runs and the writer's next INSERT is refused before the server."""
    s = Session()
    s.add(trade("T0"))
    s.flush()                                                      # T0
    with pytest.raises(UnsupportedStatement):
        s.execute(text("COMMIT"))                                  # T1
    assert migration_refused(url)                                  # T2
    s.rollback()
    before = state(url)
    with MigrationLock(url):
        s.add(trade("T3"))
        with pytest.raises(Exception) as exc:                      # T3
            s.flush()
        assert chain_has(exc.value, MigrationInProgress)
        s.rollback()
    s.close()
    assert state(url) == before and "T3" not in state(url)[3]


@needs_pg
def test_a4_a_driver_level_commit_behind_sqlalchemys_back_cannot_bypass(Session, url):
    """The stale state itself: the transaction is committed at the driver,
    so PostgreSQL has released the key while SQLAlchemy still believes the
    transaction is open. The next write must ask PostgreSQL — and be refused."""
    s = Session()
    s.add(trade("T0"))
    s.flush()                                                      # T0: key held
    s.connection().connection.dbapi_connection.commit()            # T1: behind SA's back
    with MigrationLock(url):                                       # T2: nothing holds it
        before = state(url)
        s.add(trade("T3"))
        with pytest.raises(Exception) as exc:                      # T3
            s.flush()
        assert chain_has(exc.value, MigrationInProgress)
        s.rollback()
        assert state(url) == before
    s.close()
    assert "T3" not in state(url)[3]


@needs_pg
def test_a4_a_driver_level_rollback_to_savepoint_cannot_bypass(Session, url):
    """PostgreSQL releases a lock taken inside a savepoint when the savepoint
    is rolled back. Done at the driver, SQLAlchemy never hears of it."""
    s = Session()
    raw = s.connection().connection.dbapi_connection
    raw.execute("SAVEPOINT stale")
    s.add(trade("IN-SAVEPOINT"))
    s.flush()                                                      # key taken inside it
    raw.execute("ROLLBACK TO SAVEPOINT stale")                     # ...and released
    assert held_here(s.connection()) == 0
    with MigrationLock(url):
        before = state(url)
        s.add(trade("AFTER"))
        with pytest.raises(Exception) as exc:
            s.flush()
        assert chain_has(exc.value, MigrationInProgress)
        s.rollback()
        assert state(url) == before
    s.close()


@needs_pg
def test_a1_stale_python_state_cannot_allow_a_write(Session, url):
    """Poison every piece of per-connection state SQLAlchemy keeps; the guard
    must not care."""
    with Session() as s:
        s.add(trade("WARM"))
        s.commit()
        conn = s.connection()
        for key in ("quantdesk_xact_schema_lock", "locked", "already_locked"):
            conn.info[key] = True
        s.rollback()
    before = state(url)
    with MigrationLock(url):
        with Session() as s:
            s.connection().info["quantdesk_xact_schema_lock"] = True
            s.add(trade("STALE"))
            with pytest.raises(Exception) as exc:
                s.commit()
            assert chain_has(exc.value, MigrationInProgress)
    assert state(url) == before


# --- A5: every write form, refused before DML, then working again --------------------

def _forms(Session, engine):
    rows = [dict(created_at=None, symbol=f"BULK{i}", side="BUY", quantity=1, entry=1.0,
                 stop_loss=1.0, status="open") for i in range(3)]
    for r in rows:
        r.pop("created_at")

    def orm_flush():
        with Session() as s:
            s.add(trade("ORM-FLUSH"))
            s.flush()
            s.commit()

    def orm_commit():
        with Session() as s:
            s.add(trade("ORM-COMMIT"))
            s.commit()

    def orm_bulk():
        with Session() as s:
            s.add_all([trade(f"ADD-ALL{i}") for i in range(3)])
            s.commit()

    def session_sql(sql):
        def run():
            with Session() as s:
                s.execute(text(sql))
                s.commit()
        return run

    def executemany():
        with Session() as s:
            s.execute(insert(TradeRecord), rows)          # QuantDesk's bulk/upsert shape
            s.commit()

    def connection_execute():
        with engine.connect() as c:
            c.execute(text(INSERT_SQL.format(s="CONN")))
            c.commit()

    def driver_sql():
        with engine.connect() as c:
            c.exec_driver_sql(INSERT_SQL.format(s="DRIVER"))
            c.commit()

    def ddl():
        with engine.connect() as c:
            c.execute(text("CREATE TABLE qd_ddl_probe (a int)"))
            c.commit()

    return {
        "orm add + flush": orm_flush, "orm add + commit": orm_commit,
        "orm add_all (insertmanyvalues)": orm_bulk,
        "session.execute INSERT": session_sql(INSERT_SQL.format(s="SESSION-INSERT")),
        "session.execute UPDATE": session_sql("UPDATE trades SET symbol = 'UPDATED'"),
        "session.execute DELETE": session_sql("DELETE FROM trades"),
        "session.execute insert(), list of rows (executemany)": executemany,
        "Connection.execute": connection_execute, "exec_driver_sql": driver_sql,
        "DDL": ddl,
        "multi-statement batch": session_sql("SELECT 1; UPDATE trades SET symbol = 'X'"),
    }


@needs_pg
def test_a5_every_write_form_is_refused_during_a_migration_and_works_after(Session,
                                                                           engine, url):
    with Session() as s:
        s.add(trade("SEED"))
        s.commit()
    forms = _forms(Session, engine)
    before = state(url)
    with MigrationLock(url):
        for name, form in forms.items():
            with pytest.raises(Exception) as exc:
                form()
            assert (chain_has(exc.value, MigrationInProgress)
                    or chain_has(exc.value, UnsupportedStatement)), name
            assert state(url) == before, name
        assert q(url, "SELECT to_regclass('public.qd_ddl_probe')") is None
    # Migration over: every form writes normally (the batch stays refused).
    for name, form in forms.items():
        if name == "multi-statement batch":
            with pytest.raises(UnsupportedStatement):
                form()
            continue
        form()
    assert q(url, "SELECT to_regclass('public.qd_ddl_probe')") == "qd_ddl_probe"


# --- B: shutdown drain fails closed ------------------------------------------------

class FakeLease:
    def __init__(self):
        self.released = 0

    def release(self):
        self.released += 1


def test_a_writer_must_say_how_its_stop_is_confirmed():
    with pytest.raises(TypeError):
        Writer("db-writer", lambda: None)            # no "assume stopped" default


def test_b1_a_stop_that_raises_is_a_drain_failure():
    lease, stuck = FakeLease(), {}

    def boom():
        raise RuntimeError("scheduler refused to stop")

    ok = release_after_drain(lease, [Writer("scheduler", boom, lambda: True)],
                             timeout=5, on_stuck=stuck.update)
    assert not ok and lease.released == 0
    assert stuck["scheduler"].startswith("stop raised RuntimeError")


def test_b4_a_stop_that_returns_while_the_worker_lives_is_not_drained():
    lease, stuck = FakeLease(), {}
    done = threading.Event()
    worker = threading.Thread(target=done.wait, args=(30,), daemon=True)
    worker.start()
    try:
        started = time.monotonic()
        ok = release_after_drain(lease, [Writer("liar", lambda: None,
                                                lambda: not worker.is_alive())],
                                 timeout=1.0, on_stuck=stuck.update)
        assert time.monotonic() - started >= 1.0           # it kept waiting
        assert not ok and lease.released == 0
        assert stuck == {"liar": "stop returned but the writer is still running"}
    finally:
        done.set()


def test_b4_it_counts_as_drained_once_the_worker_really_ends():
    lease = FakeLease()
    done = threading.Event()
    worker = threading.Thread(target=done.wait, args=(30,), daemon=True)
    worker.start()
    threading.Timer(0.5, done.set).start()
    assert release_after_drain(lease, [Writer("slow", lambda: None,
                                              lambda: not worker.is_alive())],
                               timeout=10, on_stuck=pytest.fail)
    assert lease.released == 1


def test_a_confirmation_that_raises_is_not_a_confirmation():
    def broken():
        raise RuntimeError("cannot tell")
    assert drain([Writer("w", lambda: None, broken)], timeout=0.3) == {
        "w": "stop returned but the writer is still running"}


def test_a_failing_non_database_step_does_not_gate_the_lease():
    lease = FakeLease()

    def boom():
        raise RuntimeError("socket stuck")
    assert release_after_drain(lease, [Step("angel-feed", boom)], timeout=2,
                               on_stuck=pytest.fail)
    assert lease.released == 1


def test_b3_scheduler_confirmation_is_positive():
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler()
    release = threading.Event()
    sched.add_job(release.wait, args=(30,))
    sched.start()
    time.sleep(0.3)
    assert not scheduler_drained(sched)                     # running
    sched.shutdown(wait=False)
    assert not scheduler_drained(sched)                     # stopped, job still running
    release.set()
    deadline = time.time() + 10
    while not scheduler_drained(sched) and time.time() < deadline:
        time.sleep(0.05)
    assert scheduler_drained(sched)


def test_b3_a_scheduler_whose_shutdown_raises_fails_the_drain():
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler()
    sched.start()
    lease, stuck = FakeLease(), {}

    def shutdown():
        raise RuntimeError("executor would not shut down")
    try:
        ok = release_after_drain(lease, [Writer("scheduler", shutdown,
                                                lambda: scheduler_drained(sched))],
                                 timeout=2, on_stuck=stuck.update)
        assert not ok and lease.released == 0 and "stop raised" in stuck["scheduler"]
    finally:
        sched.shutdown(wait=True)


def test_b5_the_paper_traders_timed_join_does_not_establish_drain():
    """The real PaperTrader.stop() (a 3 s timed join) returns while a worker
    that has not reached its stop check is still alive: not drained."""
    from app.strategy_v2.paper import PaperTrader
    trader = PaperTrader(session_factory=lambda: None)
    done = threading.Event()
    trader._thread = threading.Thread(target=done.wait, args=(60,), daemon=True)
    trader._thread.start()
    lease, stuck = FakeLease(), {}
    try:
        ok = release_after_drain(lease, [Writer("v2-paper", trader.stop,
                                                lambda: not trader.running)],
                                 timeout=4.0, on_stuck=stuck.update)
        assert not ok and lease.released == 0
        assert stuck == {"v2-paper": "stop returned but the writer is still running"}
    finally:
        done.set()
    trader._thread.join(5)
    assert release_after_drain(FakeLease(), [Writer("v2-paper", trader.stop,
                                                    lambda: not trader.running)],
                               timeout=5, on_stuck=pytest.fail)


def test_main_confirms_every_database_writer_positively():
    source = (BACKEND / "app" / "main.py").read_text()
    assert 'Writer("v2-paper", v2_paper.stop, lambda: not v2_paper.TRADER.running)' in source
    assert "lambda: scheduler_drained(scheduler)" in source
    assert 'Step("chain-publisher"' in source and 'Step("angel-feed"' in source
    assert "lambda: True" not in source


# --- B6: a failed drain with a real open write transaction ---------------------------

@needs_pg
@pytest.mark.parametrize("how", ["stop raises", "stop lies"])
def test_b6_a_failed_drain_keeps_every_protection_in_place(Session, url, how):
    from apscheduler.schedulers.background import BackgroundScheduler
    lease = writer_lease("api", url, heartbeat=0, on_lost=lambda: None).acquire()
    writing, finish = threading.Event(), threading.Event()

    def job():
        with Session() as s:
            s.add(trade("INFLIGHT"))
            s.flush()                       # the transaction holds the key
            writing.set()
            finish.wait(60)
            s.commit()

    sched = BackgroundScheduler()
    sched.add_job(job)
    sched.start()
    assert writing.wait(10)

    def raises():
        raise RuntimeError("shutdown failed")

    stop = raises if how == "stop raises" else (lambda: None)
    stuck = {}
    try:
        ok = release_after_drain(lease, [Writer("scheduler", stop,
                                                lambda: scheduler_drained(sched))],
                                 timeout=1.0, on_stuck=stuck.update)
        assert not ok and "scheduler" in stuck
        assert lease.active                                  # never voluntarily released
        engine = create_engine(url, isolation_level="AUTOCOMMIT")
        with engine.connect() as c:
            modes = sorted(h["application_name"].split(":")[0] if h["application_name"]
                           else "?" for h in migration_guard.holders(c))
        engine.dispose()
        assert len(modes) == 2                               # the lease + the open write
        assert migration_refused(url)
    finally:
        finish.set()
        sched.shutdown(wait=True)
        lease.release()
    assert state(url)[3] == ("INFLIGHT",)


FAILSAFE = textwrap.dedent("""
    import sys, threading
    sys.path.insert(0, {backend!r})
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app import migration_guard
    from app.migration_guard import Writer, protect_writes, release_after_drain, writer_lease
    from app.models import TradeRecord
    e = create_engine({url!r}); protect_writes(e); S = sessionmaker(bind=e)
    lease = writer_lease("api", {url!r}, heartbeat=0, on_lost=lambda: None).acquire()
    writing = threading.Event()
    def job():
        s = S()
        s.add(TradeRecord(symbol="FAILSAFE", side="BUY", quantity=1, entry=1.0,
                          stop_loss=1.0, status="open"))
        s.flush(); writing.set(); threading.Event().wait(600)
    t = threading.Thread(target=job, daemon=True); t.start(); writing.wait(10)
    def on_stuck(failed):
        print("DRAIN FAILED", flush=True)
        sys.stdin.readline()                 # hold, so the test can look
        migration_guard._die(failed)         # the real fail-safe: os._exit(70)
    release_after_drain(lease, [Writer("job", lambda: None, lambda: not t.is_alive())],
                        timeout=0.5, on_stuck=on_stuck)
    print("RELEASED - MUST NOT HAPPEN", flush=True)
""")


@needs_pg
def test_b6_the_fail_safe_exit_is_what_releases_the_locks(url):
    proc = subprocess.Popen([sys.executable, "-c", FAILSAFE.format(backend=str(BACKEND),
                                                                   url=url)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        assert proc.stdout.readline().strip() == "DRAIN FAILED"
        assert migration_refused(url)             # alive: lease + open write still hold it
        proc.stdin.write("go\n")
        proc.stdin.flush()
        assert proc.wait(timeout=30) == 70
        assert "MUST NOT HAPPEN" not in proc.stdout.read()
    finally:
        proc.kill()
    deadline = time.time() + 10
    while migration_refused(url) and time.time() < deadline:
        time.sleep(0.1)
    assert not migration_refused(url)             # gone with the process
    assert state(url)[0] == 0                     # the open write never committed
