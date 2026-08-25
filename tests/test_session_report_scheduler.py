"""Scheduler health in the end-of-session report, and what it refuses to say.

The report already read what was *stored*. This adds the one thing the
stored data can only imply: were scheduled cycles being lost while the
session ran. On 24 and 25-Aug-2026 the answer was yes, and the only trace
was two hundred APScheduler skip lines nobody escalated.

Almost every test here is about a refusal rather than a measurement, because
the watchdog counts since the *process* started and this report is about a
*session*, and those two windows are not the same. Getting that wrong
produces exactly the finding this whole script exists to avoid:

  - a desk restarted at 14:00 reported as having missed the morning
  - a session the counters never covered reported as clean
  - a desk that was deliberately switched off reported as broken

So the observation window is the overlap of the two, expectations are
measured against that overlap and nothing else, and when the overlap is
empty the answer is "unknown" — never a number.

`test_an_unreachable_desk_is_never_counted_against_the_session` is the one
that matters most. A report that grades an offline desk as failing is a
report that fires every night and is therefore read on no morning.
"""
import importlib.util
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.market_hours import IST  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "session_report", ROOT / "scripts" / "session_report.py")
session_report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session_report)

SESSION = date(2026, 8, 25)                     # a Tuesday
OPEN = datetime(2026, 8, 25, 9, 15, tzinfo=IST)
CLOSE = datetime(2026, 8, 25, 15, 30, tzinfo=IST)
AFTER = datetime(2026, 8, 25, 15, 37, tzinfo=IST)

# 09:15 to 15:30 is 375 minutes: 75 agent ticks, 375 collector polls, 4500
# ticker polls. Those are the numbers a full session should produce.
FULL = {"nifty-agent": 75, "option-collector": 375, "price-ticker": 4500}


def job(successes, skips=0, errors=0, starved=False, consecutive=0):
    return {"successes": successes, "skips": skips, "errors": errors,
            "missed": 0, "starved": starved, "consecutive_skips": consecutive,
            "last_success": None}


def payload(jobs=None, since=OPEN, healthy=True, problems=None):
    return {
        "healthy": healthy,
        "market_open": False,
        "counting_since": since.astimezone(UTC).isoformat() if since else None,
        "jobs": jobs if jobs is not None else {},
        "problems": problems or [],
    }


def health(jobs=None, since=OPEN, now=AFTER, **kw):
    return session_report.scheduler_health(
        SESSION, payload(jobs, since, **kw), None, now=now)


# ---------------------------------------------------------------------------
# the app is up and the counters cover the session
# ---------------------------------------------------------------------------

def test_a_full_healthy_session_reports_every_job(monkeypatch):
    block = health({name: job(count) for name, count in FULL.items()})

    assert block["available"] is True
    assert block["covers_session"] is True
    assert block["healthy"] is True
    for name, expected in FULL.items():
        assert block["jobs"][name]["expected"] == expected, name
        assert block["jobs"][name]["completed"] == expected, name
        assert block["jobs"][name]["skipped"] == 0, name
    assert block["starvation"] == []


def test_expectations_come_from_each_jobs_own_interval():
    """5 minutes, 60 seconds and 5 seconds across a 375-minute session."""
    block = health({name: job(0) for name in FULL})
    assert {n: j["expected"] for n, j in block["jobs"].items()} == FULL


def test_skipped_runs_are_reported_as_starvation_events():
    block = health({"option-collector": job(180, skips=195)})
    assert block["starvation"] == [
        "option-collector: 195 run(s) skipped because the previous one was "
        "still going"]


def test_a_degraded_scheduler_says_so():
    block = health({"option-collector": job(180, skips=195, starved=True)},
                   healthy=False, problems=["option-collector: starved"])
    assert block["healthy"] is False


# ---------------------------------------------------------------------------
# the window, which is the whole difficulty
# ---------------------------------------------------------------------------

def test_a_process_started_mid_session_is_only_judged_on_what_it_saw():
    """The desk restarted at 14:00. It cannot be blamed for the morning, and
    saying it completed 20 of 75 runs would blame it for exactly that."""
    block = health({"nifty-agent": job(18)},
                   since=datetime(2026, 8, 25, 14, 0, tzinfo=IST))

    assert block["jobs"]["nifty-agent"]["expected"] == 18   # 90 minutes / 5
    assert block["observed_share"] == pytest.approx(90 / 375, abs=0.01)


def test_counters_that_postdate_the_session_are_not_used_at_all():
    """The real case the day this was written: the report ran at 17:37
    against a process restarted at 17:32. Zero skips there is not evidence
    of a healthy session — it is evidence of nothing."""
    block = health({"nifty-agent": job(0)},
                   since=datetime(2026, 8, 25, 17, 32, tzinfo=IST),
                   now=datetime(2026, 8, 25, 17, 37, tzinfo=IST))

    assert block["covers_session"] is False
    assert block["jobs"] == {}
    assert "say nothing about this one" in block["note"]


def test_counters_from_before_a_past_session_are_clipped_to_it():
    """A long-running process reporting on an older session must be measured
    on that session's hours, not on its own uptime."""
    block = health({"nifty-agent": job(75)},
                   since=datetime(2026, 8, 1, 9, 0, tzinfo=IST),
                   now=datetime(2026, 8, 30, 9, 0, tzinfo=IST))

    assert block["observed_share"] == 1.0
    assert block["jobs"]["nifty-agent"]["expected"] == 75


def test_a_session_still_in_progress_expects_only_the_hours_so_far():
    block = health({"nifty-agent": job(9)},
                   now=datetime(2026, 8, 25, 10, 0, tzinfo=IST))
    assert block["jobs"]["nifty-agent"]["expected"] == 9      # 45 minutes / 5


# ---------------------------------------------------------------------------
# the refusals
# ---------------------------------------------------------------------------

def test_an_unreachable_desk_reports_unknown_not_broken():
    block = session_report.scheduler_health(
        SESSION, None, "ConnectError: [Errno 111] Connection refused")

    assert block["available"] is False
    assert block["covers_session"] is False
    assert block["jobs"] == {}
    assert "Connection refused" in block["reason"]
    # Crucially: no claim either way.
    assert "healthy" not in block


def test_an_app_whose_scheduler_never_started_is_not_a_silent_zero():
    """Counts of zero from a process with no scheduler mean "not running",
    not "ran and did nothing"."""
    block = health(since=None)
    assert block["covers_session"] is False
    assert block["jobs"] == {}
    assert "has not started" in block["note"]


def test_a_job_the_scheduler_never_saw_is_named_rather_than_assumed():
    block = health({"price-ticker": job(4500)})
    agent = block["jobs"]["nifty-agent"]
    assert agent["completed"] == 0
    assert agent["starved"] is None
    assert "no record of this job running" in agent["note"]


# ---------------------------------------------------------------------------
# fetching, which must never raise
# ---------------------------------------------------------------------------

def test_a_refused_connection_is_a_reason_not_an_exception(monkeypatch):
    import httpx

    def refuse(*a, **k):
        raise httpx.ConnectError("[Errno 111] Connection refused")

    monkeypatch.setattr(httpx, "get", refuse)
    body, reason = session_report.fetch_scheduler("http://localhost:9999")
    assert body is None
    assert "Connection refused" in reason


def test_a_non_200_is_a_reason_not_a_payload(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "get",
                        lambda *a, **k: httpx.Response(503, text="down"))
    body, reason = session_report.fetch_scheduler("http://localhost:8000")
    assert (body, reason) == (None, "HTTP 503")


def test_a_non_json_body_is_a_reason_not_a_crash(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "get",
                        lambda *a, **k: httpx.Response(200, text="<html>"))
    body, reason = session_report.fetch_scheduler("http://localhost:8000")
    assert body is None
    assert "did not return JSON" in reason


def test_a_hung_endpoint_cannot_hang_the_report(monkeypatch):
    """This runs unattended after the close and must always print."""
    seen = {}

    def record(url, timeout=None, **k):
        seen["timeout"] = timeout
        raise TimeoutError("read timeout")

    import httpx
    monkeypatch.setattr(httpx, "get", record)
    body, reason = session_report.fetch_scheduler("http://localhost:8000")
    assert body is None and "TimeoutError" in reason
    assert seen["timeout"] == session_report.SCHEDULER_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# how it lands in the report
# ---------------------------------------------------------------------------

def seed_a_working_session(db):
    """Enough stored data that the report has no complaint of its own, so a
    scheduler finding is visibly the scheduler's and not a side effect."""
    import numpy as np
    import pandas as pd
    from app.data.importer import import_index_candles

    stamps = [(OPEN + timedelta(minutes=5 * i)).astimezone(UTC) for i in range(75)]
    close = 24_000 + np.arange(75, dtype=float)
    import_index_candles(db, pd.DataFrame({
        "timestamp": stamps, "open": close, "high": close + 5,
        "low": close - 5, "close": close, "volume": [1000.0] * 75,
    }), "NIFTY", "5m", "test")


def report_with(db, scheduler):
    return session_report.check(db, SESSION, "NIFTY", "5m", scheduler)


def test_an_unreachable_desk_is_never_counted_against_the_session(db):
    """The property this whole section turns on.

    A report that grades a deliberately-offline desk as failing is a report
    that fires every night, and one that fires every night is read on no
    morning. The loss lines above already catch a desk that was off when it
    should have been collecting; the scheduler block must add nothing.
    """
    seed_a_working_session(db)
    offline = session_report.scheduler_health(SESSION, None, "Connection refused")
    report = report_with(db, offline)

    assert not [p for p in report["problems"] if "scheduler" in p.lower()]
    assert report["scheduler"]["available"] is False


def test_counters_that_miss_the_session_add_no_problems(db):
    seed_a_working_session(db)
    report = report_with(db, health(
        {"nifty-agent": job(0)},
        since=datetime(2026, 8, 25, 17, 32, tzinfo=IST),
        now=datetime(2026, 8, 25, 17, 37, tzinfo=IST)))

    assert not [p for p in report["problems"] if "scheduler" in p.lower()]


def test_starvation_becomes_a_reported_problem(db):
    seed_a_working_session(db)
    report = report_with(db, health(
        {"option-collector": job(180, skips=195, starved=True, consecutive=12)}))

    starved = [p for p in report["problems"] if "starved" in p.lower()]
    assert starved, report["problems"]
    assert "12 consecutive runs skipped" in starved[0]
    assert report["ok"] is False


def test_a_job_far_behind_its_due_count_is_reported(db):
    seed_a_working_session(db)
    report = report_with(db, health({"nifty-agent": job(37)}))

    behind = [p for p in report["problems"] if "nifty-agent" in p]
    assert behind, report["problems"]
    assert "37 of about 75" in behind[0]


def test_a_healthy_scheduler_adds_nothing(db):
    seed_a_working_session(db)
    report = report_with(db, health({n: job(c) for n, c in FULL.items()}))
    assert not [p for p in report["problems"] if "scheduler" in p.lower()]


def test_omitting_the_scheduler_leaves_the_report_exactly_as_it_was(db):
    """`--no-api`, and every existing caller. The section is additive."""
    seed_a_working_session(db)
    report = session_report.check(db, SESSION, "NIFTY", "5m")
    assert "scheduler" not in report


def test_a_non_trading_day_asks_nothing_of_the_scheduler(db):
    """Saturday. Nothing was expected, so nothing is judged."""
    report = session_report.check(db, date(2026, 8, 23), "NIFTY", "5m",
                                  health({n: job(0) for n in FULL}))
    assert report["trading_day"] is False
    assert "scheduler" not in report
    assert report["problems"] == []


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def test_an_unknown_scheduler_renders_as_unknown_not_as_absence():
    """A missing section is indistinguishable from a healthy one, which is
    the confusion that let two sessions drain away."""
    lines = session_report.render_scheduler(
        session_report.scheduler_health(SESSION, None, "Connection refused"))
    text = "\n".join(lines)
    assert "unknown" in text
    assert "no cycle counts are claimed" in text


def test_a_healthy_scheduler_renders_every_job():
    lines = session_report.render_scheduler(
        health({n: job(c) for n, c in FULL.items()}))
    text = "\n".join(lines)
    assert "healthy" in text
    for name in FULL:
        assert name in text


def test_a_degraded_scheduler_renders_the_starvation_events():
    lines = session_report.render_scheduler(health(
        {"option-collector": job(180, skips=195, starved=True, consecutive=12)},
        healthy=False))
    text = "\n".join(lines)
    assert "DEGRADED" in text
    assert "STARVED" in text
    assert "195 run(s) skipped" in text


def test_no_scheduler_block_renders_no_lines():
    assert session_report.render_scheduler(None) == []
