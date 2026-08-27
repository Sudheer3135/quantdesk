"""Angel One SmartAPI — authentication and tick decoding.

Split from the feed worker on purpose. Everything here is a pure-ish
function of its inputs: log in and get tokens, decode a binary tick into a
price and a timestamp. The socket lifecycle — connecting, reconnecting,
resubscribing, deciding the feed has gone quiet — lives in
`workers/angel_feed.py`, because that part is all state and no arithmetic
and testing the two together would mean testing neither.

**Credentials never leave this module.** They are read from settings here,
held on the session object, and handed to the SDK. Nothing returns them,
nothing logs them, and no API response carries them. `AngelSession.redacted`
is the only representation that is allowed out, and it exists so the health
endpoint can say "authenticated" without saying with what.

Two decoding facts that are not obvious and would each be a silent, ruinous
bug:

  **Prices arrive in paise.** `last_traded_price` is the price multiplied by
  100 — NIFTY at 24,334.55 arrives as 2433455. Publishing that unscaled
  would not crash anything; it would put a number a hundred times too large
  on the dashboard and into the change column.

  **`exchange_timestamp` is epoch milliseconds.** Reading it as seconds
  dates every tick to January 1970, which the staleness check would then
  report as a feed 56 years behind the market rather than as a parse bug.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from ..config import get_settings

log = logging.getLogger(__name__)

# What the SDK calls the NSE cash segment, and LTP-only subscription mode.
# Mirrored as plain integers so this module — and its tests — do not need
# the vendor SDK importable just to talk about a mode.
NSE_CM = 1
LTP_MODE = 1

# Angel sends the price as an integer number of paise.
PAISE = 100.0

# The two directions are not symmetric, and treating them as one guard gets
# this wrong.
#
# *Late* is legitimate and can be very late: on subscribe Angel replays the
# instrument's last traded price, which on a Monday morning is Friday's
# close — some sixty-five hours old. Rejecting that would throw away a real
# quote. Staleness is reported downstream by `freshness`; it is not this
# function's business.
#
# *Early* is never legitimate. A tick cannot print in the future, so a
# source_time ahead of now means the units or the zone were misread — and
# that is precisely the signature of an IST wall-clock decoded as UTC, which
# lands 5h30m ahead. A minute of tolerance absorbs ordinary clock skew
# between this machine and the exchange.
MAX_FUTURE_SECONDS = 60

# And a floor for the other unit error. Reading epoch milliseconds as
# seconds dates every tick to January 1970 — decades, not days. Seven days
# clears the longest legitimate replay (a long weekend) by a wide margin
# while still catching a timestamp that is off by a factor of a thousand.
MAX_AGE_SECONDS = 7 * 86400


class AngelError(RuntimeError):
    """Angel refused, or answered with something unusable."""


class AngelNotConfigured(AngelError):
    """Angel is switched on but the environment did not supply everything."""


@dataclass(frozen=True)
class AngelCredentials:
    """What is needed to log in. Constructed from the environment only."""
    api_key: str
    client_code: str
    secret: str            # MPIN on newer accounts, password on older ones
    totp_secret: str

    @property
    def redacted(self) -> dict:
        """The only shape allowed out of the backend.

        Names the client code's last two characters so an operator can tell
        *which* account is connected without the value being useful to
        anyone who sees it.
        """
        return {
            "client_code": f"…{self.client_code[-2:]}" if self.client_code else None,
            "api_key_present": bool(self.api_key),
            "totp_configured": bool(self.totp_secret),
        }


def load_credentials() -> AngelCredentials:
    """Read the credentials from settings, or say exactly what is missing.

    Refuses rather than half-configuring. A feed that starts with three of
    four values and fails at the login call reports an authentication error,
    which sends whoever is debugging it to Angel's status page instead of to
    their own `.env`.
    """
    s = get_settings()
    secret = s.angel_mpin or s.angel_password
    missing = [
        name for name, value in (
            ("ANGEL_API_KEY", s.angel_api_key),
            ("ANGEL_CLIENT_CODE", s.angel_client_code),
            ("ANGEL_MPIN or ANGEL_PASSWORD", secret),
            ("ANGEL_TOTP_SECRET", s.angel_totp_secret),
        ) if not value
    ]
    if missing:
        raise AngelNotConfigured(
            "ANGEL_ENABLED is set but these are not: " + ", ".join(missing))

    return AngelCredentials(
        api_key=s.angel_api_key, client_code=s.angel_client_code,
        secret=secret, totp_secret=s.angel_totp_secret)


# --------------------------------------------------------------------------
# keeping the vendor's logger from doing what this module refuses to
# --------------------------------------------------------------------------

REDACTED = "«redacted»"

# The SDK logs the entire request body at ERROR level when a login is
# refused, which puts the MPIN, the fresh TOTP and the API key on stderr —
# and from there into Docker's log, and from there into whatever collects it.
# Observed on smartapi-python 1.5.5:
#
#   Request: {'clientcode': 'C1', 'password': '1111', 'totp': '610559'}
#   Headers: {... 'X-PrivateKey': '<api key>' ...}
#
# Nothing in this file writes that, and no API response carries it, and it
# would still have leaked. So the values are scrubbed at the logger.
#
# Two mechanisms, because either alone has a hole. Matching the literal
# values cannot miss a field name we did not think of; matching the field
# names catches the TOTP, which is generated per attempt and therefore not a
# value we hold. Anything that survives both is not a credential we own.
_SENSITIVE_FIELDS = re.compile(
    r"((?:password|mpin|totp|clientcode|privatekey|jwttoken|refreshtoken"
    r"|feedtoken|x-privatekey|access_token|accesstoken)"
    r"[\'\"]?\s*[:=]\s*[\'\"]?)"      # the field name and its separator
    r"([^\'\",}\s]+)",                 # the value, up to the next delimiter
    re.IGNORECASE)


def redact(text: str, secrets=()) -> str:
    """Remove credential values from a line of text.

    Two passes, because either alone leaves a hole. The literal pass cannot
    miss a field name nobody thought of; the field-name pass catches the
    TOTP, which is generated per attempt and is therefore not a value we
    hold to compare against.

    The literal pass ignores anything under three characters — a two-letter
    client code would otherwise match inside ordinary words and turn the log
    into confetti.
    """
    for secret in secrets:
        if secret and len(str(secret)) >= 3:
            text = text.replace(str(secret), REDACTED)
    return _SENSITIVE_FIELDS.sub(lambda m: m.group(1) + REDACTED, text)


class CredentialFilter(logging.Filter):
    """Scrubs credentials out of records before a handler formats them."""

    def __init__(self, secrets=()) -> None:
        super().__init__(name="quantdesk-angel-redaction")
        self.secrets = tuple(str(s) for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:                 # pragma: no cover - defensive
            return True
        scrubbed = redact(message, self.secrets)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True


def install_log_redaction(credentials: AngelCredentials | None = None) -> None:
    """Attach the scrubber to the vendor's logger and to ours.

    `logzero.logger` has `propagate = False` and its own stderr handler, so
    it never reaches the root logger's filters — it has to be attached to
    directly. Idempotent: called on every login, and a second filter would
    just do the same work twice.
    """
    secrets: tuple[str, ...] = ()
    if credentials is not None:
        secrets = (credentials.api_key, credentials.secret,
                   credentials.totp_secret, credentials.client_code)

    targets = [logging.getLogger()]
    try:
        import logzero
        targets.append(logzero.logger)
    except ImportError:                   # pragma: no cover - packaging
        pass

    for logger in targets:
        for existing in list(logger.filters):
            if isinstance(existing, CredentialFilter):
                logger.removeFilter(existing)
        logger.addFilter(CredentialFilter(secrets))
        # A filter on the logger does not run for handlers attached to it in
        # every path, so the handler gets one too.
        for handler in getattr(logger, "handlers", []):
            for existing in list(handler.filters):
                if isinstance(existing, CredentialFilter):
                    handler.removeFilter(existing)
            handler.addFilter(CredentialFilter(secrets))


@dataclass
class AngelSession:
    """A live Angel session: the tokens the socket needs, and nothing else."""
    auth_token: str
    feed_token: str
    refresh_token: str
    client_code: str
    api_key: str
    created_at: datetime

    @property
    def redacted(self) -> dict:
        return {
            "client_code": f"…{self.client_code[-2:]}" if self.client_code else None,
            "has_auth_token": bool(self.auth_token),
            "has_feed_token": bool(self.feed_token),
            "created_at": self.created_at.isoformat(),
        }

    def __repr__(self) -> str:            # pragma: no cover - trivial
        """Never render the tokens.

        A session lands in a traceback the first time the socket raises, and
        a default dataclass repr would put a live JWT into the log file.
        """
        return f"AngelSession({self.redacted})"


def current_totp(secret: str, *, now: datetime | None = None) -> str:
    """The six digits Angel wants, from the shared secret.

    Imported lazily so the module can be read, and most of it tested,
    without pyotp installed.
    """
    try:
        import pyotp
    except ImportError as exc:            # pragma: no cover - packaging
        raise AngelError(
            "pyotp is required for Angel authentication — it generates the "
            "TOTP that `generateSession` expects") from exc

    totp = pyotp.TOTP(secret)
    return totp.now() if now is None else totp.at(now)


def _smart_connect(api_key: str):
    """The vendor client, imported at the point of use."""
    try:
        from SmartApi import SmartConnect
    except ImportError as exc:            # pragma: no cover - packaging
        raise AngelError(
            "smartapi-python is not installed — the Angel feed cannot "
            "authenticate without it") from exc
    return SmartConnect(api_key=api_key)


def login(credentials: AngelCredentials | None = None, *,
          connect_factory=None) -> AngelSession:
    """Generate an Angel session. Returns the tokens the websocket needs.

    `connect_factory` exists so the whole path — TOTP, the call, the shape
    of the response, the failure modes — is testable without a live account
    and without the SDK installed.
    """
    credentials = credentials or load_credentials()
    # Before the first call, not after: the SDK logs the request body on a
    # refused login, and a refusal is exactly when the scrubber is needed.
    install_log_redaction(credentials)
    client = (connect_factory or _smart_connect)(credentials.api_key)

    otp = current_totp(credentials.totp_secret)
    try:
        response = client.generateSession(
            credentials.client_code, credentials.secret, otp)
    except Exception as exc:
        # Deliberately does not include the exception's own text at INFO.
        # Some SDK errors echo the request back, and the request contains
        # the MPIN.
        log.warning("Angel generateSession failed: %s", type(exc).__name__)
        raise AngelError(f"Angel login failed: {type(exc).__name__}") from exc

    return session_from_response(response, client, credentials)


def session_from_response(response, client, credentials: AngelCredentials
                          ) -> AngelSession:
    """Pull the tokens out of Angel's login response, or say why not.

    Angel answers a rejected login with HTTP 200 and `status: false`, so
    trusting the transport here would produce a session object holding
    `None` for every token and a websocket that fails much later with
    something unrelated.
    """
    if not isinstance(response, dict):
        raise AngelError(
            f"Angel returned {type(response).__name__}, not a login response")

    if response.get("status") is False or not response.get("data"):
        raise AngelError(
            "Angel refused the login: "
            f"{response.get('message') or response.get('errorcode') or 'no reason given'}")

    data = response["data"] or {}
    auth_token = data.get("jwtToken") or data.get("accessToken")
    refresh_token = data.get("refreshToken") or ""

    # The feed token comes from a separate call on the SDK, and it is the
    # one the socket actually authenticates with.
    feed_token = data.get("feedToken")
    if not feed_token and hasattr(client, "getfeedToken"):
        try:
            feed_token = client.getfeedToken()
        except Exception as exc:
            raise AngelError(
                f"Angel logged in but would not issue a feed token: "
                f"{type(exc).__name__}") from exc

    if not auth_token or not feed_token:
        raise AngelError(
            "Angel's login response carried no "
            + ("auth token" if not auth_token else "feed token"))

    return AngelSession(
        auth_token=auth_token, feed_token=feed_token,
        refresh_token=refresh_token, client_code=credentials.client_code,
        api_key=credentials.api_key, created_at=datetime.now(UTC))


# --------------------------------------------------------------------------
# ticks
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Tick:
    """One decoded LTP print."""
    token: str
    price: float
    source_time: datetime
    sequence: int | None = None
    raw_timestamp_ms: int | None = None


class MalformedTick(ValueError):
    """A message that is not a usable LTP tick.

    Its own type because the feed's response is to count it and carry on,
    which is different from its response to a socket error. A feed that
    treated one bad frame as a disconnection would reconnect in a loop.
    """


def decode_tick(message, *, now: datetime | None = None) -> Tick:
    """Turn one SmartWebSocketV2 message into a price and an instant.

    Raises `MalformedTick` for anything it cannot read. Returning a
    best-effort price with a guessed timestamp would put an invented number
    on the desk, which is the one thing this platform never does.
    """
    if not isinstance(message, dict):
        raise MalformedTick(f"expected a dict, got {type(message).__name__}")

    token = message.get("token")
    raw_price = message.get("last_traded_price")
    raw_stamp = message.get("exchange_timestamp")

    if raw_price is None:
        raise MalformedTick("no last_traded_price in the message")

    try:
        price = float(raw_price) / PAISE
    except (TypeError, ValueError) as exc:
        raise MalformedTick(f"last_traded_price {raw_price!r} is not a number") from exc

    if price <= 0:
        # Angel sends a zero LTP for an instrument that has not traded. It
        # is not a price, and publishing it would draw the index at zero.
        raise MalformedTick(f"last_traded_price {price} is not a tradable price")

    if raw_stamp is None:
        raise MalformedTick("no exchange_timestamp in the message")

    try:
        source_time = datetime.fromtimestamp(int(raw_stamp) / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise MalformedTick(
            f"exchange_timestamp {raw_stamp!r} is not epoch milliseconds") from exc

    reference = now or datetime.now(UTC)
    skew = (reference - source_time).total_seconds()
    if skew < -MAX_FUTURE_SECONDS:
        raise MalformedTick(
            f"exchange_timestamp {raw_stamp!r} decodes to "
            f"{source_time.isoformat()}, {-skew / 3600:.2f}h in the future — "
            "a tick cannot print ahead of now, so the timestamp units or "
            "timezone are wrong, not the feed")
    if skew > MAX_AGE_SECONDS:
        raise MalformedTick(
            f"exchange_timestamp {raw_stamp!r} decodes to "
            f"{source_time.isoformat()}, {skew / 86400:.0f} days old — that "
            "is a unit error, not a late tick")

    return Tick(
        token=str(token) if token is not None else "",
        price=round(price, 2),
        source_time=source_time,
        sequence=message.get("sequence_number"),
        raw_timestamp_ms=int(raw_stamp),
    )


def token_list(token: str, exchange_type: int = NSE_CM) -> list[dict]:
    """The subscription shape SmartWebSocketV2 expects."""
    return [{"exchangeType": int(exchange_type), "tokens": [str(token)]}]
