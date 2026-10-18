"""The controller's one PostgreSQL session, held open across the critical stages.

Writer exclusion uses QuantDesk's own coordination key (backend/app/
migration_guard.py at 2d1d25a): every supported write transaction takes the
key (0x5144, 1) SHARED with `pg_try_advisory_xact_lock_shared` before its first
write and refuses to write if it cannot; every writer process holds it SHARED
for its lifetime (the writer lease). A migration holds it EXCLUSIVE.

After the API has stopped and drained, with PostgreSQL still running, the
controller takes the key EXCLUSIVE on this session:

  - success proves no writer lease and no write transaction remains;
  - while it is held, every supported write anywhere refuses itself before
    reaching the database.

The key is a session lock: it lasts exactly as long as this one server
session. So the controller proves, at every later stage, that the very
session that took it (same server process id) still holds it EXCLUSIVE
(`verify_writer_key`); a lost session means a writer could have entered, and
the shutdown is abandoned and restored.

Through the cluster's stop the session is kept busy in one bounded statement
(`hold_through_shutdown`), having first read the WAL insert position while
it still holds the key. Its ending is an observation, not a proof: SQLSTATE
57P01 says only that THIS session was ended by an administrator command - a
fast shutdown or an individual `pg_terminate_backend` give the same code - and
nothing about whether other sessions could still commit after it ended. The
evidence the controller requires instead is the WAL: after the clean stop,
the shutdown checkpoint must lie exactly at the position read under the key,
so no WAL record at all - no commit by anyone - was written after the last
moment the key was proved held (controller._cluster_shut_down). Any other
WAL (a writer's commit, or harmless background WAL) abandons the shutdown.
That PostgreSQL places the shutdown checkpoint exactly there when nothing
else was written is UNVERIFIED on a real cluster. With
`client_connection_check_interval` the server also ends the holding
statement within a second if this client is killed, so an abandoned
shutdown does not leave a server process holding the key.

No QuantDesk code or behaviour changes: this is the coordination the
application already defines.

The role (`quantdesk_power`) is read-only by default and can read
`paper_positions.status` and `trades.status` only. Advisory-lock functions and
`pg_stat_activity` are available to every role.
"""
from __future__ import annotations

import os
import re
import selectors
import subprocess
import time

WRITER_KEY = (0x5144, 1)   # migration_guard.LOCK_CLASS, migration_guard.LOCK_SCHEMA
ADMIN_SHUTDOWN = re.compile(r"^FATAL:\s+57P01\s*$", re.M)   # psql VERBOSITY=sqlstate
TERMINATED_BY_SHUTDOWN = "terminated-by-shutdown"   # 57P01: this session only (see above)
LSN = re.compile(r"^[0-9A-F]{1,8}/[0-9A-F]{1,8}$")


def lsn_value(text: str) -> int:
    """A WAL position 'HI/LO' as an integer; ValueError if malformed."""
    if not isinstance(text, str) or not LSN.match(text.strip().upper()):
        raise ValueError(f"{text!r} is not a WAL position")
    high, low = text.strip().upper().split("/")
    return (int(high, 16) << 32) | int(low, 16)


class DatabaseError(Exception):
    pass


class PsqlSession:
    """A psql process speaking a marker protocol; any error ends the session."""

    def __init__(self, cfg: dict, popen=subprocess.Popen):
        pg = cfg["postgres"]
        env = {"PATH": "/usr/bin:/bin", "PGSERVICEFILE": pg["service_file"],
               "PGSERVICE": pg["service"], "PGPASSFILE": pg["pass_file"],
               "PGCONNECT_TIMEOUT": "5", "PGAPPNAME": "quantdesk-power",
               "PGOPTIONS": "-c default_transaction_read_only=on "
                            f"-c statement_timeout={int(cfg['timeouts']['db_statement'] * 1000)} "
                            "-c lock_timeout=1000 -c client_connection_check_interval=1000"}
        self.timeout = cfg["timeouts"]["db_statement"]
        env["LC_ALL"] = env["LANG"] = "C"
        self.proc = popen([cfg["programs"]["psql"], "-X", "-q", "-A", "-t", "-v",
                           "ON_ERROR_STOP=1", "-v", "VERBOSITY=sqlstate"],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env, bufsize=0)
        self._seq = 0
        self._buffer = b""
        self.key_pid: int | None = None
        self.wal_lsn: str | None = None
        self._guarding = False

    def query(self, sql: str) -> list[str]:
        """Run one statement; returns its output lines. Raises DatabaseError on anything else."""
        marker = self._send(sql)
        lines, deadline = [], time.monotonic() + self.timeout + 5
        fd = self.proc.stdout.fileno()
        selector = selectors.DefaultSelector()
        selector.register(fd, selectors.EVENT_READ)
        try:
            while True:
                while b"\n" in self._buffer:
                    raw, self._buffer = self._buffer.split(b"\n", 1)
                    line = raw.decode(errors="replace")
                    if line == marker:
                        return lines
                    lines.append(line)
                    if len(lines) > 10000:
                        raise DatabaseError("the database answered with too many lines")
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    self.close(kill=True)
                    raise DatabaseError("the database did not answer in time")
                chunk = os.read(fd, 65536)
                if not chunk:
                    raise DatabaseError("the database session ended: "
                                        + self._stderr()[:200])
                self._buffer += chunk
        finally:
            selector.close()

    def _send(self, sql: str) -> str:
        if self._guarding:
            raise DatabaseError("the session is holding the key through the shutdown")
        if self.proc.poll() is not None:
            raise DatabaseError("the database session has ended")
        self._seq += 1
        marker = f"__QD_POWER_{self._seq}__"
        try:
            self.proc.stdin.write(f"{sql.strip().rstrip(';')};\n\\echo {marker}\n".encode())
            self.proc.stdin.flush()
        except OSError as exc:
            raise DatabaseError(f"the database session could not be written ({exc})") from exc
        return marker

    def _stderr(self) -> str:
        try:
            self.proc.wait(timeout=2)
            return (self.proc.stderr.read() or b"").decode(errors="replace")
        except (subprocess.TimeoutExpired, OSError):
            return ""

    def scalar(self, sql: str) -> str:
        rows = self.query(sql)
        if len(rows) != 1:
            raise DatabaseError(f"expected one row, got {len(rows)}")
        return rows[0]

    def take_writer_key(self) -> bool:
        row = self.scalar(f"SELECT pg_try_advisory_lock({WRITER_KEY[0]}, {WRITER_KEY[1]})::text"
                          " || ',' || pg_backend_pid()")
        got, pid = row.split(",")
        if got == "true":
            self.key_pid = int(pid)
        return got == "true"

    def verify_writer_key(self) -> None:
        """Raise DatabaseError unless the session that took the key still holds it EXCLUSIVE."""
        if self.key_pid is None:
            raise DatabaseError("the writer key was never taken")
        row = self.scalar(
            "SELECT pg_backend_pid() || ',' || (SELECT count(*) FROM pg_locks WHERE "
            f"locktype = 'advisory' AND classid = {WRITER_KEY[0]} AND objid = {WRITER_KEY[1]} "
            "AND objsubid = 2 AND mode = 'ExclusiveLock' AND granted "
            "AND pid = pg_backend_pid())")
        pid, held = row.split(",")
        if int(pid) != self.key_pid or held != "1":
            raise DatabaseError("the writer key is no longer held by the controller's session")

    def hold_through_shutdown(self, seconds: int) -> None:
        """Verify the key, read the WAL insert position under it, then keep this session in
        one statement while the cluster stops."""
        self.verify_writer_key()
        position = self.scalar("SELECT pg_current_wal_insert_lsn()")
        try:
            lsn_value(position)
        except ValueError as exc:
            raise DatabaseError(str(exc)) from exc
        self.verify_writer_key()                  # still the same session, still held
        self.wal_lsn = position.strip().upper()
        self.query(f"SET statement_timeout = '{int(seconds)}s'")
        self._send(f"SELECT pg_sleep({int(seconds)})")
        self._guarding = True

    def guard_intact(self) -> bool:
        """True while the holding statement is still running: alive, and nothing said yet."""
        if not self._guarding or self.proc.poll() is not None or self._buffer:
            return False
        selector = selectors.DefaultSelector()
        try:
            for stream in (self.proc.stdout, self.proc.stderr):
                selector.register(stream.fileno(), selectors.EVENT_READ)
            return not selector.select(0)
        finally:
            selector.close()

    def shutdown_outcome(self, wait: float) -> str:
        """How the holding statement ended, once the cluster's stop has returned."""
        if not self._guarding:
            return "the session was not holding the key through the shutdown"
        try:
            _, err = self.proc.communicate(timeout=wait)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.communicate()
            return "the holding statement was still running after the cluster's stop"
        text = (err or b"").decode(errors="replace")
        if ADMIN_SHUTDOWN.search(text):
            return TERMINATED_BY_SHUTDOWN
        return "the session ended otherwise: " + (" ".join(text.split())[:200] or "no message")

    def open_positions(self) -> tuple[int, int]:
        row = self.scalar("SELECT (SELECT count(*) FROM paper_positions WHERE status = 'open')"
                          " || ',' || (SELECT count(*) FROM trades WHERE status = 'open')")
        paper, journal = row.split(",")
        return int(paper), int(journal)

    def role_sessions(self, role: str) -> int:
        if not role.replace("_", "").isalnum():
            raise DatabaseError("unexpected role name")
        return int(self.scalar(f"SELECT count(*) FROM pg_stat_activity WHERE usename = "
                               f"'{role}' AND pid <> pg_backend_pid()"))

    def close(self, kill: bool = False) -> None:
        if self.proc.poll() is not None:
            self._close_streams()
            return
        if kill or self._guarding:
            self.proc.kill()
        else:
            try:
                self.proc.stdin.write(b"\\q\n")
                self.proc.stdin.flush()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self._close_streams()

    def _close_streams(self) -> None:
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None
