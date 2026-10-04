# Post-repair validation probes

Independent probes behind `VALIDATION_REPORT.md`. They check whether the uncommitted Codex repairs satisfy the acceptance checklist. Machine-readable statuses are in `validation_status.json` (51 IDs: **18 PASS · 26 FAIL · 4 UNVERIFIED · 3 BLOCKED BY DATA**).

## Fixed inputs

| | |
|---|---|
| Code | git HEAD `c8c1e101c2be730d8264dd9dd9deedc359372fc9` plus the uncommitted working tree (76 dirty paths at validation time) |
| Code hash | `d8868d6028327c6c3944f70fdef7da4d4c6146fda67d7708ab95fe61508f6852`, the sha256 over `backend/app/**/*.py` (relative path + bytes, sorted), the same algorithm as `app.backtest.measurement.provenance` |
| Data snapshot | `../snapshot.sqlite`, sha256 `9b49ef771fa13fce4a99415f71766f4bf63d83b20994672fbcad08dde0aba89d` (git-ignored; the probes need the local copy) |
| Python | the repo's `.venv` (Python 3.13) |

If the code hash differs when you rerun, you're validating different code. The results below then no longer describe it.

## Safety

- Every probe opens the snapshot with SQLite `mode=ro` (read-only). None connects to the live PostgreSQL database.
- No probe writes to `backend/`, `tests/`, the database or configuration. They only print JSON to stdout.
- Run with `PYTHONDONTWRITEBYTECODE=1`, so importing `app` doesn't write `.pyc` files under `backend/`.
- `build_audit_evaluator.sh` writes only to a fresh `mktemp -d` directory **outside** the repository.

## Rerunning

From the repository root:

```sh
D=research/audit_2026_09_22/post_repair_validation
export PYTHONDONTWRITEBYTECODE=1

# r1–r7 and r9: the current working tree
PYTHONPATH=backend .venv/bin/python $D/r1_audit_repros.py      # likewise r2 … r7, r9

# r8: the audit-time evaluator, then the repaired one
OLD=$($D/build_audit_evaluator.sh)
PYTHONPATH=$OLD   .venv/bin/python $D/r8_eval.py old
PYTHONPATH=backend .venv/bin/python $D/r8_eval.py new
rm -rf "$OLD"
```

Captured outputs from the confirming rerun (22 September 2026, same code hash and snapshot) are in `results/`. r2, r3 and r7 each take a few minutes.

## Probes

Status is the checklist status each result supports. Several items also rest on pytest evidence named in `VALIDATION_REPORT.md`.

### `r1_audit_repros.py`: the audit's original bug reproductions
- **Validates:** CC-1, TC-4, FL-1, PL-1
- **Input:** synthetic frames, the same as `../checks.py`
- **Expected if repaired:** the forming bar is dropped; relative volume on a prefix equals the full frame; bid 100 / ask 102 fills at 102 / 100; an entry into the final bar settles.
- **Actual:** forming bar retained = `false`; relative-volume bug reproduces = `false`; fills 102.0 / 100.0 (audit: 96 / 109); final-bar entry → 1 trade (audit: 0).
- **Status:** PASS for CC-1, TC-4, FL-1 and PL-1.

### `r2_prefix.py`: prefix invariance
- **Validates:** TC-4, HTF-1, HTF-3 (and the day-regime part of HTF-4)
- **Input:** the snapshot's NIFTY 5m (6,750 bars), plus a copy with synthetic random volume; 42 seeded cut points
- **Expected:** the value computed from `bars[:k]` equals row k−1 of the full-history computation. Checked for EMA 20/50/100/200, ATR14, VWAP, VWAP bands, relative volume, closed 15m and 1h bins, day and hour regime, confirmed swings and FVG formation.
- **Actual:** 0 mismatches everywhere. On real snapshot volume, VWAP, bands and relative volume are all null (placeholder volume disabled).
- **Status:** PASS for TC-4, HTF-1 and HTF-3. HTF-4 stays UNVERIFIED because the VIX as-of rule has no test.

### `r3_signal.py`: forming-bar injection, boundary, warmup
- **Validates:** CC-1, CC-2, TC-5
- **Input:** the snapshot; 25 cut points for injection and 30 for warmup (seed 11)
- **Expected:**
  - a forming bar changes neither the signal nor the plan;
  - 1ms before the close gives 2 of 3 bars, and exactly at the close gives 3 of 3;
  - the signal is the same under any declared warmup.
- **Actual:**
  - forming bar: signal changed 0 of 25; **`plan.build` changed 17 of 25**;
  - boundary: 2 then 3; finality delay not modelled;
  - warmup: 300-bar vs 5-day windows differ 0 of 30; **5-day vs full history differs 3 of 30**.
- **Status:**
  - CC-1 PASS (the plan exposure is recorded as a remaining issue and a latent bug);
  - CC-2 PASS;
  - TC-5 **FAIL**.

### `r4_costs_vwap_life.py`: costs, VWAP, volume, lifecycle
- **Validates:** CA-1, CA-2, FC-1, FC-2, PL-1, PL-3 (engine), SE-3, SE-4
- **Input:** a hand-calculated fee example; synthetic prices 1, 2, 3; synthetic single-session bars
- **Expected:**
  - fee code equals the hand calculation;
  - loss legs charged buy@120 / sell@100;
  - VWAP σ = 0.816497;
  - placeholder and constant volume both unavailable;
  - stop, target and gap-stop exits correct;
  - one position at a time;
  - entries refused after two losses.
- **Actual:**
  - fees: win ₹63.362161, code = hand ✔; loss correct ₹61.907161, but the min/max legs used by `outcomes.py` and `engine.py` give ₹63.362161 ✘;
  - VWAP σ 0.816497 ✔;
  - placeholder volume → null ✔; **constant 1000 → treated as real** ✘;
  - exits: stop 99, target 102, gap stop 97, final bar `end_of_data` ✔;
  - a signal on every bar → 1 entry ✔; 2 losses → no further entries ✔.
- **Status:**
  - FC-1, SE-3 and SE-4 PASS; PL-1 supporting evidence;
  - CA-1, CA-2 and FC-2 **FAIL**;
  - PL-3 is FAIL overall (the evaluator is not fixed; see the report).

### `r5_life2.py`: time exit, session boundary, gapped entry
- **Validates:** PL-2, SE-2, SE-1
- **Input:** synthetic bars over two sessions
- **Expected:** a time exit; no fill from a late-session signal on either day; levels on a gapped fill follow a declared policy.
- **Actual:**
  - time exit 09:45 → 11:50 (25 bars; 24 configured, minor);
  - signals at 15:20 and 15:25 → no fill;
  - 14:55 signal → exits `session end` at 15:15;
  - fill at 101.5 silently moves the stop 99 → 100.5 and the target 102 → 103.5.
- **Status:** PL-2 PASS, SE-1 PASS, SE-2 **FAIL**.

### `r6_repro.py`: determinism and provenance with the default config
- **Validates:** RP-1 (supporting), RP-2, RP-4
- **Input:** the last 1,500 snapshot bars with the default engine config
- **Expected:** identical output on two runs; provenance records the code, config, data, git commit and dirty state.
- **Actual:**
  - identical output, sha256 `adffd656…`, but **0 trades**: the default ₹100k with a 75-unit lot refuses every signal;
  - provenance has `code_sha256 d8868d6028327c6c…`, config and data hashes;
  - **no git commit and no dirty flag**.
- **Status:** RP-4 **FAIL**; RP-2 FAIL (no per-signal provenance; see the report). A run with 0 trades is not enough for RP-1, which is why r7 exists.

### `r7_repro_trades.py`: determinism with trades present
- **Validates:** RP-1
- **Input:** the last 1,500 snapshot bars, `RiskConfig(capital=100000, lot_size=1)`
- **Expected:** byte-identical output on two runs, with trades and entries equal to closes.
- **Actual:** both runs sha256 `d7cbdd4556da6842fb1dcca91f852af129a7fc7ba203464c798217157642b926`; 40 trades, 40 entries, 40 closes.
- **Status:** PASS.

### `r8_eval.py`: the old audit evaluator vs the repaired one
- **Validates:** RP-3 (also informs TC-3)
- **Input:** the snapshot's 1,276 stored signals. `old` runs on the tree from `build_audit_evaluator.sh` (HEAD's `outcomes.py`, `feed.py` and `costs.py`, byte-identical to the audit per `../source_manifest.json`).
- **Expected:** `old` reproduces the audit's 377 / 21.75% / −0.619R / −233.218 / profit factor 0.332.
- **Actual:**
  - `old` = 377 / 21.75 / −0.619 / −233.218 / 0.332, an exact match;
  - `new` = 372 / 21.77 / −0.618 / −229.991 / 0.332. That is 1 duplicate bar and 4 overnight fills removed; all 372 are labelled `legacy_inferred_last_closed_bar`.
- **Status:** RP-3 PASS. TC-3 stays UNVERIFIED: no stored signal carries a recorded bar, and no test covers that path.
- **Note:** this is for regression understanding only. The unchanged number is not evidence about strategy quality.

### `r9_last.py`: clock grid and daily-limit reset
- **Validates:** TC-1, PL-6
- **Input:** the snapshot; synthetic bars over two sessions
- **Expected:** off-grid 15:30 bars quarantined and every session holding 75 bars (or flagged); daily limits block after two losses and reset the next session.
- **Actual:** 12 bars at 15:30 in the snapshot, **12 survive** the session filter; **18 sessions ≠ 75 bars**, none flagged. Entries: 06-01 14:20 and 14:30, blocked, then 06-02 09:20 and 09:45.
- **Status:** TC-1 **FAIL**, PL-6 PASS.

### `build_audit_evaluator.sh`
A helper for r8. It rebuilds the audit-time evaluator in a temporary directory outside the repository and prints that path.

## Items with no probe

These statuses rest on code inspection or test runs described in `VALIDATION_REPORT.md`, not on a probe here:

- **FAIL:** TC-2, TC-6, OC-1, OC-4, OC-5, PL-4, FL-2, FL-4, FL-5, RP-2, OS-1 to OS-7
- **UNVERIFIED:** CC-3, HTF-4, CA-3
- **BLOCKED BY DATA:** OC-2, OC-3, FL-3. The snapshot and the live database hold 120,838 option rows with 0 bid/ask pairs and no exchange-time column.
- **Pytest evidence** (CC-4, HTF-2, PL-5, and parts of the others): `tests/test_measurement_pipeline.py` and `tests/test_v2_paper.py`. When validated, the full suite stood at 23 failed / 1,462 passed.
