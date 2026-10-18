"""Facts the controller establishes itself before and during a shutdown.

Each function returns a `Fact`: PASS, BLOCKED (a definite reason not to shut
down) or UNKNOWN (it could not be established). Anything that raises is
UNKNOWN. The controller proceeds only when every fact it needs is PASS.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import struct
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import fsafe
from .system import CommandFailed, CommandTimeout, Runner, Systemd, active

IST = ZoneInfo("Asia/Kolkata")
PASS, BLOCKED, UNKNOWN = "PASS", "BLOCKED", "UNKNOWN"
PRESENT, ABSENT = "PRESENT", "ABSENT"      # the API after its stop (or UNKNOWN)
ROOT_UID = 0          # owner required of operator-written directories and files
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
BOOT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HOLD_SCHEMA = "quantdesk_power_hold/1"
HOLD_KINDS = ("KEEP_RUNNING", "ALLOW_DEALLOCATION")
HOLD_REASONS = ("SPECIAL_SESSION", "MAINTENANCE", "INVESTIGATION", "INCIDENT")
HOLD_FIELDS = {"schema", "hold_id", "kind", "reason_code", "from_ist", "to_ist",
               "authorization_reference", "recorded_by", "recorded_at_utc"}


@dataclass
class Fact:
    name: str
    status: str
    detail: str
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail,
                "data": self.data}


def worst(facts: list[Fact]) -> str:
    statuses = {f.status for f in facts}
    return UNKNOWN if UNKNOWN in statuses else BLOCKED if BLOCKED in statuses else PASS


def boot_id(path: str = BOOT_ID_PATH) -> str:
    with open(path) as handle:
        value = handle.read().strip()
    if not BOOT_ID.match(value):
        raise ValueError("the boot id is malformed")
    return value


# -- time ---------------------------------------------------------------------------
def in_window(now: datetime, cfg: dict) -> Fact:
    local = now.astimezone(IST)
    window = cfg["window"]
    first = time.fromisoformat(window["first_attempt_ist"])
    last = time.fromisoformat(window["last_attempt_ist"])
    ok = local.weekday() in window["weekdays"] and first <= local.time() <= last
    return Fact("attempt_window", PASS if ok else BLOCKED,
                f"{local:%a %H:%M} IST is {'inside' if ok else 'outside'} the attempt window "
                f"{first:%H:%M}-{last:%H:%M} on configured weekdays")


def clock_synchronized(runner: Runner, cfg: dict) -> Fact:
    try:
        out = runner.run([cfg["programs"]["timedatectl"], "show", "-p", "NTPSynchronized",
                          "--value"], cfg["timeouts"]["command"])
        ok = out.returncode == 0 and out.stdout.strip() == "yes"
        return Fact("clock", PASS if ok else UNKNOWN,
                    "the clock is NTP-synchronized" if ok else "the clock is not synchronized")
    except CommandFailed as exc:
        return Fact("clock", UNKNOWN, f"clock synchronization could not be read: {exc}")


# -- calendar -----------------------------------------------------------------------
def _calendar_dates(cfg: dict) -> dict:
    path = cfg["calendar_file"]
    held = fsafe.open_dir(os.path.dirname(path), private=True)
    try:
        raw = fsafe.read_file(held, os.path.basename(path), max_bytes=4 * 1024 * 1024)
    finally:
        held.close()
    calendar = json.loads(raw)
    if not isinstance(calendar, dict) or not isinstance(calendar.get("dates"), dict):
        raise ValueError("the calendar has no dates")
    return calendar["dates"]


def calendar_status(dates: dict, day: date) -> str:
    """The observer's rule: a date is known only once its own entry is reviewed."""
    entry = dates.get(day.isoformat())
    if not isinstance(entry, dict):
        return UNKNOWN
    reviewed = entry.get("review_status") == "REVIEWED" and all(
        isinstance(entry.get(k), str) and entry[k].strip()
        for k in ("reviewer", "reviewed_at", "official_reference"))
    listed = entry.get("session_type")
    return listed if reviewed and listed in ("NORMAL", "CLOSED", "SPECIAL") else UNKNOWN


def special_sessions(cfg: dict, now: datetime) -> Fact:
    """A special session today or tomorrow, listed or possible, blocks.

    BLOCKED: a reviewed SPECIAL entry. UNKNOWN: an entry that is not a reviewed
    NORMAL or CLOSED one (unreviewed, malformed, an unknown session type), or no
    entry at all - a special session there cannot be ruled out. Nothing on this
    machine, no hold and no controller setting, can permit a shutdown past it:
    the calendar must say so after review.
    """
    try:
        dates = _calendar_dates(cfg)
    except (OSError, ValueError) as exc:
        return Fact("special_sessions", UNKNOWN, f"the calendar cannot be read: {exc}")
    today = now.astimezone(IST).date()
    days = {d.isoformat(): calendar_status(dates, d) for d in (today, today + timedelta(days=1))}
    found = [d for d, status in days.items() if status == "SPECIAL"]
    if found:
        return Fact("special_sessions", BLOCKED, f"a reviewed special session is listed for "
                    f"{', '.join(found)}", {"dates": days})
    uncertain = [d for d, status in days.items() if status not in ("NORMAL", "CLOSED")]
    if uncertain:
        return Fact("special_sessions", UNKNOWN, f"no reviewed NORMAL or CLOSED entry for "
                    f"{', '.join(uncertain)}: a special session cannot be ruled out",
                    {"dates": days})
    return Fact("special_sessions", PASS, "today and tomorrow are reviewed and not special",
                {"dates": days})


# -- holds ----------------------------------------------------------------------------
def _ist(value) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("a hold time has no timezone")
    return parsed


def read_holds(cfg: dict, now: datetime) -> tuple[list[dict], Fact]:
    """Structured operator holds. Unreadable or malformed holds make the fact UNKNOWN.

    The hold directory is provisioned with the controller (README, "Files"), so a
    missing one is missing evidence, never "no holds": UNKNOWN, which blocks a
    shutdown and leaves the status record without a cycle (the monitor alerts).
    """
    try:
        held = fsafe.open_dir(cfg["holds_dir"], owner=ROOT_UID, private=True)
    except FileNotFoundError:
        return [], Fact("holds", UNKNOWN, "the hold directory is missing: an operator's hold "
                        "cannot be ruled out")
    except OSError as exc:
        return [], Fact("holds", UNKNOWN, f"the hold directory cannot be opened safely: {exc}")
    holds, problems = [], []
    try:
        for name in fsafe.names(held):
            try:
                if not name.endswith(".json"):
                    raise ValueError("not a .json file")
                record = json.loads(fsafe.read_file(held, name, max_bytes=16 * 1024,
                                                    owner=ROOT_UID))
                if not isinstance(record, dict) or set(record) != HOLD_FIELDS:
                    raise ValueError("unexpected or missing fields")
                if record["schema"] != HOLD_SCHEMA or record["kind"] not in HOLD_KINDS \
                        or record["reason_code"] not in HOLD_REASONS:
                    raise ValueError("unknown schema, kind or reason code")
                for key in ("hold_id", "authorization_reference", "recorded_by"):
                    if not isinstance(record[key], str) or not record[key].strip():
                        raise ValueError(f"{key} is empty")
                start, end = _ist(record["from_ist"]), _ist(record["to_ist"])
                _ist(record["recorded_at_utc"])
                if not start < end:
                    raise ValueError("the hold ends before it starts")
                holds.append({**record, "active": start <= now <= end})
            except (OSError, ValueError, TypeError) as exc:
                problems.append(f"{name}: {exc}")
    finally:
        held.close()
    if problems:
        return holds, Fact("holds", UNKNOWN, "a hold record is unusable: " + "; ".join(problems))
    keeping = [h["hold_id"] for h in holds if h["active"] and h["kind"] == "KEEP_RUNNING"]
    if keeping:
        return holds, Fact("holds", BLOCKED, f"active KEEP_RUNNING hold {', '.join(keeping)}")
    return holds, Fact("holds", PASS, f"{len(holds)} hold record(s), none keeping the VM up now")


# -- package management ----------------------------------------------------------------
def _flock_struct() -> tuple[str, int]:
    if sys.platform.startswith("linux"):
        return "hhqqi", 32          # l_type, l_whence, l_start, l_len, l_pid (+ padding)
    if sys.platform == "darwin":
        return "qqihh", 24          # l_start, l_len, l_pid, l_type, l_whence
    raise OSError(f"F_GETLK layout unknown on {sys.platform}")


def lock_holder(path: str) -> int | None:
    """The pid holding a POSIX write lock on `path`, without taking it; None if unlocked."""
    layout, size = _flock_struct()
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if layout == "hhqqi":
            query = struct.pack(layout, fcntl.F_WRLCK, os.SEEK_SET, 0, 0, 0).ljust(size, b"\0")
            reply = fcntl.fcntl(fd, fcntl.F_GETLK, query)
            l_type, _, _, _, l_pid = struct.unpack(layout, reply[:struct.calcsize(layout)])
        else:
            query = struct.pack(layout, 0, 0, 0, fcntl.F_WRLCK, os.SEEK_SET).ljust(size, b"\0")
            reply = fcntl.fcntl(fd, fcntl.F_GETLK, query)
            _, _, l_pid, l_type, _ = struct.unpack(layout, reply[:struct.calcsize(layout)])
        return None if l_type == fcntl.F_UNLCK else l_pid
    finally:
        os.close(fd)


def package_activity(cfg: dict, systemd: Systemd) -> Fact:
    """No package manager lock is held and no apt unit is running."""
    holders, problems = {}, []
    for path in cfg["package_locks"]:
        try:
            pid = lock_holder(path)
            if pid is not None:
                holders[path] = pid
        except FileNotFoundError:
            continue
        except OSError as exc:
            problems.append(f"{path}: {exc.strerror or exc}")
    running = []
    for unit in cfg["apt_units"]:
        try:
            if active(systemd.show(unit)):
                running.append(unit)
        except CommandFailed as exc:
            problems.append(f"{unit}: {exc}")
    if problems:
        return Fact("package_activity", UNKNOWN, "package activity cannot be established: "
                    + "; ".join(problems))
    if holders or running:
        return Fact("package_activity", BLOCKED, "package management is in progress",
                    {"lock_holders": holders, "running_units": running})
    return Fact("package_activity", PASS, "no package lock held and no apt unit running")


# -- the API process ---------------------------------------------------------------------
def _within(budget, default: float, what: str) -> float:
    """The wait for one inspection: `default`, shortened to what `budget()` leaves."""
    if budget is None:
        return default
    remaining = budget()
    if remaining <= 0:
        raise CommandTimeout(f"no time left to {what}")
    return min(default, remaining)


def api_identity(cfg: dict, runner: Runner, systemd: Systemd, budget=None) -> Fact:
    """Exactly one API process, systemd's MainPID, with a start time.

    `budget`, when given, is a callable returning the seconds left before a shared
    deadline; each inspection is then refused when none is left and otherwise waits
    no longer than that. Without it every inspection keeps its default timeout.
    """
    try:
        if budget is None:
            props = systemd.show(cfg["units"]["api"])
        else:
            props = systemd.show(cfg["units"]["api"],
                                 timeout=_within(budget, float("inf"), "read the API unit"))
        pid = props.get("MainPID", "0")
        mono = props.get("ExecMainStartTimestampMonotonic", "0")
        if props["ActiveState"] != "active" or not pid.isdigit() or pid == "0" \
                or not mono.isdigit() or mono == "0":
            return Fact("api_identity", UNKNOWN, "systemd reports no running API process")
        out = runner.run([cfg["programs"]["ps"], "-eo", "pid=,args="],
                         _within(budget, cfg["timeouts"]["command"], "read the process table"))
        if out.returncode != 0:
            return Fact("api_identity", UNKNOWN, "the process table could not be read")
        pids = sorted(int(p) for p, _, args in (line.strip().partition(" ")
                                                for line in out.stdout.splitlines())
                      if p.isdigit() and cfg["api_process_match"] in args)
        if pids != [int(pid)]:
            return Fact("api_identity", UNKNOWN, f"API processes {pids} are not exactly "
                        f"systemd's MainPID {pid}")
        return Fact("api_identity", PASS, f"one API process, MainPID {pid}",
                    {"main_pid": int(pid), "main_started_monotonic_usec": int(mono)})
    except CommandFailed as exc:
        return Fact("api_identity", UNKNOWN, f"the API process cannot be established: {exc}")


def _process_table(cfg: dict, runner: Runner) -> list[tuple[int, str]]:
    out = runner.run([cfg["programs"]["ps"], "-eo", "pid=,args="], cfg["timeouts"]["command"])
    if out.returncode != 0:
        raise CommandFailed("the process table could not be read")
    rows = []
    for line in out.stdout.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit():
            rows.append((int(pid), args))
    if not any(pid == 1 for pid, _ in rows):
        raise CommandFailed("the process table is implausible (no process 1)")
    return rows


def api_presence(cfg: dict, runner: Runner, systemd: Systemd) -> Fact:
    """After the API's stop: PRESENT, ABSENT or UNKNOWN (in `data["presence"]`).

    ABSENT only when systemd reports the unit inactive with no main or control
    process AND a plausible process table holds no API process. Anything that
    cannot be read is UNKNOWN, never taken as absence.
    """
    def fact(presence: str, detail: str, **data) -> Fact:
        status = {ABSENT: PASS, PRESENT: BLOCKED}.get(presence, UNKNOWN)
        return Fact("api_presence", status, detail, {"presence": presence, **data})

    try:
        props = systemd.show(cfg["units"]["api"])
        rows = _process_table(cfg, runner)
    except CommandFailed as exc:
        return fact(UNKNOWN, f"the API's absence cannot be established: {exc}")
    found = sorted(pid for pid, args in rows if cfg["api_process_match"] in args)
    state, main = props.get("ActiveState"), props.get("MainPID")
    if found:
        return fact(PRESENT, f"API process(es) {found} are still running", pids=found)
    if state in ("active", "activating", "deactivating", "reloading") or \
            (isinstance(main, str) and main.isdigit() and main != "0"):
        return fact(PRESENT, f"systemd reports the API {state} with MainPID {main}")
    if state in ("inactive", "failed") and main == "0":
        return fact(ABSENT, "systemd reports the API stopped and no API process is running")
    return fact(UNKNOWN, f"systemd reports the API as {state!r} with MainPID {main!r}")


# -- metadata endpoint isolation ----------------------------------------------------------
IMDS_ADDRESS = "169.254.169.254"


def _is_drop_rule(expr) -> bool:
    """Exactly: ip daddr == 169.254.169.254, meta skuid != 0, [counter], drop."""
    if not isinstance(expr, list):
        return False
    items = [e for e in expr if not (isinstance(e, dict) and set(e) == {"counter"})]
    if len(items) != 3:
        return False
    daddr, skuid, verdict = items
    return (daddr == {"match": {"op": "==", "left": {"payload": {"protocol": "ip",
                                                                 "field": "daddr"}},
                                "right": IMDS_ADDRESS}}
            and isinstance(skuid, dict) and set(skuid) == {"match"}
            and skuid["match"].get("op") == "!="
            and skuid["match"].get("left") == {"meta": {"key": "skuid"}}
            and skuid["match"].get("right") in (0, "root") and set(skuid["match"]) ==
            {"op", "left", "right"}
            and verdict == {"drop": None})


def imds_ruleset_problem(listing, family: str, table: str) -> str | None:
    """Why the listed table does not restrict the metadata service to root, or None.

    Judged from nftables' JSON structure, never from text or comments: one
    table, one base chain on the `output` hook of type `filter`, and as its
    only rule the drop above. Any other chain or rule - one placed before the
    drop, a jump, an accept, a wider identity, another hook - is refused, even
    if its comment claims otherwise.
    """
    if not isinstance(listing, dict) or not isinstance(listing.get("nftables"), list):
        return "the listing is not nftables JSON"
    objects = [o for o in listing["nftables"] if isinstance(o, dict) and "metainfo" not in o]
    tables = [o["table"] for o in objects if "table" in o]
    chains = [o["chain"] for o in objects if "chain" in o]
    rules = [o["rule"] for o in objects if "rule" in o]
    others = [o for o in objects if not {"table", "chain", "rule"} & set(o)]
    if others:
        return f"the table holds other objects ({sorted(set().union(*map(set, others)))})"
    if len(tables) != 1 or tables[0].get("family") != family or tables[0].get("name") != table:
        return "the listing is not exactly the isolation table"
    if len(chains) != 1:
        return f"the table has {len(chains)} chains, not one"
    chain = chains[0]
    if (chain.get("family"), chain.get("table")) != (family, table):
        return "the chain belongs to another table"
    if chain.get("type") != "filter" or chain.get("hook") != "output":
        return (f"the chain is not a filter chain on the output hook "
                f"(type {chain.get('type')!r}, hook {chain.get('hook')!r})")
    if not isinstance(chain.get("prio"), int) or isinstance(chain.get("prio"), bool):
        return "the chain has no numeric priority"
    if chain.get("policy") not in ("accept", "drop"):
        return "the chain has no valid policy"
    if len(rules) != 1:
        return f"the chain has {len(rules)} rules, not exactly the drop rule"
    rule = rules[0]
    if (rule.get("family"), rule.get("table"), rule.get("chain")) != \
            (family, table, chain.get("name")):
        return "the rule is not in the isolation chain"
    if not _is_drop_rule(rule.get("expr")):
        return "the rule is not 'ip daddr 169.254.169.254 meta skuid != 0 drop'"
    return None


def imds_isolation(cfg: dict, runner: Runner) -> Fact:
    """The nftables table restricting the metadata endpoint is loaded and effective."""
    try:
        family, table = cfg["imds"]["nft_table"].split()
        out = runner.run([cfg["programs"]["nft"], "-j", "list", "table", family, table],
                         cfg["timeouts"]["command"])
        if out.returncode != 0:
            return Fact("imds_isolation", UNKNOWN, "the IMDS isolation table is not loaded")
        try:
            listing = json.loads(out.stdout)
        except ValueError:
            listing = None
        problem = imds_ruleset_problem(listing, family, table)
        if problem:
            return Fact("imds_isolation", UNKNOWN, f"the IMDS isolation is not effective: "
                        f"{problem}")
        return Fact("imds_isolation", PASS, "the IMDS isolation table is loaded and is "
                    "structurally the root-only drop on the output hook")
    except CommandFailed as exc:
        return Fact("imds_isolation", UNKNOWN, f"nftables could not be read: {exc}")
