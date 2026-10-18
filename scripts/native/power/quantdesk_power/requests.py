"""One-use shutdown requests from the unprivileged requester to the root controller.

A request carries no instruction. It says only "please evaluate now": a
random nonce, its creation time and the boot it was made in. The controller
re-derives every fact itself, so a forged or replayed request can at most
start an evaluation, which still needs every condition to hold.

The requester (user `quantdesk-power`) creates `req-<nonce>.json` in the
request directory (`/run/quantdesk-power/requests`, root:quantdesk-power,
mode 1730: the group may create, nobody but root may list or remove others'
files) with O_CREAT | O_EXCL | O_NOFOLLOW and mode 0600.

The controller accepts a request only if:
  - its name is `req-<32 hex>.json` and the nonce inside matches the name;
  - it is a regular file with one link, owned by the requester's uid, mode
    0600 or 0400, at most 1 KiB, with exactly the expected fields;
  - it was created in the running boot, within the last two minutes and not
    in the future (five seconds of clock tolerance);
  - its nonce has never been consumed: the nonce is recorded in
    `<state_root>/consumed/` with O_EXCL before the request is acted on, so a
    copy of an earlier request is refused even across reboots.
Every request file is removed once read, accepted or not; refusals are kept
in `<state_root>/refused/` for review. At most one request is accepted per
controller run.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
from datetime import UTC, datetime, timedelta

from . import fsafe

SCHEMA = "quantdesk_power_request/1"
NAME = re.compile(r"^req-([0-9a-f]{32})\.json$")
FIELDS = {"schema", "nonce", "created_at_utc", "boot_id"}
MAX_BYTES = 1024
MAX_AGE = timedelta(seconds=120)
SKEW = timedelta(seconds=5)


def make_request(request_dir: fsafe.HeldDir, boot_id: str, now: datetime) -> str:
    """Create one request; returns its nonce. Run as the unprivileged requester."""
    nonce = secrets.token_hex(16)
    body = json.dumps({"schema": SCHEMA, "nonce": nonce,
                       "created_at_utc": now.astimezone(UTC).isoformat(),
                       "boot_id": boot_id}, sort_keys=True).encode()
    fd = os.open(f"req-{nonce}.json",
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                 dir_fd=request_dir.fd)
    try:
        os.write(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)
    return nonce


def _problem(raw: bytes, nonce: str, boot_id: str, now: datetime) -> str | None:
    try:
        body = json.loads(raw)
    except ValueError:
        return "not JSON"
    if not isinstance(body, dict) or set(body) != FIELDS:
        return "unexpected or missing fields"
    if body["schema"] != SCHEMA:
        return "unknown schema"
    if body["nonce"] != nonce:
        return "the nonce does not match the file name"
    if body["boot_id"] != boot_id:
        return "made in another boot"
    try:
        made = datetime.fromisoformat(body["created_at_utc"])
    except (TypeError, ValueError):
        return "unreadable creation time"
    if made.tzinfo is None:
        return "creation time has no timezone"
    if made > now + SKEW:
        return "created in the future"
    if now - made > MAX_AGE:
        return "stale"
    return None


def _remove(request_dir: fsafe.HeldDir, name: str) -> None:
    """Remove an entry so the request directory empties (the path unit triggers on it).

    A directory is not a request; an empty one is removed with rmdir, anything
    else that cannot be unlinked stays and is reported as refused.
    """
    st = os.stat(name, dir_fd=request_dir.fd, follow_symlinks=False)
    if stat.S_ISDIR(st.st_mode):
        os.rmdir(name, dir_fd=request_dir.fd)
    else:
        os.unlink(name, dir_fd=request_dir.fd)


def consume(request_dir: fsafe.HeldDir, state_dir: fsafe.HeldDir, requester_uid: int,
            boot_id: str, now: datetime) -> tuple[str | None, list[dict]]:
    """Read, validate and remove every pending request.

    Returns the nonce of the one accepted request (or None) and the refusals.
    """
    consumed = fsafe.subdir(state_dir, "consumed", owner=os.geteuid(), create=True, mode=0o700)
    refused_dir = fsafe.subdir(state_dir, "refused", owner=os.geteuid(), create=True,
                               mode=0o700)
    accepted, refusals = None, []
    try:
        for name in sorted(os.listdir(request_dir.fd)):
            reason, nonce = None, None
            match = NAME.match(name)
            if not match:
                reason = "unexpected file name"
            else:
                nonce = match.group(1)
                try:
                    raw = fsafe.read_file(request_dir, name, max_bytes=MAX_BYTES,
                                          owner=requester_uid, private=True)
                    reason = _problem(raw, nonce, boot_id, now)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    reason = f"not a safe request file ({exc})"
            try:
                _remove(request_dir, name)
            except FileNotFoundError:
                continue
            except OSError as exc:
                reason = reason or f"could not be removed ({exc.strerror})"
            if reason is None and accepted is not None:
                reason = "another request was already accepted in this run"
            if reason is None:
                marker = json.dumps({"nonce": nonce, "consumed_at_utc":
                                     now.astimezone(UTC).isoformat()}).encode()
                if not fsafe.publish(consumed, nonce, marker, mode=0o400):
                    reason = "replayed: this nonce was consumed before"
            if reason is None:
                accepted = nonce
                continue
            refusal = {"name": name[:80], "reason": reason,
                       "refused_at_utc": now.astimezone(UTC).isoformat()}
            refusals.append(refusal)
            fsafe.publish(refused_dir, f"{now.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}",
                          json.dumps(refusal, sort_keys=True).encode(), mode=0o400)
    finally:
        consumed.close()
        refused_dir.close()
    return accepted, refusals
