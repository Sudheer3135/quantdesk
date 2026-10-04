"""Writers and migrations exclude each other in the database itself.

One PostgreSQL advisory lock key, (QUANTDESK, SCHEMA), used at two levels.

TRANSACTION LOCK — the hard invariant (Pass 2E-A.2)
    Every write transaction on the application engine takes the key SHARED
    with `pg_try_advisory_xact_lock_shared`, on the very connection that
    runs it, before its first write statement. PostgreSQL holds it until
    that transaction commits or rolls back and then releases it itself. A
    migration holds the key EXCLUSIVE, so while one runs the attempt fails
    and the write statement is never sent; while a write transaction is
    open, a migration cannot take the key. Installed once, on the engine
    (`protect_writes`), so every Session, flush and `execute` of every
    supported writer passes through it without calling anything.

PROCESS LEASE — lifecycle, not correctness
    A writer process also holds the key SHARED on a dedicated connection
    for its lifetime (`WriterLease`): it fails startup fast during a
    migration, makes running QuantDesk processes visible to `migrate`, and
    orders "lock, then check the schema, then start". If that connection
    dies the lease is gone, and the heartbeat that notices is availability
    monitoring only — the transaction lock is what keeps a write out of a
    migration, heartbeat or not.

MIGRATION
    `MigrationLock` takes the key EXCLUSIVE on one connection, and that same
    connection is handed to Alembic (`config.attributes["connection"]`), so
    the connection that owns the lock is the connection that migrates. If it
    dies, the lock and the migration die together; nothing else carries on.

SQLite has no advisory locks. It is the test and scratch backend only: the
lease is a no-op that says so, writes go unguarded there, and `app.migrate`
refuses to apply migrations to it.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import create_engine, event, make_url, text
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.elements import (
    ReleaseSavepointClause,
    RollbackToSavepointClause,
    SavepointClause,
)

from . import schema_check
from .redact import describe, error_text

log = logging.getLogger(__name__)

# pg_advisory_*lock(int4, int4): the pair shows in pg_locks as classid/objid
# with objsubid = 2. 0x5144 is "QD".
LOCK_CLASS = 0x5144
LOCK_SCHEMA = 1
HEARTBEAT_SECONDS = 5.0
DRAIN_SECONDS = 120.0


class CoordinationError(RuntimeError):
    """The writer/migration protocol refused the operation."""


class MigrationInProgress(CoordinationError):
    """A migration holds the exclusive lock; no write may happen."""


class WritersActive(CoordinationError):
    """Writers hold the shared lock; no migration may start."""

    def __init__(self, message: str, holders: list[dict]):
        super().__init__(message)
        self.holders = holders


class SchemaNotReady(CoordinationError):
    """The lock was free but the schema is not this code's head."""

    def __init__(self, status: schema_check.SchemaStatus):
        super().__init__(status.message)
        self.status = status


def _backend(url: str) -> str | None:
    try:
        return make_url(url).get_backend_name()
    except Exception:  # noqa: BLE001 — unparseable: neither sqlite nor coordinated
        return None


def coordinated(url: str) -> bool:
    return _backend(url) == "postgresql"


def scratch(url: str) -> bool:
    """SQLite: tests and scratch runs, the only backend allowed to skip the
    protocol. Anything else that is not PostgreSQL is refused, not skipped."""
    return _backend(url) == "sqlite"


def _default_url() -> str:
    from .config import get_settings
    return get_settings().database_url


def _connect(url: str, application_name: str, *, autocommit: bool, connect_timeout: int = 5):
    """A dedicated connection. The lease's is autocommit: its lock is a
    session lock and an open transaction on it would stall the very DDL it
    coordinates. The migration's is not: Alembic runs transactional DDL on
    it."""
    engine = create_engine(
        url, poolclass=NullPool,
        **({"isolation_level": "AUTOCOMMIT"} if autocommit else {}),
        connect_args={"application_name": application_name[:63],
                      "connect_timeout": connect_timeout,
                      # A peer that vanishes without closing the socket is
                      # noticed by the server within about a minute.
                      "keepalives": 1, "keepalives_idle": 30,
                      "keepalives_interval": 10, "keepalives_count": 3})
    try:
        return engine, engine.connect()
    except Exception:
        engine.dispose()
        raise


def holders(conn) -> list[dict]:
    """Who holds or waits for the coordination key on this database —
    process leases and open write transactions alike."""
    rows = conn.execute(text(
        "SELECT l.pid, l.mode, l.granted, a.application_name, "
        "       host(a.client_addr) AS client_addr, a.backend_start "
        "FROM pg_locks l LEFT JOIN pg_stat_activity a ON a.pid = l.pid "
        "WHERE l.locktype = 'advisory' AND l.classid = :c AND l.objid = :o "
        "  AND l.objsubid = 2 AND l.pid <> pg_backend_pid() "
        "  AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
        "ORDER BY a.backend_start"), {"c": LOCK_CLASS, "o": LOCK_SCHEMA}).mappings()
    return [dict(r) for r in rows]


def _summary(found: list[dict]) -> str:
    return "; ".join(f"pid {h['pid']} {h['application_name'] or '?'} "
                     f"({'shared' if h['mode'] == 'ShareLock' else 'exclusive'})"
                     for h in found) or "none visible"


# --- The transaction lock: the hard invariant ------------------------------
#
# Every write statement asks PostgreSQL, on its own connection and in its
# own transaction, for the shared key — every time. No Python state records
# that a transaction "already has" it: a cached flag is exactly what a raw
# COMMIT or a driver-level ROLLBACK TO SAVEPOINT made stale (Pass 2E-A.3).
# Re-taking a shared transaction lock the transaction already holds is a
# no-op for PostgreSQL, so the only cost is one round trip per write.

class UnsupportedStatement(CoordinationError):
    """A statement the protected engine will not send: several statements in
    one string, raw transaction control, or text it cannot scan safely."""


# How many authority checks this process has made. Diagnostics and tests
# only; nothing reads it to decide anything.
AUTHORITY_CHECKS = 0

# Statements that cannot change data. Anything else — INSERT, UPDATE,
# DELETE, MERGE, COPY, DDL, LOCK, CALL, DO, a WITH that may wrap DML, an
# EXPLAIN that may ANALYZE one — is treated as a write. Conservative on
# purpose: a false "write" only takes a shared lock; a false "read" would
# let a write into a migration.
_READ_ONLY = frozenset({"SELECT", "SHOW", "VALUES", "TABLE", "SET", "RESET", "DECLARE",
                        "FETCH", "MOVE", "CLOSE", "DISCARD", "LISTEN", "UNLISTEN",
                        "DEALLOCATE"})
# Transaction control. SQLAlchemy runs BEGIN/COMMIT/ROLLBACK through the
# driver, never as SQL, and its savepoints as compiled clause objects; so a
# statement like these arriving as text is raw and refused.
_CONTROL = frozenset({"BEGIN", "START", "COMMIT", "END", "ROLLBACK", "ABORT",
                      "SAVEPOINT", "RELEASE"})
_SA_SAVEPOINTS = (SavepointClause, RollbackToSavepointClause, ReleaseSavepointClause)
_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
_IDENT = re.compile(r"[A-Za-z0-9_$]")


def statements(sql: str) -> list[str]:
    """Split SQL into its top-level statements, PostgreSQL-aware: semicolons
    inside '...' (with '' escapes), E'...' (with backslash escapes), "..."
    identifiers, $tag$...$tag$ bodies, -- comments and nested /* */ comments
    do not count. Empty pieces (a trailing ';', only a comment) are dropped.
    Unterminated quoting raises UnsupportedStatement: fail closed."""
    parts, begin, i, n = [], 0, 0, len(sql)
    while i < n:
        c = sql[i]
        prev = sql[i - 1] if i else " "
        if c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
        elif c == "/" and sql.startswith("/*", i):
            depth, i = 1, i + 2
            while depth and i < n:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise UnsupportedStatement("unterminated /* comment in SQL")
        elif c in "'\"":
            escapes = c == "'" and prev in "eE" and (i < 2 or not _IDENT.match(sql[i - 2]))
            i += 1
            while True:
                if i >= n:
                    raise UnsupportedStatement("unterminated quoted text in SQL")
                if escapes and sql[i] == "\\":
                    i += 2
                elif sql[i] == c:
                    if sql.startswith(c * 2, i):
                        i += 2                   # '' or "" is an escaped quote
                    else:
                        i += 1
                        break
                else:
                    i += 1
        elif c == "$" and not _IDENT.match(prev) and (m := _DOLLAR.match(sql, i)):
            close = sql.find(m.group(0), m.end())
            if close < 0:
                raise UnsupportedStatement("unterminated $-quoted text in SQL")
            i = close + len(m.group(0))
        elif c == ";":
            parts.append(sql[begin:i])
            begin = i = i + 1
        else:
            i += 1
    parts.append(sql[begin:])
    return [p for p in parts if _verb(p) is not None]


def _verb(statement: str) -> str | None:
    """The first keyword, past whitespace, comments and parentheses."""
    i, n = 0, len(statement)
    while i < n:
        c = statement[i]
        if c.isspace() or c == "(":
            i += 1
        elif statement.startswith("--", i):
            j = statement.find("\n", i)
            i = n if j < 0 else j + 1
        elif statement.startswith("/*", i):
            depth, i = 1, i + 2                   # PostgreSQL block comments nest
            while depth and i < n:
                if statement.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif statement.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
        else:
            word = re.match(r"[A-Za-z_]+", statement[i:])
            return word.group(0).upper() if word else statement[i]
    return None


def is_write(statement: str) -> bool:
    """For ONE statement (see `statements`): may it change data?"""
    verb = _verb(statement)
    if verb is None:
        return False
    if verb == "SELECT" and re.search(r"\bINTO\b", statement, re.IGNORECASE):
        return True                      # SELECT ... INTO creates a table
    return verb not in _READ_ONLY and verb not in _CONTROL


def _sqlalchemy_savepoint(context) -> bool:
    compiled = getattr(context, "compiled", None)
    return isinstance(getattr(compiled, "statement", None), _SA_SAVEPOINTS)


def _guard_write(conn, cursor, statement, parameters, context, executemany) -> None:
    global AUTHORITY_CHECKS
    if _sqlalchemy_savepoint(context):
        return                                        # begin_nested()'s own machinery
    pieces = statements(statement or "")
    if len(pieces) > 1:
        raise UnsupportedStatement(
            f"{len(pieces)} statements in one execute; the application engine "
            "runs one statement at a time")
    verb = _verb(pieces[0]) if pieces else None
    if verb in _CONTROL:
        raise UnsupportedStatement(
            f"raw {verb} refused; use the SQLAlchemy transaction API "
            "(commit, rollback, begin_nested)")
    if not pieces or not is_write(pieces[0]):
        return
    raw = conn.connection.dbapi_connection
    if getattr(raw, "autocommit", False):
        # Each statement would be its own transaction: a lock taken now
        # would be gone before the write ran.
        raise CoordinationError("an autocommit write cannot hold the schema lock; "
                                "QuantDesk writes run inside a transaction")
    # Every write, every time: same physical connection, same transaction.
    AUTHORITY_CHECKS += 1
    with raw.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock_shared(%s, %s)",
                    (LOCK_CLASS, LOCK_SCHEMA))
        got = cur.fetchone()[0]
    if not got:
        raise MigrationInProgress("a migration holds the schema lock; this write "
                                  "was refused before it reached the database")


def protect_writes(engine) -> bool:
    """Install the per-write guard on an engine. Idempotent; a no-op (False)
    on SQLite. One listener, no per-transaction state to keep in step."""
    if engine.dialect.name != "postgresql":
        return False
    if not event.contains(engine, "before_cursor_execute", _guard_write):
        event.listen(engine, "before_cursor_execute", _guard_write)
    return True


# --- The process lease: lifecycle, visibility, fail-fast startup -----------

def _terminate_self() -> None:
    """Stop this process: SIGTERM for a clean shutdown, then a hard exit."""
    log.critical("writer lease lost and not recoverable — stopping this process")
    threading.Timer(20.0, lambda: os._exit(75)).start()
    os.kill(os.getpid(), signal.SIGTERM)


@dataclass
class WriterLease:
    """The shared key, held for the lifetime of one writer process.

    Lifecycle only: the transaction lock (`protect_writes`) is what keeps
    writes out of a migration. `heartbeat` <= 0 disables the monitor."""
    role: str
    url: str
    on_lost: Callable[[], None] = _terminate_self
    heartbeat: float = HEARTBEAT_SECONDS

    def __post_init__(self):
        self._engine = self._conn = None
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.active = False

    @property
    def application_name(self) -> str:
        return f"quantdesk-writer:{self.role}:{socket.gethostname()}:{os.getpid()}"

    def _take(self) -> None:
        engine, conn = _connect(self.url, self.application_name, autocommit=True)
        try:
            got = conn.execute(text("SELECT pg_try_advisory_lock_shared(:c, :o)"),
                               {"c": LOCK_CLASS, "o": LOCK_SCHEMA}).scalar()
            if not got:
                raise MigrationInProgress(
                    f"{describe(self.url)}: a migration holds the schema lock "
                    f"({_summary(holders(conn))}); {self.role} not started")
            # Only now, with migrations shut out, is the schema worth checking.
            status = schema_check.check(self.url)
            if not status.ok:
                raise SchemaNotReady(status)
        except BaseException:
            _close_quietly(conn, engine)
            raise
        self._engine, self._conn = engine, conn

    def acquire(self) -> WriterLease:
        if scratch(self.url):
            log.warning("%s: %s has no advisory locks; writer/migration coordination "
                        "is off (test and scratch databases only)",
                        self.role, describe(self.url))
            return self
        if not coordinated(self.url):
            raise CoordinationError(f"{describe(self.url)}: writers are coordinated on "
                                    "PostgreSQL only; refusing to start")
        try:
            self._take()
        except CoordinationError:
            raise
        except Exception as exc:  # noqa: BLE001 — unreachable, auth, driver
            raise CoordinationError(f"{describe(self.url)}: cannot take the writer "
                                    f"lock: {error_text(exc, self.url)}") from None
        self.active = True
        if self.heartbeat > 0:
            self._thread = threading.Thread(target=self._watch, daemon=True,
                                            name=f"writer-lease-{self.role}")
            self._thread.start()
        log.info("%s holds the shared schema lock on %s", self.role, describe(self.url))
        return self

    def _alive(self) -> bool:
        try:
            with self._guard:
                return self._conn is not None and bool(self._conn.execute(text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND classid = :c AND objid = :o AND objsubid = 2 "
                    "AND mode = 'ShareLock' AND granted AND pid = pg_backend_pid()"),
                    {"c": LOCK_CLASS, "o": LOCK_SCHEMA}).scalar())
        except Exception:  # noqa: BLE001
            return False

    def _watch(self) -> None:
        while not self._stop.wait(self.heartbeat):
            if self._alive():
                continue
            log.error("%s: writer lease connection lost; re-taking the shared lock", self.role)
            with self._guard:
                self._close()
                try:
                    self._take()
                    log.warning("%s: writer lease re-taken", self.role)
                    continue
                except Exception as exc:  # noqa: BLE001
                    log.critical("%s: cannot re-take the writer lease: %s", self.role,
                                 error_text(exc, self.url))
            self.active = False
            self.on_lost()
            return

    def _close(self) -> None:
        conn, engine, self._conn, self._engine = self._conn, self._engine, None, None
        _close_quietly(conn, engine)             # closing the session releases the lock

    def release(self) -> None:
        self._stop.set()
        with self._guard:
            self._close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=self.heartbeat + 5)
        self.active = False

    def __enter__(self) -> WriterLease:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def writer_lease(role: str, url: str | None = None, **kw) -> WriterLease:
    """A lease for one writer process; use as a context manager or call
    `acquire()` at startup and `release()` at shutdown."""
    return WriterLease(role=role, url=url or _default_url(), **kw)


def _close_quietly(conn, engine) -> None:
    if conn is not None:
        try:
            conn.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("closing a coordination connection: %s", error_text(exc))
    if engine is not None:
        engine.dispose()


# --- Draining in-process writers before the lease goes ---------------------
#
# The lease is released voluntarily only when every in-process database
# writer is *positively confirmed* stopped (Pass 2E-A.3): its stop returned
# without raising AND its own confirmation says it can no longer write. A
# stop that raises, a stop that returns while the writer lives on, a
# confirmation that raises, or a timeout: all are drain failures, and a
# failure keeps the lease held and ends the process instead.

@dataclass
class Writer:
    """An in-process database writer. `stop` signals it and may block while
    it drains; `stopped` must positively confirm the writer itself is gone —
    no default, because "the stop call returned" proves nothing."""
    name: str
    stop: Callable[[], object]
    stopped: Callable[[], bool]


@dataclass
class Step:
    """A shutdown step that writes no database rows (Redis publishers). Run
    in order and bounded by the same deadline, but it never gates the lease;
    a failure is logged."""
    name: str
    stop: Callable[[], object]


@dataclass
class _Run:
    item: Writer | Step
    thread: threading.Thread | None = None
    error: BaseException | None = None
    returned: bool = False


def scheduler_drained(scheduler) -> bool:
    """APScheduler, positively: stopped, no job instance running in any
    executor, and no pool thread alive. Anything unreadable is "no"."""
    try:
        from apscheduler.schedulers.base import STATE_STOPPED
        if scheduler.state != STATE_STOPPED:
            return False
        for executor in scheduler._executors.values():          # noqa: SLF001
            if any(count > 0 for count in executor._instances.values()):  # noqa: SLF001
                return False
            pool = getattr(executor, "_pool", None)
            if any(t.is_alive() for t in getattr(pool, "_threads", ())):
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


def _confirmed(writer: Writer) -> bool:
    try:
        return writer.stopped() is True
    except Exception:  # noqa: BLE001 — cannot confirm is not confirmed
        return False


def drain(items: list[Writer | Step], timeout: float = DRAIN_SECONDS) -> dict[str, str]:
    """Stop everything in order, each allowed to finish what it is doing.
    Returns {writer name: why it is not confirmed drained}; empty means every
    Writer is positively confirmed stopped. Steps never appear in it."""
    deadline = time.monotonic() + timeout
    runs: list[_Run] = []
    for item in items:
        run = _Run(item)

        def call(run=run):
            try:
                run.item.stop()
                run.returned = True
            except BaseException as exc:  # noqa: BLE001 — recorded, never swallowed
                run.error = exc

        run.thread = threading.Thread(target=call, name=f"drain-{item.name}", daemon=True)
        run.thread.start()
        run.thread.join(max(0.0, deadline - time.monotonic()))
        runs.append(run)

    def failures() -> dict[str, str]:
        out = {}
        for run in runs:
            if isinstance(run.item, Step):
                continue
            if run.error is not None:
                out[run.item.name] = f"stop raised {type(run.error).__name__}: " \
                                     f"{error_text(run.error)}"
            elif run.thread.is_alive():
                out[run.item.name] = "stop did not return"
            elif not _confirmed(run.item):
                out[run.item.name] = "stop returned but the writer is still running"
        return out

    while True:
        failed = failures()
        # An exception is final: waiting cannot turn it into a confirmed stop.
        pending = [n for n, why in failed.items() if not why.startswith("stop raised")]
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    for run in runs:
        if isinstance(run.item, Step) and (run.error is not None or run.thread.is_alive()):
            log.warning("shutdown step %s did not finish cleanly (no database writes)",
                        run.item.name)
    return failed


def _die(failed: dict[str, str]) -> None:
    log.critical("writers not confirmed drained: %s. Exiting without releasing the "
                 "schema lease; PostgreSQL releases it when this process is gone", failed)
    os._exit(70)


def release_after_drain(lease: WriterLease, items: list[Writer | Step], *,
                        timeout: float = DRAIN_SECONDS,
                        on_stuck: Callable[[dict[str, str]], None] = _die) -> bool:
    """Drain, then release the lease — only if every Writer is positively
    confirmed stopped. Otherwise the lease stays held and `on_stuck` ends
    the process: the lease is never released while something may write."""
    failed = drain(items, timeout)
    if failed:
        on_stuck(failed)
        return False
    lease.release()
    return True


# --- The migration's exclusive authority -----------------------------------

class MigrationLock:
    """The exclusive key, on the one connection the migration runs on.

    `connection` is handed to Alembic; the revision checks read on it too.
    Every failure — connect, lock, holder listing, cleanup — leaves here as a
    redacted CoordinationError."""

    def __init__(self, url: str | None = None):
        self.url = url or _default_url()
        self._engine = self._conn = None

    @property
    def connection(self):
        return self._conn

    def _fail(self, what: str, exc: BaseException) -> CoordinationError:
        return CoordinationError(f"{describe(self.url)}: {what}: {error_text(exc, self.url)}")

    def __enter__(self) -> MigrationLock:
        if not coordinated(self.url):
            raise CoordinationError(f"{describe(self.url)} has no advisory locks; "
                                    "migrations are coordinated on PostgreSQL only")
        try:
            self._engine, self._conn = _connect(
                self.url, f"quantdesk-migration:{socket.gethostname()}:{os.getpid()}",
                autocommit=False)
        except Exception as exc:  # noqa: BLE001
            raise self._fail("cannot connect", exc) from None
        try:
            got = self._conn.execute(text("SELECT pg_try_advisory_lock(:c, :o)"),
                                     {"c": LOCK_CLASS, "o": LOCK_SCHEMA}).scalar()
            found = [] if got else holders(self._conn)
            # End the read transaction; the session lock outlives it, and
            # Alembic must find the connection idle to run its own.
            self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            self._release()
            raise self._fail("cannot take the migration lock", exc) from None
        if not got:
            self._release()
            raise WritersActive(f"{describe(self.url)}: {len(found)} writer(s) hold the "
                                f"schema lock ({_summary(found)}); stop them first", found)
        return self

    def still_held(self) -> bool:
        """Diagnostics only. Correctness does not depend on it: Alembic runs
        on this connection, so losing it stops the migration outright."""
        try:
            held = bool(self._conn.execute(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND classid = :c AND objid = :o AND objsubid = 2 "
                "AND mode = 'ExclusiveLock' AND granted AND pid = pg_backend_pid()"),
                {"c": LOCK_CLASS, "o": LOCK_SCHEMA}).scalar())
            self._conn.rollback()
            return held
        except Exception:  # noqa: BLE001
            return False

    def _release(self) -> None:
        conn, engine, self._conn, self._engine = self._conn, self._engine, None, None
        if conn is not None:
            try:
                conn.rollback()
                conn.execute(text("SELECT pg_advisory_unlock(:c, :o)"),
                             {"c": LOCK_CLASS, "o": LOCK_SCHEMA})
                conn.commit()
            except Exception as exc:  # noqa: BLE001 — closing releases it regardless
                log.debug("migration unlock: %s", error_text(exc, self.url))
        _close_quietly(conn, engine)

    def __exit__(self, *exc) -> None:
        self._release()


def run_as_writer(role: str, main: Callable[[], int]) -> int:
    """Run a standalone writer script under a lease held for its whole run.
    A refusal (migration in progress, schema not at head, database
    unreachable) prints why and returns 3 without calling `main`. Its writes
    are guarded per transaction by the engine regardless."""
    try:
        lease = writer_lease(role).acquire()
    except CoordinationError as exc:
        print(f"{role} not started: {exc}", file=sys.stderr)
        return 3
    try:
        return main()
    finally:
        lease.release()
