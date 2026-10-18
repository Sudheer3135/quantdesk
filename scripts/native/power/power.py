"""QuantDesk power controller command line.

    power.py request  --config REQUEST_FILE  unprivileged requester (quantdesk-power): file one
                                             request; reads only the requester's own config
    power.py control  --config FILE   root controller: reconcile, then act on one request
    power.py recover  --config FILE   root: settle unfinished transactions (boot, hourly)
    power.py resolve  --config FILE --txn ID --finding F --evidence-ref TEXT --recorded-by NAME
    power.py gate     --config FILE --unit UNIT   root, ExecStartPre=+: may this workload start?
    power.py status   --config FILE [--json]
    power.py plan-special --record FILE   print a special session's monitoring plan

Exit status: 0 done or nothing to do (gate: allowed); 1 a transaction is
unresolved, a request was refused, or (gate) the start is refused; 2 the
configuration, journal or invocation is unusable (gate: refused as well).
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from quantdesk_power import config as config_module  # noqa: E402
from quantdesk_power import (  # noqa: E402
    controller,
    facts,
    fsafe,
    gate,
    journal,
    locks,
    recovery,
    special,
)
from quantdesk_power.azure import Arm  # noqa: E402
from quantdesk_power.db import PsqlSession  # noqa: E402
from quantdesk_power.system import Runner, Systemd  # noqa: E402


def real_env(cfg: dict) -> controller.Env:
    programs = cfg["programs"]
    runner = Runner(allowed=frozenset({*programs.values(), cfg["observer"]["python"]}))
    return controller.Env(
        cfg=cfg, runner=runner, systemd=Systemd(runner, programs["systemctl"]),
        clock=lambda: datetime.now(UTC), boot_id=facts.boot_id,
        uid_of=lambda user: pwd.getpwnam(user).pw_uid, open_db=PsqlSession, arm=Arm(cfg),
        observer_lock=locks.ObserverLock, sleep=time.sleep)


def _recorded_transactions(cfg: dict) -> list[journal.Transaction]:
    """The journal as it is, for reading. Never creates the state directory
    or the journal: a missing one is missing evidence, and the gate must go
    on refusing for that reason rather than find a fresh, empty journal."""
    try:
        state_dir = fsafe.open_dir(cfg["state_root"], owner=os.geteuid(), private=True)
    except FileNotFoundError:
        return []
    try:
        try:
            jrnl = journal.Journal(state_dir, owner=os.geteuid(), create=False)
        except FileNotFoundError:
            return []
        try:
            return jrnl.all()
        finally:
            jrnl.close()
    finally:
        state_dir.close()


def status(cfg: dict, as_json: bool) -> int:
    txns = _recorded_transactions(cfg)
    now = datetime.now(UTC)
    holds, hold_fact = facts.read_holds(cfg, now)
    allowed, why = gate.decide(cfg, facts.boot_id)
    out = {
        "workload_start": {"allowed": allowed, "reason": why},
        "now_utc": now.isoformat(),
        "transactions": [{"txn_id": t.txn_id, "state": t.state,
                          "started_at_utc": t.first["at_utc"],
                          "last_at_utc": t.records[-1]["at_utc"], "terminal": t.terminal}
                         for t in sorted(txns, key=lambda t: t.first["at_utc"])][-20:],
        "unresolved": [t.txn_id for t in txns if not t.terminal],
        "holds": hold_fact.as_dict(),
        "active_holds": [h["hold_id"] for h in holds if h["active"]],
        "attempt_window": facts.in_window(now, cfg).as_dict(),
    }
    if as_json:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print(f"power status at {out['now_utc']}")
        print(f"  unresolved transactions: {', '.join(out['unresolved']) or 'none'}")
        for t in out["transactions"]:
            print(f"  {t['txn_id']}  {t['state']:<32} {t['last_at_utc']}")
        print(f"  workload start: {'allowed' if allowed else 'REFUSED'} ({why})")
        print(f"  holds: {out['holds']['status']} ({out['holds']['detail']})")
        print(f"  attempt window: {out['attempt_window']['detail']}")
    return 1 if out["unresolved"] or not allowed else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="power", description=__doc__)
    parser.add_argument("command", choices=["request", "control", "recover", "resolve",
                                            "gate", "status", "plan-special"])
    parser.add_argument("--unit")
    parser.add_argument("--config")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--txn")
    parser.add_argument("--finding", choices=sorted(recovery.FINDINGS))
    parser.add_argument("--evidence-ref")
    parser.add_argument("--recorded-by")
    parser.add_argument("--record")
    args = parser.parse_args(argv)
    if args.command == "plan-special":
        if not args.record:
            parser.error("plan-special needs --record")
        try:
            with open(args.record) as handle:
                print(json.dumps(special.plan(json.load(handle)), indent=2))
        except (OSError, ValueError) as exc:
            print(f"plan-special: refused: {exc}", file=sys.stderr)
            return 2
        return 0
    if not args.config:
        parser.error("--config is required")
    try:
        if args.command == "request":
            cfg = config_module.load_request(args.config)
        else:
            cfg = config_module.load(args.config)
    except config_module.ConfigError as exc:
        print(f"power: configuration refused: {exc}", file=sys.stderr)
        return 2
    try:
        if args.command == "request":
            return controller.file_request(cfg, datetime.now(UTC), facts.boot_id)
        if args.command == "gate":
            if not args.unit:
                parser.error("gate needs --unit")
            allowed, why = gate.decide(cfg, facts.boot_id)
            print(f"power gate: {args.unit}: {'allowed' if allowed else 'REFUSED'}: {why}",
                  file=sys.stdout if allowed else sys.stderr)
            return 0 if allowed else 1
        if args.command == "status":
            return status(cfg, args.json)
        env = real_env(cfg)
        if args.command == "control":
            return controller.handle_requests(env, recovery.reconcile)
        state_dir = controller.open_state(cfg)
        try:
            try:
                held = locks.coordination_lock(state_dir)
            except locks.Busy:
                print("power: another controller run is in progress; nothing done")
                return 1
            try:
                jrnl = journal.Journal(state_dir, owner=os.geteuid())
                if args.command == "recover":
                    try:
                        unresolved = recovery.reconcile(env, jrnl)
                    finally:
                        controller.publish_status(env, jrnl)
                    for txn in unresolved:
                        print(f"power: UNRESOLVED {txn.txn_id} {txn.state}")
                    return 1 if unresolved else 0
                if not (args.txn and args.finding and args.evidence_ref and args.recorded_by):
                    parser.error("resolve needs --txn, --finding, --evidence-ref and "
                                 "--recorded-by")
                try:
                    print(recovery.resolve(env, jrnl, args.txn, args.finding, args.evidence_ref,
                                           args.recorded_by))
                finally:
                    controller.publish_status(env, jrnl)
                return 0
            finally:
                os.close(held)
        finally:
            state_dir.close()
    except journal.CorruptJournal as exc:
        print(f"power: JOURNAL UNUSABLE: {exc}; nothing more is done until an operator has "
              "examined it", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"power: refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - the gate in particular must fail closed
        print(f"power: refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
