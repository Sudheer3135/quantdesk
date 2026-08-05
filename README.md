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
   (or mock)        │  one interface   │
                    └────────┬─────────┘
                             │  candles · option chain · VIX
                             ▼
            ┌────────────────────────────────────┐
            │             analytics              │
            │  indicators  structure  smc        │
            │  options     signal_engine         │
            └────────────────┬───────────────────┘
                             │  Signal + checks + reasons
                             ▼
            ┌────────────────────────────────────┐
            │        risk manager (veto)         │
            │  sizing · trade cap · kill switch  │
            └────────────────┬───────────────────┘
                             │
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
   PostgreSQL            Redis              FastAPI  ──▶  React dashboard
   signals, journal      cache, pubsub      /docs
                             ▲
                    APScheduler agent
                    every 5 minutes
```

Data flows one way. The analytics layer knows nothing about brokers, the risk
layer knows nothing about HTTP, and the API knows nothing about pandas. That
separation is what lets you swap Zerodha for another broker by writing one
file.

---

## What each analytics module does

| Module | What it computes |
|---|---|
| `indicators.py` | EMA 20/50/100/200, Wilder ATR(14), session-anchored VWAP with volume-weighted bands, relative volume |
| `structure.py` | Non-repainting fractal swings, trend state, BOS and CHoCH |
| `smc.py` | Fair value gaps (with fill tracking), order blocks, liquidity pools at equal highs and lows, stop-hunt sweeps |
| `options.py` | PCR by OI and volume, max pain, OI-based support and resistance, fresh call/put writing and unwinding, IV skew |
| `signal_engine.py` | Weighted confluence of all of the above into one decision |

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
   `BROKER=free` for real NIFTY data at no cost.
2. **A backtest is not a promise.** This one enters at the next bar's open,
   charges costs and slippage, and assumes the stop fills first when a candle
   touches both stop and target. It is deliberately pessimistic and still
   optimistic compared to real fills.
3. **The weights are a starting guess.** They were not fitted to anything.
   Fit them on your own data, and be suspicious when you do — it is very easy
   to fit noise.
4. **Kite does not publish IV.** The `iv` fields from the live adapter are
   zero until you compute them yourself.
5. **Option chain analysis reads positioning, not direction.** PCR and max
   pain tell you where the crowd sits. Crowds are sometimes right.
6. **This is not financial advice.** It is an instrument that shows its
   working. What you do with the reading is your decision and your risk.

---

## Roadmap

See [docs/ROADMAP.md](docs/ROADMAP.md) for the remaining phases: the option
buying strategy backtest, the AI chat assistant, the morning report, the
Bloomberg-style terminal, and the trade review scorer.
