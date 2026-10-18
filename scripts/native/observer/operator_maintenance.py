#!/usr/bin/env python3
"""Record planned inactivity for the observer's reports. Run by the operator only.

A maintenance record explains a period in which heartbeats or assessments are
expected to be absent. The observer copies active records into its reports.

It is information and nothing more. It does not pause any Healthchecks.io
check, silence an alert, change a result, or stop, start or restart
anything. Pausing or resuming a hosted check is done by hand in
Healthchecks.io and needs its own authorization, which this record cites.

    operator_maintenance.py --maintenance-root /etc/quantdesk-observer/operator/maintenance \\
        --start "2026-10-17T22:00:00+05:30" --end "2026-10-18T07:00:00+05:30" \\
        --reason "planned VM stop" --affected-checks liveness \\
        --authorization-reference "<where the pause was authorized>" --recorded-by <operator>
"""
import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from quantdesk_observer.healthchecks import CHECKS  # noqa: E402
from quantdesk_observer.store import Store, publish  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("maintenance-root", "start", "end", "reason", "authorization-reference",
                 "recorded-by"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--affected-checks", required=True, nargs="+", choices=CHECKS)
    args = parser.parse_args(argv)
    try:
        start, end = datetime.fromisoformat(args.start), datetime.fromisoformat(args.end)
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError
    except ValueError:
        parser.error("--start and --end must be ISO times with a time zone, end after start")
    root = Path(args.maintenance_root)
    if not root.is_dir():
        parser.error(f"{root} does not exist; the operator creates it, not this tool")
    record = {"schema": "observer_maintenance/1", "maintenance_id": Store.new_id(),
              "start_ist": args.start, "end_ist": args.end, "reason": args.reason,
              "affected_checks": args.affected_checks,
              "authorization_reference": args.authorization_reference,
              "recorded_by": args.recorded_by,
              "recorded_at_utc": datetime.now(UTC).isoformat(),
              "effect": "information only; no check is paused and no result is changed"}
    path = root / f"maintenance-{record['maintenance_id']}.json"
    if not publish(path, record, mode=0o444):
        parser.error(f"{path} already exists")
    print(f"recorded {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
