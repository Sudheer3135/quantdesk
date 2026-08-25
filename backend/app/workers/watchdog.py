"""Scheduler health — the part of the outage that nothing was watching.

On 25-Aug-2026 the desk stopped collecting at 12:47 and nobody found out
until the end-of-session report ran three hours later. The container was up,
the process was healthy, the dashboard price was ticking, and APScheduler
had logged two hundred lines saying it was skipping runs.

That is the whole failure: `max_instances=1` is correct — two option
collectors polling NSE at once is exactly the burst that gets an IP blocked
— but the skip it produces is APScheduler's business and was reported on
APScheduler's logger, in APScheduler's words, at a level nothing escalated.
A lost collector cycle is a permanent hole in an archive that cannot be
rebuilt, and it was being recorded as library chatter.

So this listens to the scheduler and says, in the desk's own voice, that a
cycle was lost. It fixes nothing. It is the difference between a failure
that is noticed at 12:48 and one that is noticed at 15:37.

Two things it is careful not to do:

  **No alarm outside the session.** Every job here is gated on market hours
  and does nothing when the market is shut, so a skip at 21:00 costs
  nothing and an alarm about it is noise. Skips are still *counted* whenever
  they happen — only the log level moves — so a diagnostic can still see
  them, and a desk that is deliberately offline stays quiet.

  **No alarm for a single slow round trip.** One skip is a tick that took
  slightly longer than its interval, which happens. Consecutive skips are a
  job that cannot finish inside its schedule at all, which is starvation.
  Only the second gets an error.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
)
from apscheduler.schedulers.base import BaseScheduler

from ..market_hours import OPEN, session_label

log = logging.getLogger(__name__)

# Back-to-back skips before this is called starvation rather than one slow
# tick. Two, because one skip is ordinary jitter and three would mean
# waiting three intervals — three minutes of option chain — to say so.
STARVED_AFTER_SKIPS = 2


@dataclass
class JobHealth:
    """What has happened to one scheduled job since the process started."""

    job_id: str
    successes: int = 0
    errors: int = 0
    skips: int = 0                       # max_instances: a run never started
    missed: int = 0                      # misfire: run time came and went
    consecutive_skips: int = 0
    last_success: datetime | None = None
    last_skip: datetime | None = None
    last_error: str | None = None

    @property
    def starved(self) -> bool:
        return self.consecutive_skips >= STARVED_AFTER_SKIPS

    def to_dict(self) -> dict:
        out = asdict(self)
        for key in ("last_success", "last_skip"):
            out[key] = out[key].isoformat() if out[key] else None
        out["starved"] = self.starved
        return out


class Watchdog:
    """Scheduler events, counted per job.

    Process-local and in memory on purpose. This answers "is the desk
    collecting *right now*", which is a question about this process; a
    durable record of what was collected already exists, in the candle and
    option tables that `data.quality` inspects.
    """

    EVENTS = (EVENT_JOB_EXECUTED | EVENT_JOB_ERROR
              | EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES)

    def __init__(self) -> None:
        self._jobs: dict[str, JobHealth] = {}
        self._lock = threading.Lock()
        # When counting began. Every number here is "since this moment", not
        # "this session", and a reader that does not know the moment cannot
        # tell a healthy job from one that started at 14:00 — so the window
        # is published beside the counts rather than left to be assumed.
        self.started_at: datetime | None = None

    # ---- wiring ---------------------------------------------------------
    def attach(self, scheduler: BaseScheduler) -> None:
        scheduler.add_listener(self.on_event, self.EVENTS)
        self.started_at = datetime.now(UTC)
        log.info("scheduler watchdog attached")

    def reset(self) -> None:
        with self._lock:
            self._jobs.clear()
            self.started_at = None

    def _job(self, job_id: str) -> JobHealth:
        health = self._jobs.get(job_id)
        if health is None:
            health = self._jobs[job_id] = JobHealth(job_id=job_id)
        return health

    # ---- events ---------------------------------------------------------
    def on_event(self, event) -> None:
        now = datetime.now(UTC)
        during_session = session_label() == OPEN

        with self._lock:
            health = self._job(event.job_id)

            if event.code == EVENT_JOB_MAX_INSTANCES:
                health.skips += 1
                health.consecutive_skips += 1
                health.last_skip = now
                self._report_skip(health, during_session)
                return

            if event.code == EVENT_JOB_MISSED:
                health.missed += 1
                # A misfire is the scheduler falling behind rather than a job
                # holding its own slot, but it costs the same cycle.
                (log.warning if during_session else log.debug)(
                    "QuantDesk job %r missed its run time — that cycle's data "
                    "was not collected.", event.job_id)
                return

            # The job actually ran. Whatever it did, it is no longer holding
            # its slot, so the starvation streak is over.
            health.consecutive_skips = 0
            if event.code == EVENT_JOB_ERROR:
                health.errors += 1
                health.last_error = repr(event.exception)
                log.error("QuantDesk job %r raised: %r", event.job_id, event.exception)
            else:
                health.successes += 1
                health.last_success = now

    def _report_skip(self, health: JobHealth, during_session: bool) -> None:
        """Say a cycle was lost, at a level that matches what it cost."""
        if not during_session:
            log.debug("job %r skipped a run outside market hours (%s total)",
                      health.job_id, health.skips)
            return

        message = (
            "QuantDesk job %r SKIPPED a scheduled run: the previous one is "
            "still going and max_instances=1. %s in a row, %s today. This "
            "cycle's data was not collected and option snapshots cannot be "
            "backfilled — check for a hung outbound request."
        )
        args = (health.job_id, health.consecutive_skips, health.skips)
        if health.starved:
            log.error(message, *args)
        else:
            log.warning(message, *args)

    # ---- reading --------------------------------------------------------
    def snapshot(self) -> dict[str, JobHealth]:
        with self._lock:
            return dict(self._jobs)

    def problems(self) -> list[str]:
        """Human-readable faults, empty when the scheduler is keeping up."""
        found = []
        for health in self.snapshot().values():
            if health.starved:
                found.append(
                    f"{health.job_id}: {health.consecutive_skips} consecutive "
                    f"skipped runs — the job cannot finish inside its interval")
            if health.errors and health.successes == 0:
                found.append(
                    f"{health.job_id}: {health.errors} error(s) and no successful "
                    f"run — last was {health.last_error}")
        return found

    def report(self) -> dict:
        jobs = self.snapshot()
        problems = self.problems()
        return {
            # False only for a fault worth acting on. A job that has skipped
            # once and recovered is not a fault.
            "healthy": not problems,
            "market_open": session_label() == OPEN,
            # The window the counts below cover. None means the scheduler was
            # never attached in this process, which is a different statement
            # from "nothing has run yet" and must not be read as either
            # health or fault.
            "counting_since": (self.started_at.isoformat()
                               if self.started_at else None),
            "jobs": {job_id: health.to_dict() for job_id, health in jobs.items()},
            "problems": problems,
        }


# One per process, attached to the one scheduler `main.lifespan` builds.
WATCHDOG = Watchdog()


def attach(scheduler: BaseScheduler) -> Watchdog:
    WATCHDOG.attach(scheduler)
    return WATCHDOG


def report() -> dict:
    return WATCHDOG.report()
