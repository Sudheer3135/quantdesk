# Roadmap

You listed ten projects. Several of them are the same system viewed from
different angles, and one of them is not a trading project at all. Here is
the order that gets you the most working software for the least wasted work.

Phase 1 is built. The rest is sequenced so each phase can only start once its
dependency actually works.

---

## Phase 1 — the platform core ✅ built

Backend, analytics, risk, backtester, agent, dashboard, Docker, CI.
This was your items **1**, **2** and **7** (partly): the trading system, the
5-minute Nifty agent, and the skeleton the assistant will hang off.

---

## Phase 2 — real data (next, and non-negotiable)

Everything downstream is worthless on mock candles.

- Connect Kite and pull two years of NIFTY 5-minute history to Postgres.
- Add a `historical` loader so backtests read from the database, not the API.
- Re-run the existing backtest on real data and compare it to the mock run.

**Definition of done:** `POST /backtest/run` returns statistics computed from
candles that actually happened.

---

## Phase 3 — the option buying strategy (your item 4)

The engine currently signals on the *index*. Option buying is a different
instrument with different maths: theta decays against you every minute, and
a correct directional call can still lose.

- Model option premium from index moves (delta approximation is enough to
  start; add theta decay per bar).
- Encode the rulebook as a named strategy: max 2 trades, 1% risk, 1:2 RR,
  VWAP + chain + volume + structure confirmation, all four required.
- Backtest across at least 200 trading days and report the statistics the
  backtester already computes: expectancy in R, profit factor, max drawdown,
  worst losing streak.

**Watch for:** a strategy needing four simultaneous confirmations may take
almost no trades. Low trade count means the statistics are not trustworthy,
not that the strategy is selective. Track both.

---

## Phase 4 — the trade review scorer (your item 6)

The journal tables and endpoints already exist. What's missing is the review.

- `POST /journal/{id}/review` takes the trade and its originating signal and
  scores entry timing, exit timing, plan adherence, and risk taken.
- Score out of 100, weighted toward **process** rather than outcome. A losing
  trade taken exactly to plan should score higher than a profitable one taken
  on impulse. Scoring outcome teaches you to gamble.

---

## Phase 5 — the morning report (your item 9)

A scheduled job at 08:45 IST that assembles global markets, FII/DII activity,
option chain, max pain, PCR, India VIX, key levels, and the sectors moving.

Most of the analytics already exist. The work is data sourcing — FII/DII
figures come from NSE, not from Kite.

---

## Phase 6 — the AI assistant (your item 7, in full)

A chat layer over everything above. It becomes genuinely useful only once the
data underneath it is real, which is why it sits here and not first.

- Chat endpoint that can call your own APIs as tools: fetch a signal, read
  the chain, query the journal, run a backtest.
- The rule that keeps it honest: it may only state numbers it retrieved from
  your endpoints, never numbers it produced itself.

---

## Phase 7 — the terminal (your item 10)

The dashboard already shows the reasoning ledger, structure and chain. The
full terminal adds live charts, an OI profile, a heatmap and portfolio risk
metrics. Build it last, because a dashboard is a window onto data — and until
Phases 2–6 are done there is not enough behind the glass to be worth it.

---

## Running alongside: the codebase review (your item 3)

Not a phase. Do this at the end of every phase:

```bash
ruff check backend/app
pytest tests -q
docker compose run --rm backend pip-audit
```

Then read your own diff and ask what a stranger would find confusing.

---

## Separate track: the portfolio website (your item 8)

This is a placements project, not a trading project. It shares no code with
QuantDesk and should be its own repository. Next.js, Tailwind, Framer Motion,
featuring this platform, NxtBuild, the sentiment pipeline and the event
management system.

Worth doing before your placement season, not before Phase 2.

---

## And your item 5 — learning quant properly

A 90-day roadmap is a real thing to build, but it works far better as a
living document you fill in as you go than as a plan generated up front. The
fastest version: work through Phases 2 and 3 above, and every time you hit
something you don't understand — implied volatility, delta, why a backtest
overfits — stop and learn that one thing properly before continuing.

Concepts learned because a bug forced you to learn them stick. Concepts
learned from a syllabus mostly don't.
