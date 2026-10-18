"""Special sessions (for example Muhurat trading on a Sunday): explicit monitoring deadlines.

The weekday schedules do not cover a special session. An operator records one
`quantdesk_special_session/1` file per reviewed special session and runs
`power.py plan-special` on it. The plan states, for that date only:

  - the one-off Azure Automation start (and that the weekday start does not run);
  - the times by which the VM must be running and the first heartbeat must have
    arrived, and the times the Azure monitor checks it (entries for its
    QD_SPECIAL_SESSIONS variable, plus one-off monitor schedules);
  - a KEEP_RUNNING power hold covering the session, so no shutdown is attempted
    until it has passed;
  - the OnCalendar lines an operator adds by hand to the hosted liveness check
    for that date (the VM never edits or pauses a hosted check).

Nothing here is applied automatically. A plan whose times are out of order or
off the session's date is refused.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .facts import IST

SCHEMA = "quantdesk_special_session/1"
FIELDS = {"schema", "session_date", "label", "official_reference", "vm_start_ist",
          "first_heartbeat_by_ist", "session_open_ist", "session_close_ist",
          "keep_running_until_ist", "authorization_reference", "recorded_by",
          "recorded_at_utc"}
MIN_BOOT = timedelta(minutes=10)
GRACE = timedelta(minutes=10)


class PlanError(ValueError):
    pass


def _moment(record: dict, key: str) -> datetime:
    try:
        value = datetime.fromisoformat(record[key])
    except (TypeError, ValueError) as exc:
        raise PlanError(f"{key} is not a timestamp") from exc
    if value.tzinfo is None:
        raise PlanError(f"{key} has no timezone")
    return value.astimezone(IST)


def plan(record: dict) -> dict:
    if not isinstance(record, dict) or set(record) != FIELDS or record["schema"] != SCHEMA:
        raise PlanError(f"not a complete {SCHEMA} record")
    for key in ("label", "official_reference", "authorization_reference", "recorded_by"):
        if not isinstance(record[key], str) or not record[key].strip():
            raise PlanError(f"{key} must be given")
    try:
        day = date.fromisoformat(record["session_date"])
    except (TypeError, ValueError) as exc:
        raise PlanError("session_date is not a date") from exc
    start = _moment(record, "vm_start_ist")
    beat = _moment(record, "first_heartbeat_by_ist")
    opens = _moment(record, "session_open_ist")
    closes = _moment(record, "session_close_ist")
    until = _moment(record, "keep_running_until_ist")
    _moment(record, "recorded_at_utc")
    if not (start + MIN_BOOT <= beat <= opens < closes < until):
        raise PlanError("times must satisfy start + 10 min <= first heartbeat <= open < close "
                        "< keep-running-until")
    if start.date() != day or opens.date() != day or closes.date() != day:
        raise PlanError("the start, open and close must fall on the session date")
    checks = sorted({beat, opens + timedelta(minutes=30), closes, until - timedelta(minutes=5)})
    first_hour_minutes = ",".join(f"{m:02d}" for m in range(beat.minute, 60, 5))
    liveness_lines = [f"{day.isoformat()} {beat.hour:02d}:{first_hour_minutes}"]
    if until.hour > beat.hour + 1:
        liveness_lines.append(f"{day.isoformat()} {beat.hour + 1:02d}..{until.hour - 1:02d}:00/5")
    return {
        "session_date": day.isoformat(), "label": record["label"],
        "azure_start": {"schedule_name": f"qd-special-start-{day.isoformat()}",
                        "frequency": "OneTime", "time_zone": "Asia/Kolkata",
                        "start_time": start.isoformat(), "runbook": "qd-start"},
        "monitor": {
            "variable_entry": {"date": day.isoformat(), "vm_start": start.isoformat(),
                               "expect_running_from": beat.isoformat(),
                               "expect_running_until": until.isoformat()},
            "one_off_schedules": [{"schedule_name": f"qd-special-monitor-{day}-{t:%H%M}",
                                   "start_time": t.isoformat(), "runbook": "qd-monitor"}
                                  for t in checks]},
        "deadlines": {"vm_running_by": beat.isoformat(),
                      "first_heartbeat_by": beat.isoformat(),
                      "first_heartbeat_alert_by": (beat + GRACE).isoformat(),
                      "no_shutdown_before": until.isoformat()},
        "power_hold": {"schema": "quantdesk_power_hold/1",
                       "hold_id": f"special-{day.isoformat()}", "kind": "KEEP_RUNNING",
                       "reason_code": "SPECIAL_SESSION", "from_ist": start.isoformat(),
                       "to_ist": until.isoformat(),
                       "authorization_reference": record["authorization_reference"],
                       "recorded_by": record["recorded_by"],
                       "recorded_at_utc": record["recorded_at_utc"]},
        "healthchecks_manual": {
            "check": "liveness",
            "add_oncalendar_lines": liveness_lines,
            "note": "added by an operator in the hosted dashboard before the session and "
                    "removed after it; the VM never changes or pauses a hosted check. The "
                    "assessment checks are weekday schedules and are not changed; a special "
                    "session's assessment schedule needs its own review."},
    }
