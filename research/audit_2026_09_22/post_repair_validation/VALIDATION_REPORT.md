# Quant Desk — independent post-repair validation

Validation date: 22 September 2026. Reviewer: independent (Claude). Baseline: `../REPORT.md` and the acceptance-test checklist (TC, CC, HTF, SE, OC, PL, FL, CA, FC, RP, OS).

| | |
|---|---|
| Code | HEAD `c8c1e101c2be730d8264dd9dd9deedc359372fc9`, **no commits since the audit**; all repairs are uncommitted (76 dirty paths). Content hash of `backend/app` (same algorithm as `app.backtest.measurement.provenance`): `d8868d6028327c6c3944f70fdef7da4d4c6146fda67d7708ab95fe61508f6852` |
| Data | Frozen audit snapshot `../snapshot.sqlite`, sha256 `9b49ef771fa13fce4a99415f71766f4bf63d83b20994672fbcad08dde0aba89d`, opened read-only |
| Framework | The acceptance checklist delivered in conversation; it was never saved to `research/` before this report |
| Changes made by the reviewer | None outside `research/`. Probes open the snapshot read-only |

Codex's tests were not accepted as evidence on their own. Every PASS rests on a test I ran and read, an independent probe in this directory, or both. Probe filenames `r1`–`r9` are referenced below; see `README.md`.

## Headline

**The full test suite is red: 23 failed, 1,462 passed.** Codex's own 146 targeted tests pass, but the repair was never reconciled against the rest of the suite.

- **20 failures are stale tests.** They encode the old, looser option-chain visibility and signal-bar timing, which are now correctly stricter.
- **2 failures are real regressions** (F-1 and F-2 below).
- **1** is a rerun of a stale test in the same group.

The option-contract selector has effectively lost its regression coverage: 19 of its tests fail.

## Temporal leakage (highest priority)

| Check | Result |
|---|---|
| Prefix invariance, 42 random cut points on the real snapshot plus a synthetic real-volume copy: EMA 20/50/100/200, ATR, VWAP, VWAP bands, relative volume, 15m/1h bins, day and hour regime, swings, FVG formation | **0 mismatches** |
| Forming 5m bar injected 2m40s into the next slot → **signal** | 0 of 25 changed ✔ |
| Same injection → **`plan.build`** (bias and entry state) | **17 of 25 changed** ✘. Production callers pre-filter, so it isn't live today, but the plan has no protection of its own |
| Exact close boundary | 10:04:59.999 excludes the 10:00 bar ✔; 10:05:00 includes it ✔; the finality delay is **0**, not modelled |
| Chain snapshot captured after the decision | Hidden until max(bucket close, `ingested_at`, `first_seen`) ✔ |
| VIX history | `session_date < today` ✔ in code. **No test** |
| Corrected bar not known at decision time | ✘ The upsert overwrites and bumps `revision`; the as-known values are lost |
| Five clocks | Only `bar_open_time`, `bar_close_time`, `signal_time` and `earliest_execution_time` exist, inside the `context` JSON, and only on new rows. **`available_at` and `received_at` don't exist.** `decision_at ≥ bar_close` holds; `available_at ≥ bar_close` is true by definition, not measured |
| Warmup | 300-bar backtest vs 5-day live: 30 of 30 identical. 5-day live vs full history: **3 of 30 signals differ**. EMA200 is used from bar 0 |

## Reproducing the old audit, then the repaired evaluator

The audit-time evaluator was not preserved. It was rebuilt from git HEAD: the audit's `outcomes.py`, `feed.py` and `costs.py` are byte-identical to HEAD according to `../source_manifest.json` (`build_audit_evaluator.sh`).

| Evaluator | Outcomes | Win % | Mean R | Sum R | Profit factor | What changed |
|---|---:|---:|---:|---:|---:|---|
| Audit (rebuilt) | **377** | 21.75 | **−0.619** | −233.218 | 0.332 | Exact match to REPORT.md ✔ |
| Repaired | 372 | 21.77 | −0.618 | −229.991 | 0.332 | −1 duplicate bar; −4 signals that would have filled at the next session's open; all 372 now labelled `legacy_inferred_last_closed_bar` |

**The repairs did not change the conclusion, and nothing here is evidence of a better strategy.**

- The timing repair relabels which bar was the signal bar. The fill is still the first open after the decision, so outcomes barely move.
- The audit's "−0.462R" sensitivity result actually entered *before* the decision time. It was optimistic, not a better estimate.
- The stored signals' inputs came from forming bars, placeholder volume and the old VWAP bands. No evaluator can fix that after the fact.
- "Mean net R" is about −2.81 in both versions and is meaningless, because option premium fees are charged on NIFTY index levels (CA-1).

## Final checklist

| ID | Status | Evidence | Relevant file | Remaining issue |
|---|---|---|---|---|
| TC-1 | **FAIL** | r9: 12 bars stamped 15:30 survive the session filter; 18 sessions ≠ 75 bars | `indicators.drop_outside_session`, `data/quality.py` (unchanged) | No clock-grid check or quarantine |
| TC-2 | **FAIL** | `models.py` unchanged; timing only in `context` JSON | `api/signals.py`, `workers/agent.py` | No `available_at` or `received_at`; no dedicated fixed columns; 1,276 older rows have nothing |
| TC-3 | UNVERIFIED | Code uses the recorded bar when present; all 1,276 snapshot signals take the legacy path | `evaluation/outcomes.py:603-621` | No test exercises the recorded-bar path |
| TC-4 | **PASS** | E1 | `indicators.enrich`, `timeframes.fold`, `regime`, `structure`, `smc` | Project's own leak detector is now blinded (F-1) |
| TC-5 | **FAIL** | r3: 3 of 30 signals differ from full history | `backtest/engine.py` (`analysis_window=300`), EMA seeding | No declared warmup; EMA200 used before it has converged |
| TC-6 | **FAIL** | Upsert sets `revision+1` and overwrites | `data/importer.py:128` | No as-known history |
| CC-1 | **PASS** | E2 | `indicators.drop_unclosed`, `api/signals.py:52` | `plan.build` accepts forming bars (17 of 25) |
| CC-2 | **PASS** | E3 | `indicators.drop_unclosed` | Finality delay is 0 |
| CC-3 | UNVERIFIED | — | — | Live input hashes aren't stored, so live/replay parity can't be tested |
| CC-4 | **PASS** | E4 | `optionbuy/chain.py` `ChainStore.available` | 20 stale tests still assume the old visibility |
| HTF-1 | **PASS** | E5 | `timeframes.fold` | Incomplete bins are dropped silently, not reported |
| HTF-2 | **PASS** | E6 | `timeframes.fold(as_of=…)` | — |
| HTF-3 | **PASS** | E7 | `regime.classify_frame`, `plan.read_bias` | Plan tested only through its inputs, not directly |
| HTF-4 | UNVERIFIED | Day regime passes prefix test (E7); VIX `< today` in code | `strategy_v2/vix.py:36` | No automated as-of test for VIX |
| SE-1 | **PASS** | E8 | `engine.run`, `outcomes.collect` | Latency 0 (see FL-4) |
| SE-2 | **FAIL** | r5: fill 101.5 moved stop 99→100.5 and target 102→103.5 | `engine.py:313-320`, `outcomes.py:263-265` | Silent shift, not declared in `assumptions` |
| SE-3 | **PASS** | E9 | `engine.run` → `risk.evaluate` | Signal study still counts risk-blocked signals; its label doesn't say so |
| SE-4 | **PASS** | E10 | `engine.run`, `PositionLedger` | Signal study remains overlapping (labelled as such) |
| OC-1 | **FAIL** | No exchange-time column in `option_candles`; importer unchanged | `data/importer.py:268` | Exchange time never persisted (history also blocked) |
| OC-2 | BLOCKED BY DATA | No per-contract quote or trade time archived | `optionbuy/chain.py` | Staleness is measured from bucket start |
| OC-3 | BLOCKED BY DATA | No quote timestamps; 0 bid/ask pairs | — | — |
| OC-4 | **FAIL** | API defaults `iv=0.13`, `expiry_weekday=1` | `api/backtest.py:139-141, 278-284` | Lot size 75 unverified; no dated calendar |
| OC-5 | **FAIL** | `ratio = pcr(c,"oi") or 0.0` | `analytics/options.py:130` (unchanged) | Missing OI still votes as PCR 0 |
| PL-1 | **PASS** | E11 | `engine.run`, `PositionLedger.finish` | — |
| PL-2 | **PASS** | E12 | `feed.can_enter`, `engine.run` | No explicit last-entry time in the index engine |
| PL-3 | **FAIL** | Engine ✔ (exits at 97); evaluator exits at the stop price | `outcomes.py:298-299` | Gapped stops still understated in the dashboard study |
| PL-4 | **FAIL** | Stop-first declared; no count of ambiguous bars | `engine.py:336` | Count not reported |
| PL-5 | **PASS** | E13 | `strategy_v2/paper.py:205-216` | Restart exit priced at last-seen premium (labelled, not executable) |
| PL-6 | **PASS** | E14 | `risk/manager`, `DayState` | — |
| FL-1 | **PASS** | E15 | `costs.buy_fill`, `costs.sell_fill` | Rule only; no historical quotes to apply it to |
| FL-2 | **FAIL** | `min_observed_pct = 0.0`; modelled fallback allowed | `optionbuy/strategy.py:113` | Modelled P&L allowed by default |
| FL-3 | BLOCKED BY DATA | 0 depth or bid/ask rows | — | — |
| FL-4 | **FAIL** | `earliest_execution_time == signal_time` | `signal_engine.py:348`, `engine.py` | No latency parameter |
| FL-5 | **FAIL** | No sensitivity output anywhere | — | Not implemented |
| CA-1 | **FAIL** | Fee arithmetic = hand calculation ₹63.362161 ✔, but evaluator uses `CostModel()` on index fills | `outcomes.py:560, 304` | Premium rates on index levels; no dated schedule or contract note |
| CA-2 | **FAIL** | r4: loss buy@120/sell@100 correct = ₹61.907; code charges ₹63.362 | `outcomes.py:305`, `engine.py:257` | min/max legs (option paths are correct) |
| CA-3 | UNVERIFIED | Engine R is net of costs | `engine.py:262-271` | Costs in sizing not tested; evaluator's net R is invalid |
| FC-1 | **PASS** | E16 | `indicators.vwap_bands` | — |
| FC-2 | **FAIL** | Placeholders 0/1/2 → unavailable ✔; **constant 1000 → treated as real** ✘ | `indicators.volume_weights` | Regime votes on relative volume = 1.0 (F-2) |
| FC-3 | **FAIL** | Rule is `volume > 2`, not source-based | `indicators.volume_weights` | Placeholder vs genuine zero decided by size, not provenance |
| RP-1 | **PASS** | E17 | `engine.run`, `measurement.provenance` | — |
| RP-2 | **FAIL** | Backtests carry code, config and data hashes ✔; signal and decision rows carry none | `models.py` | No version, parameter hash or input fingerprint per signal |
| RP-3 | **PASS** | E18 | rebuilt audit evaluator | Codex didn't keep a frozen comparator; per-repair attribution is coarse |
| RP-4 | **FAIL** | No commit, no dirty-tree flag; 76 uncommitted files | `measurement.provenance` | The code hash can't be traced back to a commit |
| OS-1 | **FAIL** | No record of data already seen, no holdout lock | — | Not implemented |
| OS-2 | **FAIL** | No session-block fold splitter | — | Not implemented |
| OS-3 | **FAIL** | No gap between folds for overlapping trades | — | Not implemented |
| OS-4 | **FAIL** | No trial log | — | Not implemented |
| OS-5 | **FAIL** | No pre-registered baselines | — | Not implemented |
| OS-6 | **FAIL** | No minimum sample size declared | — | Not implemented |
| OS-7 | **FAIL** | Equity curve is per trade | `engine.compute_stats` | No daily marked-to-market ledger |

**Tally:** 18 PASS · 26 FAIL · 4 UNVERIFIED · 3 BLOCKED BY DATA (51 items)

### Evidence for each PASS

All PASSes: code hash `d8868d60…`, no commit, snapshot `9b49ef77…`.

- **E1 (TC-4)**
  - Probe `r2_prefix.py`: 42 cut points; 9 indicator columns, both HTF levels, day and hour regime, swings and FVGs. Expected identical values; got **0 mismatches**.
  - `tests/test_measurement_pipeline.py::test_volume_prefix_does_not_read_future_availability` passes.
- **E2 (CC-1)**
  - Tests `::test_closed_bar_boundary` (6 cases) and `::test_signal_uses_closed_prefix_and_records_all_clocks` pass.
  - `r1`: the audit's forming-bar reproduction no longer reproduces (1 row → 0).
  - `r3`: a forming bar changed 0 of 25 signals.
- **E3 (CC-2):** `r3`: 1ms before the close gives 2 of 3 bars (expected 2); exactly at the close gives 3 of 3 (expected 3).
- **E4 (CC-4):** `::test_option_bucket_and_contract_not_visible_early` passes. Nothing is visible at +5m; it becomes visible at +7m.
- **E5 (HTF-1):** `::test_missing_piece_never_shifts_clock_buckets` passes, and `r2` found 0 bin mismatches.
- **E6 (HTF-2):** `::test_htf_not_visible_before_close` passes: nothing at 09:20, 09:25 or 09:29; one bin at 09:30.
- **E7 (HTF-3):** `r2`: 30 regime comparisons, 0 mismatches.
- **E8 (SE-1)**
  - `::test_terminal_bar_is_closed_once_and_reproducible` asserts entry time ≥ signal time.
  - `r5`: fills happen at the next open.
- **E9 (SE-3):** `r4` and `r9`: after two stops, later signals are refused and never reach the ledger.
- **E10 (SE-4)**
  - `r4`: a BUY on every bar gives 1 entry and 1 close.
  - `::test_invalid_quantity_and_overlap_are_rejected` passes.
- **E11 (PL-1)**
  - `r1`: an entry into the final bar now gives 1 trade (the audit saw 0).
  - `r4`: exits as `end_of_data`, with entries = closes = 1.
- **E12 (PL-2):** `r5`: signals at 15:20 and 15:25 give 0 fills; a 14:55 signal exits at 15:15 the same day.
- **E13 (PL-5):** `tests/test_v2_paper.py::test_a_position_stranded_by_a_restart_is_closed_and_labelled` and `::test_a_restart_inside_the_session_resumes_monitoring` both pass.
- **E14 (PL-6):** `r9`: two stops on day 1, then blocked; day 2 resumes.
- **E15 (FL-1)**
  - `::test_observed_book_fills_at_touch_not_ltp` passes for last prices 1, 95, 110 and 1000.
  - `r1`: bid 100 / ask 102 now fills at 102.0 / 100.0 (the audit got 96 / 109).
- **E16 (FC-1)**
  - `r4`: prices 1, 2, 3 give σ = **0.816497** (expected 0.816497).
  - `::test_weighted_vwap_variance_and_session_reset` passes.
- **E17 (RP-1):** `r7`: two runs on 1,500 snapshot bars, 40 trades each, sha256 `d7cbdd45…` both times, with entries = closes = 40.
- **E18 (RP-3):** `r8`: the rebuilt audit evaluator gives exactly 377 / 21.75% / −0.619R / −233.218 / 0.332.

---

## A–J

**A. Fixed correctly**
- Forming-bar refusal in the signal path.
- Clock-binned higher-timeframe bars, visible only after they close.
- Prefix-invariant features.
- VWAP variance and session reset.
- Placeholder 0/1/2 volume disabled.
- Bid/ask touch fills.
- Final-bar settlement with a reconciled position ledger.
- Gap-through stops in the engine.
- No overnight fills.
- One position at a time.
- Chain buckets hidden until captured.
- Deterministic backtests with code, config and data hashes.
- Honest dashboard label ("Signal outcomes: hypothetical · overlapping · before fees").

**B. Partially fixed**
- **Signal clocks:** 4 of the 5 times, stored in JSON rather than columns.
- **Gap stops:** fixed in the engine, not in the evaluator.
- **Cost legs:** correct in the option paths, still swapped in the evaluator and index engine.
- **Volume:** correct for 0/1/2, wrong for constant values.
- **Provenance:** present on backtests, absent on signals and decisions.
- **Forming-bar protection:** the signal protects itself; the plan doesn't.

**C. Still failing**
- TC-1, TC-2, TC-5, TC-6
- SE-2
- OC-1, OC-4, OC-5
- PL-3, PL-4
- FL-2, FL-4, FL-5
- CA-1, CA-2
- FC-2, FC-3
- RP-2, RP-4
- All seven out-of-sample items

**D. Unverified:** TC-3, CC-3, HTF-4, CA-3.

**E. Blocked by data:** OC-2, OC-3, FL-3. Both the snapshot and the live database hold 120,838 option rows with **0 bid/ask pairs** and no exchange time. Realistic historical option execution is **not certified**.

**F. New bugs introduced by the repair**
1. **The look-ahead detector is blinded.** `feed.verify_causality` now checks a hardcoded list of 9 columns instead of every enriched column, so a leak in any new feature goes undetected. `test_lookahead.py::test_the_causality_check_actually_catches_a_leak` fails.
2. **Constant volume is treated as real.** The placeholder rule changed from "more than one distinct value" to "value > 2". Any constant volume above 2 now counts as traded volume, and the regime engine votes on it (`test_regime_engine.py::test_synthetic_volume_is_reported_as_unavailable_not_as_neutral` fails).
3. **The suite was left red** (23 failures), and 20 stale tests weren't updated.
4. **Asymmetric forming-bar protection.** The signal refuses forming bars; `plan.build` silently uses them (latent, 17 of 25).
5. **Minor issues:**
   - The time exit fires after 25 bars, not the configured 24.
   - `build_analysis` repeats its decision-time and forming-bar filter block.
   - The default engine config (₹100k, 75-unit lot) refuses every signal on real data (0 trades), so a default run proves nothing.

**G. Now trustworthy on the dashboard**
- The Signal outcomes strip, **only as what its label says**: a deterministic, hypothetical, overlapping signal study (372 outcomes, −0.618R).
- The v2 panel's gate states, refusal counts and zero-fill record.
- VWAP and relative volume showing "unavailable" on the placeholder feed, instead of made-up values.

**H. Still not trustworthy**
- Win rate, average win/loss, Sum R and Avg R as trading performance.
- The per-signal win/loss tags.
- Any net or rupee figure from the evaluator.
- Breakdowns by regime, hour, weekday or VIX.
- Confidence as a measure of quality.
- PCR and chain readings wherever OI is missing.
- Row-count coverage monitors.
- Every option backtest result.

**I. Can the execution ledger be used for research?** Not yet. The index-engine `PositionLedger` is sound as infrastructure: valid state machine, reconciled, deterministic, no orphan or duplicate closes found. But its trades still carry swapped cost legs (CA-2), silently shifted levels (SE-2), no latency (FL-4) and a per-trade equity curve (OS-7). For options it cannot produce observed-execution evidence at all (E).

**J. Ready for strategy research?** No.

## READY FOR STRATEGY RESEARCH: **NO**

Blockers:
1. **Green suite.** Resolve all 23 failures: update the 20 stale tests to the new, stricter rules, and fix the 2 real regressions.
2. **Restore the look-ahead detector** to check every enriched column, and keep its planted-leak test green (F-1).
3. **Persist the five clocks** plus strategy version, parameter hash and input fingerprint as columns on every signal and decision (TC-2, RP-2). Commit the repair (RP-4).
4. **Clock-grid validation and quarantine** (TC-1), a **declared warmup policy** (TC-5), and **as-known bar revisions** (TC-6).
5. **One execution truth in both engine and evaluator:**
   - a declared rule for gapped entries (SE-2)
   - gapped stop exits (PL-3)
   - a count of bars hitting both stop and target (PL-4)
   - latency (FL-4)
   - a slippage sensitivity grid (FL-5)
6. **Costs:** correct legs everywhere (CA-2); no option rates on index levels, plus a dated fee schedule checked against one contract note (CA-1).
7. **Volume source decided by provenance, not by size** (FC-2, FC-3).
8. **Option data rules:** dated expiries and lot sizes (OC-4); missing OI treated as unavailable (OC-5); exchange time stored (OC-1); observed-only as the default (FL-2).
9. **Out-of-sample infrastructure** (OS-1 to OS-7): record of data already seen, locked holdout, session-block folds with gaps between them, trial log, pre-registered baselines, minimum sample size, daily marked-to-market ledger.
10. **Forward collection** of timestamped bid/ask/depth with contract IDs. Without it, OC-2, OC-3 and FL-3 stay blocked, and no option result can be called observed.
