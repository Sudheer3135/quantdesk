"""One place a price becomes a published payload.

There are two sources of a live NIFTY price now — an Angel One websocket
that pushes, and the free-data poller that asks — and they must produce
byte-identical payload *shapes*. If they do not, the dashboard reads one
shape correctly and the other approximately, and the day the feed falls back
is the day the desk quietly starts displaying something slightly different
without saying so.

So neither source builds its own payload. Both call `publish_price`, which
owns the shape, the previous-price memory, and the clocks.

**Four timestamps, and they answer different questions.** Conflating any two
of them is how a stale price looks current:

    source_time   when the exchange printed this price. The only basis on
                  which staleness can honestly be judged — but see the
                  resolution caveat below before deriving anything
                  sub-second from it.
    received_at   when this process first held it. On a push feed that is
                  the socket callback; on a poll it is the HTTP response.
                  Stamped by us, so it is the only one of the four with
                  reliably fine resolution.
    at            when we published it to Redis.
    age_seconds   at - source_time. What the desk is actually looking at.

**A timestamp cannot be read more finely than it was written.** Angel
quantises `exchange_timestamp` to a whole second, so every quantity derived
from `source_time` inherits up to 1000ms of rounding that sweeps uniformly
rather than settling. At the 15s and 60s thresholds `classify_age` uses,
that is noise and can be ignored. At the one-second resolution a dashboard
renders an age in, it is the entire signal: on 15-Sep-2026 the freshness
pill flickered "just now" / "1s ago" / "just now" on consecutive seconds
while the feed sat provably steady at 97ms between ticks with no
reconnects and no fallbacks.

The rule that follows, and the reason `source_time_quantum_ms` is
published alongside the latency: judge *staleness* from `source_time`, at
coarse thresholds, where rounding does not reach. Measure *how long since
data last arrived* from `received_at`, which we stamp ourselves. Never
render a sub-second age from `source_time`.

The two differences between them are worth naming separately, because they
fail for different reasons and have different fixes:

    feed_latency_ms     received_at - source_time. The exchange, the vendor
                        and the network. Nothing here can improve it — and
                        on a single tick it is dominated by the rounding
                        described above, so read it with
                        `source_time_quantum_ms` beside it and prefer the
                        rolling minimum in `AngelFeed.status()` when the
                        question is how fast the feed actually is.
    publish_latency_ms  at - received_at. Ours. Parsing, throttling, and
                        the hop to Redis. If this is large, we are the
                        problem.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from ..cache import publish
from ..market_hours import is_open as market_is_open

log = logging.getLogger(__name__)

CHANNEL = "prices"
CACHE_KEY = "price:latest"

# How far behind the market a price may be before the desk should say so.
#
# Both numbers are set against the measured behaviour of the free sources:
# Yahoo's quote is typically 2-6s behind the print and occasionally 11s, so
# anything inside 15s is ordinary and calling it "delayed" would train the
# eye to ignore the warning. Past 60s the source has stopped refreshing —
# that is a real fault, not jitter.
#
# A push feed is far tighter than this, and that is fine: these are the
# thresholds at which a price stops being usable, not a target.
LIVE_SECONDS = 15
DELAYED_SECONDS = 60


def classify_age(age_seconds: float | None) -> str:
    """Name how far behind the market a price is.

    Kept as a plain function of one number so it can be tested without a
    broker, a socket or a clock, and so the dashboard and the API cannot
    drift into disagreeing about what "stale" means.

    `None` means the source would not say when the price was printed. That
    is reported as "unknown" rather than "live": a price we cannot date is
    precisely the one that should not be presented as current.
    """
    if age_seconds is None:
        return "unknown"
    if age_seconds <= LIVE_SECONDS:
        return "live"
    if age_seconds <= DELAYED_SECONDS:
        return "delayed"
    return "stale"


# Remembered between publishes so the dashboard can show direction and
# change without a second request for the previous value. Keyed by symbol
# and shared across sources on purpose: a failover from Angel to Yahoo must
# not reset the change column to zero, because that would read as the market
# having returned to its open.
_previous: dict[str, float] = {}


def reset_previous() -> None:
    """Forget the remembered prices. For tests."""
    _previous.clear()


def _millis(later: datetime, earlier: datetime | None) -> float | None:
    """Elapsed milliseconds, floored at zero.

    A latency cannot be negative, but a measured one can be: Angel stamps
    `exchange_timestamp` to the second, so a tick that leaves the exchange
    at 10:05:48.994 carries 10:05:49 and appears to arrive six milliseconds
    before it was sent. Reporting -6.34 ms on the dashboard reads as a
    broken clock rather than as the rounding it is, and it drags the rolling
    p50 below anything real. Zero is the honest floor: the transit was too
    short to measure at the source's resolution.
    """
    if earlier is None:
        return None
    return round(max(0.0, (later - earlier).total_seconds()) * 1000, 2)


def source_quantum_ms(printed: datetime | None) -> float | None:
    """How coarse the source's own clock is, in milliseconds.

    A latency is only as precise as the timestamp it is measured from, and
    Angel's is a whole second: across 167 consecutive index ticks sampled
    on 15-Sep-2026 not one `exchange_timestamp` carried a sub-second digit,
    and the resulting `feed_latency_ms` was spread dead flat across every
    100ms bucket from 0 to 1000 — 12, 12, 18, 16, 14, 14, 20, 11, 15, 23,
    12. Real transit time does not look like that; it clusters with a tail.
    A flat band exactly one second wide is the shape of a rounded clock,
    not of a network.

    So `feed_latency_ms` on any single tick is the true transit plus up to
    a second of rounding, and on that sample the rounding was most of it:
    p50 562ms against a floor of 56ms, on a feed whose ticks were 97ms
    apart with no reconnects. Publishing 562ms to two decimal places
    invites someone to act on a number that is mostly an artefact.

    This reports the width of that uncertainty rather than hiding it.
    Detected per tick instead of hard-coded, so it costs nothing if Angel
    starts sending milliseconds and it stays correct for the polled
    sources, whose timestamps are their own business.

    A genuine millisecond stamp lands exactly on a second about once in a
    thousand ticks and is briefly reported as coarse. That is the harmless
    direction: it overstates the uncertainty of one reading and never
    understates it.
    """
    if printed is None:
        return None
    return 1000.0 if printed.microsecond == 0 else 0.0


def build_payload(
    symbol: str,
    price: float,
    *,
    source: str,
    source_time: str | None,
    received_at: datetime | None = None,
    published_at: datetime | None = None,
    transport: str = "poll",
    previous: float | None = None,
) -> dict:
    """The published shape, as a pure function.

    Separated from the publish so the payload can be asserted on without a
    Redis, and so the two sources demonstrably build the same thing.
    """
    published = published_at or datetime.now(UTC)

    age = None
    printed: datetime | None = None
    if source_time:
        try:
            printed = datetime.fromisoformat(source_time)
            age = round((published - printed).total_seconds(), 3)
        except (TypeError, ValueError):
            log.debug("unparseable source_time %r", source_time)

    return {
        "symbol": symbol,
        "price": price,
        "previous": previous,
        "change": None if previous is None else round(price - previous, 2),
        "direction": "flat" if previous is None or price == previous
        else "up" if price > previous else "down",
        "source": source,
        # How the price reached us, which is not the same as who sent it.
        # "stream" is a pushed tick; "poll" is a request we made. A desk
        # reading a 4-second age wants to know which of those it is looking
        # at before deciding whether the number is worrying.
        "transport": transport,
        "source_time": source_time,
        "received_at": received_at.isoformat() if received_at else None,
        "at": published.isoformat(),
        "age_seconds": age,
        "freshness": classify_age(age),
        "feed_latency_ms": _millis(received_at, printed) if received_at else None,
        # The uncertainty attached to the number above, because the number
        # above is not measurable more finely than this. 1000.0 means the
        # true feed latency is somewhere in a one-second window and this
        # tick cannot say where; 0.0 means the source dated itself finely
        # enough to be believed. Anything rendering a sub-second age must
        # read `received_at`, which we stamp ourselves, not `source_time`.
        "source_time_quantum_ms": source_quantum_ms(printed),
        "publish_latency_ms": _millis(published, received_at),
        "market_open": market_is_open(),
    }


def publish_price(
    symbol: str,
    price: float,
    *,
    source: str,
    source_time: str | None,
    received_at: datetime | None = None,
    published_at: datetime | None = None,
    transport: str = "poll",
) -> dict:
    """Remember this price, then tell everyone. Returns what was published.

    Never raises. A Redis outage costs this price and nothing more — the
    caller is either a scheduled job or a socket callback, and neither may
    die because the cache blinked.
    """
    previous = _previous.get(symbol)
    _previous[symbol] = price

    payload = build_payload(
        symbol, price, source=source, source_time=source_time,
        received_at=received_at, published_at=published_at,
        transport=transport, previous=previous)

    publish(CHANNEL, json.dumps(payload), cache_key=CACHE_KEY, ttl=120)
    return payload
