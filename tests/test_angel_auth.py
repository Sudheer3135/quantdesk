"""Angel authentication and tick decoding.

No account, no SDK, no network. `login` takes a `connect_factory` seam for
exactly this reason: the failure modes that matter here — Angel answering a
rejected login with HTTP 200, a response with no feed token, an exception
whose text contains the MPIN — are all things you cannot provoke against a
live account on purpose.
"""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.brokers import angel
from app.brokers.angel import (
    AngelCredentials,
    AngelError,
    AngelNotConfigured,
    MalformedTick,
    decode_tick,
    login,
    token_list,
)
from app.config import get_settings

CREDS = AngelCredentials(api_key="key-abc", client_code="S12345",
                         secret="1234", totp_secret="JBSWY3DPEHPK3PXP")

# 2026-08-26 10:15:00 IST == 04:45:00 UTC.
MOMENT = datetime(2026, 8, 26, 4, 45, tzinfo=UTC)


class FakeConnect:
    """Stands in for SmartConnect. Records what it was asked."""

    def __init__(self, response=None, feed_token="feed-xyz", raises=None):
        self.response = response if response is not None else {
            "status": True,
            "data": {"jwtToken": "jwt-123", "refreshToken": "refresh-456",
                     "feedToken": feed_token},
        }
        self.raises = raises
        self.calls = []

    def generateSession(self, client_code, password, totp):   # noqa: N802
        self.calls.append((client_code, password, totp))
        if self.raises:
            raise self.raises
        return self.response

    def getfeedToken(self):                                   # noqa: N802
        return "feed-from-call"


def factory(connect):
    return lambda api_key: connect


# ---- configuration -----------------------------------------------------

def test_missing_credentials_name_themselves(monkeypatch):
    """A feed that starts with three of four values and fails at the login
    call reports an authentication error, which sends whoever is debugging
    it to Angel's status page instead of to their own .env."""
    monkeypatch.setenv("ANGEL_ENABLED", "true")
    monkeypatch.setenv("ANGEL_API_KEY", "key")
    monkeypatch.setenv("ANGEL_CLIENT_CODE", "")
    monkeypatch.setenv("ANGEL_MPIN", "1234")
    monkeypatch.setenv("ANGEL_TOTP_SECRET", "")
    get_settings.cache_clear()

    with pytest.raises(AngelNotConfigured) as caught:
        angel.load_credentials()

    assert "ANGEL_CLIENT_CODE" in str(caught.value)
    assert "ANGEL_TOTP_SECRET" in str(caught.value)
    assert "ANGEL_API_KEY" not in str(caught.value)


def test_mpin_is_preferred_over_password(monkeypatch):
    monkeypatch.setenv("ANGEL_API_KEY", "key")
    monkeypatch.setenv("ANGEL_CLIENT_CODE", "S12345")
    monkeypatch.setenv("ANGEL_PASSWORD", "old-password")
    monkeypatch.setenv("ANGEL_MPIN", "1234")
    monkeypatch.setenv("ANGEL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    get_settings.cache_clear()

    assert angel.load_credentials().secret == "1234"


def test_password_is_accepted_when_there_is_no_mpin(monkeypatch):
    monkeypatch.setenv("ANGEL_API_KEY", "key")
    monkeypatch.setenv("ANGEL_CLIENT_CODE", "S12345")
    monkeypatch.setenv("ANGEL_PASSWORD", "old-password")
    monkeypatch.setenv("ANGEL_MPIN", "")
    monkeypatch.setenv("ANGEL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    get_settings.cache_clear()

    assert angel.load_credentials().secret == "old-password"


# ---- login --------------------------------------------------------------

def test_a_successful_login_returns_the_tokens_the_socket_needs():
    connect = FakeConnect()
    session = login(CREDS, connect_factory=factory(connect))

    assert session.auth_token == "jwt-123"
    assert session.feed_token == "feed-xyz"
    assert session.refresh_token == "refresh-456"
    assert session.client_code == "S12345"


def test_the_login_sends_a_freshly_generated_totp():
    connect = FakeConnect()
    login(CREDS, connect_factory=factory(connect))

    client_code, secret, otp = connect.calls[0]
    assert (client_code, secret) == ("S12345", "1234")
    assert otp.isdigit() and len(otp) == 6
    # And it is the code for *this* moment, not a constant.
    assert otp == angel.current_totp(CREDS.totp_secret)


def test_a_rejected_login_is_not_a_session_even_though_it_returned_200():
    """Angel answers a refused login with HTTP 200 and status: false.
    Trusting the transport would build a session holding None for every
    token, and the socket would fail much later with something unrelated."""
    connect = FakeConnect(response={"status": False,
                                    "message": "Invalid totp",
                                    "errorcode": "AB1050"})
    with pytest.raises(AngelError, match="Invalid totp"):
        login(CREDS, connect_factory=factory(connect))


def test_a_response_with_no_feed_token_falls_back_to_the_separate_call():
    connect = FakeConnect(feed_token=None)
    session = login(CREDS, connect_factory=factory(connect))
    assert session.feed_token == "feed-from-call"


def test_a_login_with_no_auth_token_is_refused():
    connect = FakeConnect(response={"status": True, "data": {"feedToken": "f"}})
    with pytest.raises(AngelError, match="no auth token"):
        login(CREDS, connect_factory=factory(connect))


def test_a_non_dict_response_is_refused_rather_than_indexed():
    connect = FakeConnect(response="upstream returned HTML")
    with pytest.raises(AngelError, match="not a login response"):
        login(CREDS, connect_factory=factory(connect))


def test_an_sdk_exception_does_not_carry_its_text_into_the_error():
    """Some SDK errors echo the request back, and the request contains the
    MPIN. The raised message names the exception type and nothing else."""
    connect = FakeConnect(raises=ValueError("request failed: mpin=1234 totp=999999"))
    with pytest.raises(AngelError) as caught:
        login(CREDS, connect_factory=factory(connect))

    assert "1234" not in str(caught.value)
    assert "999999" not in str(caught.value)
    assert "ValueError" in str(caught.value)


def test_a_session_never_renders_its_tokens():
    """A session lands in a traceback the first time the socket raises, and
    a default dataclass repr would put a live JWT into the log file."""
    session = login(CREDS, connect_factory=factory(FakeConnect()))
    rendered = repr(session)

    assert "jwt-123" not in rendered
    assert "feed-xyz" not in rendered
    assert "refresh-456" not in rendered
    assert "…45" in rendered            # enough to identify the account


def test_the_redacted_shape_identifies_without_revealing():
    session = login(CREDS, connect_factory=factory(FakeConnect()))
    block = session.redacted

    assert block["client_code"] == "…45"
    assert block["has_auth_token"] is True
    assert "jwt-123" not in str(block)
    assert "key-abc" not in str(block)


# ---- tick decoding ------------------------------------------------------

def ltp(price_paise=2433455, stamp_ms=None, **extra):
    return {
        "subscription_mode": 1, "exchange_type": 1, "token": "99926000",
        "sequence_number": 42,
        "exchange_timestamp": (int(MOMENT.timestamp() * 1000)
                               if stamp_ms is None else stamp_ms),
        "last_traded_price": price_paise,
        "subscription_mode_val": "LTP",
    } | extra


def test_the_price_is_converted_out_of_paise():
    """Angel sends the price multiplied by 100. Publishing it unscaled would
    not crash anything — it would put a number a hundred times too large on
    the dashboard and into the change column."""
    tick = decode_tick(ltp(2433455), now=MOMENT)
    assert tick.price == 24_334.55


def test_the_timestamp_is_read_as_epoch_milliseconds():
    """Reading it as seconds dates every tick to 1970, which the staleness
    check would report as a feed 56 years behind the market."""
    tick = decode_tick(ltp(stamp_ms=int(MOMENT.timestamp() * 1000)), now=MOMENT)
    assert tick.source_time == MOMENT
    assert tick.source_time.tzinfo is not None


def test_an_ist_wall_clock_read_as_utc_is_caught_as_a_bug():
    """5h30m *ahead* is the signature of an IST wall-clock decoded as UTC.
    A tick cannot print in the future, so this is a parse error — and
    calling it staleness would send somebody to Angel's status page over a
    bug in our own file."""
    shifted = MOMENT + timedelta(hours=5, minutes=30)
    with pytest.raises(MalformedTick, match="in the future"):
        decode_tick(ltp(stamp_ms=int(shifted.timestamp() * 1000)), now=MOMENT)


def test_milliseconds_read_as_seconds_is_caught_as_a_bug():
    """Off by a factor of a thousand dates every tick to 1970."""
    with pytest.raises(MalformedTick, match="unit error"):
        decode_tick(ltp(stamp_ms=int(MOMENT.timestamp())), now=MOMENT)


def test_a_normally_late_tick_is_still_accepted():
    late = MOMENT - timedelta(seconds=45)
    tick = decode_tick(ltp(stamp_ms=int(late.timestamp() * 1000)), now=MOMENT)
    assert tick.source_time == late


def test_a_replayed_previous_close_is_accepted_rather_than_discarded():
    """On subscribe Angel replays the instrument's last traded price. On a
    Monday morning that is Friday's close, some sixty-five hours old.
    Rejecting it would throw away a real quote; reporting it as stale is
    `freshness`'s job, not the decoder's."""
    friday_close = MOMENT - timedelta(hours=65)
    tick = decode_tick(ltp(stamp_ms=int(friday_close.timestamp() * 1000)),
                       now=MOMENT)
    assert tick.source_time == friday_close


def test_small_clock_skew_against_the_exchange_is_tolerated():
    """This machine's clock is not the exchange's. A second or two ahead is
    ordinary and must not be read as a decoding failure."""
    barely_ahead = MOMENT + timedelta(seconds=5)
    tick = decode_tick(ltp(stamp_ms=int(barely_ahead.timestamp() * 1000)),
                       now=MOMENT)
    assert tick.source_time == barely_ahead


@pytest.mark.parametrize("message, reason", [
    ({}, "no last_traded_price"),
    (ltp(price_paise=None), "no last_traded_price"),
    (ltp(price_paise="abc"), "not a number"),
    (ltp(price_paise=0), "not a tradable price"),
    (ltp(price_paise=-500), "not a tradable price"),
    ("a string", "expected a dict"),
    (None, "expected a dict"),
    (b"\x01\x02", "expected a dict"),
])
def test_a_malformed_tick_is_refused_rather_than_guessed(message, reason):
    if isinstance(message, dict) and "exchange_timestamp" not in message:
        message = message
    with pytest.raises(MalformedTick, match=reason):
        decode_tick(message, now=MOMENT)


def test_a_missing_timestamp_is_refused():
    message = ltp()
    del message["exchange_timestamp"]
    with pytest.raises(MalformedTick, match="no exchange_timestamp"):
        decode_tick(message, now=MOMENT)


def test_an_unparseable_timestamp_is_refused():
    with pytest.raises(MalformedTick, match="epoch milliseconds"):
        decode_tick(ltp(stamp_ms="yesterday"), now=MOMENT)


def test_a_zero_price_is_not_published_as_a_price():
    """Angel sends a zero LTP for an instrument that has not traded. It is
    not a price, and publishing it would draw the index at zero."""
    with pytest.raises(MalformedTick):
        decode_tick(ltp(price_paise=0), now=MOMENT)


def test_the_token_and_sequence_travel_with_the_tick():
    tick = decode_tick(ltp(), now=MOMENT)
    assert tick.token == "99926000"
    assert tick.sequence == 42
    assert tick.raw_timestamp_ms == int(MOMENT.timestamp() * 1000)


# ---- subscription shape --------------------------------------------------

def test_the_subscription_shape_is_what_the_sdk_expects():
    assert token_list("99926000", 1) == [
        {"exchangeType": 1, "tokens": ["99926000"]}]


def test_the_subscription_coerces_types_that_arrive_from_the_environment():
    """Settings come from strings. An exchangeType of "1" is rejected by the
    SDK's binary packer with an error that names neither field."""
    assert token_list(99926000, "1") == [
        {"exchangeType": 1, "tokens": ["99926000"]}]
