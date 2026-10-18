"""Running the observer's shutdown-evidence mode and checking what it returns.

The controller runs `observer.py shutdown-evidence` itself, as the observer's
account (never as root), passing the observer lock it already holds as an
inherited descriptor. The record comes back on the child's standard output;
nothing is read from a file anyone else could have placed.

A record is accepted only if every binding holds:
  - schema `observer_shutdown_evidence/1` and a valid content hash;
  - the transaction id this controller asked for;
  - an observer version on the configured allowlist;
  - the boot this controller is running in;
  - the observer lock it read under is the very file the controller holds
    (device and inode), held by inheritance;
  - the API identity the controller itself established: before the drain, the
    same MainPID and start time; after it, the same start time with the unit
    no longer running;
  - the configuration and calendar bytes the controller hashed itself;
  - generated after the controller froze the observer, not in the future, and
    no older than the configured maximum.
Only then is its verdict used, and only CLEAR permits continuing.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta

from .system import CommandFailed, Runner

SCHEMA = "observer_shutdown_evidence/1"
MAX_OUTPUT = 1024 * 1024
SKEW = timedelta(seconds=5)


class EvidenceRejected(Exception):
    pass


def content_hash(record: dict) -> str:
    body = {k: v for k, v in record.items() if k != "integrity"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()


def file_digest(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def request(cfg: dict, runner: Runner, txn_id: str, lock_fd: int, api_stopped: bool) -> dict:
    obs = cfg["observer"]
    argv = [obs["python"], "-I", obs["entry"], "shutdown-evidence", "--config", obs["config"],
            "--txn", txn_id, "--lock-fd", str(lock_fd)]
    if api_stopped:
        argv.append("--api-stopped")
    try:
        result = runner.run(argv, cfg["timeouts"]["evidence"], user=obs["user"],
                            env={"PATH": "/usr/bin:/bin", "TZ": "Asia/Kolkata"},
                            pass_fds=(lock_fd,))
    except CommandFailed as exc:
        raise EvidenceRejected(f"the observer's evidence could not be obtained: {exc}") from exc
    if result.returncode != 0:
        raise EvidenceRejected(f"the observer refused to give evidence (exit "
                               f"{result.returncode}): {result.stderr.strip()[:200]}")
    if len(result.stdout) > MAX_OUTPUT:
        raise EvidenceRejected("the observer's evidence is too large")
    try:
        record = json.loads(result.stdout)
    except ValueError as exc:
        raise EvidenceRejected("the observer's evidence is not JSON") from exc
    return record


def validate(record, *, cfg: dict, txn_id: str, boot_id: str, lock_identity: tuple[int, int],
             api: dict, api_stopped: bool, frozen_at: datetime, now: datetime) -> dict:
    """Check every binding; returns the record or raises EvidenceRejected."""
    def refuse(why: str):
        raise EvidenceRejected(why)

    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        refuse("not observer shutdown evidence")
    integrity = record.get("integrity")
    if not isinstance(integrity, dict) or integrity.get("content_sha256") != content_hash(record):
        refuse("the evidence's content hash does not match")
    if record.get("txn_id") != txn_id:
        refuse("the evidence was made for another transaction")
    if record.get("observer_version") not in cfg["observer"]["accepted_versions"]:
        refuse(f"observer version {record.get('observer_version')!r} is not accepted")
    if record.get("verdict") not in ("CLEAR", "BLOCKED", "UNKNOWN"):
        refuse("the evidence has no valid verdict")
    lock = record.get("lock")
    if not isinstance(lock, dict) or lock.get("mode") != "inherited" \
            or (lock.get("device"), lock.get("inode")) != tuple(lock_identity):
        refuse("the evidence was not read under the lock this controller holds")
    if record.get("boot_id") != boot_id:
        refuse("the evidence belongs to another boot")
    seen = record.get("api")
    if not isinstance(seen, dict):
        refuse("the evidence does not identify the API process")
    if seen.get("main_started_monotonic_usec") != api["main_started_monotonic_usec"]:
        refuse("the evidence names another API process start")
    if api_stopped:
        if seen.get("state") != "stopped":
            refuse("the evidence does not show the API stopped")
    elif seen.get("main_pid") != api["main_pid"] or seen.get("pids") != [api["main_pid"]]:
        refuse("the evidence names another API process")
    try:
        if record.get("config_sha256") != file_digest(cfg["observer"]["config"]):
            refuse("the evidence was made under different observer configuration bytes")
        if record.get("calendar_file") != cfg["calendar_file"] \
                or record.get("calendar_sha256") != file_digest(cfg["calendar_file"]):
            refuse("the evidence was made under a different calendar")
    except OSError as exc:
        refuse(f"the configuration or calendar could not be hashed ({exc.strerror})")
    try:
        made = datetime.fromisoformat(record.get("generated_at_utc"))
    except (TypeError, ValueError):
        refuse("the evidence has no readable generation time")
    if made.tzinfo is None or made < frozen_at - SKEW or made > now + SKEW:
        refuse("the evidence was generated outside this transaction's frozen interval")
    if now - made > timedelta(seconds=cfg["observer"]["evidence_max_age_seconds"]):
        refuse("the evidence is too old")
    return record


def obtain(cfg: dict, runner: Runner, *, txn_id: str, boot_id: str, lock_fd: int,
           api: dict, api_stopped: bool, frozen_at: datetime, clock) -> dict:
    record = request(cfg, runner, txn_id, lock_fd, api_stopped)
    identity = os.fstat(lock_fd)
    return validate(record, cfg=cfg, txn_id=txn_id, boot_id=boot_id,
                    lock_identity=(identity.st_dev, identity.st_ino), api=api,
                    api_stopped=api_stopped, frozen_at=frozen_at,
                    now=clock().astimezone(UTC))
