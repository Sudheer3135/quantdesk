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
docker compose up --build
```

First build takes 3–5 minutes. You are looking for:

```
backend  | Uvicorn running on http://0.0.0.0:8000
backend  | Nifty agent scheduled every 5 minutes.
frontend | Local: http://localhost:5173/
```

Now open:

- **http://localhost:5173** — the dashboard
- **http://localhost:8000/docs** — every API endpoint, with a Try it button
- **http://localhost:8000/health** — should say `"status": "ok"`

Stop it all with `Ctrl-C`. Wipe the database and start clean with
`docker compose down -v`.

---

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
curl -s -X POST "http://localhost:8000/market/archive/backfill?days=59" \
  | python3 -m json.tool
```

Leave the agent running during market hours and that archive grows every
five minutes. Details and limits: **[docs/FREE_DATA.md](docs/FREE_DATA.md)**.

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
backend/app/risk/         the veto layer — sizing, trade caps, kill switch
backend/app/backtest/     bar-by-bar simulator with costs and slippage
backend/app/brokers/      mock and Kite adapters behind one interface
backend/app/api/          FastAPI routes
backend/app/workers/      the 5-minute agent
frontend/src/             the dashboard
tests/                    twelve tests that pin the maths down
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
