"""Azure Automation runbook `qd-monitor` (Python 3): independent watch on the VM's power state.

Runs in Azure, not on the VM, so it sees what the VM cannot report about
itself. It reads the VM's power state and tags with the Automation account's
managed identity (custom role: read and instanceView/read on this VM only;
see role-automation.json), compares them with the operating policy, and
FAILS THE JOB when they disagree. A failed job is what alerts: the alert rule
`qd-runbook-failed` (alerts.template.json) emails the owner. A monitor that
does not run at all can only be caught by a log search alert
(alerts.monitor-missing.proposal.json), which needs new resources and is an
approval blocker: a metric alert has no samples to judge when nothing ran.

Policy (IST), evaluated at each scheduled run:
  weekday 07:50-17:30            the VM must be running
                                  (missing startup; unexpected stop)
  weekday 21:30-07:20, weekends  the VM must be deallocated
                                  (running: the controller never ran, was
                                  blocked, or a deallocation is unresolved;
                                  stopped-but-allocated or a transition still
                                  in progress: an incomplete shutdown)
  a special-session window       the VM must be running; QD_SPECIAL_SESSIONS
                                  must exist ("[]" when there are none) and
                                  every entry must carry date, vm_start,
                                  expect_running_from and expect_running_until,
                                  timezone-aware, all on that date (IST), with
                                  vm_start < from < until, and no date twice;
                                  anything else is a ConfigurationError (the
                                  job fails: an alert), never "none"
  an active hold                 the tag `qd-power-hold` (ISO time) keeps a
                                  running VM from being reported after hours;
                                  it never excuses a stopped VM in daytime, a
                                  special session, or any status problem below
Times between those windows (07:20-07:50, 17:30-21:30) are transitions and
are not judged. The same alert repeats at every scheduled run while the
condition lasts; that is the reminder.

The controller's shutdown status (`qd-power-status` tag, quantdesk_power/
status.py) is checked as well, at every run, and anything wrong with it FAILS
THE JOB - never a pass:
  missing; malformed; not signed with the shared key; for another VM; from
  the future; older than one already seen (a replay); a different record with
  a sequence already seen (a conflict); flagged UNRESOLVED, RESTORE_FAILED,
  RESUME_FAILED, CORRUPT or IN_PROGRESS (so an unresolved incident or an
  unfinished restoration keeps alerting while the VM is deallocated, and
  whatever the hold tag says); older than STATUS_MAX_AGE while the VM runs (stale);
  still the deallocation-terminal record of the boot that asked for the
  deallocation while the VM runs again; a deallocated VM whose last record is
  not a submitted deallocation (deallocated outside the controller, or stale);
  and NOT FROM THE CURRENT OPERATING CYCLE. The cycle is the latest required
  startup - every weekday 07:30 IST, holidays included, and each special
  session's VM start (QD_SPECIAL_SESSIONS `vm_start`) - computed here,
  independently, and compared with the record's signed cycle. A record from
  an earlier cycle (a weekend's shutdown record reused after Monday's
  required startup) is an alert: there is no evidence that startup happened.
  Only within CYCLE_GRACE of a startup is the previous cycle's record still
  accepted. Overnight and weekend downtime stays quiet because no startup
  was required since its record. An unreadable special-session list
  exempts nothing: it is an alert.
The last accepted record is kept in the Automation variable
QD_POWER_STATUS_LAST.

NOT YET POSSIBLE: publishing that tag needs a new permission for the VM's
identity, and the key (QD_POWER_STATUS_KEY, an encrypted variable, and the
controller's key file) needs provisioning. Both are approval blockers; until
then this runbook fails every run on the missing status, which is the
intended fail-closed behaviour.

Inputs (Automation variables, or environment variables of the same name when
run locally): QD_VM_RESOURCE_ID, QD_SPECIAL_SESSIONS (JSON list of
{date, vm_start, expect_running_from, expect_running_until}), QD_POWER_STATUS_KEY (hex),
QD_POWER_STATUS_LAST.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
ARM = "https://management.azure.com"
API_VERSION = "2024-07-01"
VM_ID = re.compile(r"^/subscriptions/[0-9a-f-]{36}/resourceGroups/[A-Za-z0-9._()-]{1,90}/"
                   r"providers/Microsoft\.Compute/virtualMachines/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DAY_START, DAY_END = time(7, 50), time(17, 30)
NIGHT_START, NIGHT_END = time(21, 30), time(7, 20)
HOLD_TAG = "qd-power-hold"
STATUS_TAG = "qd-power-status"
STATUS_MAX_AGE = timedelta(minutes=150)   # the controller republishes hourly while up
STATUS_SKEW = timedelta(minutes=5)
# The status codec, kept identical to quantdesk_power/status.py (tested).
STATUS_SCHEMA = "qdps1"
STATUS_FLAGS = ("OK", "IN_PROGRESS", "UNRESOLVED", "RESTORE_FAILED", "RESUME_FAILED", "CORRUPT")
STATUS_FIELDS = ("schema", "vm", "boot", "seq", "txn", "state", "sub", "flag", "cycle", "epoch",
                 "mac")
# IN_PROGRESS alerts too: no run publishes it in the normal course (the submission is
# UNRESOLVED; every run otherwise ends settled), so it means a restoration or resumption
# that did not finish - and no hold may hide that.
ALERT_FLAGS = ("UNRESOLVED", "RESTORE_FAILED", "RESUME_FAILED", "CORRUPT", "IN_PROGRESS")
SPECIAL_FIELDS = ("date", "vm_start", "expect_running_from", "expect_running_until")
CONFIG = "configuration error: "          # marks problems that make the job a ConfigurationError
STARTUP = time(7, 30)
CYCLE_GRACE = timedelta(minutes=20)
STATUS_PATTERNS = {"vm": r"[0-9a-f]{12}", "boot": r"[0-9a-f-]{36}", "seq": r"[1-9][0-9]{0,9}",
                   "txn": r"[0-9a-f]{32}|-", "state": r"[A-Z_]{1,40}", "sub": r"[0-9a-f]{8}|-",
                   "flag": "|".join(STATUS_FLAGS), "cycle": r"[0-9]{12}|-",
                   "epoch": r"[0-9]{9,11}", "mac": r"[0-9a-f]{32}"}
DEALLOCATION_SUBMITTED = ("SUBMITTING", "ACCEPTED", "COMPLETED", "UNKNOWN", "UNKNOWN_RETAINED",
                          "OBSERVED_NEW_BOOT", "POLL_SUCCEEDED",
                          "OPERATOR_CONFIRMED_DEALLOCATED")
DEALLOCATED_TERMINAL = ("COMPLETED", "POLL_SUCCEEDED", "OPERATOR_CONFIRMED_DEALLOCATED")


def power_state(instance_view: dict) -> str:
    """'running', 'deallocated', 'stopped', 'deallocating', 'starting', 'stopping' or 'unknown'."""
    codes = [s.get("code", "") for s in instance_view.get("statuses", []) if isinstance(s, dict)]
    states = [c.split("/", 1)[1] for c in codes if c.startswith("PowerState/")]
    return states[0] if len(states) == 1 else "unknown"


def _moment(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


CANONICAL_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def session_date(value) -> date | None:
    """A session date only in canonical YYYY-MM-DD form: a string of that shape that
    parses to a real calendar date and prints back identically. Compact (20261108) and
    ISO week (2026-W45-7) forms, which date.fromisoformat also accepts, are refused."""
    if not isinstance(value, str) or not CANONICAL_DATE.match(value):
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return None
    return day if day.isoformat() == value else None


def special_entry_problem(entry) -> str | None:
    """Why a QD_SPECIAL_SESSIONS entry is unusable, or None. Every field is required."""
    if not isinstance(entry, dict):
        return "not an object"
    missing = [name for name in SPECIAL_FIELDS if name not in entry]
    if missing:
        return f"missing {', '.join(missing)}"
    day = session_date(entry["date"])
    if day is None:
        return "date is not a canonical YYYY-MM-DD calendar date"
    moments = {name: _moment(entry[name]) for name in SPECIAL_FIELDS[1:]}
    unreadable = [name for name, value in moments.items() if value is None]
    if unreadable:
        return f"{', '.join(unreadable)} not a timestamp with a timezone"
    elsewhere = [name for name, value in moments.items() if value.astimezone(IST).date() != day]
    if elsewhere:
        return (f"{', '.join(elsewhere)} not on the session date {day} (IST): a wrong date or a "
                "schedule crossing midnight")
    if not moments["vm_start"] < moments["expect_running_from"] < moments["expect_running_until"]:
        return "times out of order: vm_start < expect_running_from < expect_running_until required"
    return None


def special_list_problems(specials) -> list[str]:
    """Every configuration problem of QD_SPECIAL_SESSIONS; empty only when all of it is usable.

    Each entry must be complete and well ordered (special_entry_problem), and no date
    may be listed twice: two entries for one date contradict each other.
    """
    if not isinstance(specials, list):
        return [f"{CONFIG}the special-session list (QD_SPECIAL_SESSIONS) is missing or not a "
                "list: special-session windows cannot be judged"]
    problems, dates = [], {}
    for entry in specials:
        problem = special_entry_problem(entry)
        if problem:
            problems.append(f"{CONFIG}a special-session entry is unreadable ({problem}): "
                            f"{entry!r:.120}")
        else:
            day = session_date(entry["date"])          # duplicates by the parsed date
            dates[day] = dates.get(day, 0) + 1
    problems += [f"{CONFIG}contradictory special-session entries: {day.isoformat()} is listed "
                 f"{count} times" for day, count in sorted(dates.items()) if count > 1]
    return problems


def evaluate(now: datetime, state: str, tags: dict, specials: list) -> list[str]:
    """The problems with `state` at `now`; empty when the VM is where policy expects it."""
    now = now.astimezone(IST)
    problems = []
    special_now = []
    problems += special_list_problems(specials)
    for entry in specials if isinstance(specials, list) else []:
        if special_entry_problem(entry):
            continue                  # reported above; its window exempts nothing
        start = _moment(entry["expect_running_from"])
        end = _moment(entry["expect_running_until"])
        if start <= now <= end:
            special_now.append(entry.get("date"))
    hold_until = _moment(tags.get(HOLD_TAG)) if isinstance(tags, dict) else None
    hold_active = hold_until is not None and hold_until >= now
    weekday, clock = now.weekday() < 5, now.time()
    if special_now or (weekday and DAY_START <= clock < DAY_END):
        if state != "running":
            why = f"special session {special_now[0]}" if special_now else "the weekday window"
            problems.append(f"the VM is {state} during {why} at {now:%a %H:%M} IST: a failed "
                            "startup or an unexpected stop")
    elif not weekday or clock >= NIGHT_START or clock < NIGHT_END:
        if state == "running" and not hold_active:
            problems.append(f"the VM is still running at {now:%a %H:%M} IST: the power "
                            "controller did not complete (it never ran, was blocked, or a "
                            "deallocation or restoration is unresolved); see power.py status")
        elif state in ("stopped", "deallocating", "stopping", "starting"):
            problems.append(f"the VM is {state} at {now:%a %H:%M} IST: an incomplete shutdown "
                            "or an operation still in progress")
        elif state == "unknown":
            problems.append(f"the VM's power state is unknown at {now:%a %H:%M} IST")
    if state == "unknown" and not problems:
        problems.append("the VM's power state could not be read")
    return problems


# -- the controller's shutdown status -----------------------------------------------------
class StatusError(ValueError):
    pass


class ConfigurationError(RuntimeError):
    """The monitor's own configuration is unusable. Still a failed job, so still the alert."""


def cycle_start(now: datetime, special_starts: list) -> datetime:
    """The latest required startup at or before `now` (identical to status.py, tested)."""
    local = now.astimezone(IST)
    day = local.date()
    while True:
        start = datetime.combine(day, STARTUP, tzinfo=IST)
        if day.weekday() < 5 and start <= local:
            break
        day -= timedelta(days=1)
    earlier = [s.astimezone(IST) for s in special_starts if s.astimezone(IST) <= local]
    return max([start, *earlier])


def cycle_text(start: datetime) -> str:
    return start.astimezone(IST).strftime("%Y%m%d%H%M")


def special_starts(specials) -> list | None:
    """Each special session's VM start; None if any entry is unusable (no exemption)."""
    if special_list_problems(specials):
        return None
    return [_moment(entry["vm_start"]) for entry in specials]


def vm_digest(resource_id: str) -> str:
    return hashlib.sha256(resource_id.lower().encode()).hexdigest()[:12]


def decode_status(text, key: bytes) -> dict:
    if not isinstance(text, str) or len(text) > 256:
        raise StatusError("the status is missing or too long")
    parts = text.split(";")
    if len(parts) != len(STATUS_FIELDS) or parts[0] != STATUS_SCHEMA:
        raise StatusError("the status is not a qdps1 record")
    fields = dict(zip(STATUS_FIELDS, parts, strict=True))
    for name, pattern in STATUS_PATTERNS.items():
        if not re.fullmatch(pattern, fields[name]):
            raise StatusError(f"the status field {name} is malformed")
    body = text.rsplit(";", 1)[0]
    mac = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(fields["mac"], mac):
        raise StatusError("the status signature does not verify (forged or altered)")
    fields["seq"], fields["epoch"] = int(fields["seq"]), int(fields["epoch"])
    return fields


def evaluate_status(now: datetime, vm_id: str, state: str, tags: dict, key: bytes | None,
                    last: str | None, specials=()) -> tuple[list[str], str | None]:
    """(problems, the record to remember as the last one seen). Never passes on doubt."""
    if not key:
        return ["the shutdown-status key is not configured: the controller's status cannot "
                "be verified"], None
    text = tags.get(STATUS_TAG) if isinstance(tags, dict) else None
    if not text:
        return ["the controller has published no shutdown status"], None
    try:
        status = decode_status(text, key)
    except StatusError as exc:
        return [f"the shutdown status is unusable: {exc}"], None
    if status["vm"] != vm_digest(vm_id):
        return ["the shutdown status belongs to another VM"], None
    published = datetime.fromtimestamp(status["epoch"], UTC)
    if published > now + STATUS_SKEW:
        return [f"the shutdown status is dated in the future ({published:%Y-%m-%d %H:%M} UTC)"], \
            None
    if last:
        try:
            previous = decode_status(last, key)
        except StatusError:
            return ["the last remembered shutdown status does not verify; replay protection "
                    "is lost"], None
        if status["seq"] < previous["seq"]:
            return [f"the shutdown status is a replay: sequence {status['seq']} is older than "
                    f"{previous['seq']} already seen"], None
        if status["seq"] == previous["seq"] and text != last:
            return [f"two different shutdown statuses carry sequence {status['seq']}"], None
    problems = cycle_problems(now, status, published, specials)
    if status["flag"] in ALERT_FLAGS:
        problems.append(f"the controller reports {status['flag']} ({status['state']}, "
                        f"transaction {status['txn']}); the VM is {state}")
    if state == "running":
        if now - published > STATUS_MAX_AGE:
            problems.append(f"the shutdown status is stale: published "
                            f"{published:%Y-%m-%d %H:%M} UTC")
        if status["state"] in DEALLOCATED_TERMINAL and status["sub"] == status["boot"][:8]:
            problems.append("the VM runs again but its last status is the deallocation of the "
                            "boot that asked for it: no status since the restart")
    elif state == "deallocated" and status["state"] not in DEALLOCATION_SUBMITTED:
        problems.append(f"the VM is deallocated but the controller's last status is "
                        f"{status['state']}: deallocated outside the controller, or stale")
    return problems, text


def cycle_problems(now: datetime, status: dict, published: datetime, specials) -> list[str]:
    starts = special_starts(list(specials) if isinstance(specials, (list, tuple)) else None)
    if starts is None:
        return [f"{CONFIG}the special-session list is unusable, so the required startups are "
                "unknown: no downtime is exempted"]
    expected = cycle_start(now, starts)
    previous = cycle_start(expected - timedelta(minutes=1), starts)
    if status["cycle"] == "-":
        return ["the shutdown status names no operating cycle (the controller could not read "
                "its special-session holds)"]
    begins = datetime.strptime(status["cycle"], "%Y%m%d%H%M").replace(tzinfo=IST)
    if published < begins - STATUS_SKEW:
        return [f"the shutdown status was published before its own cycle "
                f"{status['cycle']} began"]
    if status["cycle"] == cycle_text(expected):
        return []
    if status["cycle"] == cycle_text(previous) and now < expected + CYCLE_GRACE:
        return []                               # the startup is still under way
    if begins > expected:
        return [f"the shutdown status names cycle {status['cycle']}, after the current "
                f"cycle {cycle_text(expected)}"]
    return [f"the shutdown status is from cycle {status['cycle']}, but a startup was required "
            f"at {expected:%a %Y-%m-%d %H:%M} IST: no evidence of that startup or of this "
            "cycle"]


# -- the Automation environment ---------------------------------------------------------
def _variable(name: str) -> str | None:
    try:
        import automationassets  # type: ignore  # provided by Azure Automation
        return automationassets.get_automation_variable(name)
    except ImportError:
        return os.environ.get(name)


def _set_variable(name: str, value: str) -> None:
    try:
        import automationassets  # type: ignore  # provided by Azure Automation
        automationassets.set_automation_variable(name, value)
    except ImportError:
        os.environ[name] = value


def _status_key() -> bytes | None:
    try:
        key = bytes.fromhex(_variable("QD_POWER_STATUS_KEY") or "")
    except ValueError:
        return None
    return key if len(key) >= 32 else None


def _token(opener=urllib.request.urlopen) -> str:
    endpoint, secret = os.environ.get("IDENTITY_ENDPOINT"), os.environ.get("IDENTITY_HEADER")
    if not endpoint or not secret:
        raise RuntimeError("no managed identity is available to this runbook")
    query = urllib.parse.urlencode({"resource": "https://management.azure.com/",
                                    "api-version": "2019-08-01"})
    request = urllib.request.Request(f"{endpoint}?{query}", headers={
        "X-IDENTITY-HEADER": secret, "Metadata": "true"})
    with opener(request, timeout=10) as response:
        return json.loads(response.read(65536))["access_token"]


def read_vm(vm_id: str, token: str, opener=urllib.request.urlopen) -> tuple[str, dict]:
    request = urllib.request.Request(
        f"{ARM}{vm_id}?$expand=instanceView&api-version={API_VERSION}",
        headers={"Authorization": f"Bearer {token}"})
    with opener(request, timeout=20) as response:
        body = json.loads(response.read(1024 * 1024))
    return power_state(body.get("properties", {}).get("instanceView", {})), body.get("tags") or {}


def main(now: datetime | None = None, opener=urllib.request.urlopen) -> int:
    vm_id = _variable("QD_VM_RESOURCE_ID")
    if not vm_id or not VM_ID.match(vm_id):
        raise RuntimeError("QD_VM_RESOURCE_ID is missing or malformed")
    raw = _variable("QD_SPECIAL_SESSIONS")
    # Absent is not "none": the variable must exist, holding [] when there are none.
    try:
        specials = None if raw is None else json.loads(raw)
    except ValueError as exc:
        raise ConfigurationError(f"QUANTDESK VM ALERT: {CONFIG}QD_SPECIAL_SESSIONS is not "
                                 "JSON") from exc
    state, tags = read_vm(vm_id, _token(opener), opener)
    now = now or datetime.now(IST)
    problems = evaluate(now, state, tags, specials)
    found, seen = evaluate_status(now, vm_id, state, tags, _status_key(),
                                  _variable("QD_POWER_STATUS_LAST"), specials)
    problems += found
    if seen is not None:
        _set_variable("QD_POWER_STATUS_LAST", seen)
    print(f"qd-monitor {now.astimezone(IST):%Y-%m-%d %H:%M} IST: power state {state}")
    if problems:
        # A failed job is the alert (qd-runbook-failed); a configuration problem is raised
        # as ConfigurationError, with every other problem found in the same run.
        error = ConfigurationError if any(p.startswith(CONFIG) for p in problems) \
            else RuntimeError
        raise error("QUANTDESK VM ALERT: " + " | ".join(problems))
    return 0


if __name__ == "__main__":
    sys.exit(main())
