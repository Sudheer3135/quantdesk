# QuantDesk

An AI-assisted market analysis platform for NIFTY. It reads price structure,
smart money concepts, VWAP, order flow proxies and the option chain, then
produces one decision — BUY, SELL or HOLD — with a written reason for every
point of confidence it assigns.

Start with **[SETUP.md](SETUP.md)**. It runs on free data with no broker
account and no subscription — see **[docs/FREE_DATA.md](docs/FREE_DATA.md)**.

---

## The one design rule

**Nothing is a black box.** Every check the engine runs returns a score
between -1 and +1 and a plain sentence explaining that score. Confidence is
the weighted sum. If you disagree with a signal you can read exactly which
check caused it, change that check's weight in `WEIGHTS`, re-run the
backtest, and see whether you were right.

A signal you cannot explain is a signal you cannot improve, and a signal you
cannot improve will eventually cost you money.

---

## Architecture

```
                    ┌──────────────────┐
   Zerodha Kite ───▶│  Broker adapter  │◀─── Mock broker (default)
   free data   ───▶ │  one interface   │
                    └────────┬─────────┘
                             │  candles · option chain · VIX
                             ▼
            ┌────────────────────────────────────┐
            │      data — the historical layer   │
            │  validation → importer → Postgres  │
            │  repository → dataset fingerprint  │
            │  quality diagnostics               │
            └───────┬──────────────────┬─────────┘
                    │ live             │ stored history
                    ▼                  ▼
      ┌───────────────────────┐   ┌─────────────────────────┐
      │       analytics       │   │   backtest              │
      │ indicators structure  │   │  feed (no look-ahead)   │
      │ smc options           │──▶│  engine · option_engine │
      │ signal_engine         │   │  costs · diagnostics    │
      └──────────┬────────────┘   └─────────────────────────┘
                 │  Signal + checks + reasons
                 ▼
      ┌────────────────────────────────────┐
      │        risk manager (veto)         │
      │  sizing · trade cap · kill switch  │
      └────────────────┬───────────────────┘
                       │
   ┌───────────────────┼───────────────────┐
   ▼                   ▼                   ▼
PostgreSQL          Redis            FastAPI ──▶ React dashboard
candles, options    cache, pubsub    /docs
signals, journal          ▲
datasets           APScheduler agent — every 5 minutes:
                   archive candles, snapshot the chain, emit one signal
```

Data flows one way. The analytics layer knows nothing about brokers, the risk
layer knows nothing about HTTP, and the API knows nothing about pandas. That
separation is what lets you swap Zerodha for another broker by writing one
file.

**Backtests read the database, never a broker.** A backtest that refetches
from a live API is a different dataset every day and reproducible on none of
them. If the archive cannot cover the window you asked for, `/backtest/run`
returns 409 with a coverage report rather than quietly running on a shorter
one — a six-week result and a two-year result look identical in the output,
and you would act on either.

---

## What each analytics module does

| Module | What it computes |
|---|---|
| `indicators.py` | EMA 20/50/100/200, Wilder ATR(14), session-anchored VWAP with volume-weighted bands, relative volume |
| `structure.py` | Non-repainting fractal swings, trend state, BOS and CHoCH |
| `smc.py` | Fair value gaps (with fill tracking), order blocks, liquidity pools at equal highs and lows, stop-hunt sweeps |
| `options.py` | PCR by OI and volume, max pain, OI-based support and resistance, fresh call/put writing and unwinding, IV skew |
| `signal_engine.py` | Weighted confluence of all of the above into one decision |

## The historical data layer

| Module | What it does |
|---|---|
| `data/validation.py` | One gate every candle passes: future-dated, weekend, exchange holiday, unclosed, structurally impossible. Counts what it rejected instead of dropping rows silently |
| `data/importer.py` | Idempotent index and option imports. Re-running writes the same rows and changes nothing |
| `data/upsert.py` | Dialect-portable `ON CONFLICT DO UPDATE`, so the write path is testable on SQLite and correct on Postgres |
| `data/repository.py` | The only read path a backtest may use, plus honest coverage answers |
| `data/dataset.py` | A sha256 over the exact rows a run read, so results can be compared |
| `data/quality.py` | Missing sessions, duplicates, impossible prices, bad ticks, synthetic volume, strike-ladder holes, absurd IV, OI discontinuities |
| `backtest/feed.py` | Hands out bars strictly in order and refuses to return one the walk has not reached |
| `backtest/costs.py` | Itemised Indian F&O charges and slippage, rather than one flat number of the wrong shape |

### The definitions this codebase uses

Terms like BOS and CHoCH mean slightly different things depending on who
taught you. Here is what this code means, so the docs and the maths agree:

- **Swing high** — highest bar within `lookback` bars on both sides.
  Confirmed only after `lookback` bars close, so it never repaints.
- **BOS** — price closes beyond the last swing point *in the direction of the
  existing trend*. Continuation.
- **CHoCH** — price closes beyond the last swing point *against* the trend.
  First warning of a reversal.
- **Fair value gap** — a three-candle imbalance where the middle candle moved
  so fast that a price range was never traded. Marked filled once price
  returns through its midpoint.
- **Liquidity pool** — two or more swing points within a fraction of ATR of
  each other. Stops cluster there.
- **Sweep** — price wicks through a pool and closes back on the original
  side. A stop hunt.

---

## Risk rules (the defaults)

| Rule | Default |
|---|---|
| Risk per trade | 1% of capital |
| Trades per day | 2 |
| Minimum reward:risk | 1:2 |
| Daily loss limit | 3% |
| Stop after consecutive losses | 2 |
| Kill switch | available, blocks all entries |

The risk manager has veto power. The signal engine only *proposes*. Nothing
reaches a broker unless `evaluate()` returns approved.

---

## Honest limitations

Read these before trusting anything on the dashboard.

1. **The default data is fake.** The mock broker generates a random walk. It
   is for testing plumbing, never for judging a strategy. Switch to
   `BROKER=free` for real NIFTY data at no cost. If mock rows ever reach the
   archive, `GET /data/quality` reports it as an error.
2. **A backtest is not a promise.** This one enters at the next bar's open,
   charges itemised costs and slippage, and assumes the stop fills first when
   a candle touches both stop and target. It is deliberately pessimistic and
   still optimistic compared to real fills.
3. **The weights are a starting guess.** They were not fitted to anything.
   Fit them on your own data, and be suspicious when you do — it is very easy
   to fit noise.
4. **Kite does not publish IV.** The `iv` fields from the live adapter are
   zero until you compute them yourself.
5. **Option chain analysis reads positioning, not direction.** PCR and max
   pain tell you where the crowd sits. Crowds are sometimes right.
6. **There is no historical option data, and there is no way to buy your way
   out of it cheaply.** NSE publishes a live snapshot of the chain, not a
   tape. `option_candles` fills forward from the day the agent starts and
   cannot be backfilled, so option backtests price with Black-Scholes at a
   constant IV and tag every trade `premium_source: "modelled"`. Start the
   agent early; this is the one thing that only gets better with time passing.
7. **Free index data has no volume.** Yahoo publishes none for `^NSEI`, so
   the adapter substitutes a constant. The volume check therefore contributes
   nothing and the remaining weights renormalise around it — the strategy
   runs one input short, and nothing about the output looks wrong.
   `GET /data/quality` says so explicitly.
8. **Option "bars" built from snapshots are not candles.** Their high and low
   come from sampled last-traded prices, so the range understates the real
   one. They carry `bar_kind: "snapshot"` and a `samples` count for exactly
   this reason.
9. **The cost rates go stale.** Indian F&O charges change by circular. The
   defaults in `backtest/costs.py` are dated October 2024. Verify against a
   contract note before believing any rupee figure.
10. **This is not financial advice.** It is an instrument that shows its
    working. What you do with the reading is your decision and your risk.

---

## The option-buying backtest

`POST /backtest/option-buying` runs the desk's own decisions as an option
buyer. It generates nothing of its own: `signal_engine` produces the
direction and the levels, `plan` produces the bias and the entry state, and
`risk.manager` keeps its veto. This module turns that decision into a
contract, a size, a fill and a premium — and says where every premium came
from.

**Three evidence labels, never blended.**

| Label | What it means |
|---|---|
| `OBSERVED` | A real tape. The contract traded at this price and the bar's range is the true one |
| `SNAPSHOT_DERIVED` | A real *price*, sampled. The close printed; the range is folded from polls and understates |
| `MODELLED` | Nothing was observed. Black-Scholes at an assumed constant IV — a calculation, not a market |

A trade whose two legs disagree is labelled `MIXED` rather than filed under
the flattering half. `pricing_policy` decides which labels may be used at
all: `observed_only` refuses to open a trade it cannot price from the
archive, `prefer_observed` falls back to the model and labels it,
`modelled_only` never reads the archive.

**It refuses rather than inventing history.** Option data cannot be
backfilled — NSE publishes a snapshot, not a tape, so a session the
collector missed is gone permanently. `observed_only` over a window with
holes returns **409 with the failing sessions named**. A run that completes
but whose fills fall below `min_observed_pct` is also refused: coverage
measured over sessions and evidence measured over the trades actually taken
come apart, and a window can pass the first while every trade lands in the
hole. `GET /backtest/option-buying/coverage` answers the same question
without walking anything.

**Contract selection is a policy, and every refusal is a named code.** Side
follows the direction; expiry is the nearest listed one inside a tenor
window, with expiry day excluded by default; strike is at-the-money, a fixed
offset, or delta-targeted, always snapped to the ladder the archive actually
listed; open interest, volume and quoted spread are checked as of the
decision bar. Signals that never became trades are counted under codes like
`no_quote_at_decision_bar` and `expiry_too_near`, so the selection is visible
instead of being a silent `continue`.

**Both look-ahead guards are structural.** `HistoricalFeed` refuses a candle
the walk has not reached; `ChainStore` carries its own clock and refuses a
quote that had not printed. Index look-ahead is loud — an absurd equity
curve. Option look-ahead is quiet: a few rupees a fill, permanently, with
nothing in the output to flag it.

### What it cannot tell you

- The archive holds bucket-resolution premiums, not a tick tape. An observed
  entry pays the last quote at the *decision* bar while the index fills at
  the next bar's open, and an observed exit fills at the close of the bar
  that triggered rather than at the trigger level. Every trade names both
  pairings in `entry_basis` and `exit_basis`.
- Sizing is always modelled. No archive holds the premium at a level the
  index never reached, so the stop premium is projected with Black-Scholes —
  at the contract's own stored IV where there is one.
- `decay_cost` is a modelled attribution: both sides are priced at the same
  index level and the same IV, differing only in time. It says what the
  clock cost, not what the trade lost.

---

## Roadmap

See [docs/ROADMAP.md](docs/ROADMAP.md) for the remaining phases: the AI chat
assistant, the morning report, and the trade review scorer.
