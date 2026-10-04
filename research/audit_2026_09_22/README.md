# Reproduce this audit

Run from the repository root with its existing virtual environment:

```sh
PYTHONPATH=backend .venv/bin/python research/audit_2026_09_22/analyze.py
PYTHONPATH=backend .venv/bin/python research/audit_2026_09_22/supplement.py
PYTHONPATH=backend .venv/bin/python research/audit_2026_09_22/checks.py
```

These read the local git-ignored `snapshot.sqlite` and write research JSON only. `snapshot.py` is the separately authorized, read-only database capture; it refuses to overwrite evidence. Configure DATABASE_URL to the correct local database to create a new snapshot in a separate audit directory. Do not publish credentials.

- `REPORT.md`: reconstruction, prioritized findings, measured diagnosis, and next experiment gates.
- `manifest.json`: consistent snapshot time/counts/hash.
- `audit_metrics.json`: actual data inventory and signal study metrics.
- `signal_outcomes.json`: all 377 hypothetical outcomes and existing study caveats.
- `supplement.json`: timing/slippage sensitivities, groupings, paper refusals, and day-block bootstrap.
- `reproductions.json`: small synthetic reproductions, explicitly distinct from actual backtest outcomes.

No strategy weights or thresholds were optimized. No production module was edited. Research outputs are not validated option-trading performance.
