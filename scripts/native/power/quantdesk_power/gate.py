"""The admission gate: may a QuantDesk workload unit start now?

Installed (later, separately) as an `ExecStartPre=+` drop-in on
quantdesk-postgres, -redis, -api and -web, so systemd asks it before every
start of those units, at boot and at any other time. It reads the journal and
nothing else, writes nothing, and takes no lock (the controller holds the
coordination lock while it restores, and restoration must pass this gate).

ALLOW when the journal is readable and validates and:
  - no transaction is Azure-unresolved;
  - no transaction is in a protected stage (FROZEN .. POSTGRES_STOPPED) in
    this boot - the controller is stopping the workload and nothing else may
    start it (its own restoration is recorded as RESTORING first);
  - no transaction says the VM was deallocated while this is still the boot
    that asked for it (that would contradict Azure's answer);
  - at most one transaction is open.
BLOCK otherwise, and whenever anything cannot be read: no boot id, no state
directory, no journal, a journal that does not validate. A block is the
safe state; it is visible (the unit fails, the observer's assessments then
fail and alert, and `power.py status` names the reason).

A protected stage from an EARLIER boot is allowed: the request was never sent
in that boot, so starting the workload is the restoration recovery performs.
Elapsed time, a running VM or a new boot never allow an unresolved one.
"""
from __future__ import annotations

import os

from . import fsafe, journal


def submission_boot(txn: journal.Transaction) -> str | None:
    for record in txn.records:
        if record["state"] == journal.SUBMITTING:
            return record["boot_id"]
    return None


def decide(cfg: dict, boot_id) -> tuple[bool, str]:
    """(allowed, reason). `boot_id` is a callable returning this boot's id."""
    try:
        boot = boot_id()
    except (OSError, ValueError) as exc:
        return False, f"this boot's id cannot be read ({exc}); refusing until it can"
    try:
        state_dir = fsafe.open_dir(cfg["state_root"], owner=os.geteuid(), private=True)
    except FileNotFoundError:
        return False, "the power controller's state directory is missing"
    except OSError as exc:
        return False, f"the power controller's state directory is unsafe ({exc})"
    try:
        try:
            jrnl = journal.Journal(state_dir, owner=os.geteuid(), create=False)
        except FileNotFoundError:
            return False, "the power controller's journal is missing"
        except OSError as exc:
            return False, f"the power controller's journal is unsafe ({exc})"
        try:
            txns = jrnl.all()
        except (journal.CorruptJournal, OSError) as exc:
            return False, f"the power controller's journal is unusable ({exc})"
        finally:
            jrnl.close()
    finally:
        state_dir.close()
    return judge(txns, boot)


def judge(txns: list[journal.Transaction], boot: str) -> tuple[bool, str]:
    open_txns = [t for t in txns if not t.terminal]
    if len(open_txns) > 1:
        return False, (f"{len(open_txns)} transactions are open at once "
                       f"({', '.join(t.txn_id for t in open_txns)}); the journal contradicts "
                       "itself")
    for txn in txns:
        if txn.state in journal.UNRESOLVED_AZURE:
            return False, (f"transaction {txn.txn_id} is {txn.state}: Azure's deallocation is "
                           "unresolved; settle it with operation-specific evidence or "
                           "`power.py resolve`")
        if txn.state in journal.PROTECTED and txn.records[-1]["boot_id"] == boot:
            return False, (f"transaction {txn.txn_id} is stopping the workload in this boot "
                           f"({txn.state}); only its own restoration may start it")
        if txn.state in journal.DEALLOCATED and submission_boot(txn) == boot:
            return False, (f"transaction {txn.txn_id} says the VM was deallocated "
                           f"({txn.state}), yet this is the boot that asked for it")
    return True, "no unresolved or in-progress shutdown"
