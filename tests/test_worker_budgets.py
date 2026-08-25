"""Each scheduled job bounds its own outbound work by its own interval.

The three jobs share one scheduler and all three carry `max_instances=1`, so
a job that overruns its interval does not run late — its next run is skipped
outright. On 24 and 25-Aug-2026 that cost a full session and half a session
of option snapshots, and 37 signals against 76 index bars, while the process
stayed up and the dashboard price kept ticking.

The bound has to come from the schedule rather than from whoever wrote the
request, which is what these check: the collector's budget is a fact about
polling every sixty seconds, not an opinion about how patient to be with
NSE.

`test_a_collector_that_runs_out_of_time_does_not_kill_the_job` is the one
that matters most. Option history only accumulates forward, so a poll that
raises out of the job is not one lost observation — it is every observation
after it.
"""
import logging
import sys
from pathlib import Path

import pytest
from apscheduler.events import EVENT_JOB_MAX_INSTANCES
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app import net
from app.api import health as health_api
from app.config import get_settings
from app.workers import agent, option_collector, ticker
from app.workers import watchdog as wd


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("BROKER", "mock")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class Recorder:
    """A broker that records the budget in force when it is called."""

    def __init__(self, boom=None):
        self.budgets = []
        self.boom = boom

    def _seen(self):
        deadline = net.current()
        self.budgets.append(None if deadline is None else deadline.budget)
        if self.boom:
            raise self.boom

    def chain_with_spot(self, *a, **k):
        self._seen()

    def quote(self, *a, **k):
        self._seen()
        return {"last_price": 24_000.0, "source": "test", "source_time": None}

    def candles(self, *a, **k):
        self._seen()


# ---------------------------------------------------------------------------
# every tick runs inside a budget derived from its own interval
# ---------------------------------------------------------------------------

def test_the_collectors_budget_comes_from_its_polling_interval(monkeypatch):
    broker = Recorder()
    monkeypatch.setattr(option_collector, "get_broker", lambda: broker)
    monkeypatch.setattr(option_collector, "market_is_open", lambda *a, **k: True)

    option_collector.tick()

    interval = get_settings().option_snapshot_interval_seconds
    assert broker.budgets == [net.budget_for(interval)]
    assert broker.budgets[0] < interval, "a poll may outlast its own schedule"


def test_the_tickers_budget_comes_from_its_polling_interval(monkeypatch):
    broker = Recorder()
    monkeypatch.setattr(ticker, "get_broker", lambda: broker)

    ticker.tick()

    interval = get_settings().ticker_interval_seconds
    assert broker.budgets == [net.budget_for(interval)]
    assert broker.budgets[0] < interval


def test_the_agents_budget_comes_from_its_own_interval(monkeypatch):
    seen = []
    monkeypatch.setattr(agent, "_analyse",
                        lambda s: seen.append(net.current().budget))
    monkeypatch.setattr(agent, "market_is_open", lambda *a, **k: True)

    agent.tick()

    interval_seconds = get_settings().agent_interval_minutes * 60
    assert seen == [net.budget_for(interval_seconds)]
    assert seen[0] < interval_seconds


def test_the_cadences_themselves_are_unchanged():
    """The fix is a bound on how long a tick may take, not a change to how
    often it fires. 5s ticker, 60s collector, 5m agent."""
    s = get_settings()
    assert s.ticker_interval_seconds == 5
    assert s.option_snapshot_interval_seconds == 60
    assert s.agent_interval_minutes == 5


# ---------------------------------------------------------------------------
# running out of time must not retire the job
# ---------------------------------------------------------------------------

def test_a_collector_that_runs_out_of_time_does_not_kill_the_job(monkeypatch, caplog):
    """Option history only accumulates forward. A poll that raises out of the
    job costs every observation after it, not just this one."""
    broker = Recorder(boom=net.BudgetExhausted("out of time"))
    monkeypatch.setattr(option_collector, "get_broker", lambda: broker)
    monkeypatch.setattr(option_collector, "market_is_open", lambda *a, **k: True)

    with caplog.at_level(logging.WARNING, logger=option_collector.log.name):
        option_collector.tick()               # must not raise

    assert "gave up to keep the schedule" in caplog.text


def test_a_collector_that_fails_outright_does_not_kill_the_job(monkeypatch):
    broker = Recorder(boom=RuntimeError("NSE 401"))
    monkeypatch.setattr(option_collector, "get_broker", lambda: broker)
    monkeypatch.setattr(option_collector, "market_is_open", lambda *a, **k: True)
    option_collector.tick()


def test_the_next_poll_starts_with_a_full_budget(monkeypatch):
    """The point of giving the slot back. A tick that spent its whole budget
    must not hand the remainder — none — to the tick after it."""
    broker = Recorder(boom=net.BudgetExhausted("out of time"))
    monkeypatch.setattr(option_collector, "get_broker", lambda: broker)
    monkeypatch.setattr(option_collector, "market_is_open", lambda *a, **k: True)

    option_collector.tick()
    option_collector.tick()

    assert len(broker.budgets) == 2
    assert broker.budgets[0] == broker.budgets[1]
    assert net.current() is None, "a budget leaked out of the tick"


# ---------------------------------------------------------------------------
# market-closed behaviour is unchanged
# ---------------------------------------------------------------------------

def test_a_closed_market_costs_no_request_and_no_budget(monkeypatch):
    """Polling a shut market wastes requests on data that has not changed and
    risks being throttled for it."""
    broker = Recorder()
    monkeypatch.setattr(option_collector, "get_broker", lambda: broker)
    monkeypatch.setattr(option_collector, "market_is_open", lambda *a, **k: False)

    option_collector.tick()
    assert broker.budgets == []


def test_the_agent_does_not_tick_a_closed_market(monkeypatch):
    seen = []
    monkeypatch.setattr(agent, "_analyse", lambda s: seen.append(1))
    monkeypatch.setattr(agent, "market_is_open", lambda *a, **k: False)

    agent.tick()
    assert seen == []


# ---------------------------------------------------------------------------
# the health endpoint
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(health_api.router)
    return TestClient(app)


def test_scheduler_health_is_queryable(client, monkeypatch):
    monkeypatch.setattr(wd, "session_label", lambda *a, **k: "open")
    wd.WATCHDOG.reset()

    body = client.get("/health/scheduler").json()
    assert body == {"healthy": True, "market_open": True,
                    "counting_since": None, "jobs": {}, "problems": []}


def test_scheduler_health_reports_a_starved_job(client, monkeypatch):
    """`/health` stayed "ok" through both lost sessions, because the process
    genuinely was up. This is the endpoint that would not have."""
    monkeypatch.setattr(wd, "session_label", lambda *a, **k: "open")
    wd.WATCHDOG.reset()
    for _ in range(wd.STARVED_AFTER_SKIPS):
        wd.WATCHDOG.on_event(
            type("E", (), {"code": EVENT_JOB_MAX_INSTANCES,
                           "job_id": "option-collector",
                           "exception": None})())

    body = client.get("/health/scheduler").json()
    wd.WATCHDOG.reset()

    assert body["healthy"] is False
    assert body["jobs"]["option-collector"]["starved"] is True


def test_the_endpoint_publishes_the_window_its_counts_cover(monkeypatch):
    """Counts are since the scheduler was attached, not since the session
    opened. A reader that cannot see the window cannot tell a healthy job
    from one that started at 14:00 — so the window travels with the counts.
    """
    from apscheduler.schedulers.background import BackgroundScheduler

    monkeypatch.setattr(wd, "session_label", lambda *a, **k: "open")
    dog = wd.Watchdog()
    assert dog.report()["counting_since"] is None, "never attached, no window"

    scheduler = BackgroundScheduler(timezone="UTC")
    dog.attach(scheduler)
    assert dog.report()["counting_since"] is not None
