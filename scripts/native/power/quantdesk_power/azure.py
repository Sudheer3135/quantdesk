"""Deallocating this VM through Azure Resource Manager, and learning what became of it.

TOKEN — the VM's system-assigned managed identity, from the instance metadata
service at the link-local address, plain HTTP to a literal IP, no proxy (the
standard-library HTTP client consults no proxy settings). Only the root
controller's cgroup may reach that address (nftables, see nftables/).

REQUEST — POST .../virtualMachines/<vm>/deallocate, HTTPS to the one host
`management.azure.com`, with a client request id. The request's stages are
kept apart, because they decide what is known:

  connection or TLS handshake fails  -> NOT_SENT  (no request byte left)
  sending the request fails          -> UNKNOWN   (part of it may have left)
  no response, or it cannot be read  -> UNKNOWN   (Azure may have received it)
  200                                -> COMPLETED (Azure reports it done)
  202                                -> ACCEPTED  (in progress; poll it)
  400, 401, 403, 404, 409, 429       -> REJECTED  (Azure answered and did not accept it)
  any other status                   -> UNKNOWN   (for example 500 or 504: an
                                        error that does not rule out acceptance)

A request is never sent twice. Nothing is inferred from time passing, from
the VM still running or from a later boot.

POLLING — only an ACCEPTED request is polled, and only through the URL Azure
returned for this operation: `Azure-AsyncOperation` (a JSON `status` of
InProgress, Succeeded, Failed or Canceled), else `Location` (202 while in
progress; 200 or 204 when done). A URL that is not HTTPS on
`management.azure.com` under this subscription is not followed. A 403 means
this identity may not read the operation; that is recorded and the outcome
stays unknown (no broader permission is assumed).

RETRY-AFTER — never polled earlier than Azure asked. `delay-seconds` (digits
only) or an HTTP-date is honoured as given, with a floor of RETRY_MIN and no
ceiling; when absent, RETRY_DEFAULT. A malformed value (negative, fractional,
NaN, infinite, unparseable) is not guessed at: RETRY_MALFORMED, a deliberately
long wait. When the next permitted read lies beyond the poll budget, nothing
is read early: the outcome stays unknown and the earliest permitted time is
returned, so a later reconciliation waits for it too.
"""
from __future__ import annotations

import email.utils
import http.client
import json
import re
import ssl
import time
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

IMDS_HOST = "169.254.169.254"
IMDS_PATH = ("/metadata/identity/oauth2/token?api-version=2018-02-01"
             "&resource=https%3A%2F%2Fmanagement.azure.com%2F")
ARM_HOST = "management.azure.com"
MAX_BODY = 64 * 1024
DEFINITE_REJECTIONS = {400, 401, 403, 404, 409, 429}
RETRY_MIN, RETRY_DEFAULT, RETRY_MALFORMED = 5.0, 15.0, 300.0
DELAY_SECONDS = re.compile(r"^[0-9]{1,9}$")

NOT_SENT, REJECTED, ACCEPTED, COMPLETED, UNKNOWN = (
    "NOT_SENT", "REJECTED", "ACCEPTED", "COMPLETED", "UNKNOWN")
POLL_SUCCEEDED, POLL_FAILED, POLL_IN_PROGRESS, POLL_UNAVAILABLE = (
    "Succeeded", "Failed", "InProgress", "Unavailable")


class AzureError(Exception):
    pass


@dataclass
class Submission:
    outcome: str
    status: int | None = None
    detail: str = ""
    async_url: str | None = None
    location_url: str | None = None
    retry_after: float | None = None
    error_code: str | None = None

    def as_dict(self) -> dict:
        return {"outcome": self.outcome, "status": self.status, "detail": self.detail[:300],
                "async_url": self.async_url, "location_url": self.location_url,
                "retry_after": self.retry_after, "error_code": self.error_code}


def retry_after(value, now: datetime) -> float:
    """Seconds to wait before the next status read, never less than Azure asked."""
    if value is None:
        return RETRY_DEFAULT
    text = str(value).strip()
    if DELAY_SECONDS.match(text):
        return max(float(int(text)), RETRY_MIN)
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return RETRY_MALFORMED
    if when is None or when.tzinfo is None:
        return RETRY_MALFORMED
    return max((when - now).total_seconds(), RETRY_MIN)


def _error_code(body: bytes) -> str | None:
    try:
        parsed = json.loads(body)
        code = parsed.get("error", {}).get("code")
        return str(code)[:100] if code else None
    except (ValueError, AttributeError):
        return None


class Arm:
    """Azure Resource Manager for one VM. `connect` builds connections (tests replace it)."""

    def __init__(self, cfg: dict, connect=None, sleep=time.sleep, monotonic=time.monotonic,
                 wall=lambda: datetime.now(UTC)):
        az = cfg["azure"]
        self.subscription = az["subscription_id"]
        self.vm_path = (f"/subscriptions/{az['subscription_id']}/resourceGroups/"
                        f"{az['resource_group']}/providers/Microsoft.Compute/virtualMachines/"
                        f"{az['vm_name']}")
        self.api_version = az["api_version"]
        self.timeout = az["request_timeout_seconds"]
        self.poll_budget = az["poll_budget_seconds"]
        self.connect = connect or self._connect
        self.sleep, self.monotonic, self.wall = sleep, monotonic, wall
        self._token: str | None = None

    def _connect(self, host: str):
        if host == IMDS_HOST:
            return http.client.HTTPConnection(IMDS_HOST, 80, timeout=self.timeout)
        if host == ARM_HOST:
            return http.client.HTTPSConnection(ARM_HOST, 443, timeout=self.timeout,
                                               context=ssl.create_default_context())
        raise AzureError(f"{host} is not an allowed host")

    # -- the managed identity's token -------------------------------------------------
    def token(self) -> str:
        if self._token:
            return self._token
        conn = self.connect(IMDS_HOST)
        try:
            conn.request("GET", IMDS_PATH, headers={"Metadata": "true"})
            response = conn.getresponse()
            body = response.read(MAX_BODY + 1)
        except (OSError, http.client.HTTPException) as exc:
            raise AzureError(f"the managed-identity token could not be obtained "
                             f"({type(exc).__name__})") from exc
        finally:
            conn.close()
        if response.status != 200 or len(body) > MAX_BODY:
            raise AzureError(f"the metadata service answered {response.status}")
        try:
            token = json.loads(body)["access_token"]
        except (ValueError, KeyError, TypeError) as exc:
            raise AzureError("the metadata service returned no token") from exc
        if not isinstance(token, str) or not token:
            raise AzureError("the metadata service returned no token")
        self._token = token
        return token

    # -- the deallocation request ------------------------------------------------------
    def deallocate(self, client_request_id: str) -> Submission:
        """Send the request once and classify what is known about it. Never raises."""
        try:
            token = self.token()
        except AzureError as exc:
            return Submission(NOT_SENT, detail=str(exc))
        path = f"{self.vm_path}/deallocate?api-version={self.api_version}"
        try:
            conn = self.connect(ARM_HOST)
            conn.connect()
        except (OSError, http.client.HTTPException, AzureError) as exc:
            return Submission(NOT_SENT, detail=f"connection failed before sending "
                                               f"({type(exc).__name__})")
        try:
            try:
                conn.request("POST", path, body=b"", headers={
                    "Authorization": f"Bearer {token}", "Content-Length": "0",
                    "x-ms-client-request-id": client_request_id,
                    "x-ms-return-client-request-id": "true"})
            except (OSError, http.client.HTTPException) as exc:
                return Submission(UNKNOWN, detail=f"sending failed part-way "
                                                  f"({type(exc).__name__})")
            try:
                response = conn.getresponse()
                body = response.read(MAX_BODY + 1)
            except (OSError, http.client.HTTPException) as exc:
                return Submission(UNKNOWN, detail=f"no readable response "
                                                  f"({type(exc).__name__})")
        finally:
            conn.close()
        status = response.status
        if status == 200:
            return Submission(COMPLETED, status, "Azure reports the deallocation completed")
        if status == 202:
            return Submission(ACCEPTED, status, "Azure accepted the deallocation",
                              async_url=response.getheader("Azure-AsyncOperation"),
                              location_url=response.getheader("Location"),
                              retry_after=retry_after(response.getheader("Retry-After"),
                                                      self.wall()))
        if status in DEFINITE_REJECTIONS:
            return Submission(REJECTED, status, "Azure did not accept the deallocation",
                              error_code=_error_code(body[:MAX_BODY]))
        return Submission(UNKNOWN, status, "Azure's answer does not rule out acceptance",
                          error_code=_error_code(body[:MAX_BODY]))

    # -- following an accepted operation ---------------------------------------------------
    def operation_url_ok(self, url: str | None) -> bool:
        if not isinstance(url, str):
            return False
        parsed = urllib.parse.urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == ARM_HOST
                and parsed.port in (None, 443) and not parsed.username
                and parsed.path.startswith(f"/subscriptions/{self.subscription}/")
                and ".." not in parsed.path.split("/"))

    def poll_once(self, url: str, kind: str) -> tuple[str, float, str]:
        """One status read: (state, seconds before the next read, detail).

        `kind` is 'async' or 'location'. A read whose answer is lost is only a
        lost read (it changes nothing in Azure): the operation stays in progress.
        """
        if not self.operation_url_ok(url):
            return POLL_UNAVAILABLE, RETRY_DEFAULT, ("the operation URL is not one this "
                                                     "controller follows")
        parsed = urllib.parse.urlsplit(url)
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        try:
            token = self.token()
            conn = self.connect(ARM_HOST)
            try:
                conn.request("GET", target, headers={"Authorization": f"Bearer {token}"})
                response = conn.getresponse()
                body = response.read(MAX_BODY + 1)
            finally:
                conn.close()
        except (OSError, http.client.HTTPException, AzureError) as exc:
            return POLL_IN_PROGRESS, RETRY_DEFAULT, (f"the status read failed "
                                                     f"({type(exc).__name__})")
        retry = retry_after(response.getheader("Retry-After"), self.wall())
        if response.status == 403:
            return POLL_UNAVAILABLE, retry, "this identity may not read the operation's status"
        if kind == "async":
            if response.status != 200:
                return POLL_IN_PROGRESS, retry, f"status read answered {response.status}"
            try:
                state = json.loads(body[:MAX_BODY]).get("status")
            except (ValueError, AttributeError):
                return POLL_IN_PROGRESS, retry, "the operation status is unreadable"
            if state == "Succeeded":
                return POLL_SUCCEEDED, retry, "the operation succeeded"
            if state in ("Failed", "Canceled"):
                return POLL_FAILED, retry, f"the operation ended {state}"
            return POLL_IN_PROGRESS, retry, f"the operation is {state!r}"
        if response.status == 202:
            return POLL_IN_PROGRESS, retry, "the operation is in progress"
        if response.status in (200, 204):
            return POLL_SUCCEEDED, retry, "the operation completed"
        return POLL_IN_PROGRESS, retry, f"status read answered {response.status}"

    def follow(self, submission_or_urls: dict, budget: float | None = None,
               not_before: datetime | None = None) -> tuple[str, str, str | None]:
        """Poll an accepted operation within the poll budget, never earlier than permitted.

        Returns (POLL_SUCCEEDED | POLL_FAILED | POLL_UNAVAILABLE | POLL_IN_PROGRESS,
        detail, earliest next read as ISO UTC or None). POLL_IN_PROGRESS means
        no terminal answer within the budget; nothing was read early to get one.
        `not_before` (a later run's view) replaces the submission's own delay.
        """
        url, kind = submission_or_urls.get("async_url"), "async"
        if not url:
            url, kind = submission_or_urls.get("location_url"), "location"
        if not url:
            return POLL_UNAVAILABLE, "Azure returned no operation URL", None
        if not_before is not None:
            wait = max((not_before - self.wall()).total_seconds(), 0.0)
        else:
            wait = submission_or_urls.get("retry_after")
            wait = RETRY_DEFAULT if wait is None else float(wait)
        deadline = self.monotonic() + (self.poll_budget if budget is None else budget)
        detail = "not polled"
        while True:
            if self.monotonic() + wait > deadline:
                resume = (self.wall() + timedelta(seconds=wait)).isoformat()
                return (POLL_IN_PROGRESS, f"no terminal status within the poll budget; the next "
                        f"read is not permitted before {resume} ({detail})", resume)
            self.sleep(wait)
            state, wait, detail = self.poll_once(url, kind)
            if state in (POLL_SUCCEEDED, POLL_FAILED, POLL_UNAVAILABLE):
                return state, detail, None
