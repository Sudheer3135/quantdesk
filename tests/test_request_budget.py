"""Bounding outbound work by the schedule that asked for it.

The failure these are written against, from 24 and 25-Aug-2026: container
DNS stopped resolving, outbound calls started hanging rather than failing,
and one option-chain fetch — contract info at three attempts, then one chain
request per published expiry at two attempts each, every attempt preceded by
a two-page cookie warm-up, all at a twelve-second socket timeout — ran for
over fourteen minutes against a job scheduled every sixty seconds. Every
poll behind it was skipped by `max_instances=1`. Half a session of option
snapshots, which nobody can re-collect, was lost.

Nothing here tests that the network is fast. They test that a call gives the
slot back, because the alternative to a slow tick is not a slower tick — it
is no ticks at all.

The property worth the most is `test_the_rate_limit_is_refused_not_shortened`:
under time pressure the tempting fix is to trim the wait NSE requires between
calls, which converts a missed poll into a burst, and a burst into a block.
"""
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app import net
from app.brokers import freedata, nse
from app.brokers.freedata import FreeDataBroker
from app.brokers.nse import NSEClient

# How a bounded call ends. Which of the two it is depends on where the budget
# ran out — refused before a request could start, or spent across attempts —
# and the distinction is not what these tests are about.
GAVE_UP = (RuntimeError, net.BudgetExhausted)

# Scaled down so the suite stays fast. Every assertion is about a ratio or an
# ordering, never about a specific number of seconds.
SPACING = 0.02
REQUEST_TIMEOUT = 0.25
BACKOFF = 0.02


@pytest.fixture(autouse=True)
def fast_nse(monkeypatch):
    monkeypatch.setattr(nse, "MIN_SECONDS_BETWEEN_CALLS", SPACING)
    monkeypatch.setattr(nse, "REQUEST_TIMEOUT_SECONDS", REQUEST_TIMEOUT)
    monkeypatch.setattr(nse, "RETRY_BACKOFF_SECONDS", BACKOFF)


class Hung:
    """A server that accepts the connection and never answers.

    Sleeps for exactly the timeout it was handed, then raises what httpx
    raises — which is what a real socket does, and the reason a per-request
    timeout alone bounded nothing: the retry structure simply multiplied it.
    """

    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    def get(self, url, timeout=None, **kw):
        with self._lock:
            self.calls.append((url, timeout))
        time.sleep(timeout if timeout is not None else REQUEST_TIMEOUT)
        raise httpx.ConnectTimeout("timed out")


class ChainDown:
    """Contract info answers; every chain request hangs.

    The shape that actually produces the fan-out. A transport that fails the
    metadata call never reaches the per-expiry loop at all, so a test written
    against one would assert over an empty list and pass having exercised
    nothing.
    """

    def __init__(self, expiries):
        self.expiries = list(expiries)
        self.calls = []
        self._lock = threading.Lock()

    def get(self, url, timeout=None, **kw):
        with self._lock:
            self.calls.append((url, timeout))
        if "contract-info" in url or url.endswith("/") or "option-chain" == url.rsplit("/", 1)[-1]:
            return httpx.Response(200, json={"expiryDates": self.expiries})
        if "expiry=" not in url:
            return httpx.Response(200, json={})
        time.sleep(timeout if timeout is not None else REQUEST_TIMEOUT)
        raise httpx.ConnectTimeout("timed out")


class Unresolvable:
    """DNS is down: the call fails immediately, having gone nowhere."""

    def __init__(self):
        self.calls = []

    def get(self, url, timeout=None, **kw):
        self.calls.append((url, timeout))
        raise httpx.ConnectError(
            "[Errno -3] Temporary failure in name resolution")


class Working:
    """Answers contract info and the chain, like NSE on a good day."""

    def __init__(self, expiry="07-Aug-2026"):
        self.expiry = expiry
        self.calls = []

    def get(self, url, timeout=None, **kw):
        self.calls.append((url, timeout))
        if "contract-info" in url:
            body = {"expiryDates": [self.expiry]}
        elif "option-chain" in url and "expiry=" in url:
            body = {"records": {"data": [{"strikePrice": 24000,
                                          "CE": {}, "PE": {}}],
                                "expiryDates": [self.expiry],
                                "underlyingValue": 24000.0}}
        else:
            body = {}
        return httpx.Response(200, json=body)


def build(transport):
    client = NSEClient.__new__(NSEClient)
    client._cookie_time = 0.0
    client._last_call = 0.0
    client._lock = threading.Lock()
    client._chain_expiry = None
    client._chain_expiry_time = 0.0
    client.client = transport
    return client


# ---------------------------------------------------------------------------
# the budget itself
# ---------------------------------------------------------------------------

def test_a_jobs_budget_is_shorter_than_its_own_interval():
    """The whole design in one assertion. A tick that may run for its full
    interval lands right back on the skip this exists to prevent."""
    for interval in (5, 60, 300):
        assert net.budget_for(interval) < interval


def test_a_slice_never_exceeds_what_is_left():
    deadline = net.Deadline(0.6, label="t")
    assert deadline.slice(10.0) <= 0.6
    assert deadline.slice(0.1) == pytest.approx(0.1)


def test_a_nearly_spent_budget_refuses_to_start_a_request():
    """Rather than handing back a uselessly small timeout — which would
    spend the last of the budget learning nothing."""
    deadline = net.Deadline(net.MIN_REQUEST_SECONDS / 2, label="t")
    time.sleep(net.MIN_REQUEST_SECONDS / 2)
    with pytest.raises(net.BudgetExhausted):
        deadline.slice(5.0)


def test_a_wait_that_does_not_fit_is_refused_not_shortened():
    """`wait` is used for protective delays. A short one is worse than none."""
    deadline = net.Deadline(0.05, label="t")
    started = time.monotonic()
    with pytest.raises(net.BudgetExhausted):
        deadline.wait(5.0)
    assert time.monotonic() - started < 0.5, "it slept anyway"


def test_the_budget_is_shared_by_everything_inside_the_block():
    with net.budget(1.0, label="job") as outer:
        assert net.current() is outer
        assert net.deadline_or(999) is outer
    assert net.current() is None


def test_two_threads_do_not_share_one_budget():
    """APScheduler runs each job in its own pool thread. A budget leaking
    between them would let the ticker spend the collector's time."""
    seen = []

    def job(seconds):
        with net.budget(seconds, label=f"job-{seconds}"):
            time.sleep(0.05)
            seen.append((seconds, net.current().budget))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(job, [2.0, 30.0]))
    assert sorted(seen) == [(2.0, 2.0), (30.0, 30.0)]


def test_a_caller_with_no_budget_still_gets_one():
    """An API handler and a one-off script have no schedule to derive a
    budget from, and must still not be able to hang forever."""
    assert net.current() is None
    standalone = net.deadline_or(7.0)
    assert standalone.budget == 7.0


# ---------------------------------------------------------------------------
# request timeout
# ---------------------------------------------------------------------------

def test_every_outbound_request_carries_a_timeout():
    """The regression in the plainest form: a `get` with `timeout=None`
    inherits httpx's default and can outlive its own schedule."""
    transport = Hung()
    client = build(transport)
    with net.budget(1.0, label="test"), pytest.raises(GAVE_UP):
        client.get_json("/api/allIndices", attempts=2)

    assert transport.calls, "no request was attempted"
    assert all(timeout is not None for _url, timeout in transport.calls)


def test_no_request_is_given_more_time_than_the_budget_has_left():
    transport = Hung()
    client = build(transport)
    with net.budget(0.9, label="test"), pytest.raises(GAVE_UP):
        client.get_json("/api/allIndices", attempts=3)

    assert all(timeout <= 0.9 for _url, timeout in transport.calls)
    # Later requests get less than earlier ones — the budget is shrinking,
    # not being reissued per call.
    timeouts = [t for _u, t in transport.calls]
    assert timeouts[-1] <= timeouts[0]


# ---------------------------------------------------------------------------
# the multiplication that cost the session
# ---------------------------------------------------------------------------

def test_a_hung_chain_fetch_returns_inside_the_collectors_interval():
    """The exact 25-Aug-2026 failure, scaled down.

    Unbounded, this is: contract info (3 attempts) + one chain request per
    published expiry (2 attempts each), every attempt preceded by a two-page
    warm-up. Against a hung server with a dozen expiries that is dozens of
    full timeouts. It has to come back inside one polling interval.
    """
    budget = 1.0
    transport = ChainDown([f"{d:02d}-Aug-2026" for d in range(1, 19)])
    client = build(transport)

    started = time.monotonic()
    with net.budget(budget, label="option-collector"), pytest.raises(GAVE_UP):
        client.raw_option_chain("NIFTY")
    elapsed = time.monotonic() - started

    # Budget, plus at most the one request that was already in flight when
    # it ran out. That is the guarantee this module claims and no more.
    assert elapsed < budget + REQUEST_TIMEOUT + 0.5, (
        f"took {elapsed:.2f}s against a {budget}s budget "
        f"over {len(transport.calls)} requests")


def test_a_failing_chain_fetch_does_not_fan_out_across_every_expiry():
    """NSE lists a dozen expiries. A healthy fetch succeeds on the first, so
    trying the rest only ever happens when something is already wrong — and
    it turns one failing call into twelve."""
    expiries = [f"{d:02d}-Aug-2026" for d in range(1, 19)]     # NSE publishes 18
    transport = ChainDown(expiries)
    client = build(transport)
    with net.budget(60.0, label="generous"), pytest.raises(GAVE_UP):
        client.raw_option_chain("NIFTY")

    tried = {url.split("expiry=")[1] for url, _t in transport.calls
             if "expiry=" in url}
    assert tried, "the fan-out loop was never reached — this proves nothing"
    assert len(tried) <= nse.MAX_CHAIN_EXPIRIES, tried
    # And an absolute bound beside it. The line above compares against the
    # very constant the code reads, so on its own it would agree with any
    # value the cap was raised to — including no cap at all.
    assert len(tried) <= 4, f"fanned out over {len(tried)} of {len(expiries)}"


def test_the_backoff_does_not_run_after_the_final_attempt():
    """It used to sleep 4.5 seconds on its way to raising."""
    transport = Unresolvable()
    client = build(transport)
    started = time.monotonic()
    with net.budget(5.0, label="test"), pytest.raises(RuntimeError):
        client.get_json("/api/allIndices", attempts=3)
    elapsed = time.monotonic() - started
    # Two inter-attempt backoffs, never a third.
    assert elapsed < BACKOFF * 3 + 1.0, f"{elapsed:.3f}s"


# ---------------------------------------------------------------------------
# the rate limit must survive the budget
# ---------------------------------------------------------------------------

def test_the_rate_limit_is_refused_not_shortened(monkeypatch):
    """Under time pressure, trimming the gap between NSE calls is the
    tempting fix and the wrong one: it produces exactly the burst that gets
    the IP blocked, and a block costs option history outright.
    """
    monkeypatch.setattr(nse, "MIN_SECONDS_BETWEEN_CALLS", 0.5)
    client = build(Working())
    client._last_call = time.monotonic()          # a call just went out

    deadline = net.Deadline(0.05, label="starved")
    started = time.monotonic()
    with pytest.raises(net.BudgetExhausted):
        client._throttle(deadline)
    assert time.monotonic() - started < 0.4, "it went early"


def test_a_refused_throttle_does_not_consume_the_next_callers_gap(monkeypatch):
    """`_last_call` must not advance for a call that never happened, or the
    next caller measures its spacing from a request nobody made."""
    monkeypatch.setattr(nse, "MIN_SECONDS_BETWEEN_CALLS", 0.5)
    client = build(Working())
    stamp = client._last_call = time.monotonic()

    with pytest.raises(net.BudgetExhausted):
        client._throttle(net.Deadline(0.05, label="starved"))
    assert client._last_call == stamp


def test_throttling_without_a_budget_is_unchanged(monkeypatch):
    """Every existing caller passes no deadline and must behave exactly as
    before — the budget is opt-in at the top, not a new global rule."""
    monkeypatch.setattr(nse, "MIN_SECONDS_BETWEEN_CALLS", SPACING)
    client = build(Working())
    stamps = []
    for _ in range(3):
        client._throttle()
        stamps.append(time.monotonic())
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert all(g >= SPACING * 0.8 for g in gaps)


# ---------------------------------------------------------------------------
# DNS / network failure, and recovery
# ---------------------------------------------------------------------------

def test_name_resolution_failure_fails_fast_rather_than_hanging():
    transport = Unresolvable()
    client = build(transport)
    started = time.monotonic()
    with net.budget(2.0, label="collector"), pytest.raises(RuntimeError):
        client.raw_option_chain("NIFTY")
    assert time.monotonic() - started < 2.0 + REQUEST_TIMEOUT + 0.5


def test_a_half_finished_warm_up_is_not_recorded_as_a_fresh_cookie():
    """Otherwise the client believes it holds a cookie it never received and
    the next call fails on a 401 it could have avoided.

    The warm-up itself gives up quietly — it is best-effort, and a cookie is
    not worth raising over. What must not survive it is the *claim* that the
    cookie is fresh; the enforcement then happens one line later, where
    `get_json` finds no budget left for the request either.
    """
    client = build(Hung())
    client._cookie_time = 0.0
    client._warm_up(deadline=net.Deadline(0.001, label="spent"))
    assert client._cookie_time == 0.0

    with pytest.raises(net.BudgetExhausted):
        client.get_json("/api/allIndices", deadline=net.Deadline(0.001, label="spent"))


class Flaky:
    """A source that is down and then comes back — a DNS outage that
    resolves, which is what actually happened on both lost sessions."""

    def __init__(self):
        self.down = True
        self.working = Working()
        self.calls = 0

    def get(self, url, timeout=None, **kw):
        self.calls += 1
        if self.down:
            raise httpx.ConnectError("[Errno -3] Temporary failure in name resolution")
        return self.working.get(url, timeout=timeout, **kw)


def test_the_collector_recovers_once_the_network_comes_back():
    """A client that failed a poll must not be wedged. Warm-up state, the
    cached expiry and the rate limiter all have to survive an outage."""
    transport = Flaky()
    client = build(transport)

    with net.budget(2.0, label="poll-1"), pytest.raises(RuntimeError):
        client.raw_option_chain("NIFTY")

    transport.down = False
    with net.budget(5.0, label="poll-2"):
        payload = client.raw_option_chain("NIFTY")
    assert payload["records"]["data"]


def test_recovery_leaves_the_expiry_cache_correct():
    transport = Flaky()
    client = build(transport)
    with net.budget(2.0, label="poll-1"), pytest.raises(RuntimeError):
        client.raw_option_chain("NIFTY")
    assert client._chain_expiry is None, "cached an expiry it never confirmed"

    transport.down = False
    with net.budget(5.0, label="poll-2"):
        client.raw_option_chain("NIFTY")
    assert client._chain_expiry == "07-Aug-2026"


# ---------------------------------------------------------------------------
# the quote fallback chain under a budget
# ---------------------------------------------------------------------------
#
# The ticker polls every five seconds, so its budget is four. That is less
# than the eight-second timeout the Yahoo quote asks for, which raises a fair
# question: does bounding the ticker cost it the NSE fallback?
#
# It must not, in the case that matters. A source that is *down* fails in
# milliseconds — refused connection, HTTP error, unresolvable name — and
# leaves almost the whole budget for the next one. Only a source that is
# slow enough to consume the entire budget loses the fallback, and in that
# case there is genuinely no time left to call it: the next poll is already
# due, and starting a second request is how a slow tick became a skipped one.


class NSEQuote:
    def all_indices(self, *a, **k):
        return {"timestamp": "25-Aug-2026 15:29",
                "data": [{"index": "NIFTY 50", "last": 24_100.0}]}


def test_a_fast_yahoo_failure_still_leaves_room_for_the_nse_fallback(monkeypatch):
    def refused(*a, **k):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(freedata.curl_requests, "get", refused)
    broker = FreeDataBroker(nse_client=NSEQuote())

    with net.budget(net.budget_for(5), label="price-ticker"):
        quote = broker.quote("NIFTY")
    assert quote["source"] == "nse"


def test_a_yahoo_call_that_eats_the_whole_budget_does_not_start_another(monkeypatch):
    """There is no time left to call a second source, and starting one anyway
    is precisely how a slow tick turns into a skipped one.

    The NSE fallback here is a real `NSEClient` over a stub socket, not a
    stub client — a fake that answers instantly would bypass the budget
    check that is the whole subject of the test.
    """
    def slow(*a, **kw):
        time.sleep(kw.get("timeout", 1))
        raise TimeoutError("yahoo timed out")

    monkeypatch.setattr(freedata.curl_requests, "get", slow)
    transport = Hung()
    broker = FreeDataBroker(nse_client=build(transport))

    with net.budget(0.6, label="price-ticker"), pytest.raises(net.BudgetExhausted):
        broker.quote("NIFTY")
    assert transport.calls == [], "it opened a second socket with no time left"


def test_a_yahoo_candle_fetch_is_bounded_too(monkeypatch):
    """The agent archives candles before it analyses. That request was capped
    at twenty seconds against a five-minute job — comfortable on its own, and
    not once the tick's other work is added to it."""
    seen = []

    def record(*a, **kw):
        seen.append(kw.get("timeout"))
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(freedata.curl_requests, "get", record)
    broker = FreeDataBroker(nse_client=NSEQuote())

    with net.budget(3.0, label="nifty-agent"), pytest.raises(Exception):  # noqa: B017
        broker.candles("NIFTY", "5m", days=5)
    assert seen and all(t <= 3.0 for t in seen)
