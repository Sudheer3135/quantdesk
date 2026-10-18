#!/usr/bin/env python3
"""Record an operator approval for the observer. Run by the operator, never by the service.

An approval lets the observer count one session whose after-close report says
OPERATIONAL EXCEPTION REQUIRED, or lets a later report stand in place of an
earlier one. It is bound to the exact report file by its SHA-256, so it
cannot be reused for another report or survive a change to that one.

This writes into the approvals directory, which the observer service account
can read and cannot write. A typed name is not authentication: what protects
an approval is who is allowed to write to that directory. No signing is done.

    operator_approval.py --report-root /var/lib/quantdesk-observer \\
        --approvals-root /etc/quantdesk-observer/operator/approvals \\
        --session-date 2026-10-14 --report-id <id> \\
        --scope count_session_under_operational_exception \\
        --exception "08:45 readiness MISSING: VM started 09:03" \\
        --astra-decision-reference "<where Astra's decision is recorded>" \\
        --approval-text-file decision.txt --recorded-by <operator>
"""
import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from quantdesk_observer.cli import COUNT_SCOPE, SUPERSEDE_SCOPE  # noqa: E402
from quantdesk_observer.store import Store, file_sha256, publish, read_json  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("report-root", "approvals-root", "session-date", "report-id", "exception",
                 "astra-decision-reference", "approval-text-file", "recorded-by"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--scope", required=True, choices=[COUNT_SCOPE, SUPERSEDE_SCOPE])
    parser.add_argument("--superseding-report-id")
    args = parser.parse_args(argv)
    store = Store(args.report_root)
    target = store.report_file(args.session_date, args.report_id)
    if target is None:
        parser.error(f"no report {args.report_id} exists for {args.session_date}")
    report = read_json(target)
    if args.scope == COUNT_SCOPE and report.get("outcome") != "OPERATIONAL EXCEPTION REQUIRED":
        parser.error(f"that report's outcome is {report.get('outcome')!r}; only an "
                     "OPERATIONAL EXCEPTION REQUIRED session can be approved for counting")
    text = Path(args.approval_text_file).read_text().strip()
    fields = {"exception": args.exception, "astra_decision_reference":
              args.astra_decision_reference, "approval_text": text,
              "recorded_by": args.recorded_by}
    if not all(value.strip() for value in fields.values()):
        parser.error("exception, decision reference, approval text and operator are all required")
    approval = {"schema": "observer_exception_approval/1", "approval_id": store.new_id(),
                "scope": args.scope, "session_date": args.session_date,
                "report_id": args.report_id, "report_sha256": file_sha256(target),
                "recorded_at_utc": datetime.now(UTC).isoformat(), **fields}
    if args.scope == SUPERSEDE_SCOPE:
        other = store.report_file(args.session_date, args.superseding_report_id or "")
        if other is None:
            parser.error("--superseding-report-id must name a report for the same session date")
        approval.update(superseding_report_id=args.superseding_report_id,
                        superseding_report_sha256=file_sha256(other))
    root = Path(args.approvals_root)
    if not root.is_dir():
        parser.error(f"{root} does not exist; the operator creates it, not this tool")
    path = root / f"approval-{args.session_date}-{approval['approval_id']}.json"
    if not publish(path, approval, mode=0o444):
        parser.error(f"{path} already exists")
    print(f"recorded {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
