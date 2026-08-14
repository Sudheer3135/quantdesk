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

## Roadmap

See [docs/ROADMAP.md](docs/ROADMAP.md) for the remaining phases: the option
buying strategy backtest, the AI chat assistant, the morning report, the
Bloomberg-style terminal, and the trade review scorer.
