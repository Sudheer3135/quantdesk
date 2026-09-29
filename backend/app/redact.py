"""Credential-free descriptions of database connections and their errors.

A connection URL can carry its password in the authority
(`postgresql://user:pw@host/db`), in the query string (`?password=pw`),
URL-encoded in either place, and a driver can echo any of that back inside
an exception — libpq repeats a malformed conninfo string verbatim. So a
diagnostic never prints the URL it was given. It is built from `describe()`
(scheme, host, port, database — nothing else) plus `redact()` of whatever
the error said. Pass 2E-A.1.

Pass 2E-B made this the boundary of every log line a process writes, not
only of database diagnostics. uvicorn's access and error loggers have their
own handlers and do not propagate, so the root-logger filters never saw them,
and they wrote the dashboard's WebSocket `?key=<API key>` into backend.log
on every connection. `install_log_redaction` now redacts at the last step
before any handler writes — every handler, whenever it was configured.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
import traceback
from collections.abc import Mapping

from sqlalchemy.engine import make_url

MASK = "***"

# Parameter names that name a secret, in URLs, conninfo strings and JSON.
SECRET_NAMES = (
    "password", "passwd", "pwd", "pass", "sslpassword", "secret", "client_secret",
    "token", "access_token", "refresh_token", "auth_token", "api_key", "apikey",
    "key", "authorization",
)


def _encoded_word(word: str) -> str:
    """The word with each character literal or percent-encoded."""
    return "".join(
        "(?:" + re.escape(ch) + "|" + "".join(f"%{b:02x}" for b in ch.encode()) + ")"
        for ch in word)


# scheme://user:SECRET@  — greedy to the last '@' of the token, so a
# password containing an unencoded '@' is still covered.
_AUTHORITY = re.compile(r"(?P<head>[a-z][a-z0-9+.\-]*://[^\s:/@]*):(?P<secret>[^\s]*)@",
                        re.IGNORECASE)
# name=SECRET, name: SECRET, "name": "SECRET", name='SECRET',
# ('name', 'SECRET'), (b'name', b'SECRET') — in a query string, a libpq
# conninfo string, a repr, JSON, a header dump or a raw ASGI header list.
# The name may carry a prefix (`feed_token`, `x-api-key`) and be
# percent-encoded, as may the separator (`key%3D…`): the server decodes
# `?k%65y=` to `key`, so the log has to treat it as `key`.
#
# Two parsers, because a URL query and a Python/header representation end a
# value at different places.
#
# _QUERY_PARAMETER: a parameter of a URL query (after `?` or `&`, or their
# encoded forms in a URL nested inside another). Its value is everything up
# to the URL's own delimiters — `&`, `#`, whitespace — whatever legal
# punctuation it contains: `?token=prefix)SUFFIX&x=1` is one value,
# `prefix)SUFFIX`, and all of it is masked. A `"` ends it only where it
# closes a logged request line (`"GET /x?key=… HTTP/1.1"`,
# `"WebSocket /ws?key=…" [accepted]`), i.e. before whitespace or the end;
# a raw `"` inside a value does not.
#
# _PARAMETER: the representations — conninfo strings, reprs, JSON, header
# dumps, raw ASGI header lists. A quoted value is masked whole, escapes
# included, so a scheme inside it (`'Negotiate …'`, `b'Digest …'`) goes with
# it. An unquoted value runs to whitespace, `&`, `#` or a quote; `;`, `,`
# and brackets do not end it, so an unquoted secret is never cut short by
# its own punctuation (a trailing `)` may be masked with it).
_NAME_PREFIX = r"(?:[a-z0-9]+(?:[_.\-]|%5f|%2d|%2e))*(?:"
_NAME_WORDS = ("|".join(_encoded_word(n) for n in sorted(SECRET_NAMES, key=len, reverse=True))
               + r")(?![a-z0-9])")
_QUERY_PARAMETER = re.compile(
    r"(?P<name>(?:(?<=[?&])|(?<=%3f)|(?<=%26))" + _NAME_PREFIX + _NAME_WORDS + r")"
    r"(?P<sep>=|%3d)"
    r"(?P<secret>(?:[^\s&#\"]|\"(?![\s]|$))+)",
    re.IGNORECASE)
_NAME = (
    # Starts after a non-word character, or after an encoded ? & / or space
    # (a URL nested inside another URL's query).
    r"(?P<name>(?:(?<![a-z0-9%])|(?<=%3f)|(?<=%26)|(?<=%2f)|(?<=%20))"
    + _NAME_PREFIX + _NAME_WORDS + r")")
_QUOTED = r"b?\"(?:[^\"\\]|\\.)*\"|b?'(?:[^'\\]|\\.)*'"
_PARAMETER = re.compile(
    _NAME
    + r"(?P<sep>[\"']?\s*(?:[=:]|%3d|%3a)\s*|[\"']\s*,\s*(?=b?[\"']))"
    r"(?P<secret>(?:(?:bearer|basic|token)\s+)?(?:" + _QUOTED + r"|[^\s&#\"']+))",
    re.IGNORECASE)
# An `Authorization:` header line: every scheme (Bearer, Basic, Negotiate,
# Digest, AWS4-HMAC-SHA256, …) carries its credential after the scheme,
# Digest in several fields, so an unquoted value is masked to the end of the
# line. A quoted one is left to _PARAMETER, which masks the quotes' content.
_AUTHORIZATION_LINE = re.compile(
    r"(?P<head>(?<![a-z0-9])(?:proxy-)?authorization[\"']?\s*:\s*+)(?!b?[\"'])[^\r\n]+",
    re.IGNORECASE)
# A JSON Web Token on its own. Angel's session tokens are JWTs.
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]+")


def describe(url) -> str:
    """scheme://host:port/database — the only parts a diagnostic may show."""
    try:
        u = make_url(url)
    except Exception:  # noqa: BLE001 — an unparseable URL is still not printed
        return "<unparseable database URL>"
    backend = u.get_backend_name()
    if backend == "sqlite":
        return f"sqlite:///{u.database or ':memory:'}"
    port = f":{u.port}" if u.port else ""
    return f"{backend}://{u.host or 'localhost'}{port}/{u.database or ''}"


def _secrets_of(url) -> list[str]:
    """The secrets the URL carries, decoded."""
    try:
        u = make_url(url)
    except Exception:  # noqa: BLE001
        return []
    values = [str(u.password)] if u.password else []
    for name, value in u.query.items():
        if name.lower() in SECRET_NAMES:
            values.extend(value if isinstance(value, tuple) else (value,))
    return [v for v in values if v]


def _any_encoding(secret: str) -> re.Pattern:
    """The secret with each character literal *or* percent-encoded (either
    hex case), and a space also as '+' — so a fully, partly or un-encoded
    echo of it matches alike."""
    parts = []
    for ch in secret:
        forms = [re.escape(ch)] + ["".join(f"%{b:02x}" for b in ch.encode())]
        if ch == " ":
            forms.append(r"\+")
        parts.append("(?:" + "|".join(forms) + ")")
    return re.compile("".join(parts), re.IGNORECASE)


def redact(text, url=None) -> str:
    """`text` with every credential removed. With `url`, that URL's own
    secrets are also removed wherever they appear verbatim, however the
    driver chose to quote them."""
    out = str(text)
    for secret in sorted(_secrets_of(url) if url is not None else (), key=len, reverse=True):
        out = _any_encoding(secret).sub(MASK, out)
    out = _AUTHORITY.sub(lambda m: f"{m.group('head')}:{MASK}@", out)
    out = _JWT.sub(MASK, out)
    out = _AUTHORIZATION_LINE.sub(lambda m: f"{m.group('head')}{MASK}", out)
    out = _QUERY_PARAMETER.sub(lambda m: f"{m.group('name')}{m.group('sep')}{MASK}", out)
    return _PARAMETER.sub(lambda m: f"{m.group('name')}{m.group('sep')}{MASK}", out)


# Secret values this process knows (its API key, broker credentials, URL
# passwords), removed from log output wherever they appear and however
# encoded, not only after a recognised parameter name. A value of
# LITERAL_MIN characters or more is removed anywhere, even inside a longer
# word. A shorter one — a 4-digit MPIN — is removed only as a whole token,
# so it cannot vanish out of the middle of a price or a timestamp. Below
# TOKEN_MIN a value cannot be told apart from ordinary text at all: it is
# protected only after a parameter name, and install_log_redaction warns.
LITERAL_MIN = 8
TOKEN_MIN = 4
_KNOWN: set[str] = set()
_LITERALS: list[re.Pattern] = []
_KNOWN_LOCK = threading.Lock()


def remember_secrets(*values) -> int:
    """Add secret values to the log boundary's literal list. Idempotent.
    Returns how many were too short to mask outside a parameter name."""
    too_short = 0
    with _KNOWN_LOCK:
        for value in values:
            value = "" if value is None else str(value)
            if not value or value in _KNOWN:
                continue
            if len(value) >= LITERAL_MIN:
                _LITERALS.append(_any_encoding(value))
            elif len(value) >= TOKEN_MIN:
                _LITERALS.append(re.compile(
                    r"(?<![A-Za-z0-9])" + _any_encoding(value).pattern + r"(?![A-Za-z0-9])",
                    re.IGNORECASE))
            else:
                too_short += 1
                continue
            _KNOWN.add(value)
        _LITERALS.sort(key=lambda p: len(p.pattern), reverse=True)
    return too_short


def scrub(text) -> str:
    """`redact`, plus every secret value this process has remembered."""
    out = str(text)
    for pattern in list(_LITERALS):
        out = pattern.sub(MASK, out)
    return redact(out)


# Settings fields that hold a secret value (config.Settings).
_SECRET_FIELD = re.compile(r"(?:^|_)(?:api_key|secret|password|mpin|access_token)$")


def settings_secrets(settings) -> list[str]:
    """Every secret value in the settings: the credential fields, and the
    passwords inside the connection URLs."""
    values = []
    for name, value in vars(settings).items():
        if isinstance(value, str) and _SECRET_FIELD.search(name):
            values.append(value)
        elif isinstance(value, str) and name.endswith("_url"):
            values.extend(_secrets_of(value))
    return values


def error_text(exc: BaseException, url=None) -> str:
    """The first line of an exception (the driver's own, when wrapped), redacted."""
    inner = getattr(exc, "orig", None) or exc
    lines = str(inner).strip().splitlines() or [type(inner).__name__]
    return redact(lines[0], url)


def _url_hint():
    """The configured database URL, for removing its literal secrets — read
    without letting a settings error escape (it may quote the URL)."""
    try:
        from .config import get_settings
        return get_settings().database_url
    except Exception:  # noqa: BLE001
        return os.environ.get("DATABASE_URL")


# --- The log boundary -------------------------------------------------------

_ORIGINAL_FORMAT = logging.Handler.format


def _redacting_format(self, record) -> str:
    """Every handler's output, redacted as the very last step — whichever
    formatter it uses (uvicorn's AccessFormatter included) and whenever the
    handler was configured."""
    return scrub(_ORIGINAL_FORMAT(self, record))


def _redacting_handle_error(self, record) -> None:
    """The stdlib prints the raw message and arguments of a record that
    failed to format. Print the failure, not the record."""
    if not (logging.raiseExceptions and sys.stderr):
        return
    kind, exc = sys.exc_info()[:2]
    try:
        sys.stderr.write(scrub(
            f"--- Logging error in {type(self).__name__} for a record from "
            f"{getattr(record, 'name', '?')}: "
            f"{getattr(kind, '__name__', kind)}: {exc} (message withheld)\n"))
    except Exception:  # noqa: BLE001, S110 — nothing left to report it to
        pass


def _redacting_thread_hook(args) -> None:
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread is not None else "?"
    text = "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                              args.exc_traceback))
    try:
        sys.stderr.write(scrub(f"Exception in thread {name}:\n{text}"))
    except Exception:  # noqa: BLE001, S110
        pass


def _redacting_excepthook(kind, exc, tb) -> None:
    sys.stderr.write(scrub("".join(traceback.format_exception(kind, exc, tb))))


def _scrub_args(args):
    if isinstance(args, tuple):
        return tuple(scrub(a) if isinstance(a, str) else a for a in args)
    if isinstance(args, Mapping):
        return {k: scrub(v) if isinstance(v, str) else v for k, v in args.items()}
    return args


def install_log_redaction(url=None, secrets=()) -> None:
    """Make every log line this process writes credential-free.

    Three layers, each idempotent:
      - the boundary: `logging.Handler.format` redacts the finished line, so
        no handler — uvicorn's own, Alembic's fileConfig, one added later —
        writes an unredacted one; a record that fails to format is reported
        without its content;
      - at creation: a record's string arguments and any traceback are
        redacted as it is made, so a consumer that reads records rather
        than formatted lines sees them clean too. The argument tuple keeps
        its shape, because formatters such as uvicorn's unpack it;
      - uncaught exceptions in threads and the main thread print redacted.

    `url` and `secrets` are secret values to remove wherever they appear."""
    too_short = remember_secrets(*secrets, *(_secrets_of(url) if url is not None else ()))
    if too_short:
        logging.getLogger(__name__).warning(
            "%d configured credential value(s) shorter than %d characters can be "
            "masked only after a parameter name; use longer ones", too_short, TOKEN_MIN)

    logging.Handler.format = _redacting_format
    logging.Handler.handleError = _redacting_handle_error
    threading.excepthook = _redacting_thread_hook
    if sys.excepthook is sys.__excepthook__:
        sys.excepthook = _redacting_excepthook

    base = logging.getLogRecordFactory()
    if getattr(base, "_quantdesk_redacting", False):
        return
    formatter = logging.Formatter()

    def factory(*args, **kwargs):
        record = base(*args, **kwargs)
        try:
            if record.args:
                record.args = _scrub_args(record.args)
            elif isinstance(record.msg, str):
                record.msg = scrub(record.msg)
            if record.exc_info:
                record.exc_text = scrub(formatter.formatException(record.exc_info))
                record.exc_info = None
            if record.stack_info:
                record.stack_info = scrub(record.stack_info)
        except Exception:  # noqa: BLE001 — a malformed record stays unprinted
            record.msg, record.args = "<unformattable log message>", None
        return record

    factory._quantdesk_redacting = True
    logging.setLogRecordFactory(factory)


def run_cli(main) -> int:
    """The last boundary of a schema/migration command. Whatever escapes
    `main` — a driver error quoting its DSN, a settings error quoting the
    URL — is reported as one redacted line, never as a traceback or repr."""
    url = _url_hint()
    install_log_redaction(url)

    def hook(kind, exc, tb):
        print(f"error: {kind.__name__}: {error_text(exc, url)}", file=sys.stderr)

    sys.excepthook = hook
    try:
        return main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {error_text(exc, url)}", file=sys.stderr)
        return 8
