"""Shutdown evidence (v0.4.14): what the observer's own records say about powering down.

The separate power controller (scripts/native/power) asks one question before
it stops QuantDesk and deallocates the VM: is any observer work unfinished?
This module answers it from the authoritative records only, through the same
validated readers every assessment uses:

  - after-close reports through `Store.reports` (confirmed, synced, no open
    publication fault), never `all_reports`;
  - the bounded phase through `phase_state` (a checkpoint that is due and not
    yet recorded is unfinished work);
  - notifications through `notification_states` (intent, reservation and
    outcome records), never `notification-state.json`;
  - faults through `Store.faults` (a resolution counts only once checked);
  - the calendar through `calendar_status` (a date is known only once reviewed).

`state.json` and `notification-state.json` are caches and are never read.

It is read-only. It writes no file, sends no notification and makes no ping;
it never constructs the notification machinery at all. It prints one sealed
record and exits. The only lock it takes is the observer's own lock: either a
descriptor the power controller already holds and passes in (`--lock-fd`), so
no observer run can start between the controller's checks and its shutdown,
or, run by hand, the lock file opened without creating it.

The record is bound to the transaction it was asked for, the boot, the API
process systemd reports, and the exact configuration and calendar bytes, so a
record cannot be replayed into another transaction, boot or configuration.

Verdict, worst first:
  UNKNOWN  something needed could not be read or is inconsistent;
  BLOCKED  observer work is unfinished (see `REASONS`);
  CLEAR    nothing the observer is responsible for is unfinished.
Only CLEAR permits a shutdown, and the power controller decides that itself.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import stat
from datetime import UTC, timedelta

from . import VERSION
from .checks import IST, UNKNOWN
from .cli import UNRESOLVED, calendar_status, load_calendar, notification_states, phase_state
from .probes import ProbeError
from .store import CorruptRecord, Store, file_sha256, seal

SCHEMA = "observer_shutdown_evidence/1"
TXN_ID = re.compile(r"^[0-9a-f]{32}$")
CLEAR, BLOCKED = "CLEAR", "BLOCKED"
VERDICTS = (CLEAR, BLOCKED, UNKNOWN)
# Calendar statuses for which an after-close report must exist before a shutdown:
# a reviewed normal session, and any date that is not reviewed (it could be one).
AFTERCLOSE_REQUIRED = ("NORMAL", UNKNOWN)
REASONS = {
    "afterclose_missing": "no confirmed after-close report for today",
    "checkpoint_due": "a review checkpoint is due and not yet recorded",
    "notification_outstanding": "an observer notification is undelivered and still current",
    "fault_open": "an observer fault is open",
    "special_session": "a reviewed special session is today or tomorrow",
}
CALENDAR_FIELDS = ("status", "reviewed", "provisional_listing", "official_reference",
                   "reviewer", "reviewed_at", "unknown_because")
NOTE_FIELDS = ("notification_id", "check", "event_type", "kind", "delivery_state", "rounds",
               "last_attempt_at_utc", "next_eligible_at_utc")


class EvidenceRefused(Exception):
    """The request itself is malformed; no record is produced."""


def _digest(path: str | os.PathLike) -> str | None:
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def hold_lock(store: Store, lock_fd: int | None) -> tuple[dict, int | None]:
    """Hold the observer lock for this read, or raise OSError.

    With `lock_fd` the caller passes a descriptor of the lock file that it
    already holds; it must be that very file, and taking the lock on it again
    is a no-op for the holder and fails for anyone else. Without it, the lock
    file is opened without creating anything and locked without waiting.
    Returns the lock's identity and a descriptor to close (None if inherited).
    """
    path = store.root / "lock"
    named = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(named.st_mode):
        raise OSError(f"{path} is not a regular file")
    owned = None
    if lock_fd is None:
        owned = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        fd = owned
    else:
        fd = lock_fd
    try:
        held = os.fstat(fd)
        if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
            raise OSError("the lock descriptor is not the observer's lock file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if owned is not None:
            os.close(owned)
        raise
    return {"mode": "own" if lock_fd is None else "inherited",
            "device": named.st_dev, "inode": named.st_ino}, owned


def collect(cfg: dict, store: Store, probe, clock, txn_id: str, config_path: str,
            lock_fd: int | None = None, api_stopped: bool = False) -> dict:
    """The sealed shutdown-evidence record for transaction `txn_id`.

    `api_stopped` is the power controller's second request, after it has
    stopped the API: the record then binds to the API unit's last start with
    the unit no longer running, instead of to a running process.
    """
    if not isinstance(txn_id, str) or not TXN_ID.match(txn_id):
        raise EvidenceRefused("the transaction id must be 32 lowercase hexadecimal characters")
    now = clock()
    unknown: list[str] = []
    blocked: list[str] = []
    record: dict = {
        "schema": SCHEMA, "txn_id": txn_id, "observer_version": VERSION,
        "generated_at_utc": now.astimezone(UTC).isoformat(),
        "generated_at_ist": now.astimezone(IST).isoformat(),
        "lock": None, "boot_id": None, "api": None,
        "config_sha256": _digest(config_path),
        "calendar_file": str(cfg.get("calendar_file")),
        "calendar_sha256": _digest(cfg.get("calendar_file") or ""),
        "calendar": None, "afterclose": None, "phase": None,
        "notifications_outstanding": None, "faults_open": None,
    }
    if record["config_sha256"] is None:
        unknown.append("the configuration file could not be read for its hash")
    if record["calendar_sha256"] is None:
        unknown.append("the calendar file could not be read for its hash")
    owned = None
    try:
        record["lock"], owned = hold_lock(store, lock_fd)
    except OSError as exc:
        unknown.append(f"the observer lock could not be held ({type(exc).__name__})")
    try:
        if record["lock"] is not None:
            _read(cfg, store, probe, now, record, unknown, blocked, api_stopped)
    finally:
        if owned is not None:
            os.close(owned)
    record["reasons"] = {"unknown": unknown, "blocked": blocked}
    record["verdict"] = UNKNOWN if unknown else BLOCKED if blocked else CLEAR
    return seal(record)


def _read(cfg, store, probe, now, record, unknown, blocked, api_stopped=False) -> None:
    try:
        if api_stopped:
            record["boot_id"] = probe.boot_id()
            name = (cfg.get("units") or {}).get("api")
            unit = probe.unit(name)
            mono = unit.get("ExecMainStartTimestampMonotonic") or ""
            if unit.get("ActiveState") != "inactive" or not mono.isdigit() or mono == "0":
                raise ProbeError("the API unit is not stopped with a recorded last start")
            record["api"] = {"unit": name, "state": "stopped",
                             "active_state": unit.get("ActiveState"),
                             "main_started_monotonic_usec": int(mono)}
        else:
            api, pids = probe._api_identity()
            record["boot_id"] = api["boot_id"]
            record["api"] = {k: api[k] for k in ("unit", "main_pid", "main_started",
                                                 "main_started_monotonic_usec")}
            record["api"].update(state="running", pids=pids)
    except (ProbeError, OSError, KeyError, TypeError) as exc:
        unknown.append(f"the boot and API process could not be established: {exc}")

    today = now.astimezone(IST).date()
    calendar = load_calendar(cfg)
    if calendar.get("error"):
        unknown.append(calendar["error"])
    listing = {}
    for day in (today, today + timedelta(days=1)):
        status = calendar_status(calendar, day)
        listing[day.isoformat()] = {k: status.get(k) for k in CALENDAR_FIELDS}
        if status["status"] == "SPECIAL":
            blocked.append(f"special_session: {day.isoformat()}")
    record["calendar"] = {"today": today.isoformat(), "dates": listing}
    required = listing[today.isoformat()]["status"] in AFTERCLOSE_REQUIRED

    try:
        found = []
        for report in store.reports(session_date=today.isoformat(), check="afterclose"):
            path = store.report_path(report)
            found.append({
                "report_id": report["report_id"], "result": report.get("result"),
                "outcome": report.get("outcome"), "invocation": report.get("invocation"),
                "catch_up": report.get("catch_up"),
                "completed_at_utc": report.get("completed_at_utc"),
                "content_sha256": report["integrity"]["content_sha256"],
                "file_sha256": file_sha256(path),
                "confirmation_file_sha256": file_sha256(store.confirmation_path(path))})
        record["afterclose"] = {"required": required, "confirmed_reports": found}
        if required and not found:
            blocked.append("afterclose_missing")
    except (CorruptRecord, OSError, ValueError, KeyError) as exc:
        unknown.append(f"the after-close reports could not be read ({type(exc).__name__})")

    try:
        state = phase_state(store, cfg, calendar, now)
        checkpoint = state["checkpoint"]
        record["phase"] = {
            "phase": state["phase"], "reasons": state["reasons"],
            "computed_reasons": state["computed_reasons"],
            "checkpoint_id": checkpoint["checkpoint_id"] if checkpoint else None,
            "attention_required": state["attention_required"],
            "calendar_uncertainty": state["calendar_uncertainty"]}
        if state["computed_reasons"] and not checkpoint:
            blocked.append("checkpoint_due")
    except (CorruptRecord, OSError, ValueError, KeyError) as exc:
        unknown.append(f"the bounded phase could not be established ({type(exc).__name__})")

    try:
        notes = [n for n in notification_states(store, now) if n["outstanding"]]
        record["notifications_outstanding"] = [{k: n.get(k) for k in NOTE_FIELDS} for n in notes]
        for note in notes:
            if note["delivery_state"] == UNRESOLVED:
                unknown.append(f"notification {note['notification_id']} has an unresolved "
                               "delivery history")
            else:
                blocked.append(f"notification_outstanding: {note['notification_id']}")
    except (CorruptRecord, OSError, ValueError, KeyError) as exc:
        unknown.append(f"the notification records could not be read ({type(exc).__name__})")

    try:
        faults = store.faults()
        open_faults = [f for f in faults if not f["resolved"]]
        record["faults_open"] = [{"fault_id": f["fault_id"], "kind": f.get("kind"),
                                  "reason_code": f["reason_code"]} for f in open_faults]
        for fault in open_faults:
            if fault.get("resolution_rejected"):
                unknown.append(f"fault {fault['fault_id']} has a resolution that does not check "
                               "out")
            else:
                blocked.append(f"fault_open: {fault['fault_id']}")
    except (CorruptRecord, OSError, ValueError, KeyError) as exc:
        unknown.append(f"the fault records could not be read ({type(exc).__name__})")
