"""Everything the observer reads from the machine, and nothing it could write.

Each method returns plain data or raises `ProbeError`. A caller that cannot
read a fact records UNKNOWN; nothing here guesses, retries, or repairs.

The three allowlists below are the observer's whole reach: the programs it
may run, the `systemctl` verb it may use, and the API paths it may fetch.
`/signals/live` is not among them and cannot be added by configuration.
"""
from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import time
import urllib.parse
import urllib.request
import zlib
from datetime import UTC, date, datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

from .store import STARTUP_CAPTURE_SCHEMA, CaptureContention, CorruptRecord, capture_key

PROGRAMS = frozenset({"git", "systemctl", "ps", "ss", "timedatectl", "psql"})
SYSTEMCTL_VERBS = frozenset({"show"})
API_PATHS = frozenset({"/health", "/health/scheduler", "/health/feed", "/v2/status"})

COMMAND_TIMEOUT = 10.0
HTTP_TIMEOUT = 5.0
API_MAX_BYTES = 1024 * 1024
SQL_STATEMENT_TIMEOUT_MS = 15000
SQL_TIMEOUT = 30.0


class ProbeError(Exception):
    """A fact could not be read. The reason is recorded, never the fact."""


class RunTimeout(BaseException):
    """The whole run used up its time.

    A BaseException on purpose: a check that catches `Exception` to record an
    unreadable fact as UNKNOWN must not also swallow the end of the run.
    """


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProbeError(f"the API answered with a redirect ({code}); it was not followed")


def loopback_base(base: str) -> str:
    """`base` if it is plain HTTP to a literal loopback address, else an error.

    A hostname is refused even if it would resolve to loopback: the observer
    must not depend on name resolution to stay on this machine.
    """
    parts = urllib.parse.urlsplit(base)
    try:
        local = ipaddress.ip_address(parts.hostname or "").is_loopback
    except ValueError:
        local = False
    if (parts.scheme != "http" or not local or parts.username or parts.password
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise ProbeError("api_base must be http:// to a literal loopback address and port")
    return base.rstrip("/")


def _run(argv: list[str], *, timeout: float = COMMAND_TIMEOUT,
         env: dict | None = None) -> str:
    if argv[0] not in PROGRAMS:
        raise ProbeError(f"{argv[0]} is not a program the observer may run")
    if argv[0] == "systemctl" and argv[1] not in SYSTEMCTL_VERBS:
        raise ProbeError(f"systemctl {argv[1]} is not permitted")
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env=env, stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"{argv[0]} did not answer within {timeout:.0f}s") from exc
    except OSError as exc:
        raise ProbeError(f"{argv[0]} could not be run: {exc.strerror}") from exc
    if done.returncode != 0:
        raise ProbeError(f"{argv[0]} exited {done.returncode}")
    return done.stdout


# QuantDesk logs with "%(asctime)s %(levelname)s %(name)s: %(message)s"
# (backend/app/main.py at the frozen revision). A line counts only when the
# named logger wrote it; the same words inside another logger's message do not.
def _line(level: str, logger: str, message: str) -> re.Pattern:
    return re.compile(rf"^\S+ \S+ {level} {re.escape(logger)}: {message}")


_TENTATIVE = _line("INFO", "apscheduler.scheduler", "Adding job tentatively")
_STARTED = _line("INFO", "apscheduler.scheduler", "Scheduler started")
_ADDED = _line("INFO", "apscheduler.scheduler", r'Added job "([^"]*)" to job store "')
_AGENT = _line("INFO", "app.workers.agent", r"Nifty agent scheduled every (\S+) minutes\.")
_TICKER = _line("INFO", "app.workers.ticker", r"price ticker running every (\S+)s ")
_COLLECTOR = _line("INFO", "app.workers.option_collector",
                   r"option chain collector running every (\S+)s ")
_NO_COLLECTOR = _line("WARNING", "app.workers.option_collector",
                      r"option_snapshot_interval_seconds is .*collector disabled")
_RUN = _line("INFO", "apscheduler.executors.default",
             r'Running job "(.*) \(trigger: ([^,"]*), next run at: [^"]*\)" \(scheduled at '
             r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?[+-]\d\d:\d\d)\)")
_INTERVAL = re.compile(r"^interval\[(\d+):(\d\d):(\d\d)\]$")
MAX_LOGGED_RUNS = 5000
# The leading "%(asctime)s" of a QuantDesk log line: the API process's local
# time, milliseconds after a comma. It carries no zone. It is read as IST only
# under an established `backend_log_basis` (see `log_basis`), never from the
# host's or systemd's display zone.
_ASCTIME = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3}) ")
IST_ZONE = timezone(timedelta(hours=5, minutes=30))
# The only application logging zone the observer can interpret: fixed +05:30,
# no daylight saving.
LOG_TIMEZONES = frozenset({"Asia/Kolkata"})
LOG_BASIS_TEXT_FIELDS = ("timezone_basis", "single_writer_basis", "verified_by")

# The backend log and the names logrotate gives it under the frozen
# revision's scripts/native/logrotate/quantdesk (daily, rotate 14, compress,
# delaycompress, copytruncate): NAME, NAME.1, then NAME.2.gz ... NAME.14.gz.
# `NAME.N` and `NAME.N.gz` are both accepted for every N; both at once is
# ambiguous. Nothing else in the directory is looked at.
MAX_ROTATED_LOGS = 14
LOG_FILE_MAX_BYTES = 64 * 1024 ** 2           # decompressed, one file, one pass
LOG_TOTAL_MAX_BYTES = 256 * 1024 ** 2         # decompressed, the whole inspection
LOG_COMPRESSED_MAX_BYTES = 16 * 1024 ** 2     # on disk, one .gz file
LOG_LINE_MAX_BYTES = 64 * 1024                # a longer line is skipped, never split
LOG_HEAD_MAX_BYTES = 1024 ** 2                # searched for a file's first timestamp
LOG_READ_SECONDS = 20.0
# systemd shows ExecMainStartTimestamp to the second, so a process shown as
# started at T began somewhere in [T, T+1). A scheduler start is admitted as
# the running process's own only if its first line is logged in
# [T + STARTUP_EARLIEST_SECONDS, T + STARTUP_LATEST_SECONDS): never inside the
# displayed second, where it could predate the process. The gap was about
# 10 s on the Azure VM on 2026-10-16. This is an admission policy, not proof
# that the process wrote the line.
STARTUP_EARLIEST_SECONDS = 1
STARTUP_LATEST_SECONDS = 60
# The running system's boot, so a monotonic start time is compared only within it.
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
_BOOT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# A captured start (v0.4.12): the start's declaration lines only, never its runs.
CAPTURE_MAX_LINES = 32
CAPTURE_MAX_BYTES = 32 * 1024
# systemd's default timestamp form, e.g. "Fri 2026-10-16 02:25:19 IST".
_UNIT_TIME = re.compile(r"^[A-Z][a-z]{2} (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (IST|UTC)$")
_UNIT_ZONES = {"IST": IST_ZONE, "UTC": UTC}


def log_time(line: str) -> datetime | None:
    found = _ASCTIME.match(line)
    if not found:
        return None
    try:
        moment = datetime.strptime(found.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return moment.replace(microsecond=int(found.group(2)) * 1000, tzinfo=IST_ZONE)


def parse_scheduler_log(lines, day: date, source=None) -> list[dict]:
    """The scheduler's own statements in QuantDesk's log, one entry per scheduler start.

    APScheduler's run lines name a job by its function name and trigger, not
    by its id, and every QuantDesk job's function is called `tick`. So a run
    line alone never says whose run it was. This keeps, for each scheduler
    start, everything that could: the jobs registered, what the agent, the
    ticker and the collector each declared about their own interval, every
    (name, trigger) pair that ran, and the scheduled times on `day` of runs
    whose interval is a minute or more. `checks.schedule_basis` decides what
    that establishes. Lines from before the first retained start are kept
    under `start_logged: False`.

    Each start also keeps `startup`: the file its first line was read from
    (`source()`, when given) and that line's logged time, which
    `attribute_startups` compares with the running API process.
    """
    def fresh(start_logged: bool, startup: dict | None = None) -> dict:
        return {"start_logged": start_logged, "startup": startup,
                "scheduler_started": False, "added": [],
                "agent_minutes": [], "ticker_seconds": [], "collector_seconds": [],
                "collector_disabled": False, "jobs_seen": [], "runs": [],
                "runs_not_kept": 0}

    segments, current = [], fresh(False)
    for line in lines:
        if _TENTATIVE.match(line):
            segments.append(current)
            logged = log_time(line)
            current = fresh(True, {"source": source() if source else None,
                                   "logged_at": logged.isoformat() if logged else None})
        elif _STARTED.match(line):
            current["scheduler_started"] = True
        elif (found := _ADDED.match(line)):
            current["added"].append(found.group(1))
        elif (found := _AGENT.match(line)):
            current["agent_minutes"].append(found.group(1))
        elif (found := _TICKER.match(line)):
            current["ticker_seconds"].append(found.group(1))
        elif (found := _COLLECTOR.match(line)):
            current["collector_seconds"].append(found.group(1))
        elif _NO_COLLECTOR.match(line):
            current["collector_disabled"] = True
        elif (found := _RUN.match(line)):
            name, trigger, stamp = found.groups()
            if [name, trigger] not in current["jobs_seen"]:
                current["jobs_seen"].append([name, trigger])
            interval = _INTERVAL.match(trigger)
            slow = interval is None or int(interval.group(1)) or int(interval.group(2))
            if slow and stamp.startswith(day.isoformat()):
                if len(current["runs"]) < MAX_LOGGED_RUNS:
                    current["runs"].append([name, trigger, stamp.replace(" ", "T", 1)])
                else:
                    current["runs_not_kept"] += 1
    segments.append(current)
    return [s for s in segments if s["start_logged"] or s["jobs_seen"] or s["added"]
            or s["agent_minutes"]]


def unit_start(unit: dict, name: str) -> dict:
    """The running API process as systemd reports it, or ProbeError."""
    found = _UNIT_TIME.match(unit.get("ExecMainStartTimestamp") or "")
    pid = unit.get("MainPID") or ""
    monotonic = unit.get("ExecMainStartTimestampMonotonic") or ""
    if (unit.get("ActiveState") != "active" or not found or not pid.isdigit() or pid == "0"
            or not monotonic.isdigit() or int(monotonic) == 0):
        raise ProbeError("systemd does not report a running API process with a start time")
    started = datetime.strptime(found.group(1), "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=_UNIT_ZONES[found.group(2)])
    return {"unit": name, "main_pid": int(pid), "main_started": started.isoformat(),
            "main_started_monotonic_usec": int(monotonic)}


def log_basis(cfg: dict) -> dict:
    """The recorded basis for reading the backend log's times and owner, or ProbeError.

    The log's timestamps carry no zone and its lines no writer. Neither can be
    learned from the host: the service environment can override the zone, and
    nothing the observer may read shows which processes write the file. So
    `backend_log_basis` in the configuration must state, after a verification
    made outside the observer, that the API process logs in Asia/Kolkata and
    is the file's only writer, with the basis of each statement, who verified
    it and when. Without it no scheduler start is attributed.
    """
    basis = cfg.get("backend_log_basis")
    if not isinstance(basis, dict):
        raise ProbeError("the backend log's timezone and single-writer basis is not established "
                         "(backend_log_basis is not configured)")
    problems = []
    if basis.get("timezone") not in LOG_TIMEZONES:
        problems.append("timezone is not Asia/Kolkata")
    if basis.get("single_writer") is not True:
        problems.append("the API process is not recorded as the log's only writer")
    for key in LOG_BASIS_TEXT_FIELDS:
        if not isinstance(basis.get(key), str) or not basis[key].strip():
            problems.append(f"{key} is missing")
    try:
        verified = datetime.fromisoformat(str(basis.get("verified_at")))
        if verified.tzinfo is None:
            problems.append("verified_at has no timezone")
    except ValueError:
        problems.append("verified_at is not a timestamp")
    if problems:
        raise ProbeError("the backend log's timezone and single-writer basis is not established: "
                         + "; ".join(problems))
    return {key: basis[key] for key in ("timezone", "single_writer", *LOG_BASIS_TEXT_FIELDS,
                                        "verified_at")}


def attribute_startups(segments: list[dict], api: dict, basis: dict | None = None) -> list[dict]:
    """Mark each scheduler start `current` only if it is the running API process's own.

    The log carries no PID, so the link is time. systemd shows the process
    start T to the second, so the process began in [T, T+1). A start whose
    first line is logged in [T+1s, T+60s) is admitted. Every other start is
    one of the following, and only `current` can establish a schedule
    (`checks.schedule_basis`):

    - `within_start_second`: logged in [T, T+1), so it could predate the
      process;
    - `earlier`: logged before T;
    - `later`: logged at or after T+60s;
    - `unattributed`: its time cannot be read;
    - `ambiguous`: there are two or more starts anywhere in [T, T+60s), and
      every one of them is ambiguous;
    - `contradicted` (v0.4.13): it would be `current`, but the log also holds
      another scheduler start at or after T, or one whose time cannot be
      read. A process starts its scheduler once, so that other start
      contradicts it, whether or not it ran anything on the session date.
      Starts before T are an earlier process's and contradict nothing.
    """
    began = datetime.fromisoformat(api["main_started"])
    earliest = began + timedelta(seconds=STARTUP_EARLIEST_SECONDS)
    latest = began + timedelta(seconds=STARTUP_LATEST_SECONDS)

    def logged(segment):
        stamp = (segment.get("startup") or {}).get("logged_at")
        return datetime.fromisoformat(stamp) if stamp else None

    near = [s for s in segments if (t := logged(s)) is not None and began <= t < latest]
    for segment in segments:
        moment = logged(segment)
        if moment is None:
            status = "unattributed"
        elif moment < began:
            status = "earlier"
        elif moment >= latest:
            status = "later"
        elif len(near) != 1:
            status = "ambiguous"
        elif moment < earliest:
            status = "within_start_second"
        else:
            status = "current"
        segment["attribution"] = {"status": status, "api_unit": api["unit"],
                                  "api_main_pid": api["main_pid"],
                                  "api_main_started": api["main_started"],
                                  "admitted_from_seconds": STARTUP_EARLIEST_SECONDS,
                                  "admitted_before_seconds": STARTUP_LATEST_SECONDS,
                                  "log_basis": basis}
    starts = [s for s in segments if s.get("start_logged")]
    rivals = [s for s in starts if s["attribution"]["status"] != "earlier"]
    if len(rivals) > 1:
        for segment in rivals:
            if segment["attribution"]["status"] == "current":
                segment["attribution"]["status"] = "contradicted"
    return segments


_DECLARATIONS = (_TENTATIVE, _STARTED, _ADDED, _AGENT, _TICKER, _COLLECTOR, _NO_COLLECTOR)


def declares(line: str) -> bool:
    """A scheduler start-up declaration (what a capture keeps), not a run line."""
    return any(pattern.match(line) for pattern in _DECLARATIONS)


def capture_binding(api: dict, runtime: list[int], basis: dict) -> dict:
    """Everything a capture is valid for: one boot, one main process, one log basis."""
    return {"boot_id": api["boot_id"], "unit": api["unit"], "main_pid": api["main_pid"],
            "main_started": api["main_started"],
            "main_started_monotonic_usec": api["main_started_monotonic_usec"],
            "api_pid": runtime[0], "log_basis": basis}


def build_capture(group: list[dict], binding: dict, logged_at: str) -> dict:
    """The original bytes of one start's declarations, in log order, with where each was."""
    raw = [mark["raw"] for mark in group]
    return {"schema": STARTUP_CAPTURE_SCHEMA, "binding": binding,
            "binding_sha256": capture_key(binding),
            "content_sha256": hashlib.sha256(b"".join(raw)).hexdigest(),
            "startup_logged_at": logged_at,
            "lines": [{"bytes_base64": base64.b64encode(mark["raw"]).decode("ascii"),
                       "source": mark["source"], "device": mark["device"],
                       "inode": mark["inode"], "offset": mark["offset"],
                       "length": len(mark["raw"])} for mark in group]}


def _source_rank(name: str, base: str) -> int | None:
    """0 for the current file, N for its Nth rotation, None for any other name."""
    if name == base:
        return 0
    for n in range(1, MAX_ROTATED_LOGS + 1):
        if name in (f"{base}.{n}", f"{base}.{n}.gz"):
            return n
    return None


def captured_lines(record: dict, binding: dict, base: str | None = None) -> list[str]:
    """The decoded lines of a stored capture, or ProbeError unless every check passes.

    Checked before anything is parsed:

    - the binding is exactly the running process's;
    - every line is base64 that decodes to one complete line within bounds;
    - the bytes hash to the recorded content hash;
    - the lines are one start's declarations, beginning with its first line;
    - the recorded start time is that first line's time;
    - (v0.4.13, given `base`) every source is the backend log or one of its
      supported rotation names. Lines run from older files to newer ones,
      and within a file at increasing, non-overlapping offsets, with the
      same device and inode for every line of that file.
    """
    where = f"startup capture {str(record.get('content_sha256'))[:12]}"
    if record.get("binding") != binding or record.get("binding_sha256") != capture_key(binding):
        raise ProbeError(f"{where} is bound to another API process or log basis")
    items = record.get("lines")
    if not isinstance(items, list) or not 1 <= len(items) <= CAPTURE_MAX_LINES:
        raise ProbeError(f"{where} does not hold one start's declarations")
    raw = []
    for item in items:
        if not (isinstance(item, dict) and isinstance(item.get("bytes_base64"), str)
                and isinstance(item.get("source"), str)
                and all(type(item.get(k)) is int and item[k] >= 0
                        for k in ("device", "inode", "offset", "length"))):
            raise ProbeError(f"{where} has a malformed line record")
        try:
            data = base64.b64decode(item["bytes_base64"], validate=True)
        except ValueError as exc:
            raise ProbeError(f"{where} has a line that is not base64") from exc
        if (not data.endswith(b"\n") or len(data) > LOG_LINE_MAX_BYTES
                or item["length"] != len(data)):
            raise ProbeError(f"{where} has a line that is not one complete log line")
        raw.append(data)
    if sum(len(data) for data in raw) > CAPTURE_MAX_BYTES:
        raise ProbeError(f"{where} is larger than a capture")
    if base is not None:
        previous = None
        for item in items:
            rank = _source_rank(item["source"], base)
            if rank is None:
                raise ProbeError(f"{where} names a source that is not the backend log or one "
                                 "of its rotations")
            if previous is not None:
                same = item["source"] == previous["source"]
                if (rank > _source_rank(previous["source"], base)
                        or (same and ((item["device"], item["inode"])
                                      != (previous["device"], previous["inode"])
                                      or item["offset"] < previous["offset"]
                                      + previous["length"]))
                        or (not same and rank == _source_rank(previous["source"], base))):
                    raise ProbeError(f"{where} has line locations that are not in log order")
            previous = item
    if hashlib.sha256(b"".join(raw)).hexdigest() != record.get("content_sha256"):
        raise ProbeError(f"{where} does not match its content hash")
    texts = [data.decode("utf-8", errors="replace") for data in raw]
    if (not _TENTATIVE.match(texts[0]) or sum(bool(_TENTATIVE.match(t)) for t in texts) != 1
            or not all(declares(t) for t in texts)):
        raise ProbeError(f"{where} is not one scheduler start's declarations")
    logged = log_time(texts[0])
    if logged is None or record.get("startup_logged_at") != logged.isoformat():
        raise ProbeError(f"{where} does not record its own start time")
    return texts


class _LogBudget:
    """Bounds on one inspection of the retained backend logs."""

    def __init__(self):
        self.total = 0
        self.deadline = time.monotonic() + LOG_READ_SECONDS

    def charge(self, size: int) -> None:
        self.total += size
        if self.total > LOG_TOTAL_MAX_BYTES:
            raise ProbeError("the retained backend logs exceed the observer's read limit")
        if time.monotonic() > self.deadline:
            raise ProbeError("reading the retained backend logs took longer than "
                             f"{LOG_READ_SECONDS:.0f}s")


class _RetainedLog:
    """One file of the backend log's rotation set, held open for the inspection."""

    def __init__(self, name: str, compressed: bool, fd: int):
        self.name, self.compressed = name, compressed
        self.handle = os.fdopen(fd, "rb")
        info = os.fstat(fd)
        self.identity, self.size = (info.st_dev, info.st_ino), info.st_size
        self.read = 0
        self.last_offset, self.last_raw = 0, b""

    def lines(self, budget: _LogBudget):
        """Decoded lines from the start of the file; a too-long line is dropped whole."""
        if self.compressed and self.size > LOG_COMPRESSED_MAX_BYTES:
            raise ProbeError(f"{self.name} is larger than the observer reads")
        self.read = 0
        try:
            self.handle.seek(0)
            stream = gzip.GzipFile(fileobj=self.handle, mode="rb") if self.compressed \
                else self.handle
            skipping = False
            while True:
                raw = stream.readline(LOG_LINE_MAX_BYTES + 1)
                if not raw:
                    return
                self.last_offset, self.last_raw = self.read, raw
                self.read += len(raw)
                budget.charge(len(raw))
                if self.read > LOG_FILE_MAX_BYTES:
                    raise ProbeError(f"{self.name} is larger than the observer reads")
                complete = raw.endswith(b"\n")
                if skipping or len(raw) > LOG_LINE_MAX_BYTES:
                    skipping = not complete
                    continue
                yield raw.decode("utf-8", errors="replace")
        except (OSError, EOFError, zlib.error) as exc:
            raise ProbeError(f"{self.name} could not be read or is corrupt: "
                             f"{type(exc).__name__}") from exc

    def first_time(self, budget: _LogBudget) -> datetime | None:
        seen = 0
        for line in self.lines(budget):
            if (moment := log_time(line)) is not None:
                return moment
            seen += len(line)
            if seen > LOG_HEAD_MAX_BYTES:
                return None
        return None


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def log_location(configured: str) -> tuple[Path, str]:
    """The approved log directory and file name, from an absolute path with no `..`."""
    path = Path(configured)
    if not path.is_absolute() or ".." in path.parts or path.name in ("", ".", ".."):
        raise ProbeError("quantdesk_log must be an absolute path without '..'")
    return path.parent, path.name


def open_log_directory(directory: Path) -> tuple[int, list[tuple[int, int]]]:
    """An fd for `directory`, opened one component at a time from `/`.

    Each component is opened relative to the one before with O_NOFOLLOW and
    O_DIRECTORY, so a symlink anywhere on the way, not only at the end, is
    refused instead of followed. Returns the fd and every directory's
    (device, inode) from `/` down, for `verify_log_directory`.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    fd = os.open("/", flags)
    identities = [_identity(os.fstat(fd))]
    try:
        for part in directory.parts[1:]:
            try:
                below = os.open(part, flags | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                raise ProbeError(f"the scheduler log could not be read: the directory "
                                 f"component {part!r} cannot be opened without following a "
                                 f"link ({exc.strerror})") from exc
            os.close(fd)
            fd = below
            identities.append(_identity(os.fstat(fd)))
        return fd, identities
    except BaseException:
        os.close(fd)
        raise


def _open_regular(directory_fd: int, name: str) -> int | None:
    """An fd for `name` in the held directory if it is a regular file, None if absent."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProbeError(f"{name} could not be read: {exc.strerror}") from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ProbeError(f"{name} is not a regular file")
    return fd


def verify_log_directory(directory: Path, ancestors: list[tuple[int, int]],
                         chosen: list[_RetainedLog], current: _RetainedLog) -> None:
    """ProbeError unless the path still leads, link-free, to the files that were read.

    The directory is walked again from `/`. Every directory on the way must
    be the same one as before, and each file read must still be under its
    name. Rotated files must be the same size; the current file may only
    have grown.
    """
    try:
        fd, now = open_log_directory(directory)
    except ProbeError as exc:
        raise ProbeError(f"the log directory changed while the log was read ({exc})") from exc
    try:
        if now != ancestors:
            raise ProbeError("the log directory or one of its parents changed while the log "
                             "was read")
        for log in chosen:
            try:
                named = os.stat(log.name, dir_fd=fd, follow_symlinks=False)
            except OSError as exc:
                raise ProbeError(f"{log.name} changed while it was read") from exc
            held = os.fstat(log.handle.fileno()).st_size
            if (_identity(named) != log.identity
                    or (held < log.read if log is current else held != log.size)):
                raise ProbeError(f"{log.name} changed while it was read")
    finally:
        os.close(fd)


def retained_logs(directory_fd: int, base: str) -> list[_RetainedLog]:
    """The backend log and its contiguous rotations, newest first, all held open.

    Only `base`, `base.N` and `base.N.gz` (N = 1 … MAX_ROTATED_LOGS) are
    opened, each inside the held directory.
    """
    opened: list[_RetainedLog] = []
    try:
        fd = _open_regular(directory_fd, base)
        if fd is None:
            raise ProbeError("the scheduler log could not be read: No such file or directory")
        opened.append(_RetainedLog(base, False, fd))
        for n in range(1, MAX_ROTATED_LOGS + 1):
            plain = _open_regular(directory_fd, f"{base}.{n}")
            packed = _open_regular(directory_fd, f"{base}.{n}.gz")
            if plain is not None and packed is not None:
                os.close(plain)
                os.close(packed)
                raise ProbeError(f"both {base}.{n} and {base}.{n}.gz exist")
            if plain is None and packed is None:
                break
            opened.append(_RetainedLog(f"{base}.{n}" + ("" if plain is not None else ".gz"),
                                       plain is None, plain if plain is not None else packed))
        return opened
    except BaseException:
        for log in opened:
            log.handle.close()
        raise


class Probe:
    """The live machine. Tests substitute an object with the same methods."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    # -- clock, host, units ------------------------------------------------
    def ntp_synchronized(self) -> bool:
        return _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]).strip() == "yes"

    def boot_time_epoch(self) -> float:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
        raise ProbeError("/proc/stat carries no btime")

    def reboot_required(self) -> bool:
        return Path("/var/run/reboot-required").exists()

    def unit(self, name: str) -> dict:
        out = _run(["systemctl", "show", name, "-p", "ActiveState", "-p", "SubState",
                    "-p", "MainPID", "-p", "NRestarts", "-p", "ActiveEnterTimestamp",
                    "-p", "ExecMainStartTimestamp", "-p", "ExecMainStartTimestampMonotonic",
                    "-p", "LoadState"])
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def processes(self) -> list[tuple[int, str]]:
        rows = []
        for line in _run(["ps", "-eo", "pid=,args="]).splitlines():
            pid, _, args = line.strip().partition(" ")
            if pid.isdigit():
                rows.append((int(pid), args.strip()))
        return rows

    def listeners(self) -> list[str]:
        return sorted({line.split()[3] for line in _run(["ss", "-ltnH"]).splitlines()
                       if len(line.split()) > 3})

    def program_present(self, name: str) -> bool:
        return shutil.which(name) is not None

    def disk_free(self, path: str) -> tuple[int, int]:
        usage = shutil.disk_usage(path)
        return usage.free, usage.total

    def log_lines(self, path: str, needles: tuple[str, ...], limit: int = 40) -> list[str]:
        try:
            with open(path, errors="replace") as handle:
                kept = [line.rstrip()[:240] for line in handle
                        if any(n in line for n in needles)]
        except OSError as exc:
            raise ProbeError(f"{path} could not be read: {exc.strerror}") from exc
        return kept[-limit:]

    def _api_process(self) -> dict:
        name = (self.cfg.get("units") or {}).get("api")
        if not isinstance(name, str) or not name:
            raise ProbeError("the API unit is not configured")
        return unit_start(self.unit(name), name)

    def _api_runtime(self, api: dict) -> list[int]:
        """The one process running the API, which must be systemd's MainPID, or ProbeError.

        Exactly one process may match `api_process_match`, and it must be the
        unit's main process. Any other process is not the one whose start
        time is being compared with the log (v0.4.11).
        """
        match = self.cfg.get("api_process_match")
        if not isinstance(match, str) or not match:
            raise ProbeError("api_process_match is not configured")
        pids = sorted(pid for pid, args in self.processes() if match in args)
        if len(pids) != 1:
            raise ProbeError(f"{len(pids)} processes run the API; the backend log's single "
                             "writer is not established")
        if pids[0] != api["main_pid"]:
            raise ProbeError(f"the API process {pids[0]} is not systemd's MainPID "
                             f"{api['main_pid']}; the log cannot be attributed to it")
        return pids

    def boot_id(self) -> str:
        """The running boot's id; a monotonic time means nothing outside its boot."""
        try:
            value = Path(BOOT_ID_PATH).read_text().strip()
        except OSError as exc:
            raise ProbeError(f"the boot id could not be read: {exc.strerror}") from exc
        if not _BOOT_ID.match(value):
            raise ProbeError("the boot id is malformed")
        return value

    def _api_identity(self) -> tuple[dict, list[int]]:
        """The boot, systemd's main process and the process table, consistent with each other."""
        boot = self.boot_id()
        api = {**self._api_process(), "boot_id": boot}
        return api, self._api_runtime(api)

    def agent_schedule(self, day: date, captures=None) -> list[dict]:
        """What QuantDesk's log says about its scheduler, for `day`. See
        `parse_scheduler_log`; nothing here decides whose job a run was.

        The scheduler states its jobs only when the API starts, and the
        nightly rotation moves those lines out of the current file. So the
        current file and its rotations (`retained_logs`) are read, newest
        first by their first timestamps, back to the file that began before
        the running API process did; those files are then parsed oldest
        first, and each start is attributed (`attribute_startups`).

        Nothing is read without an established `log_basis` and exactly one
        API process, which must be systemd's MainPID, before and after. The
        directory is opened from `/` without following any link
        (`open_log_directory`) and the files only inside it.

        Anything that suggests the files moved while they were read is a
        ProbeError, never a partial answer:

        - the API process or its runtime changing;
        - the directory or a parent being replaced;
        - a name now naming another file;
        - a rotated file changing size;
        - the current file shrinking;
        - two files overlapping in time.

        With `captures` (the observer's `StartupCaptures`, v0.4.12), the
        running process's start, once found in the log, is kept as its
        original bytes. If it has since rotated away, the kept bytes stand in
        for it; see `_settle_captures`. Every conclusion is still drawn here
        and in `checks`, from the run lines the retained log holds now.
        """
        self._day_bounds(day)
        basis = log_basis(self.cfg)
        directory, base = log_location(self.cfg["quantdesk_log"])
        # Both evidence sources agree before any log is opened (v0.4.11), and
        # the boot and monotonic start are read with them (v0.4.12).
        api, runtime = self._api_identity()
        binding = capture_binding(api, runtime, basis)
        with contextlib.ExitStack() as hold:
            if captures is not None:
                # v0.4.13: one decision for this binding at a time. Contention
                # is UNKNOWN at once; nothing loaded before the lock is used.
                try:
                    hold.enter_context(captures.lock(binding))
                except CaptureContention as exc:
                    raise ProbeError(str(exc)) from exc
                except (CorruptRecord, OSError) as exc:
                    raise ProbeError(f"the startup capture store could not be locked for the "
                                     f"running API process: {exc}") from exc
            return self._read_schedule(day, basis, directory, base, api, runtime, binding,
                                       captures)

    def _read_schedule(self, day, basis, directory, base, api, runtime, binding, captures):
        stored = self._stored_captures(captures, binding, base)
        began = datetime.fromisoformat(api["main_started"])
        budget = _LogBudget()
        directory_fd, ancestors = open_log_directory(directory)
        logs: list[_RetainedLog] = []
        try:
            logs = retained_logs(directory_fd, base)
            needed = len(logs)
            for index, log in enumerate(logs):
                first = log.first_time(budget)
                if first is not None and first < began:
                    needed = index + 1
                    break
            chosen = list(reversed(logs[:needed]))
            reading = {"name": None}
            edges: list[tuple[str, datetime | None, datetime | None, str | None]] = []
            marks: list[dict] = []

            def stream():
                for log in chosen:
                    reading["name"] = log.name
                    first = last = None
                    head = None
                    for line in log.lines(budget):
                        if (moment := log_time(line)) is not None:
                            if first is None:
                                first, head = moment, line
                            last = moment
                        if declares(line):
                            marks.append({"raw": log.last_raw, "source": log.name,
                                          "device": log.identity[0], "inode": log.identity[1],
                                          "offset": log.last_offset,
                                          "tentative": bool(_TENTATIVE.match(line))})
                        yield line
                    edges.append((log.name, first, last, head))

            segments = parse_scheduler_log(stream(), day, source=lambda: reading["name"])
            for (older, _, older_last, older_head), (newer, newer_first, _, newer_head) in \
                    pairwise(edges):
                if older_last and newer_first and (newer_first < older_last
                                                   or newer_head == older_head):
                    raise ProbeError(f"{older} and {newer} overlap; the log was being rotated "
                                     "or is out of order")
            verify_log_directory(directory, ancestors, chosen, logs[0])
            try:
                after = self._api_identity()
            except ProbeError as exc:
                raise ProbeError(f"the API process changed while the log was read ({exc})") \
                    from exc
            if after != (api, runtime):
                raise ProbeError("the API process changed while the log was read")
        finally:
            for log in logs:
                log.handle.close()
            os.close(directory_fd)
        segments = attribute_startups(segments, api, basis)
        if captures is None:
            return segments
        return self._settle_captures(day, segments, marks, stored, captures, binding, api, basis)

    @staticmethod
    def _stored_captures(captures, binding: dict, base: str) -> list[tuple[dict, list[str]]]:
        """The running process's kept captures, each checked, or ProbeError."""
        if captures is None:
            return []
        try:
            records = captures.load(binding)
        except (CorruptRecord, OSError) as exc:
            raise ProbeError(f"the startup capture for the running API process is unreadable: "
                             f"{exc}") from exc
        found = [(record, captured_lines(record, binding, base)) for record in records]
        if len({record["content_sha256"] for record, _ in found}) > 1:
            raise ProbeError(f"{len(found)} different startup captures exist for the running API "
                             "process; all are kept and none is chosen")
        return found

    @staticmethod
    def _confirm_alone(captures, binding: dict, content: str) -> None:
        """Re-read under the lock: nothing but `content` may be stored for this process."""
        try:
            found = captures.contents(binding)
        except (CorruptRecord, OSError) as exc:
            raise ProbeError(f"the startup capture for the running API process could not be "
                             f"confirmed: {exc}") from exc
        if not found <= {content}:
            raise ProbeError(f"{len(found)} different startup captures exist for the running API "
                             "process; all are kept and none is chosen")

    @staticmethod
    def _publish(captures, record: dict) -> str:
        try:
            return "captured" if captures.publish(record) else "already captured"
        except (CorruptRecord, OSError) as exc:
            return f"capture failed: {type(exc).__name__}"

    def _settle_captures(self, day, segments, marks, stored, captures, binding, api, basis):
        """Keep the running process's start, or use the kept one, after every check passed.

        If the retained log holds any start at or after the process start, the
        log decides:

        - exactly one `current` start is captured;
        - the same bytes already kept add nothing;
        - different bytes are kept as well, and the result is UNKNOWN.

        If the log holds no such start, the one kept capture for this exact
        binding stands in for it. This needs a retained log with no
        declaration lines at all, since kept bytes are never combined with
        others. The retained run lines are then counted under the captured
        start, exactly as if the start were still in the log.
        """
        starts = [s for s in segments if s["start_logged"]]
        groups: list[list[dict]] = []
        for mark in marks:
            if mark["tentative"]:
                groups.append([mark])
            elif groups:
                groups[-1].append(mark)
        live = [s for s in starts
                if s["attribution"]["status"] in ("current", "within_start_second", "ambiguous",
                                                  "later")]
        if live:
            current = [s for s in live if s["attribution"]["status"] == "current"]
            if len(live) != 1 or len(current) != 1:
                return segments
            segment = current[0]
            group = groups[starts.index(segment)]
            if (len(group) > CAPTURE_MAX_LINES
                    or sum(len(mark["raw"]) for mark in group) > CAPTURE_MAX_BYTES):
                segment["capture"] = {"status": "not captured: the start-up declarations "
                                                "exceed the capture bounds"}
                return segments
            record = build_capture(group, binding, segment["startup"]["logged_at"])
            if stored and stored[0][0]["content_sha256"] != record["content_sha256"]:
                kept = self._publish(captures, record)
                raise ProbeError("the retained start-up evidence differs from the evidence "
                                 "captured for the same API process; both are kept and neither "
                                 f"is chosen ({kept})")
            segment["capture"] = {"status": "already captured" if stored
                                  else self._publish(captures, record),
                                  "content_sha256": record["content_sha256"]}
            self._confirm_alone(captures, binding, record["content_sha256"])
            return segments
        if not stored:
            return segments
        if marks:
            raise ProbeError("the retained log holds start-up declarations from before the "
                             "running API process's start; the captured start is not combined "
                             "with them")
        if len(segments) > 1:
            raise ProbeError("the retained log holds more than one scheduler start's lines")
        record, texts = stored[0]
        (segment,) = parse_scheduler_log(texts, day)
        for retained in segments:          # the lines before any retained start, if any
            segment["jobs_seen"] = retained["jobs_seen"]
            segment["runs"] = retained["runs"]
            segment["runs_not_kept"] = retained["runs_not_kept"]
        segment["startup"]["source"] = f"capture {record['content_sha256'][:12]}"
        segment["capture"] = {"status": "reused", "content_sha256": record["content_sha256"],
                              "captured_at_utc": record.get("captured_at_utc"),
                              "captured_by_run": record.get("captured_by_run"),
                              "lines": [{k: item[k] for k in ("source", "device", "inode",
                                                              "offset", "length")}
                                        for item in record["lines"]]}
        self._confirm_alone(captures, binding, record["content_sha256"])
        return attribute_startups([segment], api, basis)

    # -- the QuantDesk checkout: read, never refreshed -----------------------
    def _git(self, *args: str) -> str:
        # GIT_OPTIONAL_LOCKS=0: `git status` must not rewrite the index of a
        # checkout the observer does not own.
        env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
        return _run(["git", "-C", self.cfg["quantdesk_repo"], *args], env=env)

    def revision(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def worktree_changes(self) -> list[str]:
        return [line for line in self._git("status", "--porcelain").splitlines() if line]

    # -- the API's own view of itself ----------------------------------------
    def api(self, path: str) -> dict:
        if path not in API_PATHS:
            raise ProbeError(f"{path} is not an API path the observer may read")
        url = loopback_base(self.cfg["api_base"]) + path
        # No proxy and no redirects: the request goes to this machine or nowhere.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(url, timeout=HTTP_TIMEOUT) as reply:
                if reply.status != 200:
                    raise ProbeError(f"GET {path} answered {reply.status}")
                body = reply.read(API_MAX_BYTES + 1)
        except ProbeError:
            raise
        except Exception as exc:
            raise ProbeError(f"GET {path} failed: {type(exc).__name__}") from exc
        if len(body) > API_MAX_BYTES:
            raise ProbeError(f"GET {path} answered more than {API_MAX_BYTES} bytes")
        try:
            parsed = json.loads(body.decode())
        except ValueError as exc:
            raise ProbeError(f"GET {path} did not answer JSON") from exc
        if not isinstance(parsed, dict):
            raise ProbeError(f"GET {path} did not answer a JSON object")
        return parsed

    # -- the database: one read-only statement at a time ---------------------
    def _sql(self, statement: str):
        """Run one SELECT that returns a single JSON value.

        Connection settings come from libpq's own environment (PGSERVICE,
        PGPASSFILE, ...), never from this code or its configuration file. The
        session is forced read-only whatever the role would otherwise allow.
        """
        env = dict(os.environ)
        env["PGOPTIONS"] = (f"-c default_transaction_read_only=on "
                            f"-c statement_timeout={SQL_STATEMENT_TIMEOUT_MS}")
        env["PGCONNECT_TIMEOUT"] = "5"
        out = _run(["psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", statement],
                   timeout=SQL_TIMEOUT, env=env)
        try:
            return json.loads(out)
        except ValueError as exc:
            raise ProbeError("the database reply was not JSON") from exc

    @staticmethod
    def _day_bounds(day: date) -> tuple[str, str]:
        if type(day) is not date:
            raise ProbeError("session date must be a date")
        return (f"{day.isoformat()} 00:00+05:30",
                f"{(day + timedelta(days=1)).isoformat()} 00:00+05:30")

    def signals(self, day: date) -> list[dict]:
        low, high = self._day_bounds(day)
        return self._sql(f"""
            select coalesce(json_agg(t order by t.id), '[]') from (
              select id, created_at, action, confidence, checks, code_id,
                     strategy_version, parameter_hash, bar_open_time,
                     provenance->'api_init_provenance' as api_init,
                     provenance->>'observation_id' as observation_id,
                     context->'option_chain'->>'oi_status' as oi_status,
                     (context->>'threshold_used')::float as threshold_used
              from signals
              where created_at >= '{low}' and created_at < '{high}') t""")

    def signal_days(self, first: date, last: date) -> list[dict]:
        """Observation counts per IST day, to notice activity on unexpected days."""
        low, _ = self._day_bounds(first)
        _, high = self._day_bounds(last)
        return self._sql(f"""
            select coalesce(json_agg(t order by t.day), '[]') from (
              select (created_at at time zone 'Asia/Kolkata')::date::text as day,
                     count(*) as observations
              from signals where created_at >= '{low}' and created_at < '{high}'
              group by 1) t""")

    def decisions(self, day: date) -> list[dict]:
        self._day_bounds(day)
        return self._sql(f"""
            select coalesce(json_agg(t order by t.id), '[]') from (
              select id, action, outcome, code, detail->'evidence' as evidence
              from paper_decisions where session_date = '{day.isoformat()}') t""")

    def positions(self, day: date) -> dict:
        self._day_bounds(day)
        return self._sql(f"""
            select json_build_object(
              'total', count(*), 'open', count(*) filter (where status = 'open'),
              'max_id', max(id),
              'session', coalesce(json_agg(p) filter
                                  (where session_date = '{day.isoformat()}'), '[]'))
            from paper_positions p""")

    def journal(self) -> dict:
        return self._sql("select json_build_object('total', count(*), 'max_id', max(id)) "
                         "from trades")

    def archive(self, day: date) -> dict:
        self._day_bounds(day)
        lag = "extract(epoch from capture_time - exchange_time)"
        return self._sql(f"""
            select json_build_object(
              'rows', count(*), 'with_exchange_time', count(exchange_time),
              'captures', count(distinct capture_time),
              'exchange_after_capture', count(*) filter (where exchange_time > capture_time),
              'exchange_other_date', count(*) filter (where
                  (exchange_time at time zone 'Asia/Kolkata')::date <> '{day.isoformat()}'),
              'captures_over_300s', count(distinct capture_time) filter (where {lag} > 300),
              'lag_min', min({lag}), 'lag_max', max({lag}),
              'lag_median', percentile_cont(0.5) within group (order by {lag}))
            from option_candles where session_date = '{day.isoformat()}'""")

    def vix_close(self, day: date) -> dict | None:
        self._day_bounds(day)
        return self._sql(f"""
            select coalesce((select row_to_json(v) from vix_daily v
                             where session_date = '{day.isoformat()}'), 'null'::json)""")
