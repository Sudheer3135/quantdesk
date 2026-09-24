#!/bin/sh
# Rebuilds the audit-time evaluator OUTSIDE the repository for r8_eval.py.
# Prints the temp directory to use as PYTHONPATH.
#
# The comparator is pinned to commit c8c1e10, the audited pre-repair
# baseline. This used to read `git show HEAD:...`, which named the same
# commit only until HEAD moved; once the pre-Repair-2A checkpoint was
# committed, the "old" side silently became the repaired code and r8
# compared HEAD with HEAD. The point of r8 is frozen-old versus
# current-repaired, so the old side now names its commit.
#
# Each extracted file is verified against the sha256 the audit recorded in
# source_manifest.json, so a wrong, rewritten or missing commit fails here
# rather than producing a plausible but meaningless comparison.
set -e
BASELINE=c8c1e101c2be730d8264dd9dd9deedc359372fc9
ROOT=$(git rev-parse --show-toplevel)
MANIFEST="$ROOT/research/audit_2026_09_22/source_manifest.json"
OUT=$(mktemp -d)
cp -R "$ROOT/backend/app" "$OUT/app"
find "$OUT" -name __pycache__ -prune -exec rm -rf {} +
for f in evaluation/outcomes.py backtest/feed.py backtest/costs.py; do
  git -C "$ROOT" show "$BASELINE:backend/app/$f" > "$OUT/app/$f"
  want=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['sha256']['backend/app/' + sys.argv[2]])" "$MANIFEST" "$f")
  got=$(shasum -a 256 "$OUT/app/$f" | cut -d' ' -f1)
  if [ "$want" != "$got" ]; then
    echo "audit baseline mismatch for $f: manifest $want, extracted $got" >&2
    rm -rf "$OUT"
    exit 1
  fi
done
echo "$OUT"
