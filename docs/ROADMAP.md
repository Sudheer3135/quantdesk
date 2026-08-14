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

## Phase 2 — real data ✅ built

Everything downstream is worthless on mock candles.

What exists now:

- **A canonical schema** — `candles` carries provenance (`source`,
  `session_date`, `ingested_at`, `volume_is_synthetic`, `revision`), and
  `option_contracts` / `option_candles` / `dataset_versions` are new. Applied
  by Alembic, which upgrades an existing archive in place rather than asking
  you to throw it away.
- **An idempotent importer** (`app/data/importer.py`) that reports what it
  rejected and why, instead of dropping rows into a log nobody reads.
- **A validation gate** (`app/data/validation.py`) covering future-dating,
  weekends, exchange holidays, unclosed bars and structurally impossible
  candles — reusing the existing guards rather than reimplementing them.
- **Data-quality diagnostics** at `GET /data/quality`: missing and short
  sessions, duplicates, impossible prices, bad ticks, synthetic volume,
  source mix, strike-ladder holes, absurd IV, OI discontinuities.
- **A database-first backtester.** `/backtest/*` reads stored history and
  returns **409 with a coverage report** when the archive cannot serve the
  window, rather than quietly backtesting a shorter one.
- **Structural look-ahead prevention** (`app/backtest/feed.py`). The engines
  no longer hold the frame; they hold a feed that will not return a bar the
  walk has not reached, and hands back the next bar's *open alone*. Proven
  by a future-poisoning test rather than asserted in a comment.
- **Itemised F&O costs** (`app/backtest/costs.py`) — brokerage, STT,
  exchange, SEBI, IPFT, stamp duty, GST, and tick- or spread-based slippage.
- **A dataset hash on every result**, so two backtests can be compared and
  you can tell a strategy change from a data change.

**What is still not solved, and cannot be by writing code:** there is no
free source of historical NIFTY *option* data. NSE publishes a snapshot, not
a tape. The option tables fill forward from the day the agent starts and no
earlier, which is why Phase 3's numbers stay partly modelled for months.

**Definition of done:** met — `POST /backtest/run` computes statistics from
stored candles and names the exact rows it used.

Still worth doing when you have a broker with deeper history: pull two years
of 5-minute data through the same importer. Nothing about the pipeline
changes; it is one `POST /data/import/index` with a bigger `days`.

---

## Phase 3 — the option buying strategy (your item 4)

Partly built ahead of schedule: Black-Scholes pricing, greeks, per-bar decay
and the option backtester all exist, and now charge itemised F&O costs.

What is left, and what blocks it:

- **Real premiums.** The engine prices every trade with Black-Scholes at a
  constant IV, because there is nothing else to price it with yet. Each
  trade is tagged `premium_source: "modelled"`. Once `option_candles` has
  accumulated, switch to observed premiums where they exist and report what
  fraction of trades used real data. **This is gated on calendar time, not
  on effort** — it needs months of snapshots, and no amount of work brings
  that forward.
- **Encode the rulebook as a named strategy**: max 2 trades, 1% risk, 1:2
  RR, VWAP + chain + volume + structure confirmation, all four required.
- **Backtest across at least 200 trading days.** Currently impossible on
  free data, which caps intraday history near 60 days. The archive is the
  answer and it fills at one day per day.

**Watch for:** a strategy needing four simultaneous confirmations may take
almost no trades. Low trade count means the statistics are not trustworthy,
not that the strategy is selective. Track both.

**Also watch for:** the volume confirmation is inactive on free data, since
Yahoo publishes no volume for `^NSEI`. A rulebook requiring four
confirmations where one can never fire is a rulebook requiring three.
`GET /data/quality` reports this; do not design around it without checking.

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
