"""NSE rate limiting under concurrency.

Audit finding M-2: `get_broker()` is lru_cached, so one NSEClient — one
httpx.Client, one `_last_call` — is shared by the agent, the price ticker,
the option collector and every API request thread. `_throttle` read that
timestamp, slept, then wrote it, with no lock. Concurrent callers all
measured the same gap, slept the same amount and fired together.

Four hours of production logs showed 70 request pairs under 0.2s, including
five NSE calls inside 68 milliseconds. The module's own docstring says
"Hammer it and you get blocked for a while", and a block costs option
snapshots outright — that history cannot be backfilled.

These tests use a short spacing so they stay fast; the property under test
is the ordering guarantee, not the specific interval.
"""
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.brokers import nse
from app.brokers.nse import NSEClient

SPACING = 0.05          # stand-in for MIN_SECONDS_BETWEEN_CALLS
TOLERANCE = 0.004       # scheduler jitter; far below the spacing itself


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(nse, "MIN_SECONDS_BETWEEN_CALLS", SPACING)
    c = NSEClient.__new__(NSEClient)         # no httpx.Client needed
    c._cookie_time = 0.0
    c._last_call = 0.0
    c._lock = threading.Lock()
    return c


def fire(client, n, workers):
    """Call _throttle from `workers` threads and record when each returned."""
    stamps = []
    guard = threading.Lock()

    def once(_):
        client._throttle()
        with guard:
            stamps.append(time.monotonic())

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(once, range(n)))
    return sorted(stamps)


def gaps(stamps):
    return [b - a for a, b in zip(stamps, stamps[1:], strict=False)]


# ---------------------------------------------------------------------------
# the regression
# ---------------------------------------------------------------------------

def test_concurrent_callers_are_spaced_not_bursted(client):
    """The exact failure: many threads, all firing at once."""
    stamps = fire(client, n=8, workers=8)
    observed = gaps(stamps)

    assert all(g >= SPACING - TOLERANCE for g in observed), \
        f"burst detected — gaps {[round(g, 4) for g in observed]}"


def test_no_pair_lands_inside_the_burst_window_seen_in_production(client):
    """70 pairs under 0.2s were observed against a 1.5s limit — that ratio,
    scaled to this test's spacing, must now be impossible."""
    stamps = fire(client, n=10, workers=10)
    burst_window = SPACING * (0.2 / 1.5)
    assert not [g for g in gaps(stamps) if g < burst_window]


def test_total_elapsed_reflects_serialisation(client):
    """Eight calls at one per SPACING cannot finish faster than 7 gaps."""
    start = time.monotonic()
    fire(client, n=8, workers=8)
    elapsed = time.monotonic() - start
    assert elapsed >= SPACING * 7 - TOLERANCE


def test_sequential_calls_still_respect_the_spacing(client):
    """The single-threaded behaviour must be unchanged."""
    stamps = []
    for _ in range(4):
        client._throttle()
        stamps.append(time.monotonic())
    assert all(g >= SPACING - TOLERANCE for g in gaps(stamps))


def test_an_idle_client_does_not_sleep(client):
    """Spacing is a floor, not a fixed delay — a first call after a long
    idle must go straight out."""
    client._last_call = time.monotonic() - (SPACING * 20)
    start = time.monotonic()
    client._throttle()
    # Same reasoning: the assertion is "did not sleep an interval", not
    # "returned within four milliseconds on a busy CI box".
    assert time.monotonic() - start < SPACING / 2


def test_a_nonsense_future_timestamp_waits_at_most_one_interval(client):
    """Defensive clamp.

    A monotonic clock cannot run backwards, so `_last_call` should never sit
    in the future — but if it somehow did, the naive arithmetic computed a
    sleep of arbitrary length and wedged a scheduler thread for as long as
    the corruption said. The wait is now bounded by the interval itself.
    """
    client._last_call = time.monotonic() + 10_000
    start = time.monotonic()
    client._throttle()
    waited = time.monotonic() - start
    # Generous bound on purpose. The bug this guards against waited 10,000
    # seconds; anything within a few intervals proves the clamp holds, and a
    # tight bound here only measures how loaded the machine is.
    assert waited < SPACING * 4, f"waited {waited:.3f}s"


# ---------------------------------------------------------------------------
# warm-up
# ---------------------------------------------------------------------------

def test_cold_start_warms_up_once_not_once_per_thread(monkeypatch):
    """Otherwise every waiting thread fetches its own cookie — a stampede
    against the endpoint we are trying to be gentle with."""
    monkeypatch.setattr(nse, "MIN_SECONDS_BETWEEN_CALLS", SPACING)

    calls = []
    guard = threading.Lock()

    class Recording:
        def get(self, url):
            with guard:
                calls.append(url)

    c = NSEClient.__new__(NSEClient)
    c._cookie_time = 0.0
    c._last_call = 0.0
    c._lock = threading.Lock()
    c.client = Recording()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: c._warm_up(), range(8)))

    # Two page loads for one warm-up, not two per thread.
    assert len(calls) == 2, calls


def test_a_fresh_cookie_is_not_refetched(monkeypatch):
    monkeypatch.setattr(nse, "MIN_SECONDS_BETWEEN_CALLS", SPACING)
    calls = []

    class Recording:
        def get(self, url):
            calls.append(url)

    c = NSEClient.__new__(NSEClient)
    c._cookie_time = 0.0
    c._last_call = 0.0
    c._lock = threading.Lock()
    c.client = Recording()

    c._warm_up()
    c._warm_up()
    assert len(calls) == 2          # second call saw a fresh cookie


def test_force_refreshes_even_when_fresh(monkeypatch):
    monkeypatch.setattr(nse, "MIN_SECONDS_BETWEEN_CALLS", SPACING)
    calls = []

    class Recording:
        def get(self, url):
            calls.append(url)

    c = NSEClient.__new__(NSEClient)
    c._cookie_time = 0.0
    c._last_call = 0.0
    c._lock = threading.Lock()
    c.client = Recording()

    c._warm_up()
    c._warm_up(force=True)
    assert len(calls) == 4
