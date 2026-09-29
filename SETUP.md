# Setup

Written for a MacBook M2. Every command is meant to be pasted as-is.

---

## Step 0 — what you need

You already have Docker, Git and VS Code. Check they are awake:

```bash
docker --version
docker compose version
git --version
```

If `docker compose version` fails, open Docker Desktop and wait for the whale
icon to stop animating.

You do **not** need a Zerodha account to run this. The platform ships with a
mock broker that produces realistic NIFTY candles and an option chain, so
everything works offline from the first minute.

---

## Step 1 — get the code in place

```bash
mkdir -p ~/projects && cd ~/projects
# unzip the archive here, or copy the quantdesk folder in
cd quantdesk
git init && git add -A && git commit -m "QuantDesk: initial platform"
```

---

## Step 2 — create your .env

```bash
cp .env.example .env
```

Open `.env`. For the first run change nothing. Two lines matter later:

| Setting | Meaning |
|---|---|
| `BROKER=mock` | Simulated data. Leave this until everything works. |
| `LIVE_TRADING=false` | Nothing can send a real order. Leave this alone for a long time. |

---

## Step 3 — start everything

```bash
docker compose --profile migrate run --rm migrate --apply   # build the schema, once
docker compose up --build
```

Starting never changes the schema. The backend checks that the database is
at exactly the revision this code expects and refuses to start otherwise,
naming what is pending. Migrating is always the explicit command above
(`./scripts/migrate.sh --apply` on a native install). Without `--apply`
it is a dry run: the schema state, the pending revisions and every running
writer. `--apply` takes the database's exclusive schema lock, which any
running writer — the backend, a backfill, a second copy anywhere — refuses,
so stop them and take a backup first. You are looking for:

```
migrate  | INFO  [alembic.runtime.migration] Running upgrade  -> 0001, Baseline
migrate  | ... is at revision 0010, as this code expects
backend  | ... is at revision 0010, as this code expects
backend  | Uvicorn running on http://0.0.0.0:8000
frontend | Local: http://localhost:5173/
```

**Upgrading an install that already has archived candles?** Stop the
backend, back the database up, then run the migrate command. The baseline
migration skips tables that already exist and 0002 backfills the new
provenance columns from the data already in the table. Your history
survives; there is no stamping step to remember.

Now open:

- **http://localhost:5173** — the dashboard
- **http://localhost:8000/docs** — every API endpoint, with a Try it button
- **http://localhost:8000/health** — should say `"status": "ok"`

Stop it all with `Ctrl-C`. Wipe the database and start clean with
`docker compose down -v`.

---

### Running on a Mac without Docker

Docker Desktop's virtual machine alone was measured at 26–43% of a CPU with
QuantDesk's four containers doing about 2% of work inside it. On a MacBook
that is heat for nothing, so the desk also runs natively:

```bash
./scripts/start.sh     # PostgreSQL 16, Redis, API and dashboard
./scripts/status.sh    # what is up, market session, where prices come from
./scripts/stop.sh      # stops all four and confirms the ports closed
```

One-time requirements, already in place on the desk's Mac:

- `brew install postgresql@16 redis` — PostgreSQL runs on **port 5433**, so
  it never collides with a separately installed PostgreSQL on 5432
- `python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt`
  — the pinned versions, not whatever the system Python has
- `cd frontend && npm ci`

`.env` is unchanged. The scripts override `DATABASE_URL` and `REDIS_URL` to
point at the local services, and bind everything to `127.0.0.1` only. Never
run this and `docker compose up` at the same time: two backends open two
Angel sessions on one account. Logs are in `logs/native/`.

The Docker database was copied across on 14-Sep-2026 (every table verified by
row count and checksum) and the `quantdesk_pgdata` volume was left in place as
a backup.

## Step 4 — check it actually thinks

Ask the engine for a signal and read its reasoning:

```bash
curl -s "http://localhost:8000/signals/live?symbol=NIFTY&timeframe=5m" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['explanation'])"
```

You should get something like:

```
HOLD NIFTY 5m at 24639.96 — confidence 29%
  [down] structure: BOS bearish — cleared 24621.51 in the trend direction.
  [down] vwap: Holding below VWAP (24663.05), 0.09% away.
  [  up] liquidity: Untouched buyside liquidity at 24676.96 is likely to attract price.
  ...
```

Run a backtest:

```bash
curl -s -X POST http://localhost:8000/backtest/run \
  -H "Content-Type: application/json" \
  -d '{"symbol":"NIFTY","timeframe":"5m","days":30,"starting_capital":200000}' \
  | python3 -m json.tool | head -30
```

On mock data this **loses money**. That is correct and it is the point: mock
candles are a random walk with no edge, so a backtest that showed profit on
them would be broken. Read it as proof the simulator isn't flattering itself.

---

## Step 4b — switch to real data (still free)

Mock candles are a random walk. To read the actual market at no cost, with
no broker account:

```bash
# in .env
BROKER=free
```

```bash
docker compose restart backend
```

You now get NIFTY 5-minute candles from Yahoo Finance, plus the live option
chain and India VIX from NSE's public endpoints. Seed your own archive with
the deepest window available:

```bash
curl -s -X POST "http://localhost:8000/data/import/index?days=59" \
  | python3 -m json.tool
```

Then check what you own and whether it is any good:

```bash
curl -s "http://localhost:8000/data/coverage" | python3 -m json.tool
curl -s "http://localhost:8000/data/quality"  | python3 -m json.tool
```

The quality report will tell you two things on day one that are worth
reading rather than skipping: your volume is a placeholder (Yahoo publishes
none for `^NSEI`, so the volume check contributes nothing), and your option
history is empty and cannot be backfilled. Both are explained in
**[docs/FREE_DATA.md](docs/FREE_DATA.md)**.

Leave the agent running during market hours and the archive grows every five
minutes — index candles *and* an option-chain snapshot. The option side is
the one that matters most to start early, because unlike index candles there
is no source to buy the history back from later.

### Backtesting reads the database

```bash
curl -s -X POST http://localhost:8000/backtest/run \
  -H 'content-type: application/json' \
  -d '{"symbol":"NIFTY","timeframe":"5m"}' | python3 -m json.tool
```

If the archive cannot cover the window, this returns **409 with a coverage
report and the command that fixes it** — not a shorter backtest. Every
successful result carries a `dataset` block naming the exact rows it read,
so two runs can be compared and you can tell a strategy change from a data
change.

---

## Step 4c — run the doctor

This is the important one. It checks every layer I could not check myself —
your Python packages, Postgres, Redis, the candle upsert, the live NSE and
Yahoo endpoints, and a running backend — and prints one report.

```bash
python3 scripts/doctor.py --api
```

Paste the whole output back to me. One run tells me exactly what broke and
where, so a round of fixes takes one message instead of five.

Flags: `--no-network` skips NSE and Yahoo, `--no-infra` skips the database.

### Record fixtures (during market hours)

```bash
python3 scripts/record_fixtures.py
```

This saves what NSE and Yahoo actually returned into `tests/fixtures/`.
Attach those files and every parser can be tested against the real shape
instead of a guess at it. They contain public market data only — no
credentials, no account details.

---

## Step 5 — run the tests

```bash
docker compose run --rm backend pytest /srv/../tests -q
```

Or natively, which is faster while you are developing:

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd .. && pytest tests -q
```

Twenty tests. They pin down VWAP resetting per session, swings not
repainting, FVGs being real imbalances, max pain sitting inside the strike
range, and the risk manager refusing a third trade, a poor reward:risk, and
anything at all when the kill switch is on — plus the NSE option chain
parser, including the strike where only one side has traded.

---

## Step 6 — a broker account (optional, and free ones exist)

Kite is around ₹2,000/month. Fyers, Angel One SmartAPI, Upstox, Shoonya and
Kotak Neo give the API away free — see docs/FREE_DATA.md for the comparison.
You need a broker account only to *place orders*, not to read data.

If you do go with Zerodha:

1. Buy a Kite Connect app at <https://developers.kite.trade> (₹2,000/month).
   You get an **API key** and an **API secret**.
2. Set the redirect URL in the Kite app settings to `http://localhost:8000/health`.
3. Put the key and secret in `.env`, and switch the broker:

```
BROKER=kite
KITE_API_KEY=your_key
KITE_API_SECRET=your_secret
```

4. Kite access tokens **expire every morning**. Each trading day you log in
   once through the browser and paste the request token back:

```bash
# get the login URL
docker compose exec backend python -c "
from app.deps import get_broker; print(get_broker().login_url())"
```

Open that URL, log in, and the browser lands on your redirect with
`?request_token=XXXX` in the address bar. Exchange it:

```bash
docker compose exec backend python -c "
from app.deps import get_broker
print(get_broker().exchange_token('XXXX'))"
```

Paste the printed access token into `KITE_ACCESS_TOKEN` in `.env` and restart
the backend. Automating this daily login is a good early task for you.

---

## Step 7 — before you ever set LIVE_TRADING=true

Do not flip that flag until all six of these are true:

1. You have run the platform in mock mode for at least two weeks.
2. You have backtested on **real** downloaded candles, not mock ones.
3. You have forward-tested on live data with orders written to the journal
   by hand, and the live results roughly match the backtest.
4. You have logged at least 30 journal trades and know your real expectancy.
5. You have tested the kill switch and confirmed it blocks entries.
6. You have read your actual Zerodha contract note and replaced
   `cost_per_round_trip` with your real number.

Most platforms that lose money skip steps 2 through 4.

---

## Where things live

```
backend/app/analytics/    the maths — indicators, structure, SMC, options, signals
backend/app/data/         the historical layer — validation, import, read, quality
backend/app/risk/         the veto layer — sizing, trade caps, kill switch
backend/app/backtest/     bar-by-bar simulator: feed, engines, costs, diagnostics
backend/app/brokers/      mock, free and Kite adapters behind one interface
backend/app/api/          FastAPI routes
backend/app/workers/      the 5-minute agent and the price ticker
backend/alembic/          schema migrations
frontend/src/             the dashboard
tests/                    the tests that pin the maths and the data down
```

---

## When something breaks

| Symptom | Fix |
|---|---|
| `port is already allocated` | `docker compose down` then retry, or change the port in `docker-compose.yml` |
| Dashboard shows "Could not reach the backend" | Backend crashed — check `docker compose logs backend` |
| `Redis unavailable` in logs | Harmless. The API caches nothing and keeps working. |
| Kite `TokenException` | Your access token expired. Redo step 6.4. |
| Backtest is slow | Lower `days`, or raise `analysis_window` only if you need more history |
