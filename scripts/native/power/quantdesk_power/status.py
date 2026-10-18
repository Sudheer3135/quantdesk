"""The controller's shutdown status: a short signed record an outside monitor can check.

One line of at most 256 characters (an Azure tag value holds 256):

  qdps1;<vm>;<boot>;<seq>;<txn>;<state>;<sub>;<flag>;<cycle>;<epoch>;<mac>

  vm     first 12 hex of SHA-256 of the VM's resource id (lower case)
  boot   this boot's id
  seq    publication number, strictly increasing (a replay shows as older)
  txn    the transaction the record is about, or '-'
  state  that transaction's journal state, or NONE
  sub    first 8 hex of the boot that submitted its deallocation, or '-'
  flag   OK | IN_PROGRESS | UNRESOLVED | RESTORE_FAILED | RESUME_FAILED | CORRUPT
  cycle  the operating cycle it was published in: the latest required startup at or
         before publication (weekday 07:30 IST, or a special session's VM start), as
         YYYYMMDDHHMM IST; '-' when the special-session holds cannot be read (the
         monitor then alerts: no exemption is assumed)
  epoch  publication time, Unix seconds UTC
  mac    first 32 hex of HMAC-SHA256(key, everything before it)

Every controller run (control, recover, resolve) publishes one, and so does
the transaction just before its deallocation request leaves, so a deallocated
VM's last word is the submission. Records are kept, append-only, under
`<state_root>/status/`.

TRANSPORT IS NOT AUTHORIZED. The only transport implemented is "none" (the
local record). Carrying it to the independent monitor (for example the VM's
`qd-power-status` tag) needs a new Azure permission for the VM's identity,
and the key needs provisioning on both sides; both are approval blockers. The
monitor (azure/qd_monitor.py) already refuses a missing record.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import fsafe, journal

IST = ZoneInfo("Asia/Kolkata")
SCHEMA = "qdps1"
FLAGS = ("OK", "IN_PROGRESS", "UNRESOLVED", "RESTORE_FAILED", "RESUME_FAILED", "CORRUPT")
FIELDS = ("schema", "vm", "boot", "seq", "txn", "state", "sub", "flag", "cycle", "epoch",
          "mac")
STARTUP = time(7, 30)          # the scheduled weekday start (holidays included)
MAX_LENGTH = 256
KEY_BYTES = (32, 1024)
STATUS_NAME = re.compile(r"^(\d{8})\.txt$")
PATTERNS = {"vm": r"[0-9a-f]{12}", "boot": r"[0-9a-f-]{36}", "seq": r"[1-9][0-9]{0,9}",
            "txn": r"[0-9a-f]{32}|-", "state": r"[A-Z_]{1,40}", "sub": r"[0-9a-f]{8}|-",
            "flag": "|".join(FLAGS), "cycle": r"[0-9]{12}|-", "epoch": r"[0-9]{9,11}",
            "mac": r"[0-9a-f]{32}"}


class StatusError(ValueError):
    pass


def cycle_start(now: datetime, special_starts: list[datetime]) -> datetime:
    """The latest required startup at or before `now`: weekday 07:30 IST or a special start.

    Identical to azure/qd_monitor.py (tested). Every weekday counts, holidays
    included, so no calendar is consulted and no day is ever exempted.
    """
    local = now.astimezone(IST)
    day = local.date()
    while True:
        start = datetime.combine(day, STARTUP, tzinfo=IST)
        if day.weekday() < 5 and start <= local:
            break
        day -= timedelta(days=1)
    earlier = [s.astimezone(IST) for s in special_starts if s.astimezone(IST) <= local]
    return max([start, *earlier])


def cycle_text(start: datetime) -> str:
    return start.astimezone(IST).strftime("%Y%m%d%H%M")


def special_starts(cfg: dict, now: datetime) -> list[datetime] | None:
    """VM starts of special sessions, from the KEEP_RUNNING SPECIAL_SESSION holds the
    special-session plan writes; None when the holds cannot be read."""
    from . import facts
    holds, fact = facts.read_holds(cfg, now)
    if fact.status == facts.UNKNOWN:
        return None
    return [datetime.fromisoformat(h["from_ist"]) for h in holds
            if h["kind"] == "KEEP_RUNNING" and h["reason_code"] == "SPECIAL_SESSION"]


def vm_digest(resource_id: str) -> str:
    return hashlib.sha256(resource_id.lower().encode()).hexdigest()[:12]


def vm_resource_id(cfg: dict) -> str:
    az = cfg["azure"]
    return (f"/subscriptions/{az['subscription_id']}/resourceGroups/{az['resource_group']}/"
            f"providers/Microsoft.Compute/virtualMachines/{az['vm_name']}")


def _mac(key: bytes, body: str) -> str:
    return hmac.new(key, body.encode(), hashlib.sha256).hexdigest()[:32]


def encode(fields: dict, key: bytes) -> str:
    body = ";".join(str(fields[name]) for name in FIELDS[:-1])
    text = f"{body};{_mac(key, body)}"
    decode(text, key)                       # never publish what the monitor would refuse
    return text


def decode(text, key: bytes) -> dict:
    """The record's fields, or StatusError if it is malformed or its MAC does not verify."""
    if not isinstance(text, str) or len(text) > MAX_LENGTH:
        raise StatusError("the status is missing or too long")
    parts = text.split(";")
    if len(parts) != len(FIELDS) or parts[0] != SCHEMA:
        raise StatusError("the status is not a qdps1 record")
    fields = dict(zip(FIELDS, parts, strict=True))
    for name, pattern in PATTERNS.items():
        if not re.fullmatch(pattern, fields[name]):
            raise StatusError(f"the status field {name} is malformed")
    body = text.rsplit(";", 1)[0]
    if not hmac.compare_digest(fields["mac"], _mac(key, body)):
        raise StatusError("the status signature does not verify")
    fields["seq"], fields["epoch"] = int(fields["seq"]), int(fields["epoch"])
    return fields


def summarize(txns: list[journal.Transaction] | None, boot: str) -> tuple[str, str, str, str]:
    """(txn, state, submission boot prefix, flag) for the journal as it stands."""
    if txns is None:
        return "-", "NONE", "-", "CORRUPT"
    ordered = sorted(txns, key=lambda t: t.first["at_utc"])

    def about(txn):
        sub = next((r["boot_id"] for r in txn.records if r["state"] == journal.SUBMITTING), None)
        return txn.txn_id, txn.state, sub.replace("-", "")[:8] if sub else "-"
    for flag, matches in (("UNRESOLVED", lambda t: t.state in journal.UNRESOLVED_AZURE),
                          ("RESTORE_FAILED", lambda t: t.state == journal.RESTORE_FAILED),
                          ("RESUME_FAILED", lambda t: t.state == journal.RESUME_FAILED),
                          # Azure settled, awaiting the next boot's resumption: not in flight.
                          ("IN_PROGRESS", lambda t: not t.terminal
                           and t.state not in journal.DEALLOCATED)):
        found = [t for t in ordered if matches(t)]
        if found:
            return (*about(found[-1]), flag)
    if ordered:
        return (*about(ordered[-1]), "OK")
    return "-", "NONE", "-", "OK"


def read_key(cfg: dict) -> bytes | None:
    path = cfg["status"]["key_file"]
    try:
        held = fsafe.open_dir(os.path.dirname(path), private=True)
    except OSError:
        return None
    try:
        key = fsafe.read_file(held, os.path.basename(path), max_bytes=KEY_BYTES[1],
                              owner=os.geteuid(), private=True)
    except OSError:
        return None
    finally:
        held.close()
    return key if len(key) >= KEY_BYTES[0] else None


def publish(cfg: dict, state_dir: fsafe.HeldDir, txns: list[journal.Transaction] | None,
            boot: str, now: datetime) -> str | None:
    """Build and keep one status record. None when no key is provisioned (nothing to sign)."""
    key = read_key(cfg)
    if key is None:
        return None
    txn, state, sub, flag = summarize(txns, boot)
    starts = special_starts(cfg, now)
    cycle = "-" if starts is None else cycle_text(cycle_start(now, starts))
    directory = fsafe.subdir(state_dir, "status", owner=os.geteuid(), create=True, mode=0o700)
    try:
        seen = [int(m.group(1)) for n in fsafe.names(directory) if (m := STATUS_NAME.match(n))]
        seq = max(seen, default=0) + 1
        text = encode({"schema": SCHEMA, "vm": vm_digest(vm_resource_id(cfg)), "boot": boot,
                       "seq": seq, "txn": txn, "state": state, "sub": sub, "flag": flag,
                       "cycle": cycle, "epoch": int(now.timestamp())}, key)
        if not fsafe.publish(directory, f"{seq:08d}.txt", text.encode() + b"\n", mode=0o400):
            raise journal.CorruptJournal(f"status record {seq} already exists")
    finally:
        directory.close()
    return text
