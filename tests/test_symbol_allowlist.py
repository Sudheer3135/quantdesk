"""Symbol validation.

Audit finding H-3: `YAHOO_SYMBOLS.get(symbol.upper(), symbol)` looked like an
allowlist but was a lookup with a passthrough default, so an unrecognised
symbol went straight into the outbound URL. `?symbol=AAPL` returned 79 real
Apple candles — the API was a free proxy to Yahoo running on this host.

The fix is a genuine allowlist on both feeds, and a 422 rather than a 500 or
a 502, because asking for a symbol we do not carry is the caller's mistake
and not an upstream fault.
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.brokers.base import UnknownSymbol
from app.brokers.freedata import YAHOO_SYMBOLS, resolve_yahoo_symbol
from app.brokers.nse import NSE_INDEX_SYMBOLS, resolve_nse_symbol

# ---------------------------------------------------------------------------
# the allowlists
# ---------------------------------------------------------------------------

def test_nifty_still_resolves():
    """The default watch symbol must be untouched by all of this."""
    assert resolve_yahoo_symbol("NIFTY") == "%5ENSEI"     # ^NSEI, encoded
    assert resolve_nse_symbol("NIFTY") == "NIFTY"


@pytest.mark.parametrize("symbol", sorted(YAHOO_SYMBOLS))
def test_every_supported_symbol_resolves(symbol):
    assert resolve_yahoo_symbol(symbol)


@pytest.mark.parametrize("symbol", ["nifty", "  NIFTY  ", "NiFtY"])
def test_case_and_whitespace_are_forgiven(symbol):
    assert resolve_yahoo_symbol(symbol) == resolve_yahoo_symbol("NIFTY")


@pytest.mark.parametrize("symbol", [
    "AAPL",                       # the proven proxy case
    "TSLA",
    "../../../etc/passwd",
    "NIFTY/../../v7/finance/quote",
    "NIFTY?x=1",
    "NIFTY#frag",
    "@evil.com",
    "",
    "   ",
])
def test_unsupported_and_malformed_symbols_are_refused(symbol):
    with pytest.raises(UnknownSymbol):
        resolve_yahoo_symbol(symbol)


@pytest.mark.parametrize("symbol", ["AAPL", "RELIANCE", "../etc", "NIFTY&expiry=x", ""])
def test_nse_refuses_anything_outside_its_index_list(symbol):
    with pytest.raises(UnknownSymbol):
        resolve_nse_symbol(symbol)


def test_nse_supports_the_index_chain_symbols():
    for symbol in NSE_INDEX_SYMBOLS:
        assert resolve_nse_symbol(symbol) == symbol


def test_resolved_values_are_url_encoded():
    """The allowlist is what makes this safe; encoding means a future entry
    containing a slash cannot silently reshape the request."""
    for symbol in YAHOO_SYMBOLS:
        resolved = resolve_yahoo_symbol(symbol)
        assert "/" not in resolved and "?" not in resolved and "#" not in resolved


# ---------------------------------------------------------------------------
# no request is made for a refused symbol
# ---------------------------------------------------------------------------

def test_a_refused_symbol_never_reaches_the_network(monkeypatch):
    """Rejection must happen before the HTTP call, not after it."""
    from app.brokers import freedata

    called = []
    monkeypatch.setattr(freedata.curl_requests, "get",
                        lambda *a, **k: called.append(a) or (_ for _ in ()).throw(
                            AssertionError("outbound request made for a refused symbol")))

    broker = freedata.FreeDataBroker(nse_client=object())
    for method in (lambda: broker.candles("AAPL"), lambda: broker.quote("AAPL")):
        with pytest.raises(UnknownSymbol):
            method()
    assert called == []


def test_nse_chain_refuses_before_any_request():
    from app.brokers.nse import NSEClient

    class Exploding(NSEClient):
        def __init__(self):
            pass
        def get_json(self, path, attempts=3):
            raise AssertionError("outbound request made for a refused symbol")

    with pytest.raises(UnknownSymbol):
        Exploding().raw_option_chain("AAPL")


# ---------------------------------------------------------------------------
# the API answers 422
# ---------------------------------------------------------------------------

def api():
    from app.main import app as real
    return TestClient(real, raise_server_exceptions=False)


@pytest.mark.parametrize("path", [
    "/market/candles?symbol=AAPL&days=1",
    "/market/candles?symbol=..%2F..%2F&days=1",
    "/market/structure?symbol=AAPL",
])
def test_api_returns_422_not_500_for_a_bad_symbol(path):
    r = api().get(path)
    assert r.status_code == 422, r.text
    assert "not a supported symbol" in r.json()["detail"]


def test_the_error_names_what_is_supported():
    detail = api().get("/market/candles?symbol=AAPL&days=1").json()["detail"]
    assert "NIFTY" in detail


def test_unknown_symbol_is_not_reported_as_an_upstream_failure():
    """It used to surface as 500, or as a 502 'could not load' — both of
    which point the reader at the feed instead of at their typo."""
    r = api().get("/market/candles?symbol=AAPL&days=1")
    assert r.status_code not in (500, 502)
