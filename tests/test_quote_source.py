"""Which source the live quote comes from, and what it promises.

The dashboard's lag was not in the network, the cache or the socket. It was
in the choice of endpoint: NSE's `/api/allIndices` sits behind a CDN and
refreshes roughly once a minute, so a poll every ten seconds returned the
same minute-old number five times in a row and looked perfectly healthy
doing it.

Measured side by side during the session on 20-Aug-2026, over 90 seconds:
Yahoo's chart metadata changed on 23 of 25 polls; NSE changed once. The two
disagreed by 1.8 points on average, 3.8 at worst.

So the order matters, and these tests pin it down. No network: the HTTP
layer is stubbed, because what is under test is the preference and the
fallback chain, not Yahoo's uptime.
"""
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.brokers import freedata
from app.brokers.freedata import FreeDataBroker


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def yahoo_payload(price, epoch):
    return {"chart": {"result": [{"meta": {
        "regularMarketPrice": price, "regularMarketTime": epoch,
    }}]}}


class FakeNSE:
    def __init__(self, value=24_100.0, timestamp="20-Aug-2026 10:34"):
        self.value, self.timestamp = value, timestamp
        self.calls = 0

    def all_indices(self):
        self.calls += 1
        if self.value is None:
            raise RuntimeError("NSE unreachable")
        return {"timestamp": self.timestamp,
                "data": [{"index": "NIFTY 50", "last": self.value}]}


@pytest.fixture
def broker():
    return FreeDataBroker(nse_client=FakeNSE())


# ---------------------------------------------------------------------------
# source preference
# ---------------------------------------------------------------------------

def test_the_fresher_source_is_preferred_and_nse_is_not_even_called(monkeypatch, broker):
    """Yahoo first. NSE must not be touched on the happy path — a call that
    is not made cannot be throttled, and NSE throttles."""
    epoch = int(datetime(2026, 8, 20, 5, 8, 59, tzinfo=UTC).timestamp())
    monkeypatch.setattr(freedata.curl_requests, "get",
                        lambda *a, **k: FakeResponse(yahoo_payload(24_205.3, epoch)))

    quote = broker.quote("NIFTY")

    assert quote["last_price"] == 24_205.3
    assert quote["source"] == "yahoo"
    assert quote["source_time"] == "2026-08-20T05:08:59+00:00"
    assert broker.nse.calls == 0


def test_quote_reports_the_exchange_clock_not_the_fetch_clock(monkeypatch, broker):
    """The timestamp must come from the payload. Stamping `now` would make
    every price look current by construction, which is precisely how a
    minute-old feed passed for live."""
    epoch = int(datetime(2026, 8, 20, 4, 0, 0, tzinfo=UTC).timestamp())
    monkeypatch.setattr(freedata.curl_requests, "get",
                        lambda *a, **k: FakeResponse(yahoo_payload(24_000.0, epoch)))

    quote = broker.quote("NIFTY")

    printed = datetime.fromisoformat(quote["source_time"])
    assert printed == datetime(2026, 8, 20, 4, 0, tzinfo=UTC)
    assert (datetime.now(UTC) - printed).total_seconds() > 60


# ---------------------------------------------------------------------------
# fallback chain
# ---------------------------------------------------------------------------

def test_falls_back_to_nse_when_yahoo_fails(monkeypatch, broker):
    def boom(*a, **k):
        raise RuntimeError("yahoo down")
    monkeypatch.setattr(freedata.curl_requests, "get", boom)

    quote = broker.quote("NIFTY")

    assert quote["source"] == "nse"
    assert quote["last_price"] == 24_100.0
    # NSE's minute-resolution stamp, converted from IST to an absolute instant.
    assert quote["source_time"] == "2026-08-20T05:04:00+00:00"
    assert broker.nse.calls == 1


def test_last_resort_candle_is_labelled_as_a_bar_not_a_live_print(monkeypatch):
    """The old code returned a bar close as if it were a quote, stamped with
    the moment we fetched it — a five-minute-old number presented as live.

    It is still the last resort, but it now says what it is and carries the
    bar's own timestamp.
    """
    nse = FakeNSE(value=None)                       # NSE down too
    broker = FreeDataBroker(nse_client=nse)

    def boom(*a, **k):
        raise RuntimeError("yahoo down")
    monkeypatch.setattr(freedata.curl_requests, "get", boom)

    bar_time = datetime(2026, 8, 20, 5, 0, tzinfo=UTC)
    monkeypatch.setattr(FreeDataBroker, "candles", lambda self, *a, **k: pd.DataFrame({
        "timestamp": [pd.Timestamp(bar_time)],
        "open": [24_190.0], "high": [24_210.0],
        "low": [24_180.0], "close": [24_200.0], "volume": [1.0],
    }))

    quote = broker.quote("NIFTY")

    assert quote["last_price"] == 24_200.0
    assert quote["source"] == "yahoo-candle"
    assert quote["source_time"] == bar_time.isoformat()


@pytest.mark.parametrize("payload", [
    {"chart": {"result": []}},
    {"chart": {"result": [{"meta": {}}]}},
    {},
])
def test_a_malformed_yahoo_payload_falls_through_instead_of_crashing(
        monkeypatch, broker, payload):
    monkeypatch.setattr(freedata.curl_requests, "get",
                        lambda *a, **k: FakeResponse(payload))

    quote = broker.quote("NIFTY")

    assert quote["source"] == "nse"


def test_a_quote_without_an_exchange_timestamp_says_so(monkeypatch, broker):
    """Yahoo occasionally omits `regularMarketTime`. None is the correct
    answer — it makes the price 'unknown' age downstream rather than 'live'."""
    monkeypatch.setattr(freedata.curl_requests, "get",
                        lambda *a, **k: FakeResponse(yahoo_payload(24_205.3, None)))

    quote = broker.quote("NIFTY")

    assert quote["last_price"] == 24_205.3
    assert quote["source_time"] is None


def test_every_broker_returns_the_same_quote_shape():
    """The ticker must never special-case a broker to find the timestamp."""
    from app.brokers.mock import MockBroker

    quote = MockBroker().quote("NIFTY")
    assert {"last_price", "source", "source_time"} <= set(quote)
    assert isinstance(quote["last_price"], float)
    datetime.fromisoformat(quote["source_time"])      # parses, or raises


def test_candles_are_untouched_by_the_quote_change(monkeypatch, broker):
    """The strategy reads `candles`, not `quote`. Changing the live price
    source must not have altered the series the signal engine sees."""
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        return FakeResponse({"chart": {"result": [{
            "timestamp": [1_787_200_000, 1_787_200_300],
            "indicators": {"quote": [{
                "open": [1.0, 2.0], "high": [1.0, 2.0],
                "low": [1.0, 2.0], "close": [1.0, 2.0], "volume": [5, 6],
            }]},
        }]}})

    monkeypatch.setattr(freedata.curl_requests, "get", fake_get)
    df = broker.candles("NIFTY", "5m", days=5)

    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert "interval=5m" in captured["url"]
    assert "range=5d" in captured["url"]
