# OneDesk / QuantDesk — initial quantitative audit

Audit date: 22 September 2026. Scope: phases 1–3 of the supplied progressive workflow, plus the evidence requirements and experiment protocol for later phases. This is an audit of the current working tree, including existing uncommitted modifications. No production strategy, risk rule, database record, or UI was changed during this audit. The repository calls the application QuantDesk; the request calls it OneDesk.

## Decision

**Do not optimize the displayed win rate or promote a replacement strategy yet.** The headline is a study of overlapping hypothetical signals, not the performance of an executable options portfolio. That study has negative expectancy under its own assumptions, but timestamp, data-quality and simulation defects prevent attributing the reported loss entirely to the strategy. No profitable or validated improved strategy is established.

A consistent read-only copy of the native PostgreSQL database was captured at the time recorded in `manifest.json`. All numbers below use that frozen copy. `snapshot.sqlite` is deliberately git-ignored; preserve it locally to reproduce this audit. Its SHA-256 is recorded in the manifest and metrics. No broker credentials are included in these artifacts.

## 1. What data actually exists

| Evidence | Available |
|---|---:|
| NIFTY 5-minute candles | 6,750 over 90 observed sessions, 15 May–22 September 2026 |
| Candle sources | 6,429 `free`; 321 `angel_hist` |
| Flagged synthetic-volume candles | 6,152 |
| Actual volume values | 6,428 × 1; 321 × 0; 1 × 2 |
| Option contracts | 1,038 |
| Option records | 120,838, all `snapshot`, across only 17 sessions |
| Option bid/ask pairs | 0 |
| Single-sample option buckets | 8,762 |
| Stored signals | 1,276 |
| Manual journal | One open BUY, 75 units, no realized P&L |
| v2 paper positions | 0 |
| v2 paper decisions | 231 |
| Daily VIX records | 326 |

Option sessions span 17 August–22 September but are discontinuous. Row counts are not independent observations: many contracts belong to one snapshot. There are no duplicate index keys or option contract/timeframe/time keys in the copy, and no impossible OHLC relationships in the index rows. This does not establish that the inputs were final, contemporaneous, complete, or economically valid.

Twelve index rows are stamped 15:30 IST even though timestamps denote bucket starts. Several September sessions have 73 rows instead of the usual 75. These need clock-grid validation; a count alone can hide a missing bucket behind an extra closing print. Observed sessions are not a verified exchange calendar coverage claim.

## 2. Reconstruction: there is more than one strategy path

### Directional signal engine

`backend/app/analytics/signal_engine.py` generates BUY/SELL/HOLD from a weighted directional score. Weights: structure 0.22, VWAP 0.16, EMA trend 0.14, liquidity 0.14, FVG 0.10, option chain 0.16, volume 0.08. Disabled checks are removed from the denominator. Confidence is the absolute normalized score, clipped to one; it is **not a fitted win probability**. A score below 0.35 yields HOLD; supplied India VIX above 20 raises this threshold to 0.45. Otherwise the sign selects BUY or SELL.

Indicators: EMA 20/50/100/200 use `ewm(span, adjust=False)`; ATR14 uses true range and `ewm(alpha=1/14, adjust=False)`; VWAP is the session cumulative typical-price/volume ratio; bands use the implementation discussed below; relative volume divides by a rolling 20-bar mean with a minimum of 10 bars. EMA/ATR initialization is first-observation based, not an initial SMA seed. Sixty-bar backtest warmup does not provide 200 observations for EMA200.

Structure uses fractal swings with three bars on each side and waits for right-side confirmation. Other inputs include structure breaks/changes, unfilled FVGs, liquidity pools and sweeps. Order blocks are calculated and exposed as context, but are not a separate weighted vote. The signal proposes entry at the last close, risk distance `max(1.2 × ATR14, 0.0008 × close)`, stop in the adverse direction and target at twice that distance. No trailing stop, partial exit, cooldown, or fitted ML model was found in this path. Minimum input length is 30 bars. The engine itself does not enforce the paper strategy's entry window or daily caps.

Option summary: PCR is total put OI / total call OI; max pain minimizes intrinsic payout across listed strikes; support/resistance are top OI strikes on their respective side of spot. Bias votes include PCR ≥1.2 or ≤0.7, presence of positive put/call OI changes, and spot relative to max pain. A score ≥2 is bullish and ≤−2 bearish. “Writing” is inferred from OI increase, not established seller initiation. Every open contract has both a buyer and seller; this interpretation is a hypothesis to test. Missing OI denominators can become a PCR of zero in `summarise`, inviting a bearish reading of unavailable information.

### Higher-timeframe plan

`analytics/plan.py` is an additional gate, not a replacement signal engine. It averages available 15-minute/hourly structure and trend readings, hourly VWAP, and option bias. Direction uses ±0.25. Neutral bias, volatile chop, unavailable ATR, or a day trend against bias prevents entry. A trend regime seeks the nearest VWAP/EMA20 anchor within 0.75 signed ATR in the bias direction; beyond 2 ATR it explicitly waits for a deeper pullback. The signed-distance rule is not a symmetric “within 0.75 ATR” rule: sufficiently negative displacement passes it. Range/squeeze uses a close at least 0.25 ATR beyond the last confirmed swing. Entry states are ENTER_NOW, WAIT_PULLBACK, WAIT_BREAKOUT, NO_ENTRY. These are hand-built rules, not trained model outputs.

Regimes are rule-based scores of efficiency, displacement, ATR expansion/compression, VWAP behavior and session structure, with TREND_UP, TREND_DOWN, RANGE, SQUEEZE and VOLATILE_CHOP among the labels. No fitted classifier or training/test pipeline was found. Comments in the plan describe earlier experiments on stored signals, so those same historical periods cannot now be declared untouched final tests.

### Risk manager

`risk/manager.py`: nominal risk 1% of current capital, maximum two entries/day, minimum reward:risk 2, realized daily loss limit 3%, stop after two consecutive losses, one open position, 20% premium-deployment cap when a unit premium is supplied, and kill switch. Lots are floored to a configurable lot size (default 75); quantities can be reduced by the deployment cap. There is no martingale. An absolute-distance RR check alone does not establish that stop and target are on correct sides of entry; v2 separately validates level direction. Correct lot size must come from effective-dated contract metadata; this audit has not verified statutory or contract defaults against current exchange records.

### Stored-signal study — the dashboard headline

`frontend/src/App.jsx` fetches `/signals/outcomes`; `panels.jsx` labels resolved outcomes “Trades.” `evaluation/outcomes.py` evaluates every actionable in-session stored signal independently, including risk-blocked signals. It does not enforce one position, two entries/day, bias agreement, or v2 contract selection. It infers a signal bar from the database write time, enters at the following stored open, shifts stop/target with the fill, then checks stop first, target, 24-bar time cap and 15:15 session exit. Headline R is before fees **but after the assumed index slippage**. Its rupee net calculation applies option-turnover fees to index levels; the module itself documents this mismatch.

### Index and legacy modelled-option backtests

`backtest/engine.py` defaults to ₹100,000, 60 warmup bars, a 300-bar analysis window, two daily entries via the risk manager, 0.02% index slippage per side and ₹120 flat round-trip costs. It generates signals from price history without the live chain/VIX arguments, so it is not a replay of the complete live rule. Stops and targets move with next-open fill displacement. Stop wins ambiguous bars; time/session exits apply. The index is a directional diagnostic, not a directly purchased option instrument.

`backtest/option_engine.py` is a separate legacy option simulation with modelled premiums; API defaults include IV 13%, ATM offset zero and parameterized Tuesday expiry. A constant-IV model cannot establish historical execution profitability. Historical expiry weekday changes and holidays require actual effective-dated expiries, not a universal weekday assumption.

### Option-buying backtest and v2 paper strategy

`optionbuy/strategy.py` defaults to ₹200,000, 75-unit lots, ENTER_NOW plus bias agreement, 24-bar hold cap, 15:15 exit and no overnight holds. BUY selects CE; SELL selects PE. Contract policies include ATM, offset and target delta, nearest or next expiry, default 1–10 calendar days to expiry. Default OI/volume floors are zero; spread ceiling is 25% where depth exists; premium floor ₹5. Chain-store freshness defaults to 10 minutes. Premium provenance is OBSERVED/SNAPSHOT_DERIVED/MODELLED/MIXED. Default `prefer_observed` allows model fallback and requires 0% observed fills; that is not an observed-only performance claim. Stops used for sizing are counterfactual Black–Scholes projections, even with archived entry premiums. No historical v2 parity should be inferred from this module.

`strategy_v2/config.py` and `rules.py`: simulated ₹350,000 account; 09:30–14:30 IST entries; 15:15 exit or 120-minute cap; no new entry on a listed expiry day; nearest eligible listed expiry at least two trading sessions away. Calls for BUY, puts for SELL. Choose absolute delta nearest 0.50 within 0.45–0.60, inferred from each quote's IV. Require a two-sided quote, ≤10-second age, ≤5% spread and ask ≥₹20. VIX needs 120 history records out of up to 252; it blocks high-percentile/spike conditions using 80th-percentile and 10% settings. Entry is ask; normal paper exit uses bid. Premium stop is the nearer of the projected index stop and 30% premium loss; target is the nearer projected target and 50% premium gain. The risk manager can reject the resulting premium RR. No trailing or partial exits were found. On missing exit depth, `paper.py` can close using the last recorded premium: labeled but not an executable liquidation price.

Paper decisions: 118 no direction, 85 entry state not ready, 14 no two-sided quote, 8 expiry day, 3 outside window, 2 unhealthy feed, 1 stale quote. **Zero fills is not a measured losing v2 strategy.**

## 3. Confirmed defects and methodological problems

| Priority | Finding and evidence | How it distorts results |
|---|---|---|
| Critical | `analytics/indicators.py:173` checks timestamp alignment, not `bar_start + duration <= decision_time`. `api/signals.py:49` consumes broker candles directly; the free broker deliberately returns aligned forming bars. `checks.py` reproduces acceptance of a current forming bar. | Live signals can use unfinished highs/lows/close; later replay uses a finalized candle that was unavailable at decision time. This is live/replay mismatch and potential look-ahead, not proof every historical signal leaked. |
| Critical | `evaluation/outcomes.py:589` searches bar **start** ≤ signal write time as though it found the last closed bar. `models.py:43` has no persisted signal source-bar timestamp. | A 10:02 signal maps to the 10:00 bar despite its 10:05 close. Entry, path and stop outcomes change. Subtracting five minutes is only a sensitivity, not a recovery of missing source timestamps. |
| High | Dashboard study selects 377 signals on 376 distinct bars; `seen_bars.add` records duplication but does not deduplicate. There are up to 13 simultaneously overlapping hypothetical outcomes. Of selected rows, 210 record risk “blocked,” 167 lack risk provenance. | “Trades” and Net R are not an executable account ledger. Dependence exaggerates sample size and apparent streak significance. |
| High | `indicators.py:75` defines real volume as >1 distinct value. Entire archive has only 0/1/2. Full-frame vs prefix causality check flags `rvol`; synthetic reproduction proves a later nonconstant value changes earlier availability. | Placeholder transitions enable a nonexistent volume feature. Full-frame consumers can leak future availability; default signal generation recomputes on prefixes, mitigating that particular leakage path but not the provenance error. |
| High | `indicators.py:64` accumulates deviations from each observation's then-current VWAP, not from the current weighted mean. On prices 1,2,3 with equal weights it reports σ=0.645497 instead of 0.816497. | Incorrect band width changes the VWAP vote between ±1 and ±0.4 and can change entry direction/threshold crossings. |
| High | `analytics/timeframes.py:63` groups by record count, not exchange-clock bins. A missing 09:20 combines 09:15,09:25,09:30 as one “15-minute” bar. | Gaps shift all subsequent higher-timeframe boundaries; incomplete history appears complete. Forming source bars also undermine completeness by count. |
| High | `backtest/costs.py:170` buys LTP+half spread and sells LTP−half spread. With bid100/ask102, LTP95 buys at96; LTP110 sells at109. | A measured spread label does not make fills executable. Entry must use contemporaneous ask and exit bid plus adverse impact, with quantity/depth limits. |
| High | `backtest/engine.py:208` reserves the final bar and never settles it; an entry opened into that bar is absent from reported trades/equity. Reproduced in `checks.py`. | End-window risk and P&L disappear. Bias can be favorable or unfavorable depending on the omitted position. Return open inventory/MTM explicitly or liquidate under a stated convention. |
| High | Index engine has no explicit last-entry/session-transition guard; stop exits use stop price even if the next available open gaps through it. | Can enter across sessions and underestimate stop losses. Shifting stops with a gapped entry also changes the proposed setup. Option-buying engine has additional cross-session/gap guards; do not conflate paths. |
| High | Snapshot importer buckets by capture time (`data/importer.py:268`); its exchange-time check rejects previous-day snapshots but does not establish per-contract same-day freshness. Archive lacks bid/ask entirely. | Same-day cached chain values can appear freshly timestamped. Latest spot paired with older premium/OI can distort IV, delta, max-pain distance and contract selection. Snapshot bucket close is not available at bucket start. |
| High | Observed option-buying entries use decision-bucket premiums while underlying entry uses next open; observed exits use trigger-bar close (`optionbuy/strategy.py:480,611`). Model fallback can price missing exits. | Decision, execution and source clocks differ; premium fill is neither a contemporaneous trigger quote nor guaranteed executable. Existing provenance labels should be retained, but labels do not repair the mismatch. |
| Medium | Net signal-study costs use min(entry,exit) as buy and max as sell (`outcomes.py:291`), and apply option rates to index levels. | Wrong turnover base and incorrect leg attribution on losses. Do not report these rupees as actual option expectancy. |
| Medium | Indicator initialization/window differs: backtest truncates to 300 bars and recomputes, live path requests five days; EMA200 starts immediately. | Identical recent bars may yield different indicator values and threshold crossings. ATR's first-TR seed also differs from SMA-seeded Wilder implementations. |
| Medium | `compute_stats` uses trade-indexed equity, only closed trades, and standard deviation of negative returns for Sortino. | Omits intratrade and idle-day risk; not a standard daily-return Sharpe/Sortino comparison. Missing downside variance becomes zero instead of an unavailable statistic. |
| Medium | Signal records lack immutable strategy/config version and input hashes. Existing code comments describe changes based on earlier data. | Stored signals mix code/data histories; rerunning today's engine cannot establish what past versions knew. No credible untouched test set has been established. |

`checks.py`, `supplement.py` and the JSON outputs distinguish executable reproductions from static inspection. The audit does not claim an exhaustive proof of absence of other bugs. Existing look-ahead guards, confirmed swings, stop-first ambiguity handling, unique keys, and explicit premium provenance are useful safeguards to retain.

## 4. Measured diagnosis, with correct units

All results in this section are **in-sample, hypothetical underlying signal outcomes**, before fees, after assumed slippage unless stated. They are not CE/PE returns and cannot be compounded into a real account curve.

| Metric | Existing signal study |
|---|---:|
| Resolved observations / distinct signal bars | 377 / 376 |
| Active signal days | 24, 5 August–22 September |
| Wins / losses | 82 / 295 |
| Win / loss rate | 21.75% / 78.25% |
| Average winner / loser | +1.414R / −1.184R |
| Realized winner-to-loser magnitude ratio | 1.195 |
| Expectancy | −0.619R |
| Sum of outcome R | −233.218R; not portfolio return |
| Profit factor in normalized R | 0.332 |
| Longest signal-order winning / losing streak | 13 / 25; overlapping, not account streaks |
| Mean / maximum signals per active day | 15.71 / 38 |
| Confidence vs R Spearman correlation | −0.057 |

At the measured payoff ratio, break-even win probability would be approximately 45.56% before fees, not the nominal 33.33% associated with exact 2R wins and 1R losses. The measured outcomes do not achieve the nominal payoff distribution.

| Diagnostic variant | N | Win rate | Mean R | R profit factor |
|---|---:|---:|---:|---:|
| Existing study | 377 | 21.75% | −0.619 | 0.332 |
| Same signals, zero assumed index slippage | 377 | 31.56% | −0.146 | 0.774 |
| Infer last closed bar by subtracting 5 minutes | 377 | 27.06% | −0.462 | 0.462 |

These are separate sensitivities, not additive effects or strategy candidates. Zero slippage changes shifted levels and which barrier resolves first, so the difference is not a simple fee subtraction. The timing variant cannot recover a missing decision-bar timestamp or prove that the live input was closed. Both variants remain negative; neither proves the repaired current strategy's true expectancy.

### Separating possible causes

- **Signal quality:** adverse under the recorded evaluator, even without assumed slippage. Confidence does not demonstrate useful ranking. Attribution to particular indicators is not established.
- **Entries:** timestamp inference, forming-bar exposure and historically lagging confluence require repair before judging a new entry filter. The code already contains a pullback gate, but the headline ignores that gate.
- **Exits:** realized payoff differs from nominal 2:1; time/session exits and slippage contribute. Gap execution, end-window omission and partial bucket information compromise evaluation.
- **Risk/overtrading:** the study counts many overlapping, risk-blocked signals. That is overcounting evidence, not proof the risk-managed system actually overtrades. v2 has no fills.
- **Option selection:** unassessable as a realized return contributor with zero v2 trades and no archived quotes. Directional losses cannot be called call/put selection losses.
- **Costs:** the 0.02% index slippage assumption materially worsens the signal study. Realistic options friction remains unmeasurable from the available archive. Current code fee defaults are dated assumptions, not verified present-day taxes.
- **Regime filtering:** subgroup evidence is sparse and dependent. A nominally favorable Tuesday or six-record VIX subgroup is not evidence for a new trading restriction.
- **Data quality/implementation:** directly demonstrated problems affect both what is observed and how it is scored; these are the first engineering priorities.

### Segmentation

| Direction | N | Win rate | Mean R |
|---|---:|---:|---:|
| BUY (underlying long direction) | 145 | 15.17% | −0.831 |
| SELL (underlying short direction) | 232 | 25.86% | −0.486 |

Stored regime labels: RANGE 140 observations/−0.493R; TREND_DOWN 28/−0.412R; VOLATILE_CHOP 22/−0.803R; SQUEEZE 15/−1.250R; TREND_UP 5/−1.197R; missing regime 167/−0.660R. These are labels recorded in the plan, not ex-post full-day classifications. They do not represent results of a strategy that actually honored the plan.

Every hour bucket has negative mean R (09:00 through 15:00). August has 264 observations at −0.720R; September 113 at −0.382R. Tuesday has 70 observations at +0.035R; all other weekday groups are negative. Do not select Tuesday after seeing that result. VIX below15 accounts for 371 outcomes at −0.634R; VIX15–20 has only six at +0.314R. No high-VIX performance conclusion is supported. Full group tables are in `audit_metrics.json` and `supplement.json`.

A seed-fixed 2,000-resample **day-block bootstrap**, preserving within-day signal clustering, gives an approximate 95% interval of [−0.824R, −0.349R] for the existing study mean. This is conditional on the flawed evaluator and observed days. It is not a validated confidence interval for an executable strategy, a prediction of future returns, or a portfolio drawdown simulation.

### Metrics deliberately not fabricated

Portfolio maximum/average drawdown, recovery factor, Sharpe, Sortino, net option P&L, after-cost return, CE/PE performance, expiry-distance performance and genuine out-of-sample results are **not established**. There is no completed paper portfolio here, and the signal study cannot supply one. One can draw a cumulative sum of overlapping Rs, but calling it portfolio equity would violate the risk rules. Expiry analysis needs the actual selected contract and effective-dated expiry calendar for every trade; a weekday proxy is insufficient.

| Evidence | Current strategy | Improved strategy | Simple baseline |
|---|---|---|---|
| Stored directional outcomes | 377, −0.619R mean under existing assumptions | Not developed or tested | Not yet tested |
| Validated executable option trades | None established | None | None |
| Before/after-cost option return | Not established | Not established | Not established |
| Untouched out-of-sample result | Not established | Not established | Not established |

This comparison is intentionally incomplete. Filling missing cells from modeled assumptions and presenting them as measured market performance would be misleading.

## 5. What to retain, remove, and repair

Retain the separation of data/analytics/risk, premium evidence labels, recorded refusals, contract-aware selection, kill switch, fixed-percentage risk, lot rounding, one-position cap, source provenance, and explicit ambiguity conventions. Keep the existing directional engine as a frozen comparator.

Remove the interpretation of confidence as win probability, placeholder volume as activity, OI increase as proven writing, the dashboard signal count as executed trades, and modelled/snapshot option P&L as observed execution evidence. No alpha component has yet earned removal on a valid ablation experiment; removing VWAP or option-chain checks solely because this flawed study lost would be an unsupported conclusion.

Repair the measurement pipeline before changing entry thresholds. Quarantine invalid session buckets; distinguish absent volume from a valid zero; preserve timestamp semantics and source-event time; carry `bar_open`, `bar_close`, `available_at`, `received_at` and `decision_at` separately. Persist signal bar, strategy version, parameter hash and input fingerprint. Historical corrections must not rewrite a past decision's information set.

## 6. Proposed research architecture and exact next experiment protocol

The existing modules are already reasonably separated. Improve their contracts instead of rewriting the whole application:

1. Data loader returns immutable, versioned input plus availability times and effective contract metadata.
2. Validator enforces exchange-clock completeness, holiday calendar, final bars, duplicates, provenance, source freshness and per-contract option coverage.
3. Causal feature layer uses one definition and warmup policy for live and replay; real volume is explicitly optional. Verify prefix invariance for indicators, regimes and signal generation.
4. Signal layer proposes direction and levels. Risk layer independently owns caps, sizing and whether trading is permitted.
5. Option selector consumes only the eligible ladder and contemporaneous quotes; fixed ATM is the baseline, delta/ITM/OTM are later controlled alternatives.
6. Event-driven simulator fills after signal availability at ask/bid plus adverse impact, checks quantity/depth, gap-through stops, ambiguous bars, time/session exits and end-window inventory. Missing quotes produce explicit unfilled/unpriced states, not assumed executions.
7. Analytics distinguishes signal observations, fills, closed trades and mark-to-market account returns. Report daily net-return risk metrics including zero-trade days.
8. Experiment configuration records strategy, fees, slippage, calendar, split dates, random seed, source hash and code version. Fees and lot sizes require effective dates and verification from supplied contract notes/official schedules before after-cost claims.

**Pre-register before running:** freeze a repaired rule-based current comparator and two simple directional baselines. The following are proposed specifications, not improved-strategy claims:

- EMA baseline: after a finalized 5-minute close, enter long on EMA20 crossing above EMA50, short on the reverse. No entry without sufficient warmup. Fill on the first executable next event; stop 1.2 ATR14 from fill, target 2R; exit at first stop/target, 120 minutes, or 15:15 IST. No trailing/partials initially.
- Opening-range baseline: define 09:15–09:30 from three complete 5-minute bars. First finalized close above its high proposes long; below its low proposes short. Enter only 09:30–14:30; use the same stop/target/time exits. Missing any opening-range bar means no trade. These thresholds are frozen comparators, not optimized parameters.
- Shared risk: at most one position, two entries/day, 1% nominal stop risk, 3% realized daily loss cap, stop after two losses, 20% deployed premium cap; floor lots, refuse if one lot exceeds budget. Include estimated costs in the budget and acknowledge gap risk. No increasing risk after losses.
- Initial instrument: separately measure underlying direction first. For the option stage, nearest eligible actual expiry at least two exchange sessions away, ATM CE for long/PE for short. Require contemporaneous two-sided depth and adequate quantity; use existing conservative v2 quote-age/spread/premium gates as a declared initial policy, not an optimized result. No quotes or stale underlying means no fill. Delta-based selection is a separate paired experiment only after this comparator works.

No candidate should be selected from these specifications until repaired development/validation tests show stable incremental value. They are exact starting experiment rules, not trading advice or deployed changes.

### Chronological validation and protection from overfitting

The present 90-session history has already been inspected and partly used in prior strategy development. Treat it as development/diagnostic data. A chronological split now can be useful for debugging, but cannot erase earlier exposure and become an untouched final test.

Record all previously used date ranges and rule changes. After freezing the corrected implementation, reserve future sessions as a new locked holdout. Within development data use expanding chronological folds, selecting parameters only on the next validation block and evaluating the subsequent block once. Purge trades/labels that overlap fold boundaries by at least the maximum holding horizon; carry past warmup bars solely as prior information. Never shuffle individual bars. Aggregate by session and effective contract, not by a random split of near-identical signals.

Market-structure research should test finalized-bar trend, momentum, ATR, opening range, previous-session levels and rejection/breakout features against fixed-horizon **future labels kept outside the features**. True volume features are currently unavailable. Test option PCR/OI concentration/IV changes only on matched source-time histories with adequate coverage. A small archive of repeated snapshot LTPs cannot resolve intrabar option momentum or execution risk.

Regimes must be computed as of the decision. Test interactions on validation data; do not declare a full day trending using its closing move and use that label at 10:00. Leave complex strong/weak trend subdivisions out until samples support them.

After selecting a candidate on validation only, perturb stops, targets, ATR lengths/multipliers, confidence cutoffs, entry times and strike offsets around the chosen settings. Prefer broad stable regions and disclose the entire grid and number of trials. Perform single-component ablations with the same execution assumptions and date windows; show both changed trade selection and paired outcome differences. Only consider ML after a simple baseline has valid net-of-cost performance and enough independent data; no ML training is justified by this audit.

For robustness, resample blocks of days/weeks and stress costs, delays, spread widening, missing quotes, gap fills and contract liquidity. Trade-order permutations alone omit clustered market conditions and path-dependent daily caps. Portfolio drawdown simulations require a valid portfolio ledger first.

## 7. Production implementation order and remaining inputs

1. Add failing regressions for the reproduced defects and correct timestamps, volume provenance, VWAP variance and clock-based aggregation.
2. Repair simulation fills/session boundaries/final inventory and build a daily marked-to-market ledger. Make live/replay decisions match on frozen inputs.
3. Re-evaluate the old strategy without optimization. Publish before/after figures attributable to each repair.
4. Run pre-registered simple baselines, then controlled entry/exit/regime ablations. Freeze any candidate before consuming new holdout data.
5. Acquire sufficient timestamped historical or forward-collected bid/ask/depth with contract IDs, expiries, lot-size effective dates, option prices/OI/IV and contemporaneous underlying. Preserve source and receipt times and collector outages. Tick or finer bars are needed to resolve stops reliably; 5-minute snapshots are not a substitute.
6. Verify date-specific costs against the intended broker's contract notes; record latency, order size, capital and execution assumptions. Obtain prior experiment logs and any independent backtest exports not present here.
7. Paper-test the identical frozen execution/risk code; monitor refused entries, stale exits, slippage, reconciliation and data quality. Promotion requires defined net expectancy/drawdown/coverage gates and an independent holdout, not a headline win-rate target.

No final improved strategy, profitability claim, full ablation, ML model, or untouched OOS result is delivered in this initial audit. Those are explicitly gated by the scientific workflow, not silently assumed complete. The immediate conclusion is a measurement-and-data repair plan supported by the actual code and frozen database, rather than an indicator rewrite.

## Verification

194 relevant existing tests passed (backtest, costs, outcomes, timeframes, option-chain store, option-buying strategy and v2 rules). The standalone synthetic reproductions also passed. Passing existing tests does not invalidate the newly exposed methodological gaps. `source_manifest.json` identifies audited source contents despite the dirty working tree.
