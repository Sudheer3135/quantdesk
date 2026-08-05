# Running QuantDesk without paying anything

Zerodha's Kite Connect is around ₹2,000 a month. It is also the outlier —
most Indian brokers give their API away free. And for *data alone* you do
not need a broker account at all.

There are two free routes. Take the first one today and the second one when
you need more.

---

## Route 1 — no account, no rupees, works right now

This is what `BROKER=free` uses. It is already built.

| What | Where it comes from | Cost |
|---|---|---|
| NIFTY 5-minute candles | Yahoo Finance (`^NSEI`) via `yfinance` | ₹0 |
| Live option chain — OI, change in OI, volume, IV, LTP | NSE public endpoint | ₹0 |
| India VIX | NSE `/api/allIndices` | ₹0 |
| Spot index value | NSE, falling back to Yahoo | ₹0 |

Turn it on:

```bash
# in .env
BROKER=free
```

```bash
docker compose restart backend
curl -s "http://localhost:8000/signals/live" | python3 -c "
import json,sys; print(json.load(sys.stdin)['explanation'])"
```

### What you give up

**Intraday history stops at about 60 days.** Yahoo caps it. This is the real
limitation, and Route 3 below is how you beat it.

**NSE's endpoints are undocumented.** They are what nseindia.com's own
front-end calls. They can change without warning, and they defend
themselves: you need a session cookie and browser-like headers, and if you
hammer them you get blocked. The client in `brokers/nse.py` handles the
cookie warm-up, waits 1.5 seconds between calls, and retries with a fresh
cookie on failure. Do not lower those numbers.

**Poll, not stream.** No websocket. The agent polls every 5 minutes, which
is exactly right for a 5-minute strategy and useless for scalping.

**No order placement.** `FreeDataBroker.place_order` raises on purpose.

**Personal use only.** Do not redistribute NSE data or build a product on
top of these endpoints.

---

## Route 2 — a free broker account (₹0 API fee)

When you want deeper history, a websocket feed, or the ability to actually
place orders, open a free API account. Several brokers charge nothing for
the API itself:

| Broker | API cost | Notes |
|---|---|---|
| **Fyers** | Free | Free historical data, quotes and market data alongside the trading API |
| **Angel One SmartAPI** | Free | Free historical data API, SDKs in Python, Java, NodeJS and more |
| **Upstox** | Free | Free trading and market data APIs, low latency |
| **Dhan** | Free to trade | Order APIs free, but real-time and historical *data* is a paid add-on |
| **Shoonya (Finvasia)** | Free | Zero brokerage positioning, free API |
| **Kotak Neo** | Free | No subscription fee for the Neo Trade API |
| Zerodha Kite | ~₹2,000/month | The one that charges |

For your purpose — free intraday history and a data feed — **Fyers or Angel
One SmartAPI** are the two to look at first, because both explicitly include
free historical data rather than charging for it separately.

Two things to check before opening any account, because they change and
because they are not the API fee:

1. **Account opening and annual maintenance charges.** These are separate
   from the API and vary by broker and by offer.
2. **Brokerage per executed order**, if you ever trade. Typically ₹20 per
   executed order in F&O.

Verify both on the broker's own pricing page rather than a comparison
article. Neither affects you while you are only reading data.

### Writing the adapter

Every broker in this codebase implements the same six methods
(`backend/app/brokers/base.py`). Copy `freedata.py`, swap the data calls,
register it in `deps.py`. It is roughly an afternoon.

---

## Route 3 — build your own history (do this on day one)

This is the part most people miss, and it is the answer to the 60-day cap.

**Every candle the platform fetches is written to Postgres.** After three
months you own three months of clean 5-minute data. After a year, a year.
No provider can revoke it, rate-limit it, or start charging for it.

Seed the archive with the deepest window the free source allows:

```bash
curl -s -X POST "http://localhost:8000/market/archive/backfill?symbol=NIFTY&timeframe=5m&days=59" \
  | python3 -m json.tool
```

Then leave the agent running during market hours. It tops the archive up
every five minutes, before it does anything else — because on a free source,
history you did not save is history you lose.

Check what you own:

```bash
curl -s "http://localhost:8000/market/archive" | python3 -m json.tool
```

```json
{ "symbol": "NIFTY", "timeframe": "5m", "candles": 4425,
  "sessions": 59, "span_days": 59 }
```

Writes are idempotent — a unique constraint on symbol, timeframe and
timestamp means overlapping fetches update rows rather than duplicating
them. Re-run the backfill as often as you like.

**Start this now, even though the platform isn't finished.** The archive is
the only part of the project that gets more valuable purely from time
passing, and it is the only part you cannot catch up on later.

---

## Free daily data, going back years

For daily-timeframe work you can get years of history free from NSE's own
bhavcopy files — the official end-of-day dumps, including F&O with open
interest. `jugaad-data` and `nsepython` both wrap these.

Useful for: swing strategies, sector studies, long-horizon backtests.
Not useful for: your 5-minute NIFTY strategy, which needs intraday bars.

---

## Recommended path

1. **Today** — set `BROKER=free`, run the backfill, leave the agent running.
   Your archive starts filling. Cost: ₹0.
2. **This month** — open a free API account (Fyers or Angel One) and write
   the adapter. Deeper history, websocket feed, a real order path when you
   eventually want one. Cost: ₹0 for the API.
3. **Only if you outgrow both** — pay for Kite. You will know when. Most
   people never get there, and paying earlier does not make the strategy
   better.

The thing that actually limits you right now is not data quality. It is that
you have not yet forward-tested anything. Free data is more than good enough
to find that out.
