"""Credential-free descriptions of database connections and their errors.

A connection URL can carry its password in the authority
(`postgresql://user:pw@host/db`), in the query string (`?password=pw`),
URL-encoded in either place, and a driver can echo any of that back inside
an exception — libpq repeats a malformed conninfo string verbatim. So a
diagnostic never prints the URL it was given. It is built from `describe()`
(scheme, host, port, database — nothing else) plus `redact()` of whatever
the error said. Pass 2E-A.1.
"""
from __future__ import annotations

import logging
import os
import re
import sys

from sqlalchemy.engine import make_url

MASK = "***"

# Parameter names that name a secret, in URLs, conninfo strings and JSON.
SECRET_NAMES = (
    "password", "passwd", "pwd", "pass", "sslpassword", "secret", "client_secret",
    "token", "access_token", "refresh_token", "auth_token", "api_key", "apikey",
)

# scheme://user:SECRET@  — greedy to the last '@' of the token, so a
# password containing an unencoded '@' is still covered.
_AUTHORITY = re.compile(r"(?P<head>[a-z][a-z0-9+.\-]*://[^\s:/@]*):(?P<secret>[^\s]*)@",
                        re.IGNORECASE)
# name=SECRET, name: SECRET, "name": "SECRET", name='SECRET' — in a query
# string, a libpq conninfo string, a repr or JSON.
_PARAMETER = re.compile(
    r"(?P<name>\b(?:" + "|".join(sorted(SECRET_NAMES, key=len, reverse=True)) + r"))"
    r"(?P<quote>[\"']?)(?P<sep>\s*[=:]\s*)"
    r"(?P<secret>\"[^\"]*\"|'[^']*'|[^\s&;,)\"'}\]]+)",
    re.IGNORECASE)


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
    return _PARAMETER.sub(
        lambda m: f"{m.group('name')}{m.group('quote')}{m.group('sep')}{MASK}", out)


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


def install_log_redaction(url=None) -> None:
    """Every log record this process creates is redacted at creation: its
    message, and any traceback, which is formatted and redacted then — so a
    handler configured later (Alembic's fileConfig) cannot print it raw."""
    base = logging.getLogRecordFactory()
    if getattr(base, "_quantdesk_redacting", False):
        return
    formatter = logging.Formatter()

    def factory(*args, **kwargs):
        record = base(*args, **kwargs)
        try:
            record.msg, record.args = redact(record.getMessage(), url), None
        except Exception:  # noqa: BLE001 — a malformed record stays unprinted
            record.msg, record.args = "<unformattable log message>", None
        if record.exc_info:
            record.exc_text = redact(formatter.formatException(record.exc_info), url)
            record.exc_info = None
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
