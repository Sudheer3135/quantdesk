"""Settling transactions a previous run did not finish.

Run before every request is acted on, at boot, and at 07:00 IST on weekdays
(quantdesk-power-recover). For each transaction that is not terminal:

  before the request was sent, or NOT_SENT, REJECTED, POLL_FAILED, or a
  restoration that was interrupted or failed
      -> restore the recorded prior state (only what was running before and is
         not running now is started), then RESTORED or RESTORE_FAILED;
  SUBMITTING with no recorded answer
      -> UNKNOWN: the request may have left; nothing is restarted;
  ACCEPTED, UNKNOWN, UNKNOWN_RETAINED, OBSERVED_NEW_BOOT
      -> if this is a later boot, OBSERVED_NEW_BOOT is recorded once for it,
         and nothing is concluded from it;
      -> if Azure gave an operation URL, its status is read within a short
         budget: Succeeded -> POLL_SUCCEEDED; Failed or Canceled -> POLL_FAILED
         and restoration; anything else leaves the outcome unknown;
      -> otherwise the transaction stays unresolved.

An operation's status is never read earlier than Azure asked (Retry-After):
the earliest permitted time is journaled with UNKNOWN_RETAINED and honoured
by every later run, and a transaction with few journal records left is no
longer polled (an operator settles it).

An unresolved transaction blocks every new shutdown and, through the
admission gate, every start of the workload units. The VM therefore stays as
it is and the independent Azure monitor reports it at each run until
operation-specific evidence or an operator's finding (`power.py resolve`)
from Azure's own records settles it. Elapsed time, a running VM or a new boot
is never taken as evidence that an operation failed or succeeded.

Azure's outcome and the workload's recovery are separate. A transaction
whose deallocation Azure settled (COMPLETED, POLL_SUCCEEDED, or an operator's
deallocation-completed finding) stays open until, in a LATER boot than the
one that asked for it, the workload that ran before the shutdown is verified
running again (controller.resume: RESUMING journaled first, per-unit
progress kept, RESUMED or RESUME_FAILED). Every recovery run retries an
interrupted or failed resumption, as it does an interrupted or failed
restoration, until the journal's reserve is reached; then an operator
settles it. Nothing in recovery ever sends a deallocation request. A
restoration or resumption whose first record (RESTORING / RESUMING) cannot
be written and read back starts nothing and raises; the next run retries it
from what the journal shows. A
deallocation-completed finding in the same boot is refused: a completed
deallocation ends the boot.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import journal
from .azure import POLL_FAILED, POLL_SUCCEEDED, RETRY_DEFAULT
from .controller import RESTORABLE, Env, restore, resume
from .gate import submission_boot
from .journal import (
    ACCEPTED,
    OBSERVED_NEW_BOOT,
    OPERATOR_CLOSED,
    OPERATOR_CONFIRMED_DEALLOCATED,
    OPERATOR_CONFIRMED_NOT_PERFORMED,
    RESTORE_FAILED,
    SUBMITTING,
    UNKNOWN,
    UNKNOWN_RETAINED,
)

RECOVERY_POLL_BUDGET = 90.0
POLL_RECORD_RESERVE = 20       # journal records kept free for findings and restoration
FINDINGS = {"deallocation-completed": OPERATOR_CONFIRMED_DEALLOCATED,
            "deallocation-not-performed": OPERATOR_CONFIRMED_NOT_PERFORMED,
            "restoration-repaired": OPERATOR_CLOSED}


def _record(env: Env, jrnl: journal.Journal, txn: journal.Transaction, state: str,
            data: dict) -> None:
    jrnl.append(txn, state, env.boot_id(), data, clock=lambda: env.now().isoformat())


def _operation(txn: journal.Transaction) -> dict | None:
    accepted = txn.data(ACCEPTED)
    if accepted and (accepted.get("async_url") or accepted.get("location_url")):
        return accepted
    return None


def not_before(txn: journal.Transaction) -> datetime | None:
    """The earliest permitted status read: the latest journaled one, else the 202's own."""
    for record in reversed(txn.records):
        value = record["data"].get("poll_not_before_utc")
        if value:
            return datetime.fromisoformat(value)
    accepted = next((r for r in txn.records if r["state"] == ACCEPTED), None)
    if accepted is None:
        return None
    delay = accepted["data"].get("retry_after")
    return datetime.fromisoformat(accepted["at_utc"]) + timedelta(
        seconds=RETRY_DEFAULT if delay is None else float(delay))


def _later_boot(env: Env, txn: journal.Transaction) -> bool:
    return submission_boot(txn) not in (None, env.boot_id())


def settle(env: Env, jrnl: journal.Journal, txn: journal.Transaction) -> None:
    state = txn.state
    room = len(txn.records) < journal.MAX_RECORDS - POLL_RECORD_RESERVE
    if state in RESTORABLE:
        if room:
            restore(env, jrnl, txn, f"recovery after the controller ended in {state}")
        return
    if state in journal.RESUMABLE:
        # Azure is settled; the workload comes back only in a later boot.
        if room and _later_boot(env, txn):
            resume(env, jrnl, txn, f"recovery found the workload not yet resumed ({state})")
        return
    if state == SUBMITTING:
        _record(env, jrnl, txn, UNKNOWN, {"detail": "the controller ended after recording the "
                                                    "submission and before recording Azure's "
                                                    "answer; the request may have been sent"})
    boot = env.boot_id()
    seen_boots = {r["boot_id"] for r in txn.records}
    if boot not in seen_boots:
        _record(env, jrnl, txn, OBSERVED_NEW_BOOT,
                {"boot_id": boot, "note": "a later boot was observed; nothing is concluded "
                                          "from it about the deallocation"})
    operation = _operation(txn)
    if operation is None:
        if txn.state in (ACCEPTED, UNKNOWN):
            _record(env, jrnl, txn, UNKNOWN_RETAINED,
                    {"detail": "no operation status can be read; the outcome stays unknown"})
        return
    if len(txn.records) >= journal.MAX_RECORDS - POLL_RECORD_RESERVE:
        return                       # polled long enough; an operator settles it
    earliest = not_before(txn)
    outcome, detail, resume_at = env.arm.follow(operation, budget=RECOVERY_POLL_BUDGET,
                                                not_before=earliest)
    if outcome == POLL_SUCCEEDED:
        _record(env, jrnl, txn, journal.POLL_SUCCEEDED, {"detail": detail})
        if _later_boot(env, txn):
            resume(env, jrnl, txn, "the operation's own status says it succeeded")
    elif outcome == POLL_FAILED:
        _record(env, jrnl, txn, journal.POLL_FAILED, {"detail": detail})
        restore(env, jrnl, txn, "the operation's own status says it did not complete")
    elif txn.state in (ACCEPTED, UNKNOWN, OBSERVED_NEW_BOOT) or (
            resume_at and (earliest is None or datetime.fromisoformat(resume_at)
                           > earliest + timedelta(seconds=1))):
        _record(env, jrnl, txn, UNKNOWN_RETAINED, {"detail": detail,
                                                   "poll_not_before_utc": resume_at})


def reconcile(env: Env, jrnl: journal.Journal) -> list[journal.Transaction]:
    """Settle what can be settled; return the transactions still unresolved."""
    for txn in jrnl.open_transactions():
        settle(env, jrnl, txn)
    return jrnl.open_transactions()


def resolve(env: Env, jrnl: journal.Journal, txn_id: str, finding: str, evidence_reference: str,
            recorded_by: str) -> str:
    """Record an operator's finding, made from Azure's own records, for one transaction."""
    if finding not in FINDINGS:
        raise ValueError(f"finding must be one of {', '.join(FINDINGS)}")
    for key, value in (("evidence_reference", evidence_reference), ("recorded_by", recorded_by)):
        if not isinstance(value, str) or not value.strip() or len(value) > 300:
            raise ValueError(f"{key} must be given (at most 300 characters)")
    txn = jrnl.load(txn_id)
    state = FINDINGS[finding]
    if state == OPERATOR_CLOSED and txn.state not in (RESTORE_FAILED, journal.RESUME_FAILED):
        raise ValueError("restoration-repaired applies only to a failed restoration or "
                         "resumption")
    if state != OPERATOR_CLOSED and txn.state not in (ACCEPTED, UNKNOWN, UNKNOWN_RETAINED,
                                                      OBSERVED_NEW_BOOT):
        raise ValueError(f"{finding} applies only to an unresolved deallocation, not "
                         f"{txn.state}")
    if state == OPERATOR_CONFIRMED_DEALLOCATED and submission_boot(txn) == env.boot_id():
        raise ValueError("deallocation-completed contradicts this machine: it is still the "
                         "boot that sent the request, and a completed deallocation ends the "
                         "boot; record deallocation-not-performed if Azure's records show that")
    _record(env, jrnl, txn, state, {"finding": finding, "evidence_reference": evidence_reference,
                                    "recorded_by": recorded_by})
    if state == OPERATOR_CONFIRMED_NOT_PERFORMED:
        return restore(env, jrnl, txn, "an operator recorded that the deallocation was not "
                                       "performed")
    if state == OPERATOR_CONFIRMED_DEALLOCATED:
        resume(env, jrnl, txn, "an operator recorded that the deallocation completed")
    return state
