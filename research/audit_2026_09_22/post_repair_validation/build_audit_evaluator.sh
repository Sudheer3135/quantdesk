#!/bin/sh
# Rebuilds the audit-time evaluator OUTSIDE the repository for r8_eval.py.
# The audit's outcomes.py, feed.py and costs.py are byte-identical to git HEAD
# c8c1e10 (verified against ../source_manifest.json); everything else is the
# current working tree. Prints the temp directory to use as PYTHONPATH.
set -e
ROOT=$(git rev-parse --show-toplevel)
OUT=$(mktemp -d)
cp -R "$ROOT/backend/app" "$OUT/app"
find "$OUT" -name __pycache__ -prune -exec rm -rf {} +
for f in evaluation/outcomes.py backtest/feed.py backtest/costs.py; do
  git -C "$ROOT" show "HEAD:backend/app/$f" > "$OUT/app/$f"
done
echo "$OUT"
