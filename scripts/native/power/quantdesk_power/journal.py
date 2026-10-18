"""The power controller's transaction journal: append-only, sealed, chained.

Each transaction is a directory `txn/<txn-id>/` holding records named
`NNNN-<STATE>.json`. A record is published with `fsafe.publish` (absent or
complete, never replaced) and carries:

  - its sequence number and state, which must match its name;
  - the SHA-256 of the previous record (`prev_sha256`), so records cannot be
    reordered, dropped or swapped without detection;
  - its own content hash (`integrity`).

Every read validates the whole chain and every transition against
`TRANSITIONS`. A journal that does not validate is never repaired or
skipped: it is `CorruptJournal`, and the controller refuses all new work
until an operator has examined it.

The current state of a transaction is its last record. Nothing is ever
overwritten, so the journal is also the audit history.

AZURE OUTCOME AND WORKLOAD RECOVERY ARE SEPARATE. COMPLETED, POLL_SUCCEEDED
and OPERATOR_CONFIRMED_DEALLOCATED settle Azure's side only; the transaction
stays open until the workload is back in a later boot: RESUMING (journaled
before anything is started) then RESUMED (every required unit verified) or
RESUME_FAILED (retried). Restoration before the request left is likewise
RESTORING first, then RESTORED or RESTORE_FAILED. Per-unit progress of both
is kept in a separate sealed, chained progress log (`ProgressLog`), so the
transaction's own states stay one record per phase.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from . import fsafe

SCHEMA = "quantdesk_power_txn/1"
TXN_ID = re.compile(r"^[0-9a-f]{32}$")
RECORD_NAME = re.compile(r"^(\d{4})-([A-Z_]+)\.json$")
MAX_RECORD_BYTES = 256 * 1024
MAX_RECORDS = 200

# States in the order a successful transaction passes through them.
ACCEPTED_REQUEST = "ACCEPTED_REQUEST"
FROZEN = "FROZEN"                       # observer timers stopped, observer lock held
EVIDENCE_CLEAR = "EVIDENCE_CLEAR"
SYSTEM_CLEAR = "SYSTEM_CLEAR"
WEB_STOPPED = "WEB_STOPPED"
API_STOPPED = "API_STOPPED"
WRITERS_EXCLUDED = "WRITERS_EXCLUDED"   # exclusive writer key held in PostgreSQL
POSITIONS_CLEAR = "POSITIONS_CLEAR"
DRAINED_EVIDENCE_CLEAR = "DRAINED_EVIDENCE_CLEAR"
REDIS_STOPPED = "REDIS_STOPPED"
POSTGRES_STOPPED = "POSTGRES_STOPPED"
SUBMITTING = "SUBMITTING"               # written before the deallocation request leaves
# Outcomes of the deallocation request.
NOT_SENT = "NOT_SENT"                   # definitively never sent
REJECTED = "REJECTED"                   # Azure answered and did not accept it
ACCEPTED = "ACCEPTED"                   # Azure accepted it (202); completion not yet known
COMPLETED = "COMPLETED"                 # Azure answered 200: the operation completed
UNKNOWN = "UNKNOWN"                     # whether Azure received it is not known
POLL_SUCCEEDED = "POLL_SUCCEEDED"       # the operation's own status says Succeeded
POLL_FAILED = "POLL_FAILED"             # the operation's own status says Failed or Canceled
UNKNOWN_RETAINED = "UNKNOWN_RETAINED"   # still unknown when this run had to end
OBSERVED_NEW_BOOT = "OBSERVED_NEW_BOOT"  # a later boot was seen; nothing is inferred from it
# Closing states.
ABORTED = "ABORTED"                     # refused before anything was changed
RESTORING = "RESTORING"
RESTORED = "RESTORED"
RESTORE_FAILED = "RESTORE_FAILED"
# An operator's recorded finding, made from Azure's own records outside this machine.
OPERATOR_CONFIRMED_DEALLOCATED = "OPERATOR_CONFIRMED_DEALLOCATED"   # terminal
OPERATOR_CONFIRMED_NOT_PERFORMED = "OPERATOR_CONFIRMED_NOT_PERFORMED"  # restoration permitted
OPERATOR_CLOSED = "OPERATOR_CLOSED"     # a failed restoration repaired by hand
# Bringing the workload back after a deallocation Azure settled, in a later boot.
RESUMING = "RESUMING"                   # journaled before any unit is started
RESUMED = "RESUMED"                     # every required unit verified running: terminal
RESUME_FAILED = "RESUME_FAILED"         # some unit did not come back; retried

PRE_SUBMIT = (ACCEPTED_REQUEST, FROZEN, EVIDENCE_CLEAR, SYSTEM_CLEAR, WEB_STOPPED, API_STOPPED,
              WRITERS_EXCLUDED, POSITIONS_CLEAR, DRAINED_EVIDENCE_CLEAR, REDIS_STOPPED,
              POSTGRES_STOPPED)
# Deallocation done: nothing more for this transaction to do.
TERMINAL = (ABORTED, RESTORED, RESUMED, OPERATOR_CLOSED)
# The deallocation may be in progress or done; workloads must not be restarted and no
# new transaction may start until operation-specific evidence or an operator settles it.
UNRESOLVED_AZURE = (SUBMITTING, ACCEPTED, UNKNOWN, UNKNOWN_RETAINED, OBSERVED_NEW_BOOT)
# Stages in which the controller has begun stopping things and holds the workload down.
PROTECTED = PRE_SUBMIT[1:]
# Azure's own answer, or an operator's finding from Azure's records, says it was deallocated.
DEALLOCATED = (COMPLETED, POLL_SUCCEEDED, OPERATOR_CONFIRMED_DEALLOCATED)
# Azure settled; the workload is (being) brought back, or failed to come back.
RESUMABLE = (*DEALLOCATED, RESUMING, RESUME_FAILED)

_FORWARD = {
    ACCEPTED_REQUEST: (FROZEN, ABORTED),
    FROZEN: (EVIDENCE_CLEAR,),
    EVIDENCE_CLEAR: (SYSTEM_CLEAR,),
    SYSTEM_CLEAR: (WEB_STOPPED,),
    WEB_STOPPED: (API_STOPPED,),
    API_STOPPED: (WRITERS_EXCLUDED,),
    WRITERS_EXCLUDED: (POSITIONS_CLEAR,),
    POSITIONS_CLEAR: (DRAINED_EVIDENCE_CLEAR,),
    DRAINED_EVIDENCE_CLEAR: (REDIS_STOPPED,),
    REDIS_STOPPED: (POSTGRES_STOPPED,),
    POSTGRES_STOPPED: (SUBMITTING,),
    SUBMITTING: (NOT_SENT, REJECTED, ACCEPTED, COMPLETED, UNKNOWN),
    NOT_SENT: (RESTORING,),
    REJECTED: (RESTORING,),
    ACCEPTED: (POLL_SUCCEEDED, POLL_FAILED, UNKNOWN_RETAINED, OBSERVED_NEW_BOOT),
    UNKNOWN: (POLL_SUCCEEDED, POLL_FAILED, UNKNOWN_RETAINED, OBSERVED_NEW_BOOT),
    UNKNOWN_RETAINED: (POLL_SUCCEEDED, POLL_FAILED, UNKNOWN_RETAINED, OBSERVED_NEW_BOOT),
    OBSERVED_NEW_BOOT: (POLL_SUCCEEDED, POLL_FAILED, UNKNOWN_RETAINED, OBSERVED_NEW_BOOT),
    POLL_FAILED: (RESTORING,),
    OPERATOR_CONFIRMED_NOT_PERFORMED: (RESTORING,),
    RESTORING: (RESTORED, RESTORE_FAILED),
    RESTORE_FAILED: (RESTORING, OPERATOR_CLOSED),
    COMPLETED: (RESUMING,),
    POLL_SUCCEEDED: (RESUMING,),
    OPERATOR_CONFIRMED_DEALLOCATED: (RESUMING,),
    RESUMING: (RESUMED, RESUME_FAILED, RESUMING),     # RESUMING again: an interrupted attempt
    RESUME_FAILED: (RESUMING, OPERATOR_CLOSED),
}
OPERATOR_FINDINGS = {OPERATOR_CONFIRMED_DEALLOCATED, OPERATOR_CONFIRMED_NOT_PERFORMED}
# An operator may record a finding only on a transaction whose Azure outcome is unresolved.
for _state in (ACCEPTED, UNKNOWN, UNKNOWN_RETAINED, OBSERVED_NEW_BOOT):
    _FORWARD[_state] = _FORWARD[_state] + tuple(sorted(OPERATOR_FINDINGS))
# Any state before the request is sent may also go to RESTORING (a refusal or a failure).
TRANSITIONS = {state: tuple(dict.fromkeys(
    nxt + ((RESTORING,) if state in PRE_SUBMIT else ())))
    for state, nxt in _FORWARD.items()}
STATES = set(TRANSITIONS) | {s for v in TRANSITIONS.values() for s in v}


class CorruptJournal(Exception):
    """The journal does not validate. Nothing is repaired; all new work is refused."""


def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def record_hash(record: dict) -> str:
    return hashlib.sha256(_canonical({k: v for k, v in record.items()
                                      if k != "integrity"})).hexdigest()


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Transaction:
    txn_id: str
    records: list[dict]

    @property
    def state(self) -> str:
        return self.records[-1]["state"]

    @property
    def first(self) -> dict:
        return self.records[0]

    def data(self, state: str) -> dict | None:
        """The data of the last record in `state`, or None."""
        for record in reversed(self.records):
            if record["state"] == state:
                return record["data"]
        return None

    def reached(self, state: str) -> bool:
        return any(r["state"] == state for r in self.records)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL


class Journal:
    """Transactions under `<state_root>/txn`, owned by root, private."""

    def __init__(self, state_dir: fsafe.HeldDir, owner: int, create: bool = True):
        self.owner = owner
        self.state_dir = state_dir
        self.txn_dir = fsafe.subdir(state_dir, "txn", owner=owner, create=create, mode=0o700)

    def close(self) -> None:
        self.txn_dir.close()

    def _dir(self, txn_id: str, create: bool = False) -> fsafe.HeldDir:
        if not TXN_ID.match(txn_id or ""):
            raise CorruptJournal(f"{txn_id!r} is not a transaction id")
        return fsafe.subdir(self.txn_dir, txn_id, owner=self.owner, create=create, mode=0o700)

    def begin(self, txn_id: str, boot_id: str, data: dict, clock=now_utc) -> Transaction:
        if txn_id in self.ids():
            raise CorruptJournal(f"transaction {txn_id} already exists")
        directory = self._dir(txn_id, create=True)
        directory.close()
        txn = Transaction(txn_id, [])
        self.append(txn, ACCEPTED_REQUEST, boot_id, data, clock)
        return txn

    def append(self, txn: Transaction, state: str, boot_id: str, data: dict,
               clock=now_utc) -> dict:
        """Publish the next record of `txn`. Raises CorruptJournal on an illegal transition."""
        if state not in STATES:
            raise CorruptJournal(f"{state} is not a journal state")
        if txn.records:
            allowed = TRANSITIONS.get(txn.state, ())
            if state not in allowed:
                raise CorruptJournal(f"{txn.state} -> {state} is not an allowed transition")
        elif state != ACCEPTED_REQUEST:
            raise CorruptJournal("a transaction must begin with ACCEPTED_REQUEST")
        if len(txn.records) >= MAX_RECORDS:
            raise CorruptJournal("the transaction has too many records")
        seq = len(txn.records) + 1
        record = {"schema": SCHEMA, "txn_id": txn.txn_id, "seq": seq, "state": state,
                  "at_utc": clock(), "boot_id": boot_id, "data": data,
                  "prev_sha256": txn.records[-1]["integrity"]["content_sha256"]
                  if txn.records else None}
        record = json.loads(_canonical(record))
        record["integrity"] = {"algorithm": "sha256", "content_sha256": record_hash(record)}
        directory = self._dir(txn.txn_id)
        try:
            if not fsafe.publish(directory, f"{seq:04d}-{state}.json", _canonical(record),
                                 mode=0o400):
                raise CorruptJournal(f"record {seq} of {txn.txn_id} already exists")
        finally:
            directory.close()
        txn.records.append(record)
        return record

    def append_durably(self, txn: Transaction, state: str, boot_id: str, data: dict,
                       clock=now_utc) -> dict:
        """`append`, then read the whole transaction back from disk and validate it.

        Used for the records that must exist before anything is started (RESTORING,
        RESUMING): the new record must be there, intact, chained and last, exactly as
        written. Anything else raises (OSError or CorruptJournal) and the caller starts
        nothing.
        """
        record = self.append(txn, state, boot_id, data, clock)
        stored = self.load(txn.txn_id)
        if stored.records != txn.records:
            raise CorruptJournal(f"{state} of {txn.txn_id} was written but does not read back "
                                 "as written")
        return record

    def ids(self) -> list[str]:
        found = fsafe.names(self.txn_dir)
        for name in found:
            if not TXN_ID.match(name):
                raise CorruptJournal(f"unexpected entry {name!r} in the journal")
        return found

    def load(self, txn_id: str) -> Transaction:
        directory = self._dir(txn_id)
        try:
            names = fsafe.names(directory)
            if not names:
                raise CorruptJournal(f"transaction {txn_id} has no records")
            if len(names) > MAX_RECORDS:
                raise CorruptJournal(f"transaction {txn_id} has too many records")
            records, prev = [], None
            for expected, name in enumerate(names, 1):
                match = RECORD_NAME.match(name)
                if not match or int(match.group(1)) != expected:
                    raise CorruptJournal(f"{txn_id}/{name}: unexpected or out-of-sequence name")
                try:
                    raw = fsafe.read_file(directory, name, max_bytes=MAX_RECORD_BYTES,
                                          owner=self.owner, private=True)
                    record = json.loads(raw)
                except (OSError, ValueError) as exc:
                    raise CorruptJournal(f"{txn_id}/{name}: unreadable ({exc})") from exc
                problem = _record_problem(record, txn_id, expected, match.group(2), prev)
                if problem:
                    raise CorruptJournal(f"{txn_id}/{name}: {problem}")
                if records and record["state"] not in TRANSITIONS.get(records[-1]["state"], ()):
                    raise CorruptJournal(f"{txn_id}/{name}: {records[-1]['state']} -> "
                                         f"{record['state']} is not an allowed transition")
                if not records and record["state"] != ACCEPTED_REQUEST:
                    raise CorruptJournal(f"{txn_id}: does not begin with ACCEPTED_REQUEST")
                records.append(record)
                prev = record["integrity"]["content_sha256"]
            return Transaction(txn_id, records)
        finally:
            directory.close()

    def all(self) -> list[Transaction]:
        return [self.load(t) for t in self.ids()]

    def open_transactions(self) -> list[Transaction]:
        """Every transaction that is not terminal, oldest first."""
        found = [t for t in self.all() if not t.terminal]
        return sorted(found, key=lambda t: t.first["at_utc"])


def _record_problem(record, txn_id: str, seq: int, state: str, prev: str | None) -> str | None:
    if not isinstance(record, dict):
        return "not a JSON object"
    expected = {"schema", "txn_id", "seq", "state", "at_utc", "boot_id", "data", "prev_sha256",
                "integrity"}
    if set(record) != expected:
        return "unexpected or missing fields"
    if record["schema"] != SCHEMA:
        return "unknown schema"
    if record["txn_id"] != txn_id or record["seq"] != seq or record["state"] != state:
        return "identity does not match its name"
    if not isinstance(record["data"], dict) or not isinstance(record["boot_id"], str):
        return "malformed data or boot id"
    integrity = record["integrity"]
    if not isinstance(integrity, dict) or integrity.get("content_sha256") != record_hash(record):
        return "content hash does not match"
    if record["prev_sha256"] != prev:
        return "does not chain to the previous record"
    return None


# -- per-unit progress of a restoration or a resumption --------------------------------------
PROGRESS_SCHEMA = "quantdesk_power_progress/1"
PROGRESS_NAME = re.compile(r"^(\d{5})\.json$")
MAX_PROGRESS = 5000
# not_running: the unit was not ready at the end of the bounded wait; unconfirmed: it was
# ready, but readiness was not held unchanged long enough before the deadline.
PROGRESS_OUTCOMES = ("started", "already_active", "failed", "verified", "not_running",
                     "unconfirmed")


class ProgressLog:
    """`<state_root>/progress/<txn-id>/NNNNN.json`: sealed, chained, never replaced.

    Each record names the phase record it belongs to (`attempt`: the journal
    sequence of the RESTORING or RESUMING record), one unit, and what happened
    to it. Validated completely on every read; anything wrong is CorruptJournal.
    """

    def __init__(self, state_dir: fsafe.HeldDir, owner: int, create: bool = True):
        self.owner = owner
        self.root = fsafe.subdir(state_dir, "progress", owner=owner, create=create, mode=0o700)

    def close(self) -> None:
        self.root.close()

    def _dir(self, txn_id: str, create: bool) -> fsafe.HeldDir:
        if not TXN_ID.match(txn_id or ""):
            raise CorruptJournal(f"{txn_id!r} is not a transaction id")
        return fsafe.subdir(self.root, txn_id, owner=self.owner, create=create, mode=0o700)

    def load(self, txn_id: str) -> list[dict]:
        try:
            directory = self._dir(txn_id, create=False)
        except FileNotFoundError:
            return []
        try:
            names = fsafe.names(directory)
            if len(names) > MAX_PROGRESS:
                raise CorruptJournal(f"progress of {txn_id} has too many records")
            records, prev = [], None
            for expected, name in enumerate(names, 1):
                match = PROGRESS_NAME.match(name)
                if not match or int(match.group(1)) != expected:
                    raise CorruptJournal(f"progress {txn_id}/{name}: unexpected name")
                try:
                    record = json.loads(fsafe.read_file(directory, name,
                                                        max_bytes=MAX_RECORD_BYTES,
                                                        owner=self.owner, private=True))
                except (OSError, ValueError) as exc:
                    raise CorruptJournal(f"progress {txn_id}/{name}: unreadable ({exc})") \
                        from exc
                problem = _progress_problem(record, txn_id, expected, prev)
                if problem:
                    raise CorruptJournal(f"progress {txn_id}/{name}: {problem}")
                records.append(record)
                prev = record["integrity"]["content_sha256"]
            return records
        finally:
            directory.close()

    def append(self, txn: Transaction, unit: str, outcome: str, detail: str, boot_id: str,
               clock=now_utc) -> dict:
        if outcome not in PROGRESS_OUTCOMES:
            raise CorruptJournal(f"{outcome} is not a progress outcome")
        phase = txn.records[-1]
        if phase["state"] not in (RESTORING, RESUMING):
            raise CorruptJournal(f"progress is recorded only during RESTORING or RESUMING, "
                                 f"not {phase['state']}")
        existing = self.load(txn.txn_id)
        if len(existing) >= MAX_PROGRESS:
            raise CorruptJournal(f"progress of {txn.txn_id} is full")
        record = {"schema": PROGRESS_SCHEMA, "txn_id": txn.txn_id, "seq": len(existing) + 1,
                  "phase": phase["state"], "attempt": phase["seq"], "unit": unit,
                  "outcome": outcome, "detail": detail[:300], "at_utc": clock(),
                  "boot_id": boot_id,
                  "prev_sha256": existing[-1]["integrity"]["content_sha256"] if existing
                  else None}
        record = json.loads(_canonical(record))
        record["integrity"] = {"algorithm": "sha256", "content_sha256": record_hash(record)}
        directory = self._dir(txn.txn_id, create=True)
        try:
            if not fsafe.publish(directory, f"{record['seq']:05d}.json", _canonical(record),
                                 mode=0o400):
                raise CorruptJournal(f"progress record {record['seq']} already exists")
        finally:
            directory.close()
        return record


def _progress_problem(record, txn_id: str, seq: int, prev: str | None) -> str | None:
    expected = {"schema", "txn_id", "seq", "phase", "attempt", "unit", "outcome", "detail",
                "at_utc", "boot_id", "prev_sha256", "integrity"}
    if not isinstance(record, dict) or set(record) != expected:
        return "unexpected or missing fields"
    if record["schema"] != PROGRESS_SCHEMA or record["txn_id"] != txn_id \
            or record["seq"] != seq or record["phase"] not in (RESTORING, RESUMING) \
            or record["outcome"] not in PROGRESS_OUTCOMES:
        return "identity, phase or outcome does not match"
    integrity = record["integrity"]
    if not isinstance(integrity, dict) or integrity.get("content_sha256") != record_hash(record):
        return "content hash does not match"
    if record["prev_sha256"] != prev:
        return "does not chain to the previous record"
    return None
