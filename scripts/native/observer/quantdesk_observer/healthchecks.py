"""Outbound pings to hosted Healthchecks.io. Prepared; nothing is connected.

Four independent checks, each with its own ping credential (a UUID that is
the whole secret): `liveness`, `premarket`, `midsession`, `afterclose`. There
is no fifth.

What a ping means:

- **success** — the associated assessment completed and passed. For
  `afterclose`, whose meaning is "after-close assessment and observer
  evidence-accounting attention", also that no review checkpoint is
  unacknowledged and no observer fault is open. Sent for nothing else: not
  on start, not for a failed or late assessment.
- **fail** — the assessment completed and did not pass, could not run, or
  (after-close) a review checkpoint is unacknowledged or the observer's own
  housekeeping failed. `liveness` means only that the observer ran recently
  and could reach out.
- no ping — the heartbeat is missing and the cause is unknown. It does not
  show that the VM is powered off. Healthchecks.io raises that alert by
  itself; it is the part the VM cannot do.

One call here makes one request, with a five-second timeout, and never
waits or tries again. Whether and when to try again is decided by the
caller from the stored attempt records, on a later invocation.

A request that fails is "undelivered; remote receipt unknown": a timeout can
follow a request the far side did receive, so neither delivery nor
non-delivery is ever claimed beyond what the reply showed, and a 2xx reply
says nothing about a Telegram message or an email reaching anyone.

The body is a few words of status. It never carries credentials, trading
records, quotes, positions or logs. The credential appears only in the URL of
the request, and every error and record is redacted before it is kept.
"""
from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CHECKS = ("liveness", "premarket", "midsession", "afterclose")
KINDS = {"success": "", "fail": "/fail"}
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ANY_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
MAX_BODY = 400
REQUEST_TIMEOUT = 5.0     # Astra's policy; not configurable
SETTINGS = frozenset({"enabled", "base_url", "credentials_file"})
UNDELIVERED = "Notification undelivered; remote receipt unknown."


class PingConfigError(Exception):
    """The ping configuration is unusable. Nothing is sent."""


def redact(text: str) -> str:
    """`text` with anything shaped like a ping credential removed."""
    return ANY_UUID.sub("<redacted>", str(text))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError(f"redirect ({code}) refused")


def https_post(url: str, body: bytes, timeout: float) -> int:
    """POST over verified TLS, without proxies or redirects. Returns the status."""
    context = ssl.create_default_context()  # verifies the certificate and the host name
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                         urllib.request.HTTPSHandler(context=context))
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "text/plain; charset=utf-8",
                                              "User-Agent": "quantdesk-observer"})
    with opener.open(request, timeout=timeout) as reply:
        reply.read(1024)
        return reply.status


def load_settings(cfg: dict) -> dict | None:
    """The validated ping settings, or None when pings are switched off.

    Credentials come from a separate file readable only by the observer
    account, never from the main configuration.
    """
    settings = cfg.get("healthchecks") or {}
    if not settings.get("enabled"):
        return None
    base = urllib.parse.urlsplit(str(settings.get("base_url", "")))
    if (base.scheme != "https" or not base.hostname or base.username or base.password
            or base.query or base.fragment or base.path not in ("", "/")):
        raise PingConfigError("healthchecks.base_url must be a plain https:// origin")
    extra = sorted(set(settings) - SETTINGS)
    if extra:
        raise PingConfigError("healthchecks settings not recognised (timeouts and retries are "
                              f"fixed by policy, not configured): {', '.join(extra)}")
    path = Path(str(settings.get("credentials_file", "")))
    try:
        if path.stat().st_mode & 0o077:
            raise PingConfigError("the ping credentials file is readable by other accounts")
        credentials = json.loads(path.read_text())
    except PingConfigError:
        raise
    except (OSError, ValueError) as exc:
        raise PingConfigError(f"the ping credentials file is unusable: {type(exc).__name__}") \
            from exc
    if (not isinstance(credentials, dict) or set(credentials) != set(CHECKS)
            or not all(isinstance(v, str) and UUID.match(v) for v in credentials.values())):
        raise PingConfigError("the ping credentials file must hold one UUID for each of "
                              + ", ".join(CHECKS))
    if len(set(credentials.values())) != len(CHECKS):
        raise PingConfigError("each check needs its own ping credential")
    return {"base": f"https://{base.netloc}", "credentials": credentials}


def ping(settings: dict, check: str, kind: str, body: str, *, post=https_post,
         timeout: float = REQUEST_TIMEOUT) -> dict:
    """Make one request. Returns a redacted outcome; never raises for a transport failure."""
    if check not in CHECKS or kind not in KINDS:
        raise PingConfigError("unknown check or ping kind")
    url = f"{settings['base']}/{settings['credentials'][check]}{KINDS[kind]}"
    payload = redact(body)[:MAX_BODY].encode()
    try:
        status = post(url, payload, min(float(timeout), REQUEST_TIMEOUT))
        delivered, note = 200 <= status < 300, f"HTTP {status}"
    except Exception as exc:  # a failed ping is recorded, never raised
        delivered, note = False, redact(f"{type(exc).__name__}: {exc}")[:200]
    return {"check": check, "kind": kind, "body": payload.decode(), "delivered": delivered,
            "result": note,
            "receipt": ("the remote endpoint answered 2xx; onward Telegram or email delivery "
                        "is not observed") if delivered else UNDELIVERED}
