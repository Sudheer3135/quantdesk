#!/usr/bin/env python3
"""Acknowledge the observer's REVIEW PENDING checkpoint. Run by the operator only.

An acknowledgment records that the owner has seen the checkpoint, so the
observer stops signalling owner attention for it. It is bound to the exact
checkpoint by its id and SHA-256.

It does nothing else. It does not reopen the observation phase, approve an
operational exception, authorize further research, or authorize any
deployment. Those are separate decisions with separate records.

It writes into the operator's acknowledgments directory, which the observer
service account can read and cannot write.

    operator_acknowledge.py --report-root /var/lib/quantdesk-observer \\
        --acknowledgments-root /etc/quantdesk-observer/operator/acknowledgments \\
        --checkpoint-id <id> --text-file note.txt --acknowledged-by <operator>
"""
import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from quantdesk_observer.cli import ACK_SCOPE  # noqa: E402
from quantdesk_observer.store import Store, file_sha256, publish  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("report-root", "acknowledgments-root", "checkpoint-id", "text-file",
                 "acknowledged-by"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args(argv)
    store = Store(args.report_root)
    checkpoint = store.checkpoint()
    if checkpoint is None or checkpoint["checkpoint_id"] != args.checkpoint_id:
        parser.error(f"no checkpoint {args.checkpoint_id} is recorded")
    text = Path(args.text_file).read_text().strip()
    if not text or not args.acknowledged_by.strip():
        parser.error("the acknowledgment text and the operator are both required")
    root = Path(args.acknowledgments_root)
    if not root.is_dir():
        parser.error(f"{root} does not exist; the operator creates it, not this tool")
    record = {"schema": "observer_checkpoint_acknowledgment/1",
              "acknowledgment_id": store.new_id(), "scope": ACK_SCOPE,
              "checkpoint_id": checkpoint["checkpoint_id"],
              "checkpoint_sha256": file_sha256(store.checkpoint_path),
              "acknowledgment_text": text, "acknowledged_by": args.acknowledged_by,
              "acknowledged_at_utc": datetime.now(UTC).isoformat(),
              "effect": "owner attention for this checkpoint is acknowledged; the phase stays "
                        "REVIEW PENDING and no exception, research or deployment is approved"}
    path = root / f"acknowledgment-{record['acknowledgment_id']}.json"
    if not publish(path, record, mode=0o444):
        parser.error(f"{path} already exists")
    print(f"recorded {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
