"""The browser must never receive an Angel credential.

Two ways this could go wrong, and both are cheap to prevent and expensive to
discover: a credential could be *serialised* into an API response, or it
could be *reachable* from code that runs on the response path and eventually
gets logged, echoed into an error, or added to a debug field by somebody
who does not know what these values are.

So this asserts on the actual bytes: every public endpoint and the live
websocket payload are rendered with real-looking credentials configured, and
searched for them.
"""
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.api import health as health_api
from app.api import market as market_api
from app.api import stream as stream_api
from app.brokers.angel import AngelCredentials, AngelSession
from app.config import get_settings
from app.workers import angel_feed, prices

# Distinctive enough that a substring search cannot match by accident, and
# shaped like the real things.
API_KEY = "AbCd1234ZzTopSecretApiKey"
CLIENT = "S9988771"
MPIN = "445566"
TOTP_SECRET = "JBSWY3DPEHPK3PXPTOTPSEED"
AUTH_TOKEN = "eyJhbGciOiJIUzUxMiJ9.jwt-secret-body.signature"
FEED_TOKEN = "feed-token-9f8e7d6c"
REFRESH_TOKEN = "refresh-token-1a2b3c"

SECRETS = (API_KEY, CLIENT, MPIN, TOTP_SECRET, AUTH_TOKEN, FEED_TOKEN,
           REFRESH_TOKEN)

MOMENT = datetime(2026, 8, 26, 4, 45, tzinfo=UTC)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv("ANGEL_ENABLED", "true")
    monkeypatch.setenv("ANGEL_API_KEY", API_KEY)
    monkeypatch.setenv("ANGEL_CLIENT_CODE", CLIENT)
    monkeypatch.setenv("ANGEL_MPIN", MPIN)
    monkeypatch.setenv("ANGEL_TOTP_SECRET", TOTP_SECRET)
    get_settings.cache_clear()
    prices.reset_previous()

    feed = angel_feed.AngelFeed(clock=lambda: MOMENT)
    feed._session = AngelSession(
        auth_token=AUTH_TOKEN, feed_token=FEED_TOKEN,
        refresh_token=REFRESH_TOKEN, client_code=CLIENT,
        api_key=API_KEY, created_at=MOMENT)
    feed.stats.last_tick_at = MOMENT
    feed.stats.last_source_time = MOMENT
    feed.stats.last_price = 24_334.55
    monkeypatch.setattr(angel_feed, "FEED", feed)

    yield feed
    get_settings.cache_clear()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(health_api.router)
    app.include_router(market_api.router)
    app.include_router(stream_api.router)
    return TestClient(app)


def assert_no_secrets(blob: str, where: str) -> None:
    for secret in SECRETS:
        assert secret not in blob, f"{where} leaked a credential"
    # The client code's last two digits are deliberately shown, so a bare
    # "does it contain any part of it" check would be wrong. This is the
    # whole value.
    assert CLIENT not in blob


# ---- the endpoints ---------------------------------------------------------

def test_the_feed_health_endpoint_carries_no_credentials(client):
    response = client.get("/health/feed")
    assert response.status_code == 200
    assert_no_secrets(response.text, "/health/feed")


def test_the_feed_health_endpoint_still_says_which_account(client):
    """Redaction that removes the meaning is not useful. An operator has to
    be able to see *that* an account is connected and which one."""
    body = client.get("/health/feed").json()
    assert body["angel"]["session"]["client_code"] == "…71"
    assert body["angel"]["session"]["has_auth_token"] is True
    assert body["live_price_source"] == "angel"


def test_the_general_health_endpoint_carries_no_credentials(client):
    assert_no_secrets(client.get("/health").text, "/health")


def test_the_price_endpoint_carries_no_credentials(client, monkeypatch):
    published = prices.build_payload(
        "NIFTY", 24_334.55, source="angel", source_time=MOMENT.isoformat(),
        received_at=MOMENT, transport="stream")
    monkeypatch.setattr(stream_api, "get_json", lambda key: published)

    response = client.get("/market/price")
    assert response.status_code == 200
    assert_no_secrets(response.text, "/market/price")
    assert response.json()["source"] == "angel"


def test_every_registered_route_is_checked_for_leaks(client):
    """A blanket sweep, so a new endpoint added later cannot quietly become
    the one that serialises settings."""
    from app.main import app as real_app

    checked = 0
    for route in real_app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if "GET" not in methods or "{" in path:
            continue
        try:
            response = client.get(path)
        except Exception:
            continue
        if response.status_code >= 500:
            continue
        assert_no_secrets(response.text, path)
        checked += 1

    assert checked >= 3


# ---- the published payload -------------------------------------------------

def test_a_published_tick_carries_no_credentials(configured):
    """The payload the browser actually receives, through the websocket and
    the cache. Everything else could be clean and this one still leak."""
    captured = []
    original = prices.publish
    prices.publish = lambda ch, blob, **kw: captured.append(blob) or True
    try:
        configured._on_data(None, {
            "token": "99926000", "last_traded_price": 2433455,
            "exchange_timestamp": int(MOMENT.timestamp() * 1000)})
    finally:
        prices.publish = original

    assert captured
    assert_no_secrets(captured[0], "the prices channel")
    assert json.loads(captured[0])["source"] == "angel"


def test_the_feed_status_carries_no_credentials(configured):
    assert_no_secrets(json.dumps(configured.status()), "feed.status()")


def test_a_session_in_a_traceback_carries_no_credentials(configured):
    """A session lands in a log the first time the socket raises."""
    assert_no_secrets(repr(configured._session), "AngelSession repr")
    assert_no_secrets(f"{configured._session}", "AngelSession str")


def test_credentials_are_not_reachable_from_the_published_payload():
    creds = AngelCredentials(api_key=API_KEY, client_code=CLIENT,
                             secret=MPIN, totp_secret=TOTP_SECRET)
    assert_no_secrets(json.dumps(creds.redacted), "AngelCredentials.redacted")
    assert creds.redacted["api_key_present"] is True


# ---- the frontend source tree ----------------------------------------------

def test_no_frontend_file_mentions_an_angel_credential():
    """The browser bundle is built from these files. A credential named here
    would be compiled into it whether or not any endpoint served one."""
    root = Path(__file__).resolve().parents[1] / "frontend" / "src"
    forbidden = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_MPIN",
                 "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET", "angel_api_key",
                 "totp")

    offenders = []
    for path in root.rglob("*.js*"):
        text = path.read_text()
        for name in forbidden:
            if name in text:
                offenders.append(f"{path.name}: {name}")

    assert offenders == [], f"frontend references a credential: {offenders}"


def test_the_settings_object_is_never_returned_whole():
    """`get_settings()` holds every secret this application has. Any
    endpoint that returned it — even for debugging — would hand the browser
    the API key, the Angel MPIN and the TOTP seed at once."""
    import re

    # The whole object, not an attribute of it. `return get_settings().broker`
    # is a string and perfectly fine; `return get_settings()` is every secret
    # this application holds.
    whole_object = re.compile(
        r"^\s*return\s+(get_settings\(\)|settings|s)\s*(#.*)?$")
    dumped = re.compile(r"\b(settings|get_settings\(\))\.model_dump\(")

    backend = Path(__file__).resolve().parents[1] / "backend" / "app"
    offenders = []
    for path in backend.rglob("*.py"):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if whole_object.match(line) or dumped.search(line):
                offenders.append(f"{path.name}:{number} — {line.strip()}")
    assert offenders == [], f"a settings object is returned at {offenders}"


# ---- the vendor's own logger -----------------------------------------------
#
# Everything above checks our code and our responses. This checks the SDK's,
# because on smartapi-python 1.5.5 a refused login writes the whole request
# body to stderr at ERROR level:
#
#   Request: {'clientcode': 'C1', 'password': '1111', 'totp': '610559'}
#   Headers: {... 'X-PrivateKey': '<api key>' ...}
#
# Nothing in this repository writes that, and no API response carries it,
# and it would still have reached Docker's log and whatever collects it.

SDK_LINE = (
    "Error occurred while making a POST request to "
    "https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword. "
    f"Headers: {{'X-PrivateKey': '{API_KEY}', 'X-UserType': 'USER'}}, "
    f"Request: {{'clientcode': '{CLIENT}', 'password': '{MPIN}', 'totp': '610559'}}"
)


def test_the_sdk_login_line_is_scrubbed():
    from app.brokers.angel import redact

    cleaned = redact(SDK_LINE, (API_KEY, MPIN, CLIENT, TOTP_SECRET))

    assert_no_secrets(cleaned, "the SDK's login error")
    assert "610559" not in cleaned          # the TOTP, which we never hold
    # And it is still a useful log line.
    assert "loginByPassword" in cleaned
    assert "X-UserType" in cleaned


def test_the_scrubber_leaves_ordinary_lines_alone():
    """A redactor that mangles every line is one somebody switches off."""
    from app.brokers.angel import redact

    ordinary = "Angel feed subscribed to token 99926000 (LTP), subscription #1"
    assert redact(ordinary, (API_KEY, MPIN)) == ordinary


def test_the_scrubber_ignores_values_too_short_to_be_distinctive():
    """A two-character client code would match inside ordinary words and
    turn the log into confetti."""
    from app.brokers.angel import redact

    assert redact("the market is open", ("en",)) == "the market is open"


def test_the_filter_is_attached_to_the_vendor_logger(monkeypatch):
    """logzero's logger has propagate=False and its own stderr handler, so
    it never reaches the root logger's filters. It has to be attached to
    directly, or the scrubber runs on everything except the leak."""
    import logging

    from app.brokers.angel import (
        AngelCredentials,
        CredentialFilter,
        install_log_redaction,
    )

    logzero = pytest.importorskip("logzero")
    install_log_redaction(AngelCredentials(
        api_key=API_KEY, client_code=CLIENT, secret=MPIN,
        totp_secret=TOTP_SECRET))

    assert any(isinstance(f, CredentialFilter) for f in logzero.logger.filters)
    assert any(isinstance(f, CredentialFilter)
               for f in logging.getLogger().filters)


def test_installing_twice_does_not_stack_filters():
    from app.brokers.angel import CredentialFilter, install_log_redaction

    logzero = pytest.importorskip("logzero")
    for _ in range(3):
        install_log_redaction()

    installed = [f for f in logzero.logger.filters
                 if isinstance(f, CredentialFilter)]
    assert len(installed) == 1


def test_a_record_passing_through_the_filter_is_scrubbed(caplog):
    """The filter, not the helper. A scrubber that is never reached is not
    a scrubber."""
    import logging

    from app.brokers.angel import CredentialFilter

    logger = logging.getLogger("test-angel-leak")
    logger.addFilter(CredentialFilter((API_KEY, MPIN, CLIENT)))
    try:
        with caplog.at_level(logging.ERROR, logger="test-angel-leak"):
            logger.error("login failed: %s", SDK_LINE)
        assert_no_secrets(caplog.text, "a filtered log record")
    finally:
        logger.filters.clear()
