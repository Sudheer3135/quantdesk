"""Making a lost scheduler cycle audible.

On 25-Aug-2026 collection stopped at 12:47 and nobody knew until the
end-of-session report ran at 15:37. The container was up, the process was
healthy, the dashboard price was ticking, and APScheduler had written two
hundred "maximum number of running instances reached" lines — on its own
logger, in its own words, at a level nothing escalated.

`max_instances=1` is right: two option collectors polling NSE at once is the
burst that gets an IP blocked. What was wrong is that the skip it produces —
a permanent hole in an archive that cannot be rebuilt — was being recorded
as library chatter.

Two properties matter as much as the detection itself:

`test_skips_outside_market_hours_do_not_raise_an_alarm` — every job here is
gated on the session and does nothing when the market is shut, so an alarm
then is noise, and a monitor that cries all night is one nobody reads.

`test_a_desk_that_has_never_run_a_job_is_not_a_fault` — a desk deliberately
offline must not look like a broken one.
"""
import logging
import sys
import threading
import time
from pathlib import Path

import pytest
from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
)
from apscheduler.schedulers.background import BackgroundScheduler

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.market_hours import CLOSED, OPEN
from app.workers import watchdog as wd


class Event:
    """The two attributes the listener reads. APScheduler's own event classes
    differ between skip and execution; only these are in common."""

    def __init__(self, code, job_id="option-collector", exception=None):
        self.code = code
        self.job_id = job_id
        self.exception = exception


@pytest.fixture
def during_session(monkeypatch):
    monkeypatch.setattr(wd, "session_label", lambda *a, **k: OPEN)


@pytest.fixture
def after_hours(monkeypatch):
    monkeypatch.setattr(wd, "session_label", lambda *a, **k: CLOSED)


@pytest.fixture
def dog():
    return wd.Watchdog()


def skip(dog, job_id="option-collector", times=1):
    for _ in range(times):
        dog.on_event(Event(EVENT_JOB_MAX_INSTANCES, job_id))


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------

def test_a_skipped_run_is_counted(dog, during_session):
    skip(dog)
    health = dog.snapshot()["option-collector"]
    assert (health.skips, health.consecutive_skips) == (1, 1)


def test_one_skip_warns_and_names_the_job(dog, during_session, caplog):
    with caplog.at_level(logging.WARNING, logger=wd.log.name):
        skip(dog)
    assert "option-collector" in caplog.text
    assert "SKIPPED" in caplog.text
    # It has to say what it cost, or it reads as a retry.
    assert "cannot be backfilled" in caplog.text


def test_consecutive_skips_escalate_to_an_error(dog, during_session, caplog):
    """One skip is a tick that ran slightly long. Two in a row is a job that
    cannot finish inside its schedule at all, which is the failure."""
    with caplog.at_level(logging.DEBUG, logger=wd.log.name):
        skip(dog, times=wd.STARVED_AFTER_SKIPS)
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert dog.snapshot()["option-collector"].starved


def test_a_starved_job_makes_the_scheduler_report_unhealthy(dog, during_session):
    skip(dog, times=wd.STARVED_AFTER_SKIPS)
    report = dog.report()
    assert report["healthy"] is False
    assert any("consecutive skipped runs" in p for p in report["problems"])


def test_a_completed_run_ends_the_streak(dog, during_session):
    """The job got its slot back. Whatever it did, it is no longer starved."""
    skip(dog, times=wd.STARVED_AFTER_SKIPS)
    dog.on_event(Event(EVENT_JOB_EXECUTED))
    health = dog.snapshot()["option-collector"]
    assert health.consecutive_skips == 0
    assert not health.starved
    assert dog.report()["healthy"] is True
    # The history is kept even though the alarm cleared.
    assert health.skips == wd.STARVED_AFTER_SKIPS


def test_a_missed_run_is_reported_separately_from_a_skip(dog, during_session):
    dog.on_event(Event(EVENT_JOB_MISSED))
    health = dog.snapshot()["option-collector"]
    assert (health.missed, health.skips) == (1, 0)


def test_a_raising_job_is_recorded_with_its_exception(dog, during_session):
    dog.on_event(Event(EVENT_JOB_ERROR, exception=RuntimeError("NSE 401")))
    health = dog.snapshot()["option-collector"]
    assert health.errors == 1
    assert "NSE 401" in health.last_error


def test_a_job_that_only_ever_errors_is_a_problem(dog, during_session):
    dog.on_event(Event(EVENT_JOB_ERROR, exception=RuntimeError("boom")))
    assert dog.report()["healthy"] is False


def test_jobs_are_tracked_apart(dog, during_session):
    skip(dog, "option-collector", times=3)
    dog.on_event(Event(EVENT_JOB_EXECUTED, "price-ticker"))
    snap = dog.snapshot()
    assert snap["option-collector"].starved
    assert not snap["price-ticker"].starved


# ---------------------------------------------------------------------------
# no false alarms
# ---------------------------------------------------------------------------

def test_skips_outside_market_hours_do_not_raise_an_alarm(dog, after_hours, caplog):
    """Every job is gated on the session and does nothing when the market is
    shut. An alarm then is noise, and noise is how the real one gets ignored."""
    with caplog.at_level(logging.DEBUG, logger=wd.log.name):
        skip(dog, times=5)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_skips_outside_market_hours_are_still_counted(dog, after_hours):
    """Silent, not blind. Only the log level moves — a diagnostic looking
    later can still see what happened."""
    skip(dog, times=5)
    assert dog.snapshot()["option-collector"].skips == 5


def test_a_desk_that_has_never_run_a_job_is_not_a_fault(dog, after_hours):
    """A deliberately offline desk must not look like a broken one."""
    report = dog.report()
    assert report["healthy"] is True
    assert report["jobs"] == {}
    assert report["problems"] == []


def test_a_healthy_session_reports_nothing(dog, during_session):
    for _ in range(20):
        dog.on_event(Event(EVENT_JOB_EXECUTED))
    assert dog.report()["healthy"] is True


# ---------------------------------------------------------------------------
# end to end, against a real scheduler
# ---------------------------------------------------------------------------

def test_a_job_that_overruns_its_interval_is_actually_detected(during_session):
    """The failure reproduced rather than simulated: a real scheduler, a real
    `max_instances=1` job that takes longer than its own interval."""
    dog = wd.Watchdog()
    scheduler = BackgroundScheduler(timezone="UTC")
    dog.attach(scheduler)

    running = threading.Event()

    def slow():
        running.set()
        time.sleep(1.0)

    scheduler.add_job(slow, "interval", seconds=0.1, id="hung-job",
                      max_instances=1, coalesce=True)
    scheduler.start()
    try:
        assert running.wait(5), "the job never started"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            health = dog.snapshot().get("hung-job")
            if health is not None and health.starved:
                break
            time.sleep(0.05)
    finally:
        scheduler.shutdown(wait=False)

    health = dog.snapshot()["hung-job"]
    assert health.skips >= 1, "a real overrun produced no skip"
    assert health.starved, f"{health.consecutive_skips} consecutive skips"
