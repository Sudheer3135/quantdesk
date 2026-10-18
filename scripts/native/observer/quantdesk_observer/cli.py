"""The scheduled runs, the correction record, and the bounded-phase state.

Each run is short, holds one lock, publishes one new report and exits. It
reads the desk and writes only under its own report directory. It cannot
approve or acknowledge anything: operator records are read from a directory
the observer service has no permission to write.

Every invocation — an assessment, a reconstruction, or the five-minute
liveness run — also does the same local housekeeping under that lock: it
checks the bounded window's deadline, records the review checkpoint if one
is due, makes sure an unacknowledged checkpoint has its one stored
notification intent, and then makes at most one delivery round. Nothing
waits, loops or runs in the background between invocations.

The accounting (reports, their confirmation, qualification, the checkpoint,
the observer's own faults and the reconciliation of evidence) is never
bounded by notification work. All notification work of one invocation —
preparing alert intents, reading notification history for delivery, routing
attention, retries, requests, outcomes, the notification-state cache and the
handling of its own failures — shares one 30-second deadline, started by
whichever of it comes first. What it cannot do in time is left for a later
invocation to do from the stored records.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import signal
import sys
import threading
import time as time_module
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from . import VERSION, checks, healthchecks
from .checks import FAIL, IST, MISSING, PASS, RECORDED, UNKNOWN
from .probes import Probe, ProbeError, RunTimeout
from .store import (
    FAULT_SCHEMA,
    INTENT_SCHEMA,
    OUTCOME_SCHEMA,
    PUBLICATION_KIND,
    REPORT_SCHEMA,
    RESERVATION_SCHEMA,
    RESOLUTION_SCHEMA,
    CorruptRecord,
    DurabilityUnconfirmed,
    Overlap,
    PublicationIncomplete,
    Store,
    file_sha256,
    is_sha256,
    read_json,
    read_record,
    seal,
    validate_report,
)

HERE = Path(__file__).resolve().parent.parent
DEFAULTS = {
    "report_root": "/var/lib/quantdesk-observer",
    "operator_root": "/etc/quantdesk-observer/operator",
    "calendar_file": str(HERE / "session_calendar.json"),
    "quantdesk_repo": "/home/azureuser/quantdesk",
    "quantdesk_log": "/home/azureuser/quantdesk/logs/native/backend.log",
    "api_base": "http://127.0.0.1:8000",
    "api_process_match": "uvicorn app.main:app",
    "units": {"api": "quantdesk-api.service", "postgres": "quantdesk-postgres.service",
              "redis": "quantdesk-redis.service", "web": "quantdesk-web.service"},
    "upgrade_log": "/var/log/unattended-upgrades/unattended-upgrades.log",
    "disk_paths": ["/", "/var/lib/quantdesk-observer"],
    "disk_min_free_bytes": 2 * 1024 ** 3,
    "disk_min_free_fraction": 0.10,
    "https_deployed": False,
    "expected_option_chain_weight": 0.16,
    "bounded_phase_after": "2026-10-13",
    "bounded_phase_sessions": 5,
    "retry_seconds": 60,
    "activity_lookback_days": 7,
    # Astra's assessment times. A run may not start before `at`, and it is
    # timely only if it has finished by `deadline`.
    "schedule": {"premarket": {"at": "08:35", "deadline": "08:45"},
                 "midsession": {"at": "12:30", "deadline": "12:45"},
                 "afterclose": {"at": "16:00", "deadline": "16:30"}},
    # Astra-approved bound on the work of a run that starts after its deadline.
    # It bounds execution only; it never makes a late run timely.
    "late_run_seconds": 180,
    "baseline": {"positions_total": None, "journal_total": None},
    "healthchecks": {"enabled": False},
}
REQUIRED = ("expected_revision", "expected_parameter_hash", "expected_v2_config_hash",
            "expected_strategy_version", "expected_listeners")
COUNT_SCOPE, SUPERSEDE_SCOPE = "count_session_under_operational_exception", "supersede_report"
ACK_SCOPE = "acknowledge_checkpoint_attention"
APPROVAL_FIELDS = ("approval_id", "scope", "report_id", "report_sha256", "session_date",
                   "exception", "astra_decision_reference", "approval_text", "recorded_by",
                   "recorded_at_utc")
ACK_FIELDS = ("acknowledgment_id", "scope", "checkpoint_id", "checkpoint_sha256",
              "acknowledgment_text", "acknowledged_by", "acknowledged_at_utc")
MAINTENANCE_FIELDS = ("maintenance_id", "start_ist", "end_ist", "reason",
                      "authorization_reference", "recorded_by", "recorded_at_utc")
EARLY, FIVE, WINDOW = ("EARLY_REVIEW", "FIVE_SESSION_CHECKPOINT",
                       "BOUNDED_WINDOW_ENDED_WITH_UNRESOLVED_EVIDENCE")
SESSION_OUTCOMES = ("QUALIFIED", "INCOMPLETE", "OPERATIONAL EXCEPTION REQUIRED")
# Astra's delivery policy. Not configurable.
INITIAL_ROUNDS, INITIAL_GAP, LATER_GAP = 6, timedelta(minutes=5), timedelta(hours=1)
NOTIFY_BUDGET_SECONDS, MAX_REQUESTS = 30.0, 4
DELIVERED, DUE, WAITING = "delivered", "undelivered: retry due", "undelivered: waiting"
UNRESOLVED = "undelivered: history unresolved; not sent again automatically"
FAULT_REASON = "observer_housekeeping_failure"
# A report that could not be published, or whose publication could not be made durable.
PUBLICATION_REASON = "observer_report_publication_failure"
CONFIRMED, ABSENT, PRESENT_UNCONFIRMED = "CONFIRMED", "ABSENT", "PRESENT_UNCONFIRMED"


class StorageUnavailable(Exception):
    """A record the observer must write before acting could not be written.

    `attempt`, when a request may already have been made, is what is known of
    it: completion unknown, remote receipt unknown.
    """

    def __init__(self, message: str, attempt: dict | None = None):
        super().__init__(message)
        self.attempt = attempt


class NotificationExpired(RunTimeout):
    """The invocation's one notification deadline has passed: no more notification work.

    A BaseException (through RunTimeout), so no generic `except Exception`
    can swallow it and carry on. It travels through every enclosing piece of
    notification work to the outermost one, which stops there. `attempt` is
    what is known of a request it cut short, if it cut one short.
    """

    attempt: dict | None = None


class TimerConflict(RuntimeError):
    """A time limit could not be installed safely, so it was not installed at all.

    Another interval timer was already running (it is left exactly as it was:
    its handler, its remaining time and its deadline), or the caller is not on
    the main thread, where alone the timer can interrupt work.
    """


class DeadlineExhausted(Exception):
    """The lock was taken, but no time was left to start the assessment."""

    def __init__(self, at: datetime):
        super().__init__(f"no time left at {at.isoformat()}")
        self.at = at
MID_FACTS = ("observations due", "observation linkage", "producer revision",
             "configuration identity", "runtime identity", "absence of a duplicate runtime",
             "PostgreSQL responsiveness", "Redis responsiveness", "scheduler/job health",
             "Angel feed readiness", "option-stream readiness")


def load_config(path: str) -> dict:
    with open(path) as handle:
        given = json.load(handle)
    cfg = {**DEFAULTS, **given}
    missing = [key for key in REQUIRED if key not in cfg]
    if missing:
        raise SystemExit(f"observer configuration lacks: {', '.join(missing)}")
    return cfg


def no_timer_running(what: str) -> None:
    """Refuse, before changing anything, to put a timer over one that is already running.

    The observer does not compose timers: an assessment and its notification
    work run one after the other, each with its own. A timer already running
    here is someone else's, and it is not cancelled, shortened or extended.
    """
    if threading.current_thread() is not threading.main_thread():
        raise TimerConflict(f"{what} can be enforced only on the main thread")
    if signal.getitimer(signal.ITIMER_REAL)[0] > 0:
        raise TimerConflict(f"{what} was not installed: another interval timer is already "
                            "running, and it is left as it is")


@contextlib.contextmanager
def time_limit(seconds: float, enabled: bool):
    """End the run with RunTimeout after `seconds`. Always at least one second."""
    if not enabled:
        yield
        return
    no_timer_running("the assessment's time limit")

    def expire(signum, frame):
        raise RunTimeout(f"the run exceeded its {seconds:.0f}s limit")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.alarm(max(1, int(seconds)))
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@contextlib.contextmanager
def hard_limit(seconds: float, enabled: bool):
    """End the enclosed work with RunTimeout once `seconds` of wall clock have passed.

    For notification work: it covers writing records as well as the request.
    A record interrupted mid-write is never seen half-written (publication is
    a hard link of a finished file), and the interrupted call returns no
    claim. A system call the kernel will not interrupt (a stalled disk, for
    instance) ends only when it returns, so this bound can be exceeded then.
    """
    if not enabled:
        yield
        return
    no_timer_running("the notification deadline")

    def expire(signum, frame):
        raise NotificationExpired(
            f"the {NOTIFY_BUDGET_SECONDS:.0f}-second notification budget expired")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, max(seconds, 0.001))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def clock_at(day: date, text: str) -> datetime:
    return checks.at(day, time.fromisoformat(text))


def stamp(report: dict, key: str, moment: datetime) -> None:
    report[f"{key}_at_utc"] = moment.astimezone(UTC).isoformat()
    report[f"{key}_at_ist"] = moment.astimezone(IST).isoformat()


def filled(record: dict, fields) -> list[str]:
    """The names in `fields` that are missing or empty in `record`."""
    return [k for k in fields if not isinstance(record.get(k), str) or not record[k].strip()]


# ---------------------------------------------------------------------------
# The session calendar: a date is known only once it has been reviewed
# ---------------------------------------------------------------------------
def load_calendar(cfg: dict) -> dict:
    try:
        calendar = json.loads(Path(cfg["calendar_file"]).read_text())
        if not isinstance(calendar["dates"], dict):
            raise TypeError
        return {"dates": calendar["dates"], "source": calendar.get("source")}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"dates": {}, "source": None, "error": f"calendar unusable: {type(exc).__name__}"}


def calendar_status(calendar: dict, day: date) -> dict:
    """NORMAL, CLOSED or SPECIAL for a reviewed date; otherwise UNKNOWN.

    A date is reviewed only when its own entry says so and names a reviewer,
    a review time and the official exchange reference. A listing that has
    not been reviewed is shown as provisional and decides nothing.
    """
    entry = calendar["dates"].get(day.isoformat())
    entry = entry if isinstance(entry, dict) else {}
    listed = entry.get("session_type")
    reviewed = (entry.get("review_status") == "REVIEWED"
                and not filled(entry, ("reviewer", "reviewed_at", "official_reference"))
                and listed in ("NORMAL", "CLOSED", "SPECIAL"))
    why = None
    if not reviewed:
        why = (calendar.get("error") or ("the date is not in the session calendar" if not entry
                                         else "the date has not been reviewed against an "
                                              "official exchange source"))
    return {"status": listed if reviewed else UNKNOWN, "provisional_listing": listed,
            "reviewed": reviewed, "hours": entry.get("hours"), "segment": entry.get("segment"),
            "official_reference": entry.get("official_reference"),
            "superseding_notice": entry.get("superseding_notice"),
            "reviewer": entry.get("reviewer"), "reviewed_at": entry.get("reviewed_at"),
            "note": entry.get("note"), "unknown_because": why}


def window(calendar: dict, cfg: dict) -> dict:
    """The bounded phase's five reviewed normal session dates, or why they are unknown.

    Every date from the day after the phase began up to the fifth normal
    session must itself be reviewed; one unreviewed date in between could be
    a session, so the fifth cannot be named past it.
    """
    need, found = cfg["bounded_phase_sessions"], []
    day = date.fromisoformat(cfg["bounded_phase_after"])
    for _ in range(120):
        day += timedelta(days=1)
        listing = calendar_status(calendar, day)
        if listing["status"] == UNKNOWN:
            return {"status": UNKNOWN, "sessions_found": found, "unresolved_date": day.isoformat(),
                    "reason": f"{day.isoformat()}: {listing['unknown_because']}"}
        if listing["status"] == "NORMAL":
            found.append(day.isoformat())
            if len(found) == need:
                due = clock_at(day, cfg["schedule"]["afterclose"]["deadline"])
                return {"status": "ESTABLISHED", "sessions": found, "due_at_ist": due.isoformat()}
    return {"status": UNKNOWN, "sessions_found": found, "unresolved_date": None,
            "reason": "the calendar does not reach a fifth reviewed normal session"}


# ---------------------------------------------------------------------------
# Operator records: read, validated, never written here
# ---------------------------------------------------------------------------
def read_approvals(store: Store) -> tuple[list[dict], list[dict]]:
    """Approvals that bind to an exact stored report, and those that do not.

    An approval counts only if it names a report that exists, carries that
    report file's SHA-256, matches its session date, and has the exact scope
    it is used for. A typed name proves nothing; the binding to the report
    content and the directory's permissions are what the observer checks.
    """
    valid, rejected = [], []
    for path in store.operator_paths("approvals"):
        try:
            approval = read_json(path)
            if approval["schema"] != "observer_exception_approval/1":
                raise ValueError("not an approval record")
            empty = filled(approval, APPROVAL_FIELDS)
            if empty:
                raise ValueError(f"fields missing or empty: {', '.join(empty)}")
            if approval["scope"] not in (COUNT_SCOPE, SUPERSEDE_SCOPE):
                raise ValueError(f"unknown scope {approval['scope']!r}")
            target = store.report_file(approval["session_date"], approval["report_id"])
            if target is None:
                raise ValueError("names a report that does not exist for that session date")
            if file_sha256(target) != approval["report_sha256"]:
                raise ValueError("the report hash does not match the stored report")
            report = read_json(target)
            if report.get("check") != "afterclose":
                raise ValueError("names a report that is not an after-close report")
            if approval["scope"] == COUNT_SCOPE and \
                    report.get("outcome") != "OPERATIONAL EXCEPTION REQUIRED":
                raise ValueError("the report's outcome is not OPERATIONAL EXCEPTION REQUIRED")
            if approval["scope"] == SUPERSEDE_SCOPE:
                other = store.report_file(approval["session_date"],
                                          str(approval.get("superseding_report_id")))
                if other is None or file_sha256(other) != approval.get(
                        "superseding_report_sha256"):
                    raise ValueError("the superseding report is missing or its hash differs")
            valid.append(approval)
        except (CorruptRecord, ValueError) as exc:
            rejected.append({"file": path.name, "reason": str(exc)})
    return valid, rejected


def read_acknowledgments(store: Store, checkpoint: dict | None) -> tuple[list[dict], list[dict]]:
    """Operator acknowledgments of the one checkpoint, bound to its exact content.

    An acknowledgment only records that the owner has seen the checkpoint. It
    is a different record, in a different directory, with a different scope
    from an exception approval, and the accounting never reads it.
    """
    valid, rejected = [], []
    for path in store.operator_paths("acknowledgments"):
        try:
            ack = read_json(path)
            if ack["schema"] != "observer_checkpoint_acknowledgment/1":
                raise ValueError("not an acknowledgment record")
            empty = filled(ack, ACK_FIELDS)
            if empty:
                raise ValueError(f"fields missing or empty: {', '.join(empty)}")
            if ack["scope"] != ACK_SCOPE:
                raise ValueError(f"unknown scope {ack['scope']!r}")
            if checkpoint is None or ack["checkpoint_id"] != checkpoint["checkpoint_id"]:
                raise ValueError("names a checkpoint that does not exist")
            if ack["checkpoint_sha256"] != file_sha256(store.checkpoint_path):
                raise ValueError("the checkpoint hash does not match the stored checkpoint")
            valid.append(ack)
        except (CorruptRecord, ValueError) as exc:
            rejected.append({"file": path.name, "reason": str(exc)})
    return valid, rejected


def active_maintenance(store: Store, now: datetime) -> list[dict]:
    """Operator maintenance records covering `now`. Information only.

    A record explains planned inactivity. It changes no result, silences no
    check and pauses nothing; pausing a hosted check is a separate, manual,
    separately authorized action.
    """
    out = []
    for path in store.operator_paths("maintenance"):
        try:
            record = read_json(path)
            if record["schema"] != "observer_maintenance/1" or filled(record, MAINTENANCE_FIELDS) \
                    or not isinstance(record.get("affected_checks"), list):
                raise ValueError("incomplete maintenance record")
            if checks.ist(record["start_ist"]) <= now <= checks.ist(record["end_ist"]):
                out.append({k: record[k] for k in (*MAINTENANCE_FIELDS, "affected_checks")})
        except (CorruptRecord, ValueError) as exc:
            out.append({"file": path.name, "unusable": str(exc)})
    return out


# ---------------------------------------------------------------------------
# Accounting and the bounded phase
# ---------------------------------------------------------------------------
def accounting(store: Store, pending: dict | None = None) -> tuple[dict, list[dict]]:
    """Which after-close report stands for each session, and whether it counts.

    The first report for a session stands. A later report for the same date
    is kept and listed, but changes nothing unless an operator approval
    explicitly supersedes the first. An OPERATIONAL EXCEPTION REQUIRED
    session counts only with a valid approval bound to that exact report.
    `pending` is a report about to be published, so that it can describe the
    phase it brings about.
    """
    reports = store.reports(check="afterclose") + ([pending] if pending else [])
    reports = [r for r in reports if r.get("outcome") in SESSION_OUTCOMES]
    blocked = unconfirmed_reports(store)
    valid, rejected = read_approvals(store)
    by_id = {r["report_id"]: r for r in reports}
    superseded = {a["report_id"]: a["superseding_report_id"] for a in valid
                  if a["scope"] == SUPERSEDE_SCOPE and a["superseding_report_id"] in by_id}
    approved = {a["report_id"]: a["approval_id"] for a in valid if a["scope"] == COUNT_SCOPE}
    sessions: dict[str, dict] = {}
    for report in sorted(reports, key=lambda r: r["completed_at_utc"]):
        entry = sessions.setdefault(report["session_date"], {"standing": report, "later": []})
        if entry["standing"] is not report:
            entry["later"].append(report["report_id"])
    out = {}
    for day, entry in sorted(sessions.items()):
        report, superseding = entry["standing"], None
        if report["report_id"] in superseded:
            superseding = report["report_id"]
            report = by_id[superseded[report["report_id"]]]
        approval = approved.get(report["report_id"])
        # A report whose publication is not confirmed durable establishes nothing
        # until a later invocation has verified it: readable is not enough.
        unconfirmed = blocked.get(report["report_id"])
        out[day] = {
            "report_id": report["report_id"], "outcome": report["outcome"],
            "collection_gates_passed": report["collection_gates_passed"],
            "valid_early_review_candidates": report["valid_early_review_candidates"],
            "qualifies": unconfirmed is None and (report["outcome"] == "QUALIFIED" or (
                report["outcome"] == "OPERATIONAL EXCEPTION REQUIRED" and approval is not None)),
            "exception_approval": approval, "supersedes_report": superseding,
            "other_reports_for_this_date": [r for r in entry["later"]
                                            if r != report["report_id"]],
            "baseline": None if unconfirmed else report.get("baseline")}
        if unconfirmed:
            out[day]["publication_unconfirmed"] = unconfirmed
    return out, rejected


def unconfirmed_reports(store: Store) -> dict[str, str]:
    """Report id -> open publication fault, for every report not yet confirmed durable."""
    return {f["report"]["report_id"]: f["fault_id"] for f in store.faults()
            if not f["resolved"] and f.get("kind") == PUBLICATION_KIND
            and isinstance(f.get("report"), dict)}


def phase_state(store: Store, cfg: dict, calendar: dict, now: datetime,
                pending: dict | None = None) -> dict:
    """The bounded observation phase, rebuilt from the preserved records.

    Review falls due at whichever comes first: a verified natural expiry_day
    pass in a complete session, five qualifying sessions, or 16:30 IST on the
    fifth reviewed normal session after the phase began — whatever reports
    exist by then. REVIEW PENDING is final once its checkpoint record exists.
    """
    sessions, rejected = accounting(store, pending)
    after, need = cfg["bounded_phase_after"], cfg["bounded_phase_sessions"]
    span = window(calendar, cfg)
    known = span["status"] == "ESTABLISHED"
    last = span["sessions"][-1] if known else None
    qualifying, reasons, trigger_session = [], [], None
    for day, entry in sessions.items():
        if day <= after or (known and day > last):
            continue
        if entry["qualifies"]:
            qualifying.append(day)
        if reasons:
            continue
        if entry["collection_gates_passed"] and entry["valid_early_review_candidates"] \
                and not entry.get("publication_unconfirmed"):
            reasons.append(EARLY)
        if len(qualifying) >= need:
            reasons.append(FIVE)
        if reasons:
            trigger_session = day
    details = None
    if not reasons and known and now >= checks.ist(span["due_at_ist"]):
        reasons, trigger_session = [WINDOW], last
        reported = {r["session_date"]: r.get("outcome")
                    for r in store.reports(check="afterclose") + ([pending] if pending else [])
                    if r["ran_checks"]}
        details = {
            "missing_reports": [d for d in span["sessions"] if d not in reported],
            "sessions_with_unknown_outcome": [d for d in span["sessions"]
                                              if reported.get(d) == UNKNOWN],
            "unresolved_session_qualifications": {
                d: sessions[d]["outcome"] for d in span["sessions"]
                if d in sessions and not sessions[d]["qualifies"]},
            "established_qualifying_sessions": len(qualifying),
            "checkpoint_due_at_ist": span["due_at_ist"],
            "note": "missing and UNKNOWN sessions are not counted, no report is invented "
                    "for them, and the window is not extended"}
    checkpoint = store.checkpoint()
    acks, rejected_acks = read_acknowledgments(store, checkpoint)
    pending_now = bool(checkpoint or reasons)
    state = {
        "schema": "observer_phase/3",
        "phase": "REVIEW PENDING" if pending_now else "BOUNDED OBSERVATION",
        "reasons": checkpoint["reasons"] if checkpoint else reasons,
        "trigger_session": checkpoint["trigger_session"] if checkpoint else trigger_session,
        "window_details": (checkpoint.get("window_details") if checkpoint else details),
        "bounded_phase_after": after, "sessions_required": need,
        "window": span, "qualifying_sessions": qualifying,
        "calendar_uncertainty": None if known else span["reason"],
        "checkpoint": checkpoint, "computed_reasons": reasons,
        "computed_trigger_session": trigger_session, "computed_window_details": details,
        "attention_required": bool(pending_now and not acks),
        "acknowledged_by": [a["acknowledgment_id"] for a in acks],
        "sessions": {d: {k: v for k, v in e.items() if k != "baseline"}
                     for d, e in sessions.items() if d > after},
        "rejected_approvals": rejected, "rejected_acknowledgments": rejected_acks}
    if checkpoint and (checkpoint["reasons"] != reasons
                       or checkpoint["trigger_session"] != trigger_session):
        state["note"] = ("the records no longer reproduce what the checkpoint recorded; the "
                         "checkpoint stands until Astra reviews the change")
    return state


def checkpoint_identity(cfg_after: str, reasons: list[str], trigger_session: str | None) -> str:
    """The same id whoever records the checkpoint, and whenever."""
    text = json.dumps([cfg_after, reasons, trigger_session])
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def settle_checkpoint(store: Store, state: dict, now: datetime,
                      report_id: str | None) -> dict | None:
    """Record REVIEW PENDING once. Returns the new checkpoint if this call recorded it."""
    if state["checkpoint"] or not state["computed_reasons"]:
        return None
    span = state["window"]
    record = {
        "schema": "observer_checkpoint/2",
        "checkpoint_id": checkpoint_identity(state["bounded_phase_after"],
                                             state["computed_reasons"],
                                             state["computed_trigger_session"]),
        "phase": "REVIEW PENDING", "reasons": state["computed_reasons"],
        "trigger_session": state["computed_trigger_session"],
        "window_details": state["computed_window_details"],
        "window": span, "qualifying_sessions": state["qualifying_sessions"],
        "sessions": state["sessions"],
        # When the window was due to end, and when this was actually noticed.
        "window_deadline_at_ist": span.get("due_at_ist"),
        "detected_at_utc": now.astimezone(UTC).isoformat(),
        "detected_at_ist": now.astimezone(IST).isoformat(),
        "recorded_by_report_id": report_id, "observer_version": VERSION,
        "finality": "this record is never replaced; acknowledging it does not reopen the "
                    "phase, and a change to the evidence behind it needs a recorded Astra "
                    "review"}
    return store.checkpoint() if store.write_checkpoint(record) else None


def trusted_baseline(store: Store, cfg: dict, before: str) -> dict | None:
    """Paper-activity counts from the last session whose reconciliation passed."""
    sessions, _ = accounting(store)
    for day in sorted(sessions, reverse=True):
        if day < before and sessions[day]["baseline"]:
            return {**sessions[day]["baseline"], "from_session": day}
    given = cfg["baseline"]
    if given.get("positions_total") is None or given.get("journal_total") is None:
        return None
    return {**given, "from_session": "configuration"}


# ---------------------------------------------------------------------------
# Events and pings
# ---------------------------------------------------------------------------
def raise_event(store: Store, session_date: str, check: str, report_id: str | None,
                moment: datetime, kind: str, subject: str, summary: str) -> None:
    store.write_event({
        "schema": "observer_event/1", "type": kind, "subject": subject,
        "session_date": session_date, "check": check, "report_id": report_id,
        "created_at_utc": moment.astimezone(UTC).isoformat(),
        "observer_version": VERSION, "summary": healthchecks.redact(summary)[:240]})


def emit(store: Store, report: dict, kind: str, subject: str, summary: str) -> None:
    raise_event(store, report["session_date"], report["check"], report["report_id"],
                datetime.fromisoformat(report["completed_at_utc"]), kind, subject, summary)


def check_status(store: Store, day: str, check: str) -> str:
    """PASS, FAIL or MISSING for an earlier assessment of the same session.

    PASS needs a run that started in its window, finished by its deadline
    and passed. Nothing observed later turns FAIL or MISSING into PASS.
    """
    blocked = unconfirmed_reports(store)
    found = [r for r in store.reports(day, check)
             if r["ran_checks"] and r["started_in_window"] and r["report_id"] not in blocked]
    if any(r["result"] == PASS and r["timely"] for r in found):
        return PASS
    if check == "premarket":
        return FAIL if found else MISSING
    return FAIL if any(r["timely"] for r in found) else MISSING


def queue(store: Store, now: datetime, event_type: str, check: str, kind: str, body: str,
          report: dict | None = None, checkpoint_id: str | None = None, *,
          notification_id: str | None = None, attention_only: bool = False,
          fault_ids: list[str] | None = None) -> dict:
    """Persist the intent to notify, before anything is sent.

    With a `notification_id` the intent is one logical record: a second
    call, from this process or another, finds it stored and adds nothing.
    """
    intent = {
        "schema": INTENT_SCHEMA, "notification_id": notification_id or store.new_id(),
        "transport": "healthchecks.io", "event_type": event_type, "check": check, "kind": kind,
        "body": healthchecks.redact(body)[:healthchecks.MAX_BODY],
        "created_at_utc": now.astimezone(UTC).isoformat(), "checkpoint_id": checkpoint_id,
        # True when nothing but the unacknowledged checkpoint made this a failure.
        "attention_only": attention_only,
        # The observer faults (not the checkpoint) this intent speaks for or was failed by.
        "fault_ids": fault_ids or [],
        "report_id": report["report_id"] if report else None,
        "session_date": report["session_date"] if report else now.date().isoformat()}
    for key in ("scheduled_at_ist", "deadline_at_ist", "started_at_utc", "completed_at_utc"):
        intent[key] = report[key] if report else None
    intent["newly_stored"] = store.write_intent(intent)
    return intent


def ensure_attention_intent(store: Store, checkpoint: dict, now: datetime) -> bool:
    """The one stored `/fail` intent of an unacknowledged checkpoint. True if this call made it.

    Its id is the checkpoint's, and it is published with the same
    create-or-leave link as every record, so repeating this — or two runs
    doing it at once — leaves exactly one.
    """
    body = (f"check=afterclose attention=YES reason={','.join(checkpoint['reasons'])} "
            f"checkpoint={checkpoint['checkpoint_id']} observer={VERSION}")
    # An intent written by an earlier observer version for this same checkpoint is the
    # same alert, whatever its schema or file name. It is used as it is, not doubled.
    stored, _ = store.notifications(tolerant=True)
    if any(n["event_type"] == "review_attention"
           and n["checkpoint_id"] == checkpoint["checkpoint_id"] for n in stored):
        return False
    return queue(store, now, "review_attention", "afterclose", "fail", body, None,
                 checkpoint["checkpoint_id"],
                 notification_id=f"attention-{checkpoint['checkpoint_id']}")["newly_stored"]


def resolve_faults(store: Store, now: datetime, check: str, kind: str,
                   invocation: str | None) -> None:
    """Resolve the open faults of one kind after the operation that failed has succeeded.

    Only faults of `kind` are resolved, and never by the invocation that
    opened them: a fault needs a later invocation to complete the very
    operation that failed. Nothing is sent on a fault's resolution.
    """
    for known in store.faults():
        if known["resolved"] or known["signature"].split(":", 1)[0] != kind \
                or known.get("detected_by_invocation") == invocation:
            continue
        if store.write_fault_resolution({
                "schema": RESOLUTION_SCHEMA, "fault_id": known["fault_id"],
                "resolved_at_utc": now.astimezone(UTC).isoformat(), "resolved_by_check": check,
                "resolved_by_invocation": invocation, "observer_version": VERSION,
                "basis": f"a later invocation completed the {kind} operation that had failed, "
                         "with the reconciliation that goes with it",
                "effect": "retries for this fault stop; no success is sent on its account"}):
            raise_event(store, now.date().isoformat(), check, None, now,
                        "observer_fault_resolved", f"fault-{known['fault_id']}",
                        f"observer fault {known['fault_id']} ({known['reason_code']}) resolved")


def fault_intent(store: Store, now: datetime, fault: dict) -> dict:
    """A fault's one `/fail` intent on the after-close identity, stored if not already there.

    The ping names the reason code and the fault id. Nothing else: no path,
    no diagnostic text, no record content.
    """
    return queue(store, now, "observer_fault", "afterclose", "fail",
                 f"check=afterclose attention=YES reason={fault['reason_code']} "
                 f"fault={fault['fault_id']} observer={VERSION}",
                 notification_id=f"fault-{fault['fault_id']}", fault_ids=[fault["fault_id"]])


def prepare_notifications(store: Store, state: dict, now: datetime) -> None:
    """The notification side of housekeeping: the checkpoint's intent, and the history read.

    An unacknowledged checkpoint gets its notification intent, whichever run
    recorded the checkpoint and whatever happened to that run afterwards; and
    the notification history is read strictly, so that damage to it is a fault.
    """
    if state["checkpoint"] and state["attention_required"]:
        ensure_attention_intent(store, state["checkpoint"], now)
    store.notifications()          # strict: damaged notification history is a fault


def ensure_deferred_intents(store: Store, cfg: dict, now: datetime) -> None:
    """Intents that earlier notification work was due to store but did not, made from the records.

    Every open observer fault has its one alert intent. The newest scheduled
    assessment of each check that was to be notified (`notification_planned`)
    and did not pass has its intent; a pass is not, because a success is never
    sent late, and attention alone is carried by the checkpoint's and the
    faults' own intents. Each is stored under its one id with create-or-leave,
    so nothing is doubled; nothing is made for anything the records do not show.
    """
    stored, _ = store.notifications(tolerant=True)
    ids = {n["notification_id"] for n in stored}
    for fault in store.faults():
        if not fault["resolved"] and f"fault-{fault['fault_id']}" not in ids:
            fault_intent(store, now, fault)
    if not (cfg.get("healthchecks") or {}).get("enabled"):
        return
    noted = {n.get("report_id") for n in stored if n["event_type"] == "assessment_result"}
    newest: dict[str, dict] = {}
    for report in store.reports():
        if legitimate_assessment(report) and (
                report["check"] not in newest
                or report["completed_at_utc"] > newest[report["check"]]["completed_at_utc"]):
            newest[report["check"]] = report
    for report in newest.values():
        passed = report["result"] == "NO SESSION" or (report["result"] == PASS
                                                      and report["timely"])
        if report.get("notification_planned") is True and not passed \
                and report["report_id"] not in noted:
            notify(store, report, now)


def prepare(store: Store, out: Outbound | None, now: datetime, check: str,
            invocation: str | None, faults: list[str], during: str, work) -> str:
    """Notification preparation: "prepared", "expired" or "failed". Never raises an ordinary error.

    A failure is classified by the operation that failed: whatever `work`
    raises (a damaged notification record, an I/O error, anything else) is a
    failure of notification preparation, not of the accounting. It is
    recorded as a `notification-preparation` fault by `notification_failure`,
    inside what is left of the same deadline, and appears in `faults` (so the
    exit is non-zero). "expired" means the deadline stopped it. Neither says
    anything about an assessment. The worst result of the invocation is kept
    on `out.preparation`.
    """
    try:
        result = "prepared" if notification_work(out, during, work) else "expired"
    except Exception as exc:
        notification_failure(store, out, now, check, exc, faults, invocation,
                             kind="notification-preparation")
        result = "failed"
    worsen_preparation(out, result)
    return result


# "incomplete": a housekeeping pass stopped before its preparation stages were all done.
PREPARATION_RANK = {None: 0, "prepared": 1, "incomplete": 2, "expired": 3, "failed": 4}


def worsen_preparation(out: Outbound | None, result: str) -> None:
    """Record a preparation result on the invocation; the worst one stands, never improved."""
    if out is not None and PREPARATION_RANK[result] > PREPARATION_RANK[out.preparation]:
        out.preparation = result


def preparation_recovered(store: Store, out: Outbound, now: datetime, check: str,
                          invocation: str | None, reconciled: bool) -> None:
    """Resolve earlier invocations' notification-preparation faults, if this one has earned it.

    Called as notification work, once, after the invocation's last
    notification preparation (housekeeping can run twice: again after
    publication is reconciled). Only when every preparation stage of this
    invocation succeeded — none failed, none expired, no housekeeping pass
    stopped part-way, at least one ran — and the accounting was reconciled.
    `out.preparation` keeps the worst result of the invocation and is never
    improved by a later pass. A report-publication fault resolving says
    nothing about this.
    """
    if reconciled and out.preparation == "prepared":
        resolve_faults(store, now, check, "notification-preparation", invocation)


def housekeeping(store: Store, cfg: dict, calendar: dict, now: datetime, check: str,
                 report_id: str | None = None, invocation: str | None = None,
                 out: Outbound | None = None, faults: list[str] | None = None) -> dict:
    """What every invocation settles locally, under the caller's lock.

    From the reviewed calendar, the stored observer records and `now` alone —
    no trading data, no network — this: ends the bounded window if its
    deadline has passed or another review condition is met, recording the
    checkpoint once, and raises that checkpoint's events. That is accounting,
    and is done whatever the notification deadline.

    Then, as notification work of `out`'s invocation (its deadline starts here
    if nothing has started it yet): the checkpoint's intent, the strict read of
    the notification history, and the intents earlier work was due to store.
    If the deadline stops that, it is left for a later invocation, and no
    earlier housekeeping fault is called resolved by this one. If it fails —
    a damaged notification record, say — that is a notification-preparation
    fault (see `prepare`), and the accounting still goes on: the state is
    persisted and the caller reconciles publication as usual. Only a failure
    of the accounting itself is raised to the caller, as before.
    """
    faults = [] if faults is None else faults
    try:
        return _housekeeping(store, cfg, calendar, now, check, report_id, invocation, out,
                             faults)
    except BaseException:
        # A pass that did not finish did not finish its preparation either, whatever
        # stopped it: this invocation can no longer count its preparation as complete.
        worsen_preparation(out, "incomplete")
        raise


def _housekeeping(store: Store, cfg: dict, calendar: dict, now: datetime, check: str,
                  report_id: str | None, invocation: str | None, out: Outbound | None,
                  faults: list[str]) -> dict:
    state = phase_state(store, cfg, calendar, now)
    if settle_checkpoint(store, state, now, report_id):
        state = phase_state(store, cfg, calendar, now)
    checkpoint = state["checkpoint"]
    if checkpoint:
        names = {EARLY: "early_review_trigger", FIVE: "five_session_checkpoint",
                 WINDOW: "bounded_window_ended"}
        for reason in checkpoint["reasons"]:
            raise_event(store, checkpoint["trigger_session"] or now.date().isoformat(), check,
                        checkpoint.get("recorded_by_report_id"), now, names[reason],
                        f"checkpoint-{checkpoint['checkpoint_id']}",
                        f"REVIEW PENDING ({reason}) after {checkpoint['trigger_session']}; "
                        "owner attention is required until the checkpoint is acknowledged")
    prepared = prepare(store, out, now, check, invocation, faults,
                       "the notification preparation",
                       lambda: prepare_notifications(store, state, now))
    store.write_state(state)
    if prepared == "prepared":
        # Everything above read, reconciled and persisted. Only now may an earlier
        # housekeeping fault be called resolved.
        resolve_faults(store, now, check, "housekeeping", invocation)

        # An earlier invocation's preparation fault is not resolved here: this
        # housekeeping may run again in the same invocation (after publication is
        # reconciled), and that pass may fail. See `preparation_recovered`.
        prepare(store, out, now, check, invocation, faults, "the deferred alert intents",
                lambda: ensure_deferred_intents(store, cfg, now))
    return state


def open_fault(store: Store, now: datetime, check: str, kind: str, exc: BaseException,
               faults: list[str], invocation: str | None = None, *,
               signature: str | None = None, detail: dict | None = None,
               reason: str = FAULT_REASON, out: Outbound | None = None) -> bool:
    """Record a failure of the observer's own work, and queue owner attention for it.

    Three things are written, in this order and each on its own: the local
    event, an independent fault record, and a `/fail` intent on the
    after-close identity. None of them needs the ordinary accounting to have
    worked, so a corrupt stored report cannot prevent them. The fault says
    nothing about any session's data and changes no report or qualification.
    One open fault per kind of failure: a failure that repeats every five
    minutes is one fault, not a new one each time.

    The intent is notification work of `out`'s invocation: past its deadline
    it is not written now, and a later invocation writes it from the fault
    record (`ensure_deferred_intents`). A failure of notification work itself
    is recorded by `notification_failure`, wholly inside that deadline.

    If even these cannot be written, that is said in `faults`, the caller
    exits non-zero, and nothing is sent on the fault's account. Returns
    whether the fault was recorded and its intent recorded or left to a later
    invocation.
    """
    text = re.sub(r"(?i)(password|passwd|token|secret|api[_-]?key|authorization)\s*[=:]\s*\S+",
                  r"\1=<redacted>", healthchecks.redact(str(exc)).replace(str(store.root),
                                                                       "<records>"))
    summary = f"{check}-{kind} failed: {type(exc).__name__}: {text[:160]}"
    faults.append(summary)
    signature = signature or f"{kind}:{type(exc).__name__}"
    try:
        raise_event(store, now.date().isoformat(), check, None, now, "operational_failure",
                    f"{check}-{kind}", summary)
        current = next((f for f in store.faults()
                        if not f["resolved"] and f["signature"] == signature), None)
        if current is None:
            current = {"schema": FAULT_SCHEMA, "fault_id": store.new_id(),
                       "reason_code": reason, "signature": signature,
                       "detected_by_check": check, "detected_by_invocation": invocation,
                       "kind": kind, "summary": summary[:240],
                       "detected_at_utc": now.astimezone(UTC).isoformat(),
                       "observer_version": VERSION,
                       "effect": "none on any session's qualification; owner attention only",
                       **(detail or {})}
            store.write_fault(current)
        notification_work(out, "the fault alert intent",
                          lambda: fault_intent(store, now, current))
    except Exception as failure:
        faults.append(f"STORAGE UNAVAILABLE: the fault could not be recorded durably "
                      f"({type(failure).__name__}); no alert can be guaranteed for it")
        return False
    return True


def publication_state(store: Store, report: dict, exc: BaseException) -> dict:
    """What a failed publication left under the report's name, read from the disk.

    ABSENT: nothing is there, and the failure came before the final link.
    PRESENT_UNCONFIRMED: this exact report (its id and content hash) is there,
      but syncing it, or publishing and syncing its confirmation, failed.
    UNKNOWN: anything else, including a name the disk cannot answer for.
    A failure after the final link is never called absent. `stage` names the
    step that failed.
    """
    path = store.report_path(report)
    linked = isinstance(exc, (DurabilityUnconfirmed, PublicationIncomplete))
    found = {"report_id": report["report_id"], "session_date": report["session_date"],
             "check": report["check"], "report_path": str(path.relative_to(store.root)),
             "content_sha256": seal(report)["integrity"]["content_sha256"],
             "file_sha256": None, "failure": type(exc).__name__,
             "stage": getattr(exc, "stage", "report-sync" if linked else "report-write")}
    try:
        try:
            path.lstat()
        except FileNotFoundError:
            state = UNKNOWN if linked else ABSENT
        else:
            stored = read_record(path, REPORT_SCHEMA)
            found["file_sha256"] = file_sha256(path)
            state = (PRESENT_UNCONFIRMED if stored["report_id"] == report["report_id"]
                     and stored["integrity"]["content_sha256"] == found["content_sha256"]
                     else UNKNOWN)
    except Exception:
        state = UNKNOWN
    return {**found, "state": state}


def publication_effect(publication: dict) -> dict:
    """What a publication fault means, in three parts, for its fault record."""
    absent = publication["state"] == ABSENT
    return {
        "publication_barrier": (
            "none: there is no report to rely on" if absent else
            f"report {publication['report_id']} counts for nothing — no readiness, "
            "qualification, baseline, early-review trigger, checkpoint or notification "
            "supersession — until it is verified and confirmed"),
        "missing_evidence": (
            f"the {publication['check']} assessment of {publication['session_date']} has no "
            "report and stays MISSING; nothing replaces it" if absent else
            "the report survives but is unavailable to every decision until confirmed"),
        "monitoring_attention": (
            "until a later real assessment of the same check is published and confirmed "
            "with this report found still absent" if absent else
            "until a later invocation verifies this exact report, confirms it and records "
            "the resolution durably"),
        "qualification_impact": (
            "the session cannot use this assessment" if absent else
            "the session cannot use this report until it is confirmed")}


def resolve_publication_faults(store: Store, now: datetime, check: str,
                               invocation: str | None) -> int:
    """Resolve a publication fault once its exact report is verified and made durable.

    Only by a later invocation, and only after it has reconciled its state:
    the report under the recorded name must carry the recorded id and both
    recorded hashes (the fault record is the independent evidence), the report
    and every directory above it must sync, its confirmation must be
    published and synced, and the resolution must be published and synced.
    If any step fails the fault stays open and the report stays excluded.
    File existence, an unrelated assessment, housekeeping or an acknowledgment
    resolve nothing. An ABSENT report's fault is resolved only as
    `resolve_absent_faults` says. Returns how many resolved.
    """
    resolved = 0
    for fault in store.faults():
        found = fault.get("report")
        if fault["resolved"] or fault.get("kind") != PUBLICATION_KIND \
                or fault.get("detected_by_invocation") == invocation \
                or not isinstance(found, dict) or found.get("state") == ABSENT:
            continue
        try:
            path = store.root / found["report_path"]
            stored = validate_report(path, read_record(path, REPORT_SCHEMA))
            # The fault's own record of the report is the independent evidence; a
            # fault that recorded no file hash has nothing to verify the bytes by.
            if not is_sha256(found.get("file_sha256")) \
                    or stored["report_id"] != found["report_id"] \
                    or stored["integrity"]["content_sha256"] != found["content_sha256"] \
                    or file_sha256(path) != found["file_sha256"]:
                continue
            store.confirm_report(path, "reconciled", {
                "evidence": {"fault_id": fault["fault_id"],
                             "recorded_file_sha256": found["file_sha256"]},
                "confirmed_at_utc": now.astimezone(UTC).isoformat(),
                "observer_version": VERSION})
            record = {
                "schema": RESOLUTION_SCHEMA, "fault_id": fault["fault_id"],
                "resolved_at_utc": now.astimezone(UTC).isoformat(), "resolved_by_check": check,
                "resolved_by_invocation": invocation, "observer_version": VERSION,
                "report_id": found["report_id"], "report_path": found["report_path"],
                "content_sha256": found["content_sha256"],
                "verified_file_sha256": file_sha256(path),
                "basis": "a later invocation reconciled its state, found this exact report "
                         "under its recorded name with its recorded id and hashes, synced "
                         "it and the directories above it, and published and synced its "
                         "confirmation",
                "effect": "the report counts from now on as what it says; nothing in it is "
                          "changed, nothing missing is supplied, and no success is sent"}
            # The same check every later reader applies, before anything is written.
            if store.resolution_problem(fault, record):
                continue
            store.write_fault_resolution(record)
        except Exception:
            continue                  # not verified, not durable: the fault stays open
        # Counted only as a later reader will see it: checked against its fault and synced.
        if next(f for f in store.faults() if f["fault_id"] == fault["fault_id"])["resolved"]:
            resolved += 1
            raise_event(store, now.date().isoformat(), check, found["report_id"], now,
                        "observer_fault_resolved", f"fault-{fault['fault_id']}",
                        f"observer fault {fault['fault_id']} ({fault['reason_code']}) resolved")
    return resolved


def resolve_absent_faults(store: Store, now: datetime, report: dict,
                          invocation: str | None) -> int:
    """End the monitoring attention of a missing report's fault, and nothing else.

    `report` is the assessment this invocation has just published and
    confirmed. It resolves an open ABSENT publication fault of an earlier
    invocation only when it is a genuine later scheduled assessment of the
    same check (see `store.replacement_problem`: not a reconstruction, replay
    or early run; completed, not interrupted; started after the failure) and
    the missing report is found still absent — no report and no confirmation
    under its name or id. Its own result need not be PASS, and it may be late
    under the existing late-run rule: it shows that publication works again.
    The missing assessment stays MISSING; nothing is qualified, approved or
    sent. The record is checked exactly as every later reader will check it
    before it is written. Returns how many resolved.
    """
    if not report["ran_checks"]:
        return 0
    path = store.report_path(report)
    if not store.confirmed(path, report):
        return 0
    replacement = {"report_id": report["report_id"], "check": report["check"],
                   "session_date": report["session_date"],
                   "report_path": str(path.relative_to(store.root)),
                   "content_sha256": report["integrity"]["content_sha256"],
                   "file_sha256": file_sha256(path)}
    resolved = 0
    for fault in store.faults():
        found = fault.get("report")
        if fault["resolved"] or fault.get("kind") != PUBLICATION_KIND \
                or fault.get("detected_by_invocation") == invocation \
                or not isinstance(found, dict) or found.get("state") != ABSENT \
                or found["check"] != report["check"] or not store.still_absent(found):
            continue
        try:
            record = {
                "schema": RESOLUTION_SCHEMA, "fault_id": fault["fault_id"],
                "resolved_at_utc": now.astimezone(UTC).isoformat(),
                "resolved_by_check": report["check"], "resolved_by_invocation": invocation,
                "observer_version": VERSION,
                "original_report": {k: found[k] for k in (
                    "report_id", "session_date", "check", "report_path", "state")},
                "replacement_report": replacement,
                "reconciliation": {"original_still_absent": True,
                                   "checked_at_utc": now.astimezone(UTC).isoformat()},
                "basis": "a later real assessment of the same check was published and "
                         "confirmed, and the missing report was found still absent",
                "effect": "monitoring attention only: the missing assessment stays MISSING, "
                          "its session is not qualified, no exception is approved and no "
                          "success is sent"}
            if store.resolution_problem(fault, record):
                continue              # not an eligible replacement: the fault stays open
            store.write_fault_resolution(record)
        except Exception:
            continue                  # not durable: the fault stays open
        if next(f for f in store.faults() if f["fault_id"] == fault["fault_id"])["resolved"]:
            resolved += 1
            raise_event(store, now.date().isoformat(), report["check"], report["report_id"],
                        now, "observer_fault_resolved", f"fault-{fault['fault_id']}",
                        f"observer fault {fault['fault_id']} ({fault['reason_code']}) resolved")
    return resolved


def settle_publication(store: Store, cfg: dict, calendar: dict, now: datetime, check: str,
                       faults: list[str], invocation: str | None,
                       report: dict | None = None, out: Outbound | None = None) -> None:
    """After successful housekeeping: resolve publication faults that may now be, and re-account.

    `report` is this invocation's own confirmed assessment, if it made one.
    """
    try:
        if resolve_publication_faults(store, now, check, invocation):
            housekeeping(store, cfg, calendar, now, check, None, invocation, out, faults)
        if report is not None:
            resolve_absent_faults(store, now, report, invocation)
    except Exception as exc:
        open_fault(store, now, check, "housekeeping", exc, faults, invocation, out=out)


class Outbound:
    """One invocation's whole outbound allowance, and the rule that nothing leaves unrecorded.

    At most one request per Healthchecks identity, four in all, five seconds
    each, inside thirty seconds of wall clock counted from the invocation's
    first notification work, whatever that is. Nothing here waits or tries
    twice; what is not sent now is left for a later invocation to consider.
    One invocation has one of these, and every notification path uses it.

    Every request is reserved on disk before it is made and its outcome is
    written after it returns. If the reservation cannot be written the
    request is not made. If the process stops between the two, the
    reservation stands as an attempt whose completion nobody knows.
    """

    def __init__(self, store: Store, cfg: dict, clock, post, alarm: bool = False, timer=None):
        self.store, self.cfg, self.clock, self.post, self.alarm = store, cfg, clock, post, alarm
        self.timer = timer or time_module.monotonic
        self.began: float | None = None
        self.used: list[str] = []
        self.withheld: list[dict] = []
        self.expiry: dict | None = None        # where the deadline passed, once it has
        self.conflict: str | None = None       # why the deadline could not be enforced
        self.preparation: str | None = None    # worst preparation result: see `prepare`
        self._guarded = False

    def start(self) -> None:
        """Fix the invocation's one notification deadline, unless it is fixed already.

        Called immediately before the first notification-specific work, whatever
        that is. Nothing moves, restarts, pauses or extends it afterwards.
        """
        if self.began is None:
            self.began = self.timer()

    def expired(self) -> bool:
        return self.expiry is not None or (self.began is not None and self.remaining() <= 0)

    def note_expiry(self, during: str, reason: str | None = None) -> None:
        """Record, once, where the deadline passed and by how much the work overran it."""
        if self.expiry is None:
            self.expiry = {"during": during,
                           "overrun_seconds": round(max(0.0, -self.remaining()), 3)}
            self.withheld.append({
                "check": None, "kind": None,
                "reason": reason or f"the notification deadline passed during {during}; "
                                    "no further notification work in this invocation"})

    def check(self, during: str) -> None:
        """Start the deadline if need be, and stop before `during` if it has passed."""
        self.start()
        if self.expired():
            self.note_expiry(during)
            raise NotificationExpired(f"the notification deadline passed before {during}")

    @contextlib.contextmanager
    def guard(self, during: str):
        """Do notification work under the one deadline, interrupting it where it can be.

        The interval timer is set to what is left of the deadline. A guard inside
        another (a request inside a delivery round) uses the outer timer: the
        deadline is never granted twice. If the timer cannot be installed
        safely (another interval timer is running, or this is not the main
        thread) nothing is installed or changed, and the invocation's
        notification work stops here as though the deadline had passed.
        """
        self.check(during)
        if self._guarded:
            yield
            return
        self._guarded = True
        try:
            with hard_limit(self.remaining(), self.alarm):
                yield
        except NotificationExpired:
            self.note_expiry(during)
            raise
        except TimerConflict as exc:
            self.conflict = str(exc)
            self.note_expiry(during, f"the notification deadline could not be enforced ({exc}); "
                                     "no notification work in this invocation")
            raise NotificationExpired(f"the notification deadline could not be enforced "
                                      f"during {during}") from exc
        finally:
            self._guarded = False

    @property
    def enabled(self) -> bool:
        return bool((self.cfg.get("healthchecks") or {}).get("enabled"))

    def remaining(self) -> float:
        if self.began is None:
            return NOTIFY_BUDGET_SECONDS
        return NOTIFY_BUDGET_SECONDS - (self.timer() - self.began)

    def _reserve(self, check: str, kind: str, body: str, notes: list[dict],
                 began: datetime, timeout: float) -> dict | None:
        """Write the reservation. None if another invocation already holds that round."""
        reservation = {
            "schema": RESERVATION_SCHEMA, "reservation_id": self.store.new_id(),
            "check": check, "kind": kind, "body": healthchecks.redact(body)[:healthchecks.MAX_BODY],
            "notification_ids": [n["notification_id"] for n in notes],
            "rounds": {n["notification_id"]: n["rounds"] + 1 for n in notes},
            "reserved_at_utc": began.isoformat(),
            "next_eligible_at_utc": {
                n["notification_id"]: (began + retry_gap(n["rounds"] + 1)).isoformat()
                for n in notes},
            "request_timeout_seconds": timeout, "state": "reserved",
            "observer_version": VERSION,
            "meaning": "a request was about to be made. Without an outcome record beside "
                       "it, whether it was made, and whether it arrived, is unknown"}
        try:
            if notes:
                if not self.store.write_reservation(reservation):
                    return None
            else:
                self.store.write_heartbeat("reservation", reservation)
        except OSError as exc:
            raise StorageUnavailable(
                f"the attempt reservation could not be written ({type(exc).__name__}); "
                "the request was not made") from exc
        return reservation

    def send(self, check: str, kind: str, body: str, notes: list[dict] | None = None):
        """Reserve, make one request, record its outcome. None when it may not be made.

        `notes` are the stored intents this one request speaks for; they share
        the one reservation. Without notes it is the liveness heartbeat.
        """
        if not self.enabled:
            return None
        nested = self._guarded                 # inside other notification work
        try:
            self.check(f"the {check} request")
        except NotificationExpired:
            if nested:
                raise                          # the enclosing work stops too
            self.withheld.append({"check": check, "kind": kind,
                                  "reason": f"the {NOTIFY_BUDGET_SECONDS:.0f}-second "
                                            "notification budget is spent"})
            return None
        left = self.remaining()
        why = ("this identity already had its one request" if check in self.used else
               f"the invocation already made {MAX_REQUESTS} requests"
               if len(self.used) >= MAX_REQUESTS else
               f"the {NOTIFY_BUDGET_SECONDS:.0f}-second notification budget is spent"
               if left < 1.0 else None)
        if why:
            self.withheld.append({"check": check, "kind": kind, "reason": why})
            return None
        began = self.clock().astimezone(UTC)
        reservation = None
        try:
            # The one deadline for all of it: the reservation, the request and the outcome.
            with self.guard(f"the {check} request"):
                reservation = self._reserve(check, kind, body, notes or [], began,
                                            min(healthchecks.REQUEST_TIMEOUT, left))
                if reservation is None:
                    self.withheld.append({"check": check, "kind": kind,
                                          "reason": "another invocation had already reserved "
                                                    "this round"})
                    return None
                self.used.append(check)
                left = self.remaining()          # recomputed immediately before the request
                if left < 1.0:
                    self.withheld.append({"check": check, "kind": kind,
                                          "reason": "the notification budget expired during the "
                                                    "reservation; nothing was sent"})
                    return self._unknown(check, kind, reservation, began,
                                         "not sent: the notification budget expired after the "
                                         "reservation was written")
                timeout = min(healthchecks.REQUEST_TIMEOUT, left)
                try:
                    outcome = healthchecks.ping(healthchecks.load_settings(self.cfg), check,
                                                kind, body, post=self.post, timeout=timeout)
                except healthchecks.PingConfigError as exc:
                    outcome = {"delivered": False, "result": healthchecks.redact(str(exc)),
                               "receipt": "nothing was sent: the ping configuration is unusable"}
                record = {
                    "schema": OUTCOME_SCHEMA, "reservation_id": reservation["reservation_id"],
                    "reserved_at_utc": reservation["reserved_at_utc"], "check": check,
                    "kind": kind, "completed_at_utc": self.clock().astimezone(UTC).isoformat(),
                    "delivered": outcome["delivered"], "result": outcome["result"],
                    "receipt": outcome["receipt"],
                    "delivery_state": DELIVERED if outcome["delivered"] else "undelivered",
                    "observer_version": VERSION}
                try:
                    if notes:
                        self.store.write_outcome(reservation, record)
                    else:
                        self.store.write_heartbeat("outcome", record)
                except OSError as exc:
                    # The request was made; its outcome is not durable. Nothing is
                    # claimed: the reservation stands, completion and receipt unknown.
                    raise StorageUnavailable(
                        f"the outcome of a request could not be written ({type(exc).__name__});"
                        " its reservation stands with completion unknown",
                        self._unknown(check, kind, reservation, began,
                                      "completion unknown: the outcome could not be made "
                                      "durable")) from exc
        except NotificationExpired as stop:
            # Stopped by the budget: in the reservation, the request or the outcome.
            # Whatever was written stays; nothing is claimed about the rest.
            self.note_expiry(f"the {check} request")
            self.withheld.append({"check": check, "kind": kind,
                                  "reason": "stopped at the notification budget"})
            attempt = self._unknown(check, kind, reservation, began,
                                    f"stopped at the {NOTIFY_BUDGET_SECONDS:.0f}-second "
                                    "notification budget; completion unknown")
            if nested:
                stop.attempt = attempt         # kept, and the enclosing work stops too
                raise
            return attempt
        return {**record, "attempted_at_utc": reservation["reserved_at_utc"],
                "request_timeout_seconds": timeout,
                "notified_at_utc": record["completed_at_utc"] if outcome["delivered"] else None}

    @staticmethod
    def _unknown(check: str, kind: str, reservation: dict | None, began: datetime,
                 result: str) -> dict:
        """What is known about a request whose outcome was not recorded: nothing more."""
        return {"check": check, "kind": kind, "delivered": False, "completion": "unknown",
                "result": result, "receipt": healthchecks.UNDELIVERED,
                "reservation_id": reservation["reservation_id"] if reservation else None,
                "attempted_at_utc": began.isoformat(), "notified_at_utc": None,
                "request_timeout_seconds": None}


def retry_gap(rounds_made: int) -> timedelta:
    """How long after its latest round an intent that has had `rounds_made` may be sent again."""
    return INITIAL_GAP if rounds_made < INITIAL_ROUNDS else LATER_GAP


def attention_context(store: Store) -> dict:
    """What is waiting for the owner right now, from the operator's and observer's records.

    DELIVERED, ACKNOWLEDGED and REVIEW PENDING are three different facts. A
    checkpoint is acknowledged only by a validated operator acknowledgment of
    that exact checkpoint; a delivered notification acknowledges nothing.
    Anything that cannot be read is `None` (unknown), never "clear".
    """
    context = {"checkpoint_id": None, "reasons": [], "open": None, "acknowledged": None,
               "open_faults": None, "resolved_faults": set(), "unconfirmed_reports": set()}
    with contextlib.suppress(CorruptRecord):
        checkpoint = store.checkpoint()
        acks, _ = read_acknowledgments(store, checkpoint)
        context.update(checkpoint_id=checkpoint["checkpoint_id"] if checkpoint else None,
                       reasons=checkpoint["reasons"] if checkpoint else [],
                       open=bool(checkpoint and not acks), acknowledged=bool(checkpoint and acks))
    with contextlib.suppress(CorruptRecord):
        faults = store.faults()
        context["open_faults"] = [f["fault_id"] for f in faults if not f["resolved"]]
        context["resolved_faults"] = {f["fault_id"] for f in faults if f["resolved"]}
        context["unconfirmed_reports"] = {
            f["report"]["report_id"] for f in faults if not f["resolved"]
            and f.get("kind") == PUBLICATION_KIND and isinstance(f.get("report"), dict)}
    return context


def legitimate_assessment(report: dict) -> bool:
    """A stored report that is its check's own scheduled assessment of its own day.

    Not a run that started early, not a reconstruction, not a run that found
    the lock held and did nothing. Such a report says what the check's
    current state is whether or not pings were switched on when it was made.
    """
    return bool(report.get("invocation") == "scheduled" and not report.get("catch_up")
                and (report["ran_checks"] or report["result"] in ("NO SESSION",
                                                                  "SPECIAL SESSION")))


def notification_states(store: Store, now: datetime, tolerant: bool = False) -> list[dict]:
    """Every stored intent with where its delivery stands, from the records alone.

    Nothing is remembered between invocations except the stored files, so the
    round count and the spacing are the same whichever process asks, and a
    reservation without an outcome counts as a round like any other. Only a
    `/fail` that is still the current statement for its check is outstanding:

      a success or a heartbeat is never sent late — that would report a
        recovery, or a moment of liveness, that nobody observed then;
      an assessment failure is dropped once a newer assessment of the same
        check exists. "Newer" is decided by the stored assessment reports,
        which are evidence whether or not pings were on, as well as by newer
        intents; an early run, a reconstruction or an unreadable report is
        not an assessment and supersedes nothing;
      a checkpoint's attention ends with the operator's acknowledgment of
        that exact checkpoint and with nothing else — not with delivery, and
        not after any number of rounds;
      an observer fault's attention ends when the fault is resolved.

    Rounds one to six are at least five minutes apart; after that, an hour.
    A history that is damaged, contradictory or silent where an older schema
    said nothing is `unresolved`: shown, and never sent again automatically.
    """
    if tolerant:
        notes, _ = store.notifications(tolerant=True)
    else:
        notes = store.notifications()
    context = attention_context(store)
    reports, _ = store.confirmed_readable_reports()
    newest_intent = {n["check"]: n["notification_id"] for n in notes
                     if n["event_type"] == "assessment_result"}
    assessed = {}
    for report in reports:
        if legitimate_assessment(report) \
                and report["report_id"] not in context["unconfirmed_reports"]:
            done = datetime.fromisoformat(report["completed_at_utc"])
            if report["check"] not in assessed or done > assessed[report["check"]][0]:
                assessed[report["check"]] = (done, report["report_id"])
    # One checkpoint, one alert: intents for the same checkpoint (for example one
    # of schema /1 and one of /2) share a history and only the oldest is acted on.
    logical: dict[str, dict] = {}
    for note in notes:
        if note["event_type"] == "review_attention" and note["checkpoint_id"]:
            first = logical.setdefault(note["checkpoint_id"], note)
            if first is not note:
                first["attempts"] = sorted(first["attempts"] + note["attempts"],
                                           key=lambda a: a["attempted_at_utc"])
                first["delivered"] = first["delivered"] or note["delivered"]
                first["unresolved"] = first["unresolved"] or note["unresolved"]
                note["same_alert_as"] = first["notification_id"]
    for note in notes:
        attempts = note["attempts"]
        last = attempts[-1] if attempts else None
        rounds = sum(a["rounds"] for a in attempts)
        eligible = None
        cleared = (note.get("checkpoint_id") is None or (
            context["acknowledged"] is True
            and note["checkpoint_id"] == context["checkpoint_id"])) and set(
            note.get("fault_ids") or []) <= context["resolved_faults"]
        attention_only = note.get("attention_only")
        if attention_only is None and note["event_type"] == "assessment_result":
            # Schema /1 did not record this. Its body did, in the observer's own words.
            body = note["body"]
            attention_only = ("attention=YES" in body and "timely=True" in body and (
                "result=PASS" in body or "result=NO SESSION" in body)) \
                if "attention=" in body and "result=" in body else None
        if note.get("same_alert_as"):
            state = f"the same alert as {note['same_alert_as']}; accounted for there"
        elif note["delivered"]:
            state = DELIVERED
        elif note["kind"] != "fail":
            state = "undelivered: not retried (a success or heartbeat is never sent late)"
        elif note["unresolved"]:
            state = UNRESOLVED
        elif note["event_type"] == "review_attention":
            state = DUE if context["open"] is None or (
                context["open"] and note["checkpoint_id"] == context["checkpoint_id"]) \
                else "undelivered: ended by acknowledgment of the checkpoint"
        elif note["event_type"] == "observer_fault":
            state = DUE if context["open_faults"] is None or set(
                note.get("fault_ids") or []) & set(context["open_faults"]) \
                else "undelivered: ended by resolution of the fault"
        elif note["event_type"] != "assessment_result":
            state = UNRESOLVED
        elif newest_intent.get(note["check"]) != note["notification_id"]:
            state = "undelivered: superseded by a newer assessment of the same check"
        elif note["check"] in assessed and note.get("report_id") != assessed[note["check"]][1] \
                and assessed[note["check"]][0] > datetime.fromisoformat(
                    note.get("completed_at_utc") or note["created_at_utc"]):
            state = "undelivered: superseded by a newer assessment of the same check"
        elif attention_only and cleared:
            state = "undelivered: ended with the attention condition that caused it"
        elif attention_only is None and note.get("checkpoint_id") and context["open"] is False:
            state = UNRESOLVED
        else:
            state = DUE
        if state == DUE:
            eligible = datetime.fromisoformat(note["created_at_utc"])
            if last:
                eligible = datetime.fromisoformat(last["attempted_at_utc"]) + retry_gap(rounds)
            if now < eligible:
                state = WAITING
        note.update(
            delivery_state=state, outstanding=state in (DUE, WAITING, UNRESOLVED),
            rounds=rounds, attempt_records=len(attempts),
            last_attempt_at_utc=last["attempted_at_utc"] if last else None,
            last_error=last["failure_reason"] if last else None,
            last_receipt=last["receipt"] if last else None,
            last_completion=last["completion"] if last else None,
            next_eligible_at_utc=eligible.astimezone(UTC).isoformat() if eligible else None,
            attention_condition_current=bool(
                (note.get("checkpoint_id") and context["open"] is not False
                 and note["checkpoint_id"] == context["checkpoint_id"])
                or set(note.get("fault_ids") or []) & set(context["open_faults"] or [])))
    return notes


def current_body(note: dict, context: dict) -> str:
    """The stored words of an intent, brought up to date about owner attention only.

    `attention=ACKNOWLEDGED` is written for one reason: a validated operator
    acknowledgment of the exact checkpoint the intent was raised for. While
    that checkpoint is unacknowledged it stays `attention=YES`, delivered or
    not. Where the checkpoint or the faults cannot be read, the stored words
    are left exactly as they are.
    """
    body = note["body"]
    if note["check"] != "afterclose" or context["open"] is None \
            or context["open_faults"] is None:
        return body
    waiting = []
    if context["open"]:
        waiting += context["reasons"]
        if f"checkpoint={context['checkpoint_id']}" not in body:
            body += f" checkpoint={context['checkpoint_id']}"
    if context["open_faults"]:
        waiting.append(FAULT_REASON)
        if FAULT_REASON not in body:
            body += f" fault={FAULT_REASON}"
    if waiting:
        return body.replace("attention=NO reason=-", f"attention=YES reason={','.join(waiting)}")
    if "attention=YES" in body:
        mine = note.get("checkpoint_id")
        if mine and mine == context["checkpoint_id"] and context["acknowledged"]:
            return body.replace("attention=YES", "attention=ACKNOWLEDGED")
        if not mine and note.get("fault_ids"):
            return body.replace("attention=YES", "attention=FAULT_RESOLVED")
    return body


def deliver_round(store: Store, out: Outbound, now: datetime,
                  fresh_id: str | None = None) -> dict:
    """This invocation's one delivery round: at most one request per identity.

    For each assessment identity the request speaks for its current state:
    the assessment this invocation has just made (`fresh_id`), else the
    newest outstanding failure whose retry is due, else an unacknowledged
    checkpoint's attention, else an open observer fault. A `/fail` on the
    after-close identity also speaks for every attention and fault intent
    there whose own retry is due: they share its single reservation, and its
    outcome is theirs. While a checkpoint is unacknowledged or a fault is
    open, a success is never sent to the after-close identity. Returns the
    outcomes by notification id.
    """
    outcomes: dict = {}
    # Preparing the round (reading and re-verifying every record it depends on)
    # is notification work too: it spends the same deadline and is interrupted
    # by it. Once the deadline has passed nothing more is prepared or sent, and
    # work that encloses this round stops as well.
    notification_work(out, "the delivery round",
                      lambda: _deliver(store, out, now, fresh_id, outcomes))
    return outcomes


def _deliver(store: Store, out: Outbound, now: datetime, fresh_id: str | None,
             outcomes: dict) -> None:
    notes = notification_states(store, now, tolerant=True)
    context = attention_context(store)
    for check in ("premarket", "midsession", "afterclose"):
        out.check(f"preparing the {check} alert")      # nothing is prepared past the deadline
        mine = [n for n in notes if n["check"] == check]
        fresh = next((n for n in mine if n["notification_id"] == fresh_id
                      and not n["attempts"]), None)
        due = [n for n in mine if n["delivery_state"] == DUE]
        assessments = [n for n in due if n["event_type"] == "assessment_result"]
        conditions = [n for n in due if n["event_type"] in ("review_attention",
                                                            "observer_fault")]
        first = fresh or (assessments[-1] if assessments else None) or (
            conditions[0] if conditions else None)
        held_back = check == "afterclose" and (context["open"] is not False
                                               or context["open_faults"] != [])
        if first is not None and first["kind"] != "fail" and held_back:
            first = conditions[0] if conditions else None
        if first is None:
            continue
        included = [first] + ([n for n in conditions if n is not first]
                              if first["kind"] == "fail" else [])
        try:
            outcome = out.send(check, first["kind"], current_body(first, context), included)
        except NotificationExpired as stop:
            for note in included if stop.attempt else []:
                outcomes[note["notification_id"]] = stop.attempt
            raise
        if outcome is None:
            continue
        for note in included:
            outcomes[note["notification_id"]] = outcome


def expose(store: Store, out: Outbound, now: datetime, faults: list[str]) -> dict:
    """Write what is still undelivered where the next person or run will see it."""
    notes = notification_states(store, now, tolerant=True)
    _, problems = store.notifications(tolerant=True)
    context = attention_context(store)
    keep = ("notification_id", "schema", "check", "kind", "event_type", "checkpoint_id",
            "body", "created_at_utc", "delivery_state", "rounds", "attempt_records",
            "last_attempt_at_utc", "last_error", "last_receipt", "last_completion",
            "next_eligible_at_utc", "attention_condition_current", "unresolved")
    summary = {
        "schema": "observer_notification_state/2",
        "as_of_utc": now.astimezone(UTC).isoformat(), "transport_enabled": out.enabled,
        "outstanding": [{k: n.get(k) for k in keep} for n in notes if n["outstanding"]],
        "delivered": sum(1 for n in notes if n["delivered"]), "intents": len(notes),
        "checkpoint_attention": {"checkpoint_id": context["checkpoint_id"],
                                 "unacknowledged": context["open"],
                                 "acknowledged": context["acknowledged"]},
        "open_observer_faults": context["open_faults"],
        "unreadable_notification_records": problems,
        "requests_made_by_this_invocation": list(out.used),
        "requests_withheld_by_this_invocation": out.withheld,
        "housekeeping_faults": faults,
        "meaning": "outstanding means undelivered and remote receipt unknown. A reservation "
                   "with no outcome is a request that may or may not have been made. A "
                   "delivered request means the remote endpoint answered 2xx; it is not "
                   "proof that a Telegram message or an email reached anyone, and it is not "
                   "an acknowledgment. One request may speak for several stored events on "
                   "the same check. A missing heartbeat means 'Heartbeat missing — cause "
                   "unknown'; it does not show the VM is off."}
    store.write_state(summary, "notification-state.json")
    return summary


def notification_work(out: Outbound | None, during: str, work) -> bool:
    """Do `work()` as notification work of `out`'s invocation. True if it ran to the end.

    It runs under the invocation's one deadline: checked before it starts
    (which starts the deadline, if nothing has yet) and interrupting it where
    it can. The outermost notification work is where the deadline stops the
    invocation's notification work: it returns False and nothing more is
    done. Inside other notification work the expiry is passed on, so that
    the enclosing work stops as well. Without `out` (a caller with no
    notification context) `work` simply runs.
    """
    if out is None:
        work()
        return True
    outermost = not out._guarded
    try:
        with out.guard(during):
            work()
    except NotificationExpired:
        if not outermost:
            raise
        return False
    return True


def notification_failure(store: Store, out: Outbound, now: datetime, check: str,
                         exc: BaseException, faults: list[str], invocation: str | None,
                         kind: str = "notification-round") -> None:
    """A failure of notification work, recorded as a fault — as notification work itself.

    Its event, fault record and alert intent are all written under what is
    left of the same deadline; no fresh allowance is given. Past it, nothing
    (more) is written: the failure is said locally, in one sanitized line,
    and the exit is non-zero. A failure while recording it is only noted
    (`open_fault`); it starts no further round.
    """
    started = []

    def record():
        started.append(True)
        open_fault(store, now, check, kind, exc, faults, invocation, out=out)
    if not notification_work(out, "recording the notification failure", record):
        faults.append(
            f"{check}-{kind} failed: {type(exc).__name__}; "
            + ("the notification deadline passed while it was being recorded, so its fault "
               "record and alert may not have been stored" if started else
               "not recorded, because the notification deadline had passed; nothing was stored"))


def expose_or_fault(store: Store, out: Outbound, now: datetime, check: str, faults: list[str],
                    invocation: str, reconciled: bool) -> dict | None:
    """Write the notification-state cache; if that fails, it is an observer fault.

    The fault and its after-close intent are recorded, and one more delivery
    round is made within what is left of this invocation's allowance. A
    failure in that round is only noted: nothing here tries the cache again
    or raises a fault about raising a fault. The fault is resolved only by a
    later invocation that writes this cache and has reconciled its state.

    All of it — the cache, the failure's fault and alert, that round and the
    resolution — is notification work under the invocation's one deadline.
    Past the deadline none of it is done: the cache is not rebuilt, and that
    is not a cache fault.
    """
    done: dict = {}

    def work():
        try:
            done["summary"] = expose(store, out, now, faults)
        except Exception as exc:
            open_fault(store, now, check, "notification-state", exc, faults, invocation,
                       out=out)
            try:
                deliver_round(store, out, now)
            except Exception as again:
                faults.append(f"{check}-notification-round after the cache failure failed: "
                              f"{type(again).__name__}")
            return
        if reconciled:
            resolve_faults(store, now, check, "notification-state", invocation)

    notification_work(out, "the notification-state cache", work)
    return done.get("summary")


def report_expiry(out: Outbound, check: str, faults: list[str] | None = None) -> None:
    """Say locally, in one sanitized line, that the notification deadline cut work short.

    Not a fault and not a notification: nothing is queued or sent about it. A
    deadline that could not be enforced at all is a fault of this invocation,
    added to `faults`, so that it exits non-zero.
    """
    if out.conflict is not None:
        print(f"{check}: NOTIFICATION DEADLINE NOT ENFORCEABLE: no notification work was done "
              f"after {out.expiry['during']}; work not done is left to a later invocation")
        if faults is not None:
            faults.append("the notification deadline could not be enforced (another interval "
                          "timer was running, or not on the main thread); it was left as it was")
    elif out.expiry is not None:
        print(f"{check}: NOTIFICATION DEADLINE: the {NOTIFY_BUDGET_SECONDS:.0f}-second "
              f"notification budget ran out during {out.expiry['during']} (over by "
              f"{out.expiry['overrun_seconds']:.1f}s); work not done is left to a later "
              "invocation, and the notification-state cache was not rebuilt")
    if out.expiry is not None:
        for item in out.withheld:
            if item["check"]:
                print(f"{check}: NOTIFICATION WITHHELD: {item['check']} {item['kind']}: "
                      f"{item['reason']}")


def notify(store: Store, report: dict, now: datetime) -> dict:
    """Store the intent to tell Healthchecks.io about a published report.

    The after-close check means "after-close assessment and observer
    evidence-accounting attention": it is failed while a checkpoint is
    unacknowledged or an observer fault is open, even when the session itself
    qualified. The report and its qualification are not touched.
    """
    passed = report["result"] == "NO SESSION" or (report["result"] == PASS and report["timely"])
    context = attention_context(store) if report["check"] == "afterclose" else None
    review = bool(context and report.get("attention_required"))
    # A fault list that cannot be read is not a clean one.
    faults = [] if context is None else (
        ["unreadable"] if context["open_faults"] is None else context["open_faults"])
    reasons = (report.get("attention_reasons") or [] if review else []) + (
        [FAULT_REASON] if faults else [])
    body = (f"check={report['check']} session={report['session_date']} "
            f"result={report['result']} outcome={report.get('outcome', '-')} "
            f"timely={report['timely']} "
            f"attention={'YES' if reasons else 'NO'} "
            f"reason={','.join(reasons) or '-'} "
            f"report={report['report_id']} observer={VERSION}")
    return queue(store, now, "assessment_result", report["check"],
                 "success" if passed and not reasons else "fail", body, report,
                 context["checkpoint_id"] if review else None,
                 # One id per report, so that it is stored once whoever stores it.
                 notification_id=f"assessment-{report['report_id']}",
                 attention_only=bool(passed and reasons),
                 fault_ids=[f for f in faults if f != "unreadable"])


# ---------------------------------------------------------------------------
# The runs
# ---------------------------------------------------------------------------
def premarket(probe, cfg, store, report, start, clock, sleep, calendar) -> list[dict]:
    day = start.date()
    deadline = clock_at(day, cfg["schedule"]["premarket"]["deadline"])
    baseline = trusted_baseline(store, cfg, day.isoformat())
    attempts, run = [], []
    report["attempts"] = attempts      # kept as they are made, so a timeout loses none
    while True:
        began = clock()
        run = checks.premarket_checks(probe, cfg, baseline, began)
        ended = clock()
        attempts.append({"started_at_ist": began.isoformat(), "completed_at_ist": ended.isoformat(),
                         "passed": checks.all_pass(run), "not_passed": checks.failures(run)})
        if checks.all_pass(run) or ended + timedelta(seconds=cfg["retry_seconds"]) >= deadline:
            break
        sleep(cfg["retry_seconds"])
    report["result"] = PASS if checks.all_pass(run) else FAIL
    return run


def _unread(what: str):
    raise ProbeError(f"could not read {what}")


def midsession(probe, cfg, store, report, start, clock, sleep, calendar) -> list[dict]:
    day = start.date()
    facts = checks.read_session(probe, day,
                                captures=store.startup_captures(report["report_id"]))
    report["evidence_reads"] = facts["read_attempts"]
    run = checks.evidence_checks(probe, cfg, facts, day, start, final=False,
                                 sink=report["checks"])
    run += [
        checks.guarded("QuantDesk revision and working tree", lambda: checks.revision(probe, cfg)),
        checks.guarded("one API/scheduler/feed runtime",
                       lambda: checks.single_runtime(probe, cfg)),
        checks.guarded("PostgreSQL healthy", lambda: checks.postgres_healthy(probe, cfg)),
        checks.guarded("Redis healthy", lambda: checks.redis_healthy(probe, cfg)),
        *checks.scheduler_jobs(probe),
        checks.guarded("Angel feed live", lambda: checks.angel_in_session(probe)),
        checks.guarded("single runtime during collection",
                       lambda: checks.runtime_continuity(facts["signals"])
                       if facts["signals"] is not None else _unread("signals")),
        checks.guarded("API counter snapshot", lambda: checks.counter_snapshot(probe, cfg, start),
                       required=False),
    ]
    readable = facts["signals"] is not None and facts["decisions"] is not None
    found = (checks.candidates(facts["signals"], facts["decisions"], facts["positions"], cfg)
             if readable else None)
    report["early_review_candidates_seen"] = (
        UNKNOWN if found is None else
        [{"decision_id": c["decision_id"], "signal_id": c["signal_id"],
          "valid": c["valid_early_review_trigger"]} for c in found])
    report["early_review_note"] = ("recorded as seen; the trigger itself is evaluated on the "
                                   "complete session by the after-close run")
    report["result"] = PASS if checks.all_pass(run) else FAIL
    return run


def operational_checks(probe, cfg, store, facts, day: date, start: datetime,
                       contemporaneous: bool, calendar: dict) -> list[dict]:
    """Requirements whose failure calls for an operator decision, not a data verdict."""
    name = day.isoformat()
    readiness = check_status(store, name, "premarket")
    mid = check_status(store, name, "midsession")
    ops = [checks.record("08:45 operational readiness", readiness, {
        PASS: "a timely pre-market run passed",
        FAIL: "a pre-market run started in its window and did not pass by 08:45",
        MISSING: "no pre-market run started in its window"}[readiness])]

    before = None
    for earlier in store.reports(name, "premarket"):
        for c in earlier["checks"]:
            if c["name"] == "API counter snapshot" and c["status"] == RECORDED:
                before = c["evidence"]
    not_observed = "not observed at the time; a later run cannot reconstruct it"

    def continuity():
        after = checks.counter_snapshot(probe, cfg, start)["evidence"]
        return checks.compare_counters(before, after, day)

    ops.append(checks.guarded("operational continuity (scheduler and feed)", continuity)
               if contemporaneous else
               checks.record("operational continuity (scheduler and feed)", UNKNOWN, not_observed))
    ops.append(checks.guarded("single runtime during collection",
                              lambda: checks.runtime_continuity(facts["signals"])
                              if facts["signals"] is not None else _unread("signals")))
    ops.append(checks.record("12:30 mid-session assessment", mid, {
        PASS: "a timely mid-session run passed", FAIL: "a timely mid-session run did not pass",
        MISSING: "no timely mid-session run exists; it stays MISSING"}[mid], required=False))
    # Unchanged counters and a single API start say what they say and no more.
    # They do not show that every service and feed was healthy at 12:30, so a
    # missing assessment is not replaced by them.
    ops.append(checks.record("mid-session operational facts", {
        PASS: PASS, FAIL: FAIL, MISSING: UNKNOWN}[mid], {
        PASS: "observed by the timely 12:30 run",
        FAIL: "the 12:30 run observed a failure",
        MISSING: "the 12:30 run is MISSING, and no timestamped evidence from its window "
                 "establishes: " + ", ".join(MID_FACTS)}[mid],
        {"established": mid == PASS, "mandatory_facts": list(MID_FACTS)}))
    listing = calendar_status(calendar, day)
    ops.append(checks.record(
        "session date confirmed as a normal session by the reviewed calendar",
        PASS if listing["status"] == "NORMAL" else UNKNOWN,
        "the reviewed calendar lists the date as a normal session"
        if listing["status"] == "NORMAL"
        else f"the date's status is {listing['status']}: {listing['unknown_because']}", listing))
    if not contemporaneous:
        ops.append(checks.record("LIVE_TRADING=false", UNKNOWN, not_observed))
    return ops


def unreported_activity(probe, store, cfg, calendar, day: date) -> list[dict] | str:
    """Earlier days that hold observations but no after-close report."""
    first = day - timedelta(days=cfg["activity_lookback_days"])
    try:
        days = probe.signal_days(first, day - timedelta(days=1))
    except ProbeError:
        return UNKNOWN
    out = []
    for row in days:
        if not any(r["ran_checks"] for r in store.reports(row["day"], "afterclose")):
            when = date.fromisoformat(row["day"])
            listing = calendar_status(calendar, when)
            out.append({"day": row["day"], "observations": row["observations"],
                        "weekday": when.strftime("%A"), "calendar": listing["status"],
                        "unexpected": when.weekday() >= 5 or listing["status"] == "CLOSED"})
    return out


def afterclose(probe, cfg, store, report, start, clock, sleep, calendar) -> list[dict]:
    day = date.fromisoformat(report["session_date"])
    contemporaneous = day == start.date()
    # One snapshot. Everything below — gates, report fields, the UNKNOWN list,
    # the paper reconciliation and the next baseline — is built from it.
    facts = checks.read_session(probe, day, contemporaneous,
                                captures=store.startup_captures(report["report_id"]))
    report["evidence_reads"] = facts["read_attempts"]
    signals, decisions, positions = facts["signals"], facts["decisions"], facts["positions"]
    baseline = trusted_baseline(store, cfg, day.isoformat())
    evidence = checks.evidence_checks(probe, cfg, facts, day, start, final=True,
                                      baseline=baseline, contemporaneous=contemporaneous,
                                      sink=report["checks"])
    listing = calendar_status(calendar, day)
    if signals == [] and listing["status"] == UNKNOWN:
        # No observations on a date nobody has confirmed as a session: neither
        # a failed session nor a holiday can be claimed.
        report.update(outcome=UNKNOWN, result=FAIL, collection_gates_passed=False,
                      valid_early_review_candidates=0)
        report["failure_reasons"].append(
            "no observations on a date whose session status is UNKNOWN; neither an incomplete "
            "session nor a non-session day is established")
        return evidence
    ops = operational_checks(probe, cfg, store, facts, day, start, contemporaneous, calendar)
    data_ok, ops_ok = checks.all_pass(evidence), checks.all_pass(ops)
    report["readiness_0845"] = ops[0]["status"]
    report["midsession_1230"] = next(c["status"] for c in ops
                                     if c["name"] == "12:30 mid-session assessment")
    report["collection_gates_passed"] = data_ok
    report["operational_requirements_passed"] = ops_ok
    report["outcome"] = ("INCOMPLETE" if not data_ok else
                         "QUALIFIED" if ops_ok else "OPERATIONAL EXCEPTION REQUIRED")
    report["result"] = PASS if report["outcome"] == "QUALIFIED" else FAIL
    readable = signals is not None and decisions is not None
    found = checks.candidates(signals, decisions, positions, cfg) if readable else None
    report["expiry_day_passed_candidates"] = UNKNOWN if found is None else found
    report["valid_early_review_candidates"] = (
        0 if found is None else sum(1 for c in found if c["valid_early_review_trigger"]))
    report["decision_funnel"] = UNKNOWN if decisions is None else checks.funnel(decisions)
    report["paper_positions"] = UNKNOWN if positions is None else {
        "total": positions.get("total"), "open": positions.get("open"),
        "opened_this_session": len(positions.get("session") or []),
        "stored": positions.get("session")}
    paper = next(c for c in evidence if c["name"] == "paper activity reconciliation")
    # Only a reconciliation that passed may serve as the next baseline.
    report["baseline"] = ({k: paper["evidence"][k] for k in ("positions_total", "journal_total")}
                          if paper["status"] == PASS else None)
    maintenance = checks.guarded("maintenance events",
                                 lambda: checks.maintenance_review(probe, cfg, start),
                                 required=False)
    reboot = checks.guarded("pending restart requirements",
                            lambda: checks.pending_restart(probe), required=False)
    report["maintenance_events"] = (
        {"review": maintenance["evidence"] or maintenance["detail"],
         "reboot_required": reboot["evidence"].get("reboot_required", UNKNOWN)}
        if contemporaneous else UNKNOWN)
    report["days_with_observations_and_no_afterclose_report"] = unreported_activity(
        probe, store, cfg, calendar, day)
    if not contemporaneous:
        report["failure_reasons"].append(
            "reconstructed after the session date: process, feed and live-trading state were "
            "not observed and are not reconstructed")
    return evidence + ops


RUNS = {"premarket": premarket, "midsession": midsession, "afterclose": afterclose}


def no_session(probe, report, day: date, pending: list) -> None:
    """NO SESSION, but only when the reads succeed and show no activity."""
    try:
        signals, positions = probe.signals(day), probe.positions(day)
    except ProbeError as exc:
        report["result"] = FAIL
        report["unknown_fields"] = ["activity on a listed non-session day"]
        report["failure_reasons"].append(
            f"the calendar lists the date as closed, but the absence of activity could not be "
            f"confirmed: {exc}")
        return
    opened = positions.get("session") if isinstance(positions.get("session"), list) else None
    if signals or opened or opened is None:
        report["result"] = FAIL
        report["unexpected_activity"] = {
            "observations": len(signals), "signal_ids": [s["id"] for s in signals][:200],
            "positions_opened": UNKNOWN if opened is None else len(opened)}
        report["failure_reasons"].append(
            f"{len(signals)} observation(s) on a date the reviewed calendar lists as closed; "
            "they are preserved here and not discarded")
        pending.append(("evidence_inconsistency", "non-session-activity",
                        f"{len(signals)} observation(s) exist on a listed non-session date"))
        return
    report["result"] = "NO SESSION"


def special_session(probe, report, day: date, pending: list) -> None:
    """A reviewed special session: recorded, never run through the 75-slot template."""
    report["result"] = "SPECIAL SESSION"
    try:
        signals = probe.signals(day)
        report["special_session_activity"] = {
            "observations": len(signals), "signal_ids": [s["id"] for s in signals][:200]}
    except ProbeError as exc:
        report["special_session_activity"] = UNKNOWN
        report["unknown_fields"] = ["activity in the special session"]
        report["failure_reasons"].append(f"activity could not be read: {exc}")
    report["failure_reasons"].append(
        "special session: it has its own hours and needs separately reviewed treatment; it is "
        "not assessed as a normal session and does not count as one")
    pending.append(("evidence_inconsistency", "special-session",
                    "a special session was recorded and not assessed; owner review is needed"))


def session_events(report: dict, pending: list) -> None:
    """The exceptions an after-close report raises about its own session."""
    outcome = report.get("outcome")
    if report.get("readiness_0845") == MISSING:
        pending.append(("missing_check", "premarket", "no pre-market run started in its window"))
    if report.get("midsession_1230") == MISSING:
        pending.append(("missing_check", "midsession", "no timely mid-session run exists"))
    if outcome == "OPERATIONAL EXCEPTION REQUIRED":
        pending.append(("operational_exception_required", "session",
                        "collection gates passed; an operational requirement did not"))
    if outcome in ("INCOMPLETE", UNKNOWN):
        pending.append(("evidence_inconsistency", "session",
                        f"session outcome {outcome}: an evidence gate failed or could not be "
                        "established"))
    listed = report.get("days_with_observations_and_no_afterclose_report")
    for row in listed if isinstance(listed, list) else []:
        pending.append(("evidence_inconsistency" if row["unexpected"] else "missing_check",
                        f"unreported-{row['day']}",
                        f"{row['observations']} observation(s) on {row['day']} "
                        f"({row['weekday']}) with no after-close report"))


def wait_for_lock(store: Store, clock, sleep, until: datetime, once: bool = False):
    """Take the observer lock, trying at most once a second and only before `until`.

    Returns (lock or None, attempts, time of the last reading before an
    attempt, time read just after the lock was actually acquired). Each attempt
    is made only after the clock has been read and found still short of
    `until`: a lock that could only be had at `until` or later is not taken,
    and with no time left no attempt is made at all. The time spent here is
    the assessment's own time: nothing extends a deadline. `once` (a run that
    started early) makes exactly one attempt and does not wait.
    """
    attempts = 0
    while True:
        now = clock()
        if not (once and not attempts) and now >= until:
            return None, attempts, now, None
        attempts += 1
        try:
            held = store.lock()
        except Overlap:
            held = None
        if held is not None:
            return held, attempts, now, clock()     # when it was actually held
        if once or now + timedelta(seconds=1) >= until:
            return None, attempts, now, None
        sleep(1)


def run_check(check: str, cfg: dict, store: Store, probe, clock, sleep, *,
              session_date: str | None = None, alarm: bool = False, post=None,
              timer=None, faults: list[str] | None = None,
              publication: dict | None = None) -> dict:
    """One assessment. Whatever happens inside, the lock is released.

    `publication`, if given, is filled with what became of the report:
    `state` CONFIRMED, ABSENT, PRESENT_UNCONFIRMED or UNKNOWN.
    """
    box: dict = {}
    try:
        return _run_check(check, cfg, store, probe, clock, sleep, box,
                          session_date=session_date, alarm=alarm, post=post, timer=timer,
                          faults=faults, publication=publication)
    finally:
        if box.get("held") is not None:
            box["held"].close()


def _run_check(check: str, cfg: dict, store: Store, probe, clock, sleep, box: dict, *,
               session_date: str | None = None, alarm: bool = False, post=None,
               timer=None, faults: list[str] | None = None,
               publication: dict | None = None) -> dict:
    faults = [] if faults is None else faults
    publication = {} if publication is None else publication
    post = post or healthchecks.https_post
    start = clock()
    day = date.fromisoformat(session_date) if session_date else start.date()
    times = cfg["schedule"][check]
    scheduled, deadline = clock_at(day, times["at"]), clock_at(day, times["deadline"])
    in_window = scheduled <= start <= deadline       # no early-start allowance
    report = {"schema": REPORT_SCHEMA, "report_id": store.new_id(), "check": check,
              # Whether this is the check's own scheduled run or one asked for by date.
              "invocation": "reconstruction" if session_date else "scheduled",
              "session_date": day.isoformat(), "observer_version": VERSION,
              "expected_quantdesk_revision": cfg["expected_revision"],
              "scheduled_at_ist": scheduled.isoformat(), "deadline_at_ist": deadline.isoformat(),
              "lateness_seconds": max(0.0, (start - scheduled).total_seconds()),
              "started_in_window": in_window, "timely": False, "ran_checks": False,
              "failure_reasons": [], "checks": [], "unknown_fields": [], "result": FAIL}
    stamp(report, "started", start)
    stamp(report, "invoked", start)
    calendar = load_calendar(cfg)
    pending: list[tuple[str, str, str]] = []
    # Wait for the lock (another run, typically liveness, may hold it for a few
    # seconds), but never past the deadline: the wait is the assessment's own
    # time. A late or reconstructed run's 180 seconds include the wait. A run
    # that starts early tries once and cannot wait its way into the window.
    if start < scheduled:
        until = start
    elif in_window and session_date is None:
        until = deadline
    else:
        until = start + timedelta(seconds=cfg["late_run_seconds"])
    held, attempts, tried, acquired = wait_for_lock(store, clock, sleep, until,
                                                    once=start < scheduled)
    box["held"] = held
    report["lock_attempts"] = attempts
    report["lock_wait_seconds"] = round(((acquired or tried) - start).total_seconds(), 3)
    if attempts:
        stamp(report, "lock_attempted", tried)
    if held is None and not attempts:
        # Its time was already used up: the lock was not even tried.
        report["assessment_status"] = MISSING
        report["deadline_exhausted"] = {
            "at_ist": tried.astimezone(IST).isoformat(),
            "limit_ist": until.astimezone(IST).isoformat(), "stage": "before the lock",
            "note": "no time was left for this assessment; it did not start and is MISSING, "
                    "not passed"}
        report["failure_reasons"].append(
            f"no time was left before {until.astimezone(IST):%H:%M:%S} IST; this run did "
            "nothing")
        pending.append(("operational_failure", f"{check}-deadline",
                        "the assessment's time ran out before it could start"))
    elif held is None:
        report["assessment_status"] = MISSING
        report["lock_contention"] = {
            "attempts": attempts, "waited_until_ist": tried.astimezone(IST).isoformat(),
            "limit_ist": until.astimezone(IST).isoformat(),
            "note": "another observer run held the lock throughout; this assessment did not "
                    "start and is MISSING, not passed"}
        report["failure_reasons"].append(
            f"another observer run held the lock for all {attempts} attempt(s) up to "
            f"{tried.astimezone(IST):%H:%M:%S} IST; this run did nothing")
        pending.append(("operational_failure", f"{check}-overlap",
                        "the observer lock was held through this assessment's window"))
    else:
        stamp(report, "lock_acquired", acquired)
    # The invocation's one notification context. Its deadline starts at the first
    # notification work below, wherever that is, and runs on through everything after.
    out = Outbound(store, cfg, clock, post, alarm, timer)

    def failed(reason: str, event: tuple[str, str, str]) -> None:
        report["result"] = FAIL
        if check == "afterclose" and report["ran_checks"]:
            report.update(outcome="INCOMPLETE", collection_gates_passed=False,
                          valid_early_review_candidates=0)
        report["failure_reasons"].append(reason)
        pending.append(event)

    run: list[dict] = []
    try:
        if held is None:
            pass
        elif start < scheduled:
            report["catch_up"] = True
            report["failure_reasons"].append(
                "started before the scheduled time; a run that starts early cannot satisfy "
                "the assessment and checked nothing")
        else:
            report["session_calendar"] = calendar_status(calendar, day)
            report["maintenance_records_active"] = active_maintenance(store, start)
            began = clock()
            # Recomputed after the lock was taken: waiting used part of the time.
            # `until` is the deadline for an on-time run and the one 180-second
            # allowance for a late one; neither is restarted, and nothing remains
            # at or after it.
            limit = (until - began).total_seconds()
            if limit <= 0:
                raise DeadlineExhausted(began)
            stamp(report, "assessment_started", began)
            with time_limit(limit, alarm):
                status = report["session_calendar"]["status"]
                if status == "CLOSED":
                    no_session(probe, report, day, pending)
                elif status == "SPECIAL":
                    special_session(probe, report, day, pending)
                else:
                    report["ran_checks"] = True
                    run = RUNS[check](probe, cfg, store, report, start, clock, sleep, calendar)
    except DeadlineExhausted as exc:
        report["assessment_status"] = MISSING
        report["deadline_exhausted"] = {
            "at_ist": exc.at.astimezone(IST).isoformat(),
            "limit_ist": until.astimezone(IST).isoformat(), "stage": "after the lock",
            "note": "the lock came too late to leave any time; no check was started and this "
                    "assessment is MISSING, not passed"}
        report["failure_reasons"].append(
            f"no time was left at {exc.at.astimezone(IST):%H:%M:%S} IST against "
            f"{until.astimezone(IST):%H:%M:%S} IST; no check was started")
        pending.append(("operational_failure", f"{check}-deadline",
                        "the assessment's time ran out before it could start"))
    except RunTimeout as exc:
        # What was finished before the limit stays on the report; the rest is absent.
        report["timed_out"] = True
        failed(f"{exc}; nothing it had not finished is assumed",
               ("operational_failure", f"{check}-timeout", str(exc)))
    except CorruptRecord as exc:
        failed(f"a stored observer record is corrupt: {exc}",
               ("evidence_inconsistency", f"{check}-corrupt-record",
                "a stored observer record could not be read; accounting is withheld"))
    except Exception as exc:  # the observer's own fault must end as a recorded failure
        failed(f"observer internal error: {type(exc).__name__}",
               ("operational_failure", f"{check}-internal-error",
                f"observer internal error: {type(exc).__name__}"))

    done = clock()
    stamp(report, "completed", done)
    run = report["checks"] = run or report["checks"]
    report["unknown_fields"] += [c["name"] for c in run if c["status"] == UNKNOWN]
    report["failure_reasons"] += checks.failures(run)
    # Work stopped by its limit is not a completed assessment, however early it stopped.
    report["timely"] = bool(report["ran_checks"] and not report.get("timed_out")
                            and in_window and done <= deadline)
    if report["ran_checks"] and not report["timely"]:
        report["failure_reasons"].append(
            f"not timely: started {report['lateness_seconds']:.0f}s after the scheduled time, "
            f"completed {done:%H:%M:%S} IST against a {deadline:%H:%M} deadline")
        if check == "premarket":
            report["result"] = FAIL
        if check == "afterclose" and report.get("outcome") == "QUALIFIED":
            # Late evidence is still evidence; the lateness stays on the record.
            report.update(outcome="OPERATIONAL EXCEPTION REQUIRED", result=FAIL,
                          operational_requirements_passed=False)
    if report["ran_checks"]:
        stamp(report, "assessment_interrupted" if report.get("timed_out")
              else "assessment_completed", done)
    try:
        report["quantdesk_revision_observed"] = probe.revision()
    except Exception:
        report["quantdesk_revision_observed"] = UNKNOWN

    assessed = held is not None and report["ran_checks"]
    if assessed and check == "afterclose":
        session_events(report, pending)
    elif assessed:
        if report["result"] != PASS:
            pending.append(("operational_failure", check, f"the {check} check did not pass"))
        try:
            if check == "midsession" and \
                    check_status(store, day.isoformat(), "premarket") == MISSING:
                pending.append(("missing_check", "premarket",
                                "no pre-market run started in its window"))
        except Exception:
            pass

    # What the phase will be once this report is published, so the report can say so.
    state = None
    if held is not None:
        try:
            state = phase_state(store, cfg, calendar, done,
                                report if assessed and check == "afterclose" else None)
            report["bounded_phase"] = {
                k: state[k] for k in ("phase", "reasons", "trigger_session", "window_details",
                                      "qualifying_sessions", "window", "calendar_uncertainty")}
            report["attention_required"] = state["attention_required"]
            report["attention_reasons"] = state["reasons"] if state["attention_required"] else []
            report["attention_meaning"] = ("owner attention required; this does not by itself "
                                           "say the session's data collection was invalid")
            checkpoint = state["checkpoint"]
            report["outside_bounded_evaluation_period"] = bool(
                checkpoint and checkpoint["trigger_session"] < report["session_date"])
        except CorruptRecord as exc:
            report["bounded_phase"] = UNKNOWN
            report["attention_required"] = True
            report["attention_reasons"] = ["CORRUPT_OBSERVER_RECORD"]
            if not any("corrupt" in r for r in report["failure_reasons"]):
                failed(f"a stored observer record is corrupt: {exc}",
                       ("evidence_inconsistency", f"{check}-corrupt-record",
                        "a stored observer record could not be read; accounting is withheld"))
        except Exception as exc:
            # The phase could not be worked out. The checks already made stay as
            # they are; what depends on the accounting is withheld, not guessed.
            report["bounded_phase"] = UNKNOWN
            report["attention_required"] = True
            report["attention_reasons"] = ["OBSERVER_PHASE_ERROR"]
            report["result"] = FAIL
            report["failure_reasons"].append(
                f"observer internal error while working out the bounded phase: "
                f"{type(exc).__name__}; the phase is UNKNOWN and qualification is withheld")
            if check == "afterclose" and report["ran_checks"]:
                report.update(outcome=UNKNOWN, valid_early_review_candidates=0,
                              qualification_withheld="the bounded-phase accounting could not "
                                                     "be established")
            pending.append(("operational_failure", f"{check}-phase-error",
                            f"the bounded phase could not be worked out: {type(exc).__name__}"))
            # Recorded now, before anything else is written, so that a report that
            # cannot be published does not take the fault with it.
            open_fault(store, done, check, "housekeeping", exc, faults, report["report_id"],
                       out=out)
    # A run that started early and a reconstruction of an earlier date are not
    # today's assessment, so they add no assessment signal. Recorded on the report
    # so that a later invocation can store the intent if this one cannot.
    report["notification_planned"] = bool(held is not None and out.enabled
                                          and session_date is None
                                          and not report.get("catch_up"))
    try:
        publication["state"] = CONFIRMED
        recorded = True
        try:
            # A: the immutable report; B to E: syncing it and every directory
            # above it, then publishing and syncing its confirmation. Only then
            # is the report evidence, for this process and any later one.
            path = store.write_report(report)
            store.confirm_report(path, "published", {
                "confirmed_at_utc": done.astimezone(UTC).isoformat(),
                "observer_version": VERSION})
        except Exception as exc:
            # Whatever the disk now holds under the report's name is left exactly
            # as it is. What it holds is found out, and recorded as a fault of
            # its own with the owner's alert; nothing is claimed beyond that.
            publication.update(publication_state(store, report, exc))
            recorded = open_fault(
                store, done, check, PUBLICATION_KIND, exc, faults, report["report_id"],
                signature=f"{PUBLICATION_KIND}:{report['report_id']}",
                detail={"report": {k: v for k, v in publication.items()},
                        "effect": publication_effect(publication)},
                reason=PUBLICATION_REASON, out=out)
        if not recorded:
            # Neither the report nor its fault could be stored. Nothing more is
            # written or sent: there is no reserved record to send on behalf of.
            return report
        for kind, subject, summary in pending:
            emit(store, report, kind, subject, summary)
        if held is not None:
            invocation = report["report_id"]
            # Local first: nothing the network does can stop the checkpoint or
            # its notification intent from being recorded.
            reconciled = False
            try:
                housekeeping(store, cfg, calendar, done, check, report["report_id"], invocation,
                             out, faults)
                reconciled = True
            except Exception as exc:
                open_fault(store, done, check, "housekeeping", exc, faults, invocation, out=out)
            if reconciled:
                mine = None
                if publication["state"] == CONFIRMED:
                    mine = read_record(store.report_path(report), REPORT_SCHEMA)
                settle_publication(store, cfg, calendar, done, check, faults, invocation,
                                   mine, out)

            # The rest is notification work, under the deadline already running (or
            # starting here, if nothing above was notification work).
            def assessment_notification():
                # Nor does a report whose publication is not confirmed add a signal.
                fresh = None
                if report["notification_planned"] and publication["state"] == CONFIRMED:
                    fresh = notify(store, report, done)["notification_id"]
                outcome = deliver_round(store, out, done, fresh).get(fresh)
                out.check("the notification's follow-up")
                if outcome is not None and not outcome["delivered"]:
                    emit(store, report, "operational_failure", f"{check}-notification",
                         f"{outcome['receipt']} A failure signal is tried again by later "
                         "runs.")
                if reconciled and out.preparation != "failed":
                    resolve_faults(store, done, check, "notification-round", invocation)
                preparation_recovered(store, out, done, check, invocation, reconciled)
            try:
                # Past the deadline this stops quietly: recorded on `out`, reported
                # below, never as a fault.
                notification_work(out, "the assessment's notification", assessment_notification)
            except Exception as exc:
                notification_failure(store, out, done, check, exc, faults, invocation)
            expose_or_fault(store, out, done, check, faults, invocation,
                            reconciled and out.preparation != "failed")
            report_expiry(out, check, faults)
    finally:
        if held is not None:
            held.close()
    return report


def correct(args, cfg: dict, store: Store, clock) -> dict:
    """An ordinary correction: a note attached to a report. It approves nothing."""
    now = clock()
    try:
        lock = store.lock(wait_seconds=10)
    except Overlap as exc:
        raise SystemExit(f"{exc}; try again when it has finished") from exc
    with contextlib.closing(lock):
        if store.report_file(args.session_date, args.report_id) is None:
            raise SystemExit(f"no report {args.report_id} exists for {args.session_date}")
        correction = {"schema": "observer_correction/1", "correction_id": store.new_id(),
                      "session_date": args.session_date, "corrects_report_id": args.report_id,
                      "recorded_at_utc": now.astimezone(UTC).isoformat(),
                      "observer_version": VERSION, "note": args.note,
                      "recorded_by": args.recorded_by,
                      "effect": "none on qualification, the bounded phase or owner attention; "
                                "only operator records can approve an exception or "
                                "acknowledge a checkpoint"}
        store.write_correction(correction)
    return correction


def liveness(cfg: dict, store: Store, clock, post, alarm: bool = False, timer=None) -> int:
    """Heartbeat first, then the local housekeeping, then one delivery round.

    The heartbeat says the observer ran recently and could reach out; it
    reads nothing from QuantDesk and says nothing about QuantDesk's health or
    about the observer's own records. The housekeeping runs whether or not
    the heartbeat got through, and whether or not pings are switched on at
    all. If the housekeeping fails, that is an independent fault: recorded,
    queued as a `/fail` on the after-close identity, sent in this same
    invocation if a request and the time remain, and a non-zero exit —
    however the heartbeat fared.
    """
    now = clock()
    out = Outbound(store, cfg, clock, post, alarm, timer)
    faults, beat, held = [], None, None
    invocation = f"liveness-{store.new_id()}"
    # The lock is taken first so the heartbeat's reservation is written under it.
    # If another run holds it, the heartbeat still goes: its reservation has a name
    # of its own and needs no shared counter.
    with contextlib.suppress(Overlap):
        held = store.lock()
    try:
        out.start()                    # the one deadline: before the heartbeat is prepared
        try:
            beat = out.send("liveness", "success", f"check=liveness observer={VERSION}")
        except StorageUnavailable as exc:
            beat = exc.attempt         # a request that may have been made: receipt unknown
            faults.append(f"STORAGE UNAVAILABLE: {exc}")
        if held is None:
            print("liveness: another observer run holds the lock and does the housekeeping")
        else:
            reconciled = False
            try:
                calendar = load_calendar(cfg)
                state = housekeeping(store, cfg, calendar, now, "liveness",
                                     invocation=invocation, out=out, faults=faults)
                reconciled = True
                settle_publication(store, cfg, calendar, now, "liveness", faults, invocation,
                                   out=out)
                print(f"liveness: phase {state['phase']}"
                      + (f" ({', '.join(state['reasons'])}); owner attention "
                         f"{'REQUIRED' if state['attention_required'] else 'acknowledged'}"
                         if state["reasons"] else ""))
            except Exception as exc:
                open_fault(store, now, "liveness", "housekeeping", exc, faults, invocation,
                           out=out)
            # The accounting in the housekeeping above is not notification work, but
            # the deadline that began with the heartbeat kept running through it.

            def delivery():
                deliver_round(store, out, now)
                out.check("the round's follow-up")
                if reconciled and out.preparation != "failed":
                    resolve_faults(store, now, "liveness", "notification-round", invocation)
                preparation_recovered(store, out, now, "liveness", invocation, reconciled)
            try:
                notification_work(out, "the delivery round", delivery)
            except Exception as exc:
                notification_failure(store, out, now, "liveness", exc, faults, invocation)
            summary = expose_or_fault(store, out, now, "liveness", faults, invocation,
                                      reconciled and out.preparation != "failed")
            report_expiry(out, "liveness", faults)
            for note in (summary or {}).get("outstanding", []):
                print(f"liveness: {healthchecks.UNDELIVERED} {note['check']} "
                      f"{note['event_type']} rounds={note['rounds']} "
                      f"state={note['delivery_state']} next={note['next_eligible_at_utc']}")
    finally:
        if held is not None:
            held.close()
    for text in faults:
        print(f"liveness: HOUSEKEEPING FAULT: {text}")
    if not out.enabled:
        print("liveness: Healthchecks.io pings are switched off")
        return 1 if faults else 0
    delivered = bool(beat and beat["delivered"])
    print("liveness: heartbeat delivered" if delivered else
          f"liveness: heartbeat: {beat['receipt'] if beat else 'not sent'}")
    return 0 if delivered and not faults else 1


def main(argv, probe=None, clock=None, sleep=None, alarm=True, post=None) -> int:
    parser = argparse.ArgumentParser(prog="observer", description=__doc__)
    parser.add_argument("command",
                        choices=[*RUNS, "liveness", "correct", "status", "shutdown-evidence"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--session-date", help="after-close reconstruction, or the correction's")
    parser.add_argument("--report-id")
    parser.add_argument("--note")
    parser.add_argument("--recorded-by")
    parser.add_argument("--txn", help="shutdown-evidence: the power transaction id")
    parser.add_argument("--lock-fd", type=int,
                        help="shutdown-evidence: an inherited descriptor holding the lock")
    parser.add_argument("--api-stopped", action="store_true",
                        help="shutdown-evidence: bind to the stopped API unit's last start")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    store = Store(cfg["report_root"], cfg["operator_root"])
    clock = clock or (lambda: datetime.now(IST))
    sleep = sleep or time_module.sleep
    post = post or healthchecks.https_post
    if args.command == "shutdown-evidence":
        # Read-only; sends nothing. See evidence.py (v0.4.14).
        from . import evidence
        try:
            record = evidence.collect(cfg, store, probe or Probe(cfg), clock, args.txn,
                                      args.config, args.lock_fd, args.api_stopped)
        except evidence.EvidenceRefused as exc:
            print(f"shutdown-evidence: refused: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(record, sort_keys=True))
        return 0
    if args.txn is not None or args.lock_fd is not None or args.api_stopped:
        parser.error("--txn, --lock-fd and --api-stopped apply to shutdown-evidence only")
    if args.command == "status":
        try:
            state = phase_state(store, cfg, load_calendar(cfg), clock())
            state["notifications_outstanding"] = [
                {k: n[k] for k in ("notification_id", "check", "event_type", "delivery_state",
                                   "rounds", "last_error", "last_completion",
                                   "next_eligible_at_utc")}
                for n in notification_states(store, clock(), tolerant=True)
                if n["outstanding"]]
            state["open_observer_faults"] = [f["fault_id"] for f in store.faults()
                                             if not f["resolved"]]
            print(json.dumps(state, indent=2, default=str))
        except CorruptRecord as exc:
            print(f"phase UNKNOWN: {exc}")
            return 1
        return 0
    if args.command == "liveness":
        return liveness(cfg, store, clock, post, alarm)
    if args.command == "correct":
        if not (args.session_date and args.report_id and args.note and args.recorded_by):
            parser.error("correct needs --session-date, --report-id, --note and --recorded-by")
        print(json.dumps(correct(args, cfg, store, clock), indent=2))
        return 0
    if args.session_date and args.command != "afterclose":
        parser.error("--session-date applies to afterclose and correct only")
    faults: list[str] = []
    publication: dict = {}
    try:
        report = run_check(args.command, cfg, store, probe or Probe(cfg), clock, sleep,
                           session_date=args.session_date, alarm=alarm, post=post,
                           faults=faults, publication=publication)
    except (OSError, StorageUnavailable) as exc:
        # The observer's own records could not be written. No result is claimed;
        # whatever was recorded stays as it is. The lock was released on the way out.
        print(f"{args.command}: STORAGE UNAVAILABLE: the run could not finish recording "
              f"({type(exc).__name__}); no result is claimed for this run")
        for text in faults:
            print(f"{args.command}: HOUSEKEEPING FAULT: {text}")
        return 1
    if publication.get("state", CONFIRMED) != CONFIRMED:
        meaning = {
            ABSENT: "the report was not published and is not on disk",
            PRESENT_UNCONFIRMED: f"report {publication['report_id']} (sha256 "
                                 f"{publication['content_sha256'][:12]}) is on disk, but its "
                                 "durability is unconfirmed; it counts for nothing until a "
                                 "later run verifies it",
        }.get(publication["state"], "whether the report was published is unknown")
        print(f"{report['check']} {report['session_date']}: REPORT {publication['state']}: "
              f"{meaning}; no result is claimed")
        for text in faults:
            print(f"{report['check']}: HOUSEKEEPING FAULT: {text}")
        return 1
    print(f"{report['check']} {report['session_date']}: {report['result']}"
          + (f" ({report['outcome']})" if report.get("outcome") else ""))
    for text in faults:
        print(f"{report['check']}: HOUSEKEEPING FAULT: {text}")
    return 0 if report["result"] in (PASS, "NO SESSION") and not faults else 1
