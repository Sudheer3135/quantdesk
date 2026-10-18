"""The observer's own records: append-only, published whole, never replaced.

A record is written to a hidden scratch file, synced, and then hard-linked to
its final name. The link fails if the name exists, so nothing is overwritten,
and a reader never sees a half-written record under a final name.

Every record the observer writes carries `integrity`: the SHA-256 of its own
content. Every read checks that hash and the record's required fields, so a
record that still parses as JSON but has lost or changed its content raises
`CorruptRecord` instead of dropping out of the accounting.

Layout under the report root (outside QuantDesk's database and research/):

    reports/<session-date>/<check>-<start-utc>-<id>.json
    confirmations/<session-date>/<same name as the report>   written only after the
                   report and every directory above it were synced; binds the
                   report's id, path, content hash and file hash
    corrections/<session-date>/correction-<utc>-<id>.json
    checkpoints/review-pending.json      written once; never changed
    outbox/events/<session-date>-<type>-<subject>.json
    outbox/notifications/<id>-intent.json        written before any delivery
    outbox/notifications/<id>-round-<n>-reservation.json   written BEFORE the request
    outbox/notifications/<id>-round-<n>-outcome.json       written after it returns; counts
                   only once checked against its reservation and synced (see below)
    outbox/notifications/<id>-attempt-<n>.json   older versions' attempt records; read only
    outbox/heartbeats/<date>/<utc>-<id>-reservation.json, -outcome.json   liveness
    faults/<fault-id>-fault.json, <fault-id>-resolution.json
    startup/<binding-sha256>/<content-sha256>.json   the exact bytes of one API
                   process's scheduler start-up declarations, bound to that
                   process (v0.4.12, see `StartupCaptures`)

A report counts as evidence only with a matching confirmation, and only after
this process has itself checked the two against each other and synced both
(see `Store.confirmed`). `reports()` returns only such reports; everything
else is diagnostic (`all_reports()`). A resolution counts only once it has
been checked against its fault and synced, likewise. So does a request's
outcome (notification or heartbeat): a readable, linked outcome file is not
enough. Until its reader has checked it against its reservation and synced it
and every directory above it, the reservation stands as a round whose
completion is unknown, and nothing about delivery is claimed.
    state.json     a cache of the phase state, rebuilt from the records
    notification-state.json   a cache of what is still undelivered, likewise

Notification records written by earlier observer versions (intent and attempt
schemas /1 and /2) are read as they are and never rewritten, renamed or
migrated; `notifications()` presents old and new history in one form in
memory only.
    lock           held for every run, correction and checkpoint change

Operator records live under a different root that the observer service can
read and cannot write: approvals/, acknowledgments/ and maintenance/.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from . import VERSION

REPORT_SCHEMA = "observer_report/3"
INTENT_SCHEMAS = ("observer_notification_intent/1", "observer_notification_intent/2")
INTENT_SCHEMA = INTENT_SCHEMAS[-1]                       # what is written now
ATTEMPT_SCHEMAS = ("observer_notification_attempt/1", "observer_notification_attempt/2")
RESERVATION_SCHEMA = "observer_notification_reservation/1"
OUTCOME_SCHEMA = "observer_notification_outcome/1"
FAULT_SCHEMA, RESOLUTION_SCHEMA = "observer_fault/1", "observer_fault_resolution/1"
CONFIRMATION_SCHEMA = "observer_report_confirmation/1"
PUBLICATION_KIND = "report-publication"
PING_CHECKS = ("liveness", "premarket", "midsession", "afterclose")
UNDELIVERED = "Notification undelivered; remote receipt unknown."
CHECKS = ("premarket", "midsession", "afterclose")
RESULTS = ("PASS", "FAIL", "NO SESSION", "SPECIAL SESSION")
OUTCOMES = ("QUALIFIED", "INCOMPLETE", "OPERATIONAL EXCEPTION REQUIRED", "UNKNOWN")
REPORT_FIELDS = {
    "report_id": str, "check": str, "session_date": str, "observer_version": str,
    "expected_quantdesk_revision": str, "scheduled_at_ist": str, "deadline_at_ist": str,
    "started_at_utc": str, "started_at_ist": str, "completed_at_utc": str,
    "completed_at_ist": str, "lateness_seconds": (int, float), "started_in_window": bool,
    "timely": bool, "ran_checks": bool, "result": str, "checks": list,
    "unknown_fields": list, "failure_reasons": list,
}


class Overlap(Exception):
    """Another observer run holds the lock."""


class CorruptRecord(Exception):
    """A stored record is unreadable, incomplete or altered. Reported, never skipped."""


class PublicationIncomplete(OSError):
    """The report is linked, but a later step of confirming it did not complete.

    `stage` is "report-sync", "confirmation-write" or "confirmation-sync".
    """

    def __init__(self, stage: str, cause: BaseException):
        super().__init__(getattr(cause, "errno", None), f"{stage} failed ({type(cause).__name__})")
        self.stage = stage


class DurabilityUnconfirmed(OSError):
    """The record's final name was linked, but making that name durable failed.

    The record is there and complete; whether it survives a crash is not
    known. It is never reported as absent.
    """


def content_hash(record: dict) -> str:
    body = {k: v for k, v in record.items() if k != "integrity"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()


def seal(record: dict) -> dict:
    """`record` as it will be stored: JSON-plain, with its own content hash."""
    plain = json.loads(json.dumps(record, default=str, allow_nan=False))
    plain["integrity"] = {"algorithm": "sha256", "content_sha256": content_hash(plain)}
    return plain


def publish(path: Path, record: dict, mode: int = 0o440) -> bool:
    """Create `path` holding all of `record`, or leave it absent. False if it exists."""
    sealed = seal(record)
    scratch = path.parent / f".tmp-{uuid.uuid4().hex}"
    fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(sealed, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(scratch, path)
        except FileExistsError:
            return False
    finally:
        os.unlink(scratch)
    try:
        sync_directory(path.parent)
    except OSError as exc:
        raise DurabilityUnconfirmed(
            exc.errno, f"{path.name} is linked, but its directory could not be synced "
                       f"({type(exc).__name__})") from exc
    return True


def sync_directory(directory: Path) -> None:
    handle = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def sync_file(path: Path) -> None:
    """Make an existing record, and its name, durable: the last step of publishing it."""
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)
    sync_directory(path.parent)


def sync_chain(path: Path, root: Path) -> None:
    """Sync a record, its directory, and every directory above it up to `root`.

    A directory made for the record (a new session date, say) is itself a
    name in its parent, so each level must be synced for the record to be
    found after a crash.
    """
    sync_file(path)
    directory = path.parent
    while directory != root and root in directory.parents:
        directory = directory.parent
        sync_directory(directory)


def is_sha256(value) -> bool:
    """A well-formed SHA-256 as the observer writes it: 64 lowercase hex digits."""
    return isinstance(value, str) and len(value) == 64 and \
        all(c in "0123456789abcdef" for c in value)


def instant(value) -> datetime | None:
    """A timezone-aware moment from its ISO text, or None if missing, malformed or naive."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.utcoffset() is not None else None


def replacement_problem(report: dict, fault: dict, resolved_at) -> str | None:
    """Why `report` cannot end the attention of a missing report's `fault`, or None.

    It must be a genuine later scheduled assessment of the same check: not a
    reconstruction, a replay of another day or an early run; its checks run to
    completion, not interrupted; a different invocation from the failed one;
    started strictly after the fault was detected, and completed no earlier
    than it started and no later than `resolved_at`. Times are compared as
    instants. Anything missing, malformed or contradictory fails.
    """
    found = fault.get("report") or {}
    if report.get("invocation") != "scheduled":
        return "not a scheduled assessment (a reconstruction or replay)"
    if report.get("catch_up"):
        return "an early run, which checks nothing"
    if report.get("check") != found.get("check"):
        return "an assessment of a different check"
    if report.get("ran_checks") is not True or report.get("timed_out") \
            or "assessment_interrupted_at_utc" in report:
        return "its checks did not run to completion"
    if report.get("report_id") in (found.get("report_id"), fault.get("detected_by_invocation")):
        return "the same invocation as the failed publication"
    started = instant(report.get("assessment_started_at_utc"))
    completed = instant(report.get("assessment_completed_at_utc"))
    ran_on = instant(report.get("started_at_ist"))
    detected, resolved = instant(fault.get("detected_at_utc")), instant(resolved_at)
    if None in (started, completed, ran_on, detected, resolved):
        return "a required time is missing or malformed"
    if report.get("session_date") != ran_on.date().isoformat():
        return "an assessment of a different day than the one it ran on"
    if not started > detected:
        return "it started before the publication failed"
    if completed < started or completed > resolved:
        return "its times contradict each other or the resolution"
    return None


def read_json(path: Path) -> dict:
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise CorruptRecord(f"{path} could not be read as a record: {type(exc).__name__}") from exc
    if not isinstance(record, dict) or not isinstance(record.get("schema"), str):
        raise CorruptRecord(f"{path} is not an observer record")
    return record


def read_record(path: Path, schema: str | tuple[str, ...] | None = None) -> dict:
    """A record the observer wrote: parsed, of an expected schema, and unaltered."""
    record = read_json(path)
    accepted = (schema,) if isinstance(schema, str) else schema
    if accepted is not None and record["schema"] not in accepted:
        raise CorruptRecord(f"{path} has schema {record['schema']!r}; this observer version "
                            f"accounts only for {', '.join(accepted)}")
    seal_ = record.get("integrity")
    try:
        intact = isinstance(seal_, dict) and seal_.get("content_sha256") == content_hash(record)
    except (TypeError, ValueError):
        intact = False
    if not intact:
        raise CorruptRecord(f"{path} does not match its own content hash")
    return record


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _moment(value: str) -> None:
    if datetime.fromisoformat(value).tzinfo is None:
        raise ValueError("timestamp without a time zone")


def moment(value) -> datetime:
    """A stored timestamp as an aware datetime, or ValueError."""
    if not isinstance(value, str):
        raise ValueError("timestamp is not text")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp without a time zone")
    return parsed


def _typed(path: Path, record: dict, fields: dict) -> None:
    wrong = [k for k, kind in fields.items() if not isinstance(record.get(k), kind)
             or isinstance(record.get(k), bool) and kind is not bool]
    if wrong:
        raise CorruptRecord(f"{path} is structurally incomplete: {', '.join(wrong)} missing "
                            "or of the wrong type")


def validate_report(path: Path, report: dict) -> dict:
    """Refuse a report that lacks what the accounting relies on."""
    problems = [f"{key} missing or of the wrong type" for key, kind in REPORT_FIELDS.items()
                if not isinstance(report.get(key), kind) or isinstance(report.get(key), bool)
                and kind is not bool]
    if not problems:
        try:
            date.fromisoformat(report["session_date"])
            for key in ("scheduled_at_ist", "deadline_at_ist", "started_at_utc",
                        "completed_at_utc"):
                _moment(report[key])
        except ValueError as exc:
            problems.append(f"unusable date or time: {exc}")
        if report["check"] not in CHECKS:
            problems.append(f"unknown check {report['check']!r}")
        if report["result"] not in RESULTS:
            problems.append(f"unknown result {report['result']!r}")
        if not all(isinstance(c, dict) and isinstance(c.get("name"), str)
                   and isinstance(c.get("status"), str) and isinstance(c.get("required"), bool)
                   for c in report["checks"]):
            problems.append("a check entry lacks its name, status or required flag")
        if path.parent.name != report["session_date"] or \
                not path.name.startswith(f"{report['check']}-") or \
                not path.name.endswith(f"-{report['report_id']}.json"):
            problems.append("the file's name or folder does not match the report inside it")
        if report["check"] == "afterclose" and report["ran_checks"]:
            if report.get("outcome") not in OUTCOMES:
                problems.append(f"after-close outcome {report.get('outcome')!r}")
            if not isinstance(report.get("collection_gates_passed"), bool) or \
                    isinstance(report.get("valid_early_review_candidates"), bool) or \
                    not isinstance(report.get("valid_early_review_candidates"), int):
                problems.append("after-close accounting fields missing or of the wrong type")
    if problems:
        raise CorruptRecord(f"{path} is structurally incomplete: {'; '.join(problems)}")
    return report


class Store:
    def __init__(self, root: str | os.PathLike, operator_root: str | os.PathLike | None = None):
        self.root = Path(root)
        self.operator_root = Path(operator_root) if operator_root else None
        # Failures only. A confirmation or resolution this process tried to make
        # and could not stays failed for the rest of the process, however its
        # file reads later. Nothing positive is remembered: every authoritative
        # read checks, and syncs, the exact evidence it relies on.
        self._failed_confirmations: set[str] = set()   # report ids
        self._failed_resolutions: set[str] = set()     # fault ids
        self._failed_outcomes: set[str] = set()        # outcome file names
        self.unconfirmed: dict[str, str] = {}          # report id -> why, for diagnostics
        self.unverified_outcomes: dict[str, str] = {}  # outcome file name -> why, likewise

    def _dir(self, *parts: str) -> Path:
        path = self.root.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True, mode=0o750)
        return path

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]

    # -- one writer at a time ------------------------------------------------
    def lock(self, wait_seconds: float = 0.0):
        handle = open(self._dir() / "lock", "a")  # noqa: SIM115 - held by the caller
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except OSError as exc:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise Overlap("another observer run is in progress") from exc
                time.sleep(0.2)

    # -- reports and corrections ---------------------------------------------
    @staticmethod
    def _stamp(text: str) -> str:
        return text.replace("-", "").replace(":", "")[:15] + "Z"

    def report_path(self, report: dict) -> Path:
        """The one name a report is published under."""
        return (self.root / "reports" / report["session_date"]
                / f"{report['check']}-{self._stamp(report['started_at_utc'])}"
                  f"-{report['report_id']}.json")

    def write_report(self, report: dict) -> Path:
        self._dir("reports", report["session_date"])
        path = self.report_path(report)
        validate_report(path, report)
        if not publish(path, report):
            raise FileExistsError(f"{path} already exists; reports are never replaced")
        return path

    def write_correction(self, correction: dict) -> Path:
        path = (self._dir("corrections", correction["session_date"])
                / f"correction-{self._stamp(correction['recorded_at_utc'])}"
                  f"-{correction['correction_id']}.json")
        if not publish(path, correction):
            raise FileExistsError(f"{path} already exists")
        return path

    def _paths(self, kind: str, session_date: str | None = None) -> list[Path]:
        base = self.root / kind
        if not base.is_dir():
            return []
        days = [base / session_date] if session_date else sorted(base.iterdir())
        return [p for day in days if day.is_dir() for p in sorted(day.glob("*.json"))]

    def all_reports(self, session_date: str | None = None,
                    check: str | None = None) -> list[dict]:
        """Every stored report, confirmed or not. Diagnostic: never evidence."""
        found = [validate_report(p, read_record(p, REPORT_SCHEMA))
                 for p in self._paths("reports", session_date)]
        return [r for r in found if check is None or r["check"] == check]

    def reports(self, session_date: str | None = None, check: str | None = None) -> list[dict]:
        """The reports that count: each one confirmed, verified and synced by this process.

        A readable report, a report with a valid seal and a report with no fault
        against it are all excluded unless its confirmation checks out.
        """
        out, blocked = [], None
        for path in self._paths("reports", session_date):
            report = validate_report(path, read_record(path, REPORT_SCHEMA))
            if check is not None and report["check"] != check:
                continue
            if blocked is None:
                blocked = self.open_publication_reports()
            if self.confirmed(path, report, blocked):
                out.append(report)
        return out

    # -- positive confirmation of a published report ----------------------------
    def confirmation_path(self, report_path: Path) -> Path:
        return self.root / "confirmations" / report_path.parent.name / report_path.name

    def _confirmation_mismatch(self, record: dict, path: Path, report: dict) -> str | None:
        if not (is_sha256(record.get("content_sha256")) and is_sha256(record.get("file_sha256"))):
            return "the confirmation lacks a well-formed hash"
        if record.get("report_id") != report["report_id"]:
            return "the confirmation names a different report"
        if record.get("report_path") != str(path.relative_to(self.root)):
            return "the confirmation names a different path"
        if record.get("content_sha256") != report["integrity"]["content_sha256"]:
            return "the report's content hash differs from the confirmed one"
        if record.get("file_sha256") != file_sha256(path):
            return "the report's bytes differ from the confirmed ones"
        return None

    def confirm_report(self, path: Path, basis: str, extra: dict | None = None) -> dict:
        """Steps B to E of publishing: sync the report, then publish and sync its confirmation.

        Raises PublicationIncomplete, naming the stage, if any step fails; the
        report is then unconfirmed for the rest of this process. A confirmation
        already under the name is used only if it is exactly this report's.
        """
        report = validate_report(path, read_record(path, REPORT_SCHEMA))
        rid = report["report_id"]
        self._failed_confirmations.add(rid)           # until every step has succeeded
        try:
            sync_chain(path, self.root)
        except OSError as exc:
            raise PublicationIncomplete("report-sync", exc) from exc
        record = {"schema": CONFIRMATION_SCHEMA, "report_id": rid,
                  "report_path": str(path.relative_to(self.root)),
                  "content_sha256": report["integrity"]["content_sha256"],
                  "file_sha256": file_sha256(path), "basis": basis, **(extra or {})}
        target = self.confirmation_path(path)
        stage = "confirmation-write"
        try:
            self._dir("confirmations", path.parent.name)
            if not publish(target, record):
                why = self._confirmation_mismatch(read_record(target, CONFIRMATION_SCHEMA),
                                                  path, report)
                if why:
                    raise CorruptRecord(f"{target.name}: {why}")
            stage = "confirmation-sync"
            sync_chain(target, self.root)
        except DurabilityUnconfirmed as exc:
            raise PublicationIncomplete("confirmation-sync", exc) from exc
        except (OSError, CorruptRecord, ValueError) as exc:
            raise PublicationIncomplete(stage, exc) from exc
        self._failed_confirmations.discard(rid)
        self.unconfirmed.pop(rid, None)
        return record

    def confirmed(self, path: Path, report: dict, blocked: set[str] | None = None) -> bool:
        """Whether `report`, stored at `path`, may be relied on. The one rule for every reader.

        Only with a confirmation that is readable, sealed, of this exact report
        (id, path, content hash and file hash), only after this process has
        synced the report, the confirmation and the directories above both, and
        only while no publication fault against the report is open. Checked
        afresh on every call. `blocked` is the open-fault set when the caller
        has just read it for the same read.
        """
        if not self._confirmation_valid(path, report):
            return False
        if blocked is None:
            blocked = self.open_publication_reports()
        if report["report_id"] in blocked:
            self.unconfirmed[report["report_id"]] = "a publication fault against it is open"
            return False
        return True

    def open_publication_reports(self) -> set[str]:
        """Report ids named by a publication fault that is not (durably) resolved."""
        return {f["report"]["report_id"] for f in self.faults()
                if not f["resolved"] and f.get("kind") == PUBLICATION_KIND
                and isinstance(f.get("report"), dict)}

    def _confirmation_valid(self, path: Path, report: dict) -> bool:
        """The confirmation half of `confirmed`, from the files as they are now.

        A confirmation this process failed to make stays failed here.
        """
        rid = report["report_id"]
        if rid in self._failed_confirmations:
            self.unconfirmed[rid] = "confirming it failed in this process"
            return False
        verdict, why = False, None
        target = self.confirmation_path(path)
        try:
            if not target.exists():
                why = "no confirmation"
            else:
                why = self._confirmation_mismatch(read_record(target, CONFIRMATION_SCHEMA),
                                                  path, report)
                if why is None:
                    sync_chain(path, self.root)
                    sync_chain(target, self.root)
                    verdict = True
        except (CorruptRecord, OSError, ValueError) as exc:
            why = f"the confirmation could not be verified and synced ({type(exc).__name__})"
        if why:
            self.unconfirmed[rid] = why
        else:
            self.unconfirmed.pop(rid, None)
        return verdict

    def report_file(self, session_date: str, report_id: str) -> Path | None:
        for path in self._paths("reports", session_date):
            if path.name.endswith(f"-{report_id}.json"):
                return path
        return None

    def corrections(self, session_date: str | None = None) -> list[dict]:
        return [read_record(p, "observer_correction/1")
                for p in self._paths("corrections", session_date)]

    # -- the checkpoint: reached once, kept for good ---------------------------
    @property
    def checkpoint_path(self) -> Path:
        return self.root / "checkpoints" / "review-pending.json"

    def checkpoint(self) -> dict | None:
        if not self.checkpoint_path.exists():
            return None
        record = read_record(self.checkpoint_path, "observer_checkpoint/2")
        if not isinstance(record.get("checkpoint_id"), str) or \
                not isinstance(record.get("reasons"), list):
            raise CorruptRecord(f"{self.checkpoint_path} is structurally incomplete")
        return record

    def write_checkpoint(self, record: dict) -> bool:
        self._dir("checkpoints")
        return publish(self.checkpoint_path, record)

    # -- operator records: read here, written elsewhere ------------------------
    def operator_paths(self, kind: str) -> list[Path]:
        if self.operator_root is None or not (self.operator_root / kind).is_dir():
            return []
        return sorted((self.operator_root / kind).glob("*.json"))

    # -- outbox ----------------------------------------------------------------
    def write_event(self, event: dict) -> bool:
        """One file per (session, type, subject); a repeat is not raised twice."""
        name = f"{event['session_date']}-{event['type']}-{event['subject']}.json"
        return publish(self._dir("outbox", "events") / name, event)

    def events(self) -> list[dict]:
        base = self.root / "outbox" / "events"
        return [read_record(p, "observer_event/1") for p in sorted(base.glob("*.json"))] \
            if base.is_dir() else []

    def write_intent(self, intent: dict) -> bool:
        """False if an intent of that id is already stored; it is left as it is."""
        return publish(self._dir("outbox", "notifications")
                       / f"{intent['notification_id']}-intent.json", intent)

    def write_reservation(self, reservation: dict) -> bool:
        """Record that a request is about to be made. False if that round is already reserved.

        Named by the first intent and its round, so the same round cannot be
        reserved twice whoever tries. Never replaced.
        """
        first = reservation["notification_ids"][0]
        number = reservation["rounds"][first]
        return publish(self._dir("outbox", "notifications")
                       / f"{first}-round-{number:03d}-reservation.json", reservation)

    def write_outcome(self, reservation: dict, outcome: dict) -> None:
        first = reservation["notification_ids"][0]
        number = reservation["rounds"][first]
        self._publish_outcome(self._dir("outbox", "notifications")
                              / f"{first}-round-{number:03d}-outcome.json", outcome)

    def write_heartbeat(self, kind: str, record: dict) -> None:
        """A liveness heartbeat's reservation or outcome. Kept apart: it is never retried."""
        stamp = self._stamp(record["reserved_at_utc"])
        path = (self._dir("outbox", "heartbeats", record["reserved_at_utc"][:10])
                / f"{stamp}-{record['reservation_id']}-{kind}.json")
        if kind == "outcome":
            self._publish_outcome(path, record)
        elif not publish(path, record):
            raise FileExistsError("that heartbeat record already exists")

    def _publish_outcome(self, path: Path, record: dict) -> None:
        """Publish an outcome and sync it and every directory above it, or raise.

        Until all of that has succeeded the outcome is failed for this process,
        whatever its file later reads as: an interruption at any point (the
        link, a directory sync, the notification deadline) leaves it so.
        """
        self._failed_outcomes.add(path.name)
        if not publish(path, record):
            raise FileExistsError("that round already has an outcome; outcomes are never replaced")
        sync_chain(path, self.root)
        self._failed_outcomes.discard(path.name)

    @staticmethod
    def _outcome(path: Path, reservation: dict) -> dict:
        """An outcome record, checked against the reservation it closes, or CorruptRecord.

        Its schema and seal, its fields and their types, the reservation it
        names (id, identity, kind and reservation time must all be that
        reservation's), and its delivery evidence (`delivered` and
        `delivery_state` must say the same thing).
        """
        record = read_record(path, OUTCOME_SCHEMA)
        _typed(path, record, {"reservation_id": str, "check": str, "kind": str,
                              "reserved_at_utc": str, "completed_at_utc": str,
                              "delivered": bool, "delivery_state": str, "result": str,
                              "receipt": str})
        if any(record[k] != reservation.get(k)
               for k in ("reservation_id", "check", "kind", "reserved_at_utc")):
            raise CorruptRecord(f"{path} is an outcome with no matching reservation")
        try:
            moment(record["completed_at_utc"])
        except ValueError as exc:
            raise CorruptRecord(f"{path} holds an unusable timestamp") from exc
        if record["delivery_state"] != ("delivered" if record["delivered"] else "undelivered"):
            raise CorruptRecord(f"{path} contradicts itself about whether it was delivered")
        return record

    def _outcome_undurable(self, path: Path) -> str | None:
        """Why a checked outcome cannot be relied on yet, or None once it has been synced.

        Checked afresh on every read: nothing positive is remembered. An
        outcome this process failed to make durable stays failed here.
        """
        if path.name in self._failed_outcomes:
            why = "making it durable failed in this process"
        else:
            try:
                sync_chain(path, self.root)
                why = None
            except OSError as exc:
                why = f"it could not be synced ({type(exc).__name__})"
        if why:
            self.unverified_outcomes[path.name] = why
        else:
            self.unverified_outcomes.pop(path.name, None)
        return why

    def heartbeats(self, day: str) -> list[dict]:
        """Each heartbeat reservation with its outcome, if the outcome checks out and is durable.

        An outcome that is not yet durable is left out (`outcome_unverified`
        says why): the heartbeat's completion is then unknown.
        """
        base = self.root / "outbox" / "heartbeats" / day
        out, names = {}, {}
        for path in sorted(base.glob("*-reservation.json")) if base.is_dir() else []:
            record = read_record(path, RESERVATION_SCHEMA)
            out[record["reservation_id"]] = {**record, "outcome": None}
            names[record["reservation_id"]] = path.name[:-len("-reservation.json")]
        for path in sorted(base.glob("*-outcome.json")) if base.is_dir() else []:
            claimed = read_record(path, OUTCOME_SCHEMA).get("reservation_id")
            if claimed not in out or names[claimed] != path.name[:-len("-outcome.json")]:
                raise CorruptRecord(f"{path} is an outcome with no reservation")
            record = self._outcome(path, out[claimed])
            why = self._outcome_undurable(path)
            if why:
                out[claimed]["outcome_unverified"] = why
            else:
                out[claimed]["outcome"] = record
        return list(out.values())

    # Reading notification history, old and new, in one form -------------------
    @staticmethod
    def _intent(path: Path) -> dict:
        intent = read_record(path, INTENT_SCHEMAS)
        _typed(path, intent, {"notification_id": str, "check": str, "kind": str,
                              "event_type": str, "body": str, "created_at_utc": str})
        try:
            moment(intent["created_at_utc"])
        except ValueError as exc:
            raise CorruptRecord(f"{path} has an unusable creation time") from exc
        if (path.name != f"{intent['notification_id']}-intent.json"
                or intent["check"] not in PING_CHECKS or intent["kind"] not in ("success", "fail")
                or not isinstance(intent.get("checkpoint_id"), str | None)):
            raise CorruptRecord(f"{path} is structurally invalid for a notification intent")
        return intent

    @staticmethod
    def _legacy_attempt(path: Path) -> dict:
        """An attempt record of schema /1 or /2, as one entry of attempt history.

        /1 made up to five requests inside one attempt and listed them as
        `transport_attempts`; each is counted as a round, and the latest time
        anywhere in the record is the time spacing is measured from. Nothing
        is assumed where the record is silent: a /1 attempt that does not list
        its requests leaves the history unresolved.
        """
        record = read_record(path, ATTEMPT_SCHEMAS)
        _typed(path, record, {"notification_id": str, "attempt_number": int,
                              "attempted_at_utc": str, "delivered": bool})
        if path.name != (f"{record['notification_id']}-attempt-"
                         f"{record['attempt_number']:03d}.json"):
            raise CorruptRecord(f"{path} does not match the attempt it holds")
        stamps, rounds, unresolved = [record["attempted_at_utc"]], 1, None
        if record["schema"] == ATTEMPT_SCHEMAS[0]:
            requests = record.get("transport_attempts")
            if requests is None:
                unresolved = "a schema /1 attempt does not list the requests it made"
            elif not isinstance(requests, list) or not all(
                    isinstance(r, dict) and isinstance(r.get("at_utc"), str)
                    and isinstance(r.get("result"), str) for r in requests):
                raise CorruptRecord(f"{path} lists transport attempts of the wrong shape")
            else:
                stamps += [r["at_utc"] for r in requests]
                rounds = max(1, len(requests))
        notified, reason = record.get("notified_at_utc"), record.get("failure_reason")
        if notified is not None:
            stamps.append(notified)
        try:
            latest = max(moment(value) for value in stamps)
        except ValueError as exc:
            raise CorruptRecord(f"{path} holds an unusable timestamp") from exc
        if (record["delivered"] and reason is not None) or (
                not record["delivered"] and notified is not None) or not isinstance(
                reason, str | None):
            raise CorruptRecord(f"{path} contradicts itself about whether it was delivered")
        return {"record": record["schema"], "record_file": path.name,
                "attempt_number": record["attempt_number"],
                "attempted_at_utc": latest.isoformat(),
                "first_recorded_at_utc": record["attempted_at_utc"],
                "delivered": record["delivered"], "completion": "recorded",
                "notified_at_utc": notified, "failure_reason": reason,
                "result": record.get("result", reason or "delivered"),
                "receipt": record.get("receipt"), "rounds": rounds,
                "request_timeout_seconds": record.get("request_timeout_seconds"),
                "carried_by_notification_id": record.get("carried_by_notification_id"),
                "reservation_id": None, "unresolved": unresolved,
                "notification_id": record["notification_id"]}

    @staticmethod
    def _reservation(path: Path) -> dict:
        record = read_record(path, RESERVATION_SCHEMA)
        _typed(path, record, {"reservation_id": str, "check": str, "kind": str,
                              "notification_ids": list, "rounds": dict, "reserved_at_utc": str,
                              "next_eligible_at_utc": dict, "state": str})
        ids = record["notification_ids"]
        try:
            moment(record["reserved_at_utc"])
            for value in record["next_eligible_at_utc"].values():
                moment(value)
            named = f"{ids[0]}-round-{record['rounds'][ids[0]]:03d}-reservation.json"
        except (ValueError, LookupError, TypeError) as exc:
            raise CorruptRecord(f"{path} is structurally invalid for a reservation") from exc
        if (path.name != named or record["state"] != "reserved" or len(set(ids)) != len(ids)
                or not all(isinstance(i, str) for i in ids)
                or set(record["rounds"]) != set(ids)
                or set(record["next_eligible_at_utc"]) != set(ids)
                or not all(isinstance(n, int) and n > 0 for n in record["rounds"].values())):
            raise CorruptRecord(f"{path} is structurally invalid for a reservation")
        return record

    def notifications(self, tolerant: bool = False):
        """Every intent with its whole attempt history, oldest first.

        Strict by default: any record that is corrupt, of an unsupported
        schema, of the wrong shape, contradictory or orphaned raises
        `CorruptRecord`. With `tolerant` the readable intents are returned
        together with the list of problems, and an intent whose own history
        could not be read in full is marked `unresolved` — so that delivery
        can still raise an alert about the damage without ever treating a
        damaged history as permission to send again.
        """
        base = self.root / "outbox" / "notifications"
        notes, problems = {}, []

        def guard(path, reader):
            try:
                return reader(path)
            except CorruptRecord as exc:
                if not tolerant:
                    raise
                problems.append(str(exc))
                return None

        paths = sorted(base.glob("*.json")) if base.is_dir() else []
        known = ("-intent.json", "-reservation.json", "-outcome.json")
        for path in paths:
            if path.name.endswith("-intent.json"):
                intent = guard(path, self._intent)
                if intent:
                    notes[intent["notification_id"]] = {**intent, "attempts": [],
                                                        "unresolved": None}
            elif not path.name.endswith(known) and "-attempt-" not in path.name:
                guard(path, lambda p: (_ for _ in ()).throw(
                    CorruptRecord(f"{p} is not a record this observer version knows")))

        def owner(path, notification_id, what):
            if notification_id in notes:
                return notes[notification_id]
            return guard(path, lambda p: (_ for _ in ()).throw(
                CorruptRecord(f"{p} is {what} for an intent that is not stored")))

        def damaged(path):
            """Whose history a file that cannot be read belongs to, from its name."""
            for notification_id, note in notes.items():
                if path.name.startswith(notification_id + "-"):
                    note["unresolved"] = f"{path.name} could not be read"

        for path in paths:
            if "-attempt-" in path.name and not path.name.endswith(known):
                attempt = guard(path, self._legacy_attempt)
                if attempt is None:
                    damaged(path)
                    continue
                note = owner(path, attempt["notification_id"], "an attempt")
                if note is not None:
                    note["attempts"].append(attempt)
                    note["unresolved"] = note["unresolved"] or attempt["unresolved"]
        reservations = {}
        for path in paths:
            if path.name.endswith("-reservation.json"):
                record = guard(path, self._reservation)
                if record is None:
                    damaged(path)
                else:
                    reservations[path.name[:-len("-reservation.json")]] = (path, record, None,
                                                                           None)
        for path in paths:
            if path.name.endswith("-outcome.json"):
                key = path.name[:-len("-outcome.json")]

                def outcome(p, key=key):
                    if key not in reservations:
                        raise CorruptRecord(f"{p} is an outcome with no matching reservation")
                    return self._outcome(p, reservations[key][1])
                record = guard(path, outcome)
                if record is None:
                    damaged(path)
                    continue
                # Checked, but not yet relied on: only an outcome synced now, with every
                # directory above it, closes its round. Otherwise the round stays open.
                why = self._outcome_undurable(path)
                reservations[key] = (*reservations[key][:2], None if why else record, why)
        for path, record, outcome, *unverified in reservations.values():
            first = record["notification_ids"][0]
            unclosed = ("interrupted: no outcome was recorded for this reservation"
                        if unverified[0] is None else
                        f"completion unknown: an outcome is stored but {unverified[0]}")
            for notification_id in record["notification_ids"]:
                note = owner(path, notification_id, "a reservation")
                if note is None:
                    continue
                done = outcome is not None
                note["attempts"].append({
                    "record": RESERVATION_SCHEMA, "record_file": path.name,
                    "attempted_at_utc": record["reserved_at_utc"],
                    "first_recorded_at_utc": record["reserved_at_utc"],
                    "round": record["rounds"][notification_id],
                    "next_eligible_at_utc": record["next_eligible_at_utc"][notification_id],
                    "delivered": bool(done and outcome["delivered"]),
                    # Reserved and never closed: the process stopped before, during or
                    # just after the request. Which of those is not known.
                    "completion": "recorded" if done else "unknown",
                    "notified_at_utc": outcome["completed_at_utc"]
                    if done and outcome["delivered"] else None,
                    "result": outcome["result"] if done else unclosed,
                    "failure_reason": None if done and outcome["delivered"] else (
                        outcome["result"] if done else unclosed),
                    "outcome_unverified": unverified[0],
                    "receipt": outcome.get("receipt") if done else UNDELIVERED,
                    "rounds": 1,
                    "request_timeout_seconds": record.get("request_timeout_seconds"),
                    "carried_by_notification_id": None if notification_id == first else first,
                    "shared_with_notification_ids": [i for i in record["notification_ids"]
                                                     if i != notification_id],
                    "reservation_id": record["reservation_id"], "unresolved": None,
                    "notification_id": notification_id})
        out = []
        for note in notes.values():
            note["attempts"].sort(key=lambda a: (moment(a["attempted_at_utc"]),
                                                 a.get("attempt_number", 0)))
            legacy = [a["attempt_number"] for a in note["attempts"] if "attempt_number" in a]
            delivered = [i for i, a in enumerate(note["attempts"]) if a["delivered"]]
            if sorted(legacy) != list(range(1, len(legacy) + 1)) or (
                    delivered and delivered[0] != len(note["attempts"]) - 1):
                problem = (f"the attempt history of {note['notification_id']} contradicts "
                           "itself (numbers missing or repeated, or attempts after delivery)")
                if not tolerant:
                    raise CorruptRecord(problem)
                problems.append(problem)
                note["unresolved"] = note["unresolved"] or problem
            for number, attempt in enumerate(note["attempts"], 1):
                attempt["attempt_number"] = number
            note["delivered"] = bool(delivered)
            out.append(note)
        out.sort(key=lambda n: (moment(n["created_at_utc"]), n["notification_id"]))
        return (out, problems) if tolerant else out

    # -- the observer's own faults: independent of any session's qualification --
    def write_fault(self, fault: dict) -> None:
        if not publish(self._dir("faults") / f"{fault['fault_id']}-fault.json", fault):
            raise FileExistsError("that fault is already recorded")

    def write_fault_resolution(self, resolution: dict) -> bool:
        """Publish a resolution and sync it and the directories above it.

        Raises if any of that fails; the fault then stays open for the rest of
        this process, whatever a later process makes of the file.
        """
        fault_id = resolution["fault_id"]
        path = self._dir("faults") / f"{fault_id}-resolution.json"
        self._failed_resolutions.add(fault_id)
        created = publish(path, resolution)
        sync_chain(path, self.root)
        self._failed_resolutions.discard(fault_id)
        return created

    def _resolution(self, fault: dict, path: Path) -> tuple[dict | None, str | None]:
        """A stored resolution, if it checks out against its fault now and has been synced."""
        if fault["fault_id"] in self._failed_resolutions:
            return None, "making the resolution durable failed in this process"
        try:
            record = read_record(path, RESOLUTION_SCHEMA)
            why = self.resolution_problem(fault, record)
            if why is None:
                sync_chain(path, self.root)
        except (CorruptRecord, OSError, ValueError, KeyError, TypeError) as exc:
            record, why = None, f"it could not be verified and synced ({type(exc).__name__})"
        return (record, None) if why is None else (None, why)

    def resolution_problem(self, fault: dict, record: dict) -> str | None:
        """Why `record` does not resolve `fault`, judged from the files as they are now.

        Used before a resolution is written and every time one is read, in any
        process. Every identity, path and hash must agree exactly among the
        resolution, the fault, the report and its confirmation; nothing missing
        is inferred.
        """
        if record.get("fault_id") != fault["fault_id"]:
            return "it names a different fault"
        found = fault.get("report")
        if fault.get("kind") != PUBLICATION_KIND or not isinstance(found, dict):
            return None
        if found.get("state") == "ABSENT":
            return self._absent_resolution_mismatch(record, fault)
        return self._report_resolution_mismatch(record, found)

    def _report_resolution_mismatch(self, record: dict, found: dict) -> str | None:
        """The fault's own report, exactly as the fault recorded it, now confirmed."""
        if not all(is_sha256(value) for value in (
                record.get("content_sha256"), record.get("verified_file_sha256"),
                found.get("content_sha256"), found.get("file_sha256"))):
            return "a required hash is missing or malformed"
        if record.get("report_id") != found["report_id"] or \
                record.get("report_path") != found["report_path"] or \
                record.get("content_sha256") != found["content_sha256"] or \
                record.get("verified_file_sha256") != found["file_sha256"]:
            return "it names a different report, path or hash than its fault"
        path = self.root / found["report_path"]
        report = validate_report(path, read_record(path, REPORT_SCHEMA))
        if report["report_id"] != found["report_id"] or \
                report["integrity"]["content_sha256"] != found["content_sha256"] or \
                file_sha256(path) != record["verified_file_sha256"]:
            return "the report under the recorded name is not the recorded one"
        if not self._confirmation_valid(path, report):
            return "the report is not confirmed"
        return None

    def _absent_resolution_mismatch(self, record: dict, fault: dict) -> str | None:
        """A missing report's fault: the original still absent, an eligible replacement."""
        found = fault["report"]
        original, replacement = record.get("original_report"), record.get("replacement_report")
        if not isinstance(original, dict) or not isinstance(replacement, dict):
            return "it does not link the missing report to a replacement"
        if any(original.get(k) != found.get(k) for k in (
                "report_id", "session_date", "check", "report_path", "state")):
            return "it names a different missing report"
        if not self.still_absent(found):
            return "the missing report is no longer absent"
        if not (is_sha256(replacement.get("content_sha256"))
                and is_sha256(replacement.get("file_sha256"))):
            return "a required replacement hash is missing or malformed"
        path = self.root / str(replacement.get("report_path"))
        report = validate_report(path, read_record(path, REPORT_SCHEMA))
        if any(replacement.get(k) != report[k] for k in ("report_id", "check", "session_date")) \
                or replacement.get("report_path") != str(path.relative_to(self.root)) \
                or path != self.report_path(report) \
                or replacement["content_sha256"] != report["integrity"]["content_sha256"] \
                or replacement["file_sha256"] != file_sha256(path):
            return "the replacement report is not the recorded one"
        if record.get("resolved_by_invocation") != report["report_id"]:
            return "it was not written by the replacement's own invocation"
        why = replacement_problem(report, fault, record.get("resolved_at_utc"))
        if why:
            return why
        if not self._confirmation_valid(path, report):
            return "the replacement report is not confirmed"
        if report["report_id"] in self._publication_fault_reports():
            return "the replacement report has a publication fault of its own"
        return None

    def _publication_fault_reports(self) -> set[str]:
        """Every report id any publication fault names, resolved or not (records only)."""
        base = self.root / "faults"
        out = set()
        for path in sorted(base.glob("*-fault.json")) if base.is_dir() else []:
            fault = read_record(path, FAULT_SCHEMA)
            if fault.get("kind") == PUBLICATION_KIND and isinstance(fault.get("report"), dict):
                out.add(fault["report"].get("report_id"))
        return out

    def still_absent(self, found: dict) -> bool:
        """No report, and no confirmation, exists under the missing report's id."""
        path = self.root / found["report_path"]
        return not (os.path.lexists(path) or os.path.lexists(self.confirmation_path(path))
                    or self.report_file(found["session_date"], found["report_id"]))

    def faults(self) -> list[dict]:
        """Every recorded fault, oldest first, with its resolution if one checks out.

        A resolution file that cannot be read, names something else, or cannot
        be synced by this process leaves its fault open (`resolution_rejected`
        says why); a surviving file is not taken on trust.
        """
        base = self.root / "faults"
        out = []
        for path in sorted(base.glob("*-fault.json")) if base.is_dir() else []:
            fault = read_record(path, FAULT_SCHEMA)
            _typed(path, fault, {"fault_id": str, "reason_code": str, "signature": str,
                                 "detected_at_utc": str})
            closing = base / f"{fault['fault_id']}-resolution.json"
            resolution, rejected = (self._resolution(fault, closing)
                                    if os.path.lexists(closing) else (None, None))
            entry = {**fault, "resolution": resolution, "resolved": resolution is not None}
            if rejected:
                entry["resolution_rejected"] = rejected
            out.append(entry)
        return sorted(out, key=lambda f: (f["detected_at_utc"], f["fault_id"]))

    def readable_reports(self) -> tuple[list[dict], list[str]]:
        """Every stored report that passes validation, and the problems with the rest.

        For deciding which assessment is current when the ordinary accounting
        has failed. A report that does not validate is never used as evidence
        of anything; it is listed.
        """
        good, bad = [], []
        for path in self._paths("reports"):
            try:
                good.append(validate_report(path, read_record(path, REPORT_SCHEMA)))
            except CorruptRecord as exc:
                bad.append(str(exc))
        return good, bad

    def confirmed_readable_reports(self) -> tuple[list[dict], list[str]]:
        """As `readable_reports`, but only the confirmed ones count as evidence."""
        good, bad = self.readable_reports()
        blocked = self.open_publication_reports() if good else set()
        return [r for r in good
                if self.confirmed(self.report_path(r), r, blocked)], bad

    def startup_captures(self, run_id: str) -> StartupCaptures:
        """Captured start-up evidence, read and written on behalf of run `run_id`."""
        return StartupCaptures(self, run_id)

    # -- the state cache -------------------------------------------------------
    def write_state(self, state: dict, name: str = "state.json") -> None:
        """Replace a cache atomically. It is derived, so it is not evidence."""
        target = self._dir() / name
        scratch = target.with_name(f".tmp-state-{uuid.uuid4().hex}")
        scratch.write_text(json.dumps(state, indent=2, sort_keys=True, default=str) + "\n")
        os.replace(scratch, target)


STARTUP_CAPTURE_SCHEMA = "observer_startup_capture/1"
STARTUP_CAPTURE_MAX_FILE_BYTES = 256 * 1024
STARTUP_CAPTURE_MAX_RECORDS = 16
# The lock that serializes everything one decision reads or writes for a binding.
STARTUP_CAPTURE_LOCK = ".lock"
# Exactly the fields a capture has; a recomputed hash never completes a record.
STARTUP_CAPTURE_FIELDS = frozenset({
    "schema", "binding", "binding_sha256", "content_sha256", "startup_logged_at", "lines",
    "captured_at_utc", "captured_by_run", "observer_version", "integrity"})
STARTUP_BINDING_FIELDS = frozenset({
    "boot_id", "unit", "main_pid", "main_started", "main_started_monotonic_usec", "api_pid",
    "log_basis"})
STARTUP_LINE_FIELDS = frozenset({"bytes_base64", "source", "device", "inode", "offset",
                                 "length"})
# The first version that wrote captures; later versions up to this one are compatible.
STARTUP_CAPTURE_FIRST_VERSION = (0, 4, 12)
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VERSION_TEXT = re.compile(r"^(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")
_BOOT_TEXT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class CaptureContention(CorruptRecord):
    """Another run holds the binding's capture lock; this run decides nothing."""


def capture_key(binding: dict) -> str:
    """The SHA-256 naming one API process's binding (boot, unit, PIDs, start, log basis)."""
    text = json.dumps(binding, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()


def _version(text) -> tuple[int, int, int] | None:
    found = _VERSION_TEXT.match(text) if isinstance(text, str) else None
    return tuple(int(part) for part in found.groups()) if found else None


def provenance_problems(record: dict) -> list[str]:
    """Why a capture record is incomplete or malformed; empty if it is complete.

    Checked after its own content hash and before anything in it is used: a
    record resealed after losing a field still fails here.
    """
    problems = []
    if set(record) != STARTUP_CAPTURE_FIELDS:
        missing = sorted(STARTUP_CAPTURE_FIELDS - set(record))
        extra = sorted(set(record) - STARTUP_CAPTURE_FIELDS)
        problems.append(f"fields missing {missing} or unexpected {extra}")
    if instant(record.get("captured_at_utc")) is None:
        problems.append("captured_at_utc is not a timezone-aware time")
    if not (isinstance(record.get("captured_by_run"), str)
            and _RUN_ID.match(record["captured_by_run"])):
        problems.append("captured_by_run is not a run identifier")
    written, current = _version(record.get("observer_version")), _version(VERSION)
    if written is None or current is None or not (
            STARTUP_CAPTURE_FIRST_VERSION <= written <= current):
        problems.append("observer_version is not a version that writes captures")
    if instant(record.get("startup_logged_at")) is None:
        problems.append("startup_logged_at is not a timezone-aware time")
    binding = record.get("binding")
    if not isinstance(binding, dict) or set(binding) != STARTUP_BINDING_FIELDS:
        problems.append("the binding is incomplete")
    else:
        if not (isinstance(binding["boot_id"], str) and _BOOT_TEXT.match(binding["boot_id"])):
            problems.append("the binding's boot id is malformed")
        if not (isinstance(binding["unit"], str) and binding["unit"]):
            problems.append("the binding's unit is missing")
        for key in ("main_pid", "api_pid", "main_started_monotonic_usec"):
            if type(binding[key]) is not int or binding[key] <= 0:
                problems.append(f"the binding's {key} is not a positive integer")
        if binding.get("api_pid") != binding.get("main_pid"):
            problems.append("the binding's API process is not its main process")
        if instant(binding["main_started"]) is None:
            problems.append("the binding's start time is not timezone-aware")
        if not isinstance(binding["log_basis"], dict) or not binding["log_basis"]:
            problems.append("the binding's log basis is missing")
        elif record.get("binding_sha256") != capture_key(binding):
            problems.append("binding_sha256 is not the binding's hash")
    if not is_sha256(record.get("content_sha256")):
        problems.append("content_sha256 is malformed")
    lines = record.get("lines")
    if not isinstance(lines, list) or not lines or not all(
            isinstance(item, dict) and set(item) == STARTUP_LINE_FIELDS for item in lines):
        problems.append("the line records are incomplete")
    return problems


class _Held:
    """Directory descriptors from `/` to startup/<key>, each opened without a link."""

    def __init__(self, fds: list[int], identities: list[tuple[int, int]]):
        self.fds, self.identities = fds, identities

    @property
    def directory(self) -> int:
        return self.fds[-1]

    def sync(self) -> None:
        for fd in reversed(self.fds[-3:]):      # the binding, startup/, and the state root
            os.fsync(fd)

    def close(self) -> None:
        for fd in self.fds:
            os.close(fd)
        self.fds = []


class StartupCaptures:
    """Original start-up log evidence kept past the backend log's rotation (v0.4.12).

    One directory per API process binding, `startup/<capture_key(binding)>/`,
    and in it one file per distinct evidence, named by the SHA-256 of the
    captured bytes. The same evidence captured again names the same file, so
    `publish` adds nothing (idempotent); different evidence for the same
    process becomes a second file, which every read reports as a conflict.
    Nothing is overwritten, repaired or deleted.

    v0.4.13 hardening:

    - The state root is reached from `/` one component at a time without
      following any link. It must be owned by this user and not writable by
      group or others; so must `startup/` and each binding directory and
      record.
    - Every operation is relative to those held descriptors. Publication is
      a scratch file, fsync, `link()` that never overwrites, unlink of the
      scratch file, and fsync of the three directories. Before success is
      accepted, the path is walked again from `/` and every directory must
      be the same one.
    - Everything one decision reads or writes for a binding happens under
      `lock(binding)`: a non-blocking `flock` on `.lock` in the binding's
      directory. Contention is `CaptureContention`, never a wait. The lock
      is released on exit, including when the process dies.
    - A record must be complete (`provenance_problems`) as well as intact.

    The hashes give internal consistency only. They do not prove
    authenticity against anyone who can write this state.
    """

    def __init__(self, store: Store, run_id: str):
        self.store, self.run_id = store, run_id
        self._held: dict[str, _Held] = {}

    @staticmethod
    def _owned(fd: int, what: str) -> os.stat_result:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise CorruptRecord(f"{what} is not owned by the observer or is writable by others")
        return info

    def _walk(self, key: str, create: bool) -> _Held | None:
        """startup/<key> under the state root, every component opened without a link."""
        root = Path(self.store.root)
        if not root.is_absolute() or ".." in root.parts:
            raise CorruptRecord("the observer's state root is not an absolute path without '..'")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        fds = [os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)]
        identities = [(os.fstat(fds[0]).st_dev, os.fstat(fds[0]).st_ino)]
        try:
            parts = [*root.parts[1:], "startup", key]
            for depth, part in enumerate(parts):
                try:
                    fd = os.open(part, flags, dir_fd=fds[-1])
                except FileNotFoundError:
                    if not create:
                        for held in fds:
                            os.close(held)
                        return None
                    try:
                        os.mkdir(part, 0o750, dir_fd=fds[-1])
                    except FileExistsError:
                        pass
                    fd = os.open(part, flags, dir_fd=fds[-1])
                except OSError as exc:
                    raise CorruptRecord(f"the startup capture path cannot be opened at {part!r} "
                                        f"without following a link ({exc.strerror})") from exc
                fds.append(fd)
                identities.append((os.fstat(fd).st_dev, os.fstat(fd).st_ino))
                if depth >= len(parts) - 3:          # the state root and below
                    self._owned(fd, f"startup capture path component {part!r}")
            # Keep descriptors for the state root, startup/ and the binding only.
            for fd in fds[:-3]:
                os.close(fd)
            return _Held(fds[-3:], identities)
        except BaseException:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise

    def _verify(self, key: str, held: _Held) -> None:
        """CorruptRecord unless a fresh walk from `/` reaches the very same directories."""
        try:
            again = self._walk(key, create=False)
        except CorruptRecord as exc:
            raise CorruptRecord(f"the startup capture directories changed: {exc}") from exc
        if again is None:
            raise CorruptRecord("the startup capture directories changed: they are gone")
        try:
            if again.identities != held.identities:
                raise CorruptRecord("the startup capture directories were replaced")
        finally:
            again.close()

    @contextlib.contextmanager
    def lock(self, binding: dict):
        """Hold the binding's capture lock for one decision, or CaptureContention at once."""
        key = capture_key(binding)
        held = self._walk(key, create=True)
        try:
            try:
                fd = os.open(STARTUP_CAPTURE_LOCK,
                             os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640,
                             dir_fd=held.directory)
            except OSError as exc:
                raise CorruptRecord(f"the startup capture lock cannot be opened "
                                    f"({exc.strerror})") from exc
            try:
                if not stat.S_ISREG(self._owned(fd, "the startup capture lock").st_mode):
                    raise CorruptRecord("the startup capture lock is not a regular file")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise CaptureContention("another observer run holds the startup capture "
                                            "lock for this API process") from exc
                self._verify(key, held)
                self._held[key] = held
                try:
                    yield
                finally:
                    del self._held[key]
            finally:
                os.close(fd)
        finally:
            held.close()

    def _locked(self, binding: dict) -> tuple[str, _Held]:
        key = capture_key(binding)
        if key not in self._held:
            raise CorruptRecord("startup captures are read and written only under the "
                                "binding's lock")
        return key, self._held[key]

    def load(self, binding: dict) -> list[dict]:
        """Every valid record for `binding`, or CorruptRecord if any one is not valid."""
        key, held = self._locked(binding)
        self._verify(key, held)
        names = sorted(n for n in os.listdir(held.directory) if not n.startswith("."))
        if len(names) > STARTUP_CAPTURE_MAX_RECORDS:
            raise CorruptRecord(f"startup/{key[:12]} holds {len(names)} records")
        records = [self._read(held.directory, key, name) for name in names]
        held.sync()
        return records

    def contents(self, binding: dict) -> set[str]:
        """The content hashes stored for `binding` now, read afresh under the lock."""
        return {record["content_sha256"] for record in StartupCaptures.load(self, binding)}

    def _read(self, directory_fd: int, key: str, name: str) -> dict:
        where = f"startup/{key[:12]}/{name}"
        if not name.endswith(".json") or not is_sha256(name[:-5]):
            raise CorruptRecord(f"{where} is not a startup capture name")
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=directory_fd)
        except OSError as exc:
            raise CorruptRecord(f"{where} could not be opened ({exc.strerror})") from exc
        try:
            info = self._owned(fd, where)
            if not stat.S_ISREG(info.st_mode) or info.st_size > STARTUP_CAPTURE_MAX_FILE_BYTES:
                raise CorruptRecord(f"{where} is not a regular file of a capture's size")
            chunks, size = [], 0
            while chunk := os.read(fd, 65536):
                size += len(chunk)
                if size > STARTUP_CAPTURE_MAX_FILE_BYTES:
                    raise CorruptRecord(f"{where} is larger than a capture")
                chunks.append(chunk)
            os.fsync(fd)
        except OSError as exc:
            raise CorruptRecord(f"{where} could not be read ({type(exc).__name__})") from exc
        finally:
            os.close(fd)
        try:
            record = json.loads(b"".join(chunks))
        except ValueError as exc:
            raise CorruptRecord(f"{where} is not a complete record") from exc
        if not isinstance(record, dict) or record.get("schema") != STARTUP_CAPTURE_SCHEMA:
            raise CorruptRecord(f"{where} is not a {STARTUP_CAPTURE_SCHEMA} record")
        seal_ = record.get("integrity")
        try:
            intact = isinstance(seal_, dict) and seal_.get("content_sha256") == content_hash(record)
        except (TypeError, ValueError):
            intact = False
        if not intact:
            raise CorruptRecord(f"{where} does not match its own content hash")
        problems = provenance_problems(record)
        if problems:
            raise CorruptRecord(f"{where} is incomplete: " + "; ".join(problems))
        if record["binding_sha256"] != key or record["content_sha256"] != name[:-5]:
            raise CorruptRecord(f"{where} is filed under another binding or content hash")
        return record

    def publish(self, record: dict) -> bool:
        """Publish a capture under the lock; False if the same evidence is already stored."""
        key, held = self._locked(record["binding"])
        if record.get("binding_sha256") != key:
            raise CorruptRecord("a capture may be published only under its own binding")
        sealed = seal({**record, "captured_at_utc": datetime.now(UTC).isoformat(),
                       "captured_by_run": self.run_id, "observer_version": VERSION})
        problems = provenance_problems(sealed)
        if problems:
            raise CorruptRecord("a capture to be published is incomplete: " + "; ".join(problems))
        data = (json.dumps(sealed, indent=2, sort_keys=True) + "\n").encode()
        name, scratch = f"{record['content_sha256']}.json", f".tmp-{uuid.uuid4().hex}"
        directory = held.directory
        fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o440, dir_fd=directory)
        try:
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(scratch, name, src_dir_fd=directory, dst_dir_fd=directory,
                        follow_symlinks=False)
                created = True
            except FileExistsError:
                created = False
        finally:
            os.unlink(scratch, dir_fd=directory)
        try:
            held.sync()
        except OSError as exc:
            raise DurabilityUnconfirmed(
                exc.errno, f"{name} is linked, but its directories could not be synced "
                           f"({type(exc).__name__})") from exc
        self._verify(key, held)
        return created
