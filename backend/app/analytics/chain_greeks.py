"""Greeks for a whole option chain, added where both transports meet.

The desk carries two chains — Angel's stream and NSE's poll — and
`publish_price`'s rule applies here too: they must produce the same
shape or the dashboard reads one correctly and the other approximately.
So this runs at the API boundary, after the two paths converge, rather
than inside either of them.

**Where the numbers come from.** Nothing here is fetched. Every greek is
derived from data the chain already carries — spot, strike, time to
expiry and the traded premium — through the Black-Scholes model in
`option_pricing`. NIFTY options are European and cash-settled, so that
is the right model rather than an approximation of one.

**IV is preferred, not assumed.** NSE publishes its own implied
volatility and Angel's stream does not. Where a source states one it is
believed; where it does not, IV is backed out of the traded premium by
bisection. Computing our own on top of a stated one would put two
different IVs on one desk and no way to tell which a row was using.

**Cost.** Forty strikes, both sides, solved and differentiated: 1.2ms
measured on 16-Sep-2026, about half a percent of one core at four
publishes a second. There is no cache here because there is nothing
worth caching.

**What is deliberately absent.** A row whose premium cannot yield an IV
— no last trade, or a price below intrinsic, which happens on untraded
far strikes — gets `None` for every greek rather than a default. A
plotted 0.00 delta and an absent delta look identical on a ladder and
mean completely different things.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from ..market_hours import IST, MARKET_CLOSE
from . import option_pricing as bs

log = logging.getLogger(__name__)

# NIFTY weeklies stop trading at the close on their expiry day, so the
# session close *is* the expiry time. Imported rather than restated: the
# desk keeps one definition of the trading day, and a second copy here
# would be free to drift the day NSE moves the close.

GREEK_FIELDS = ("delta", "gamma", "theta", "vega", "rho")

# Theta, converted from the model's unit to the one every broker's chain
# is quoted in.
#
# `option_pricing.greeks` returns decay per *trading* day, and that is
# right for the thing it was written for: the strategy engine reasons
# about holding a position across sessions, and 252 is the number of
# sessions in a year. But every published chain — Kite, Sensibull, NSE's
# own — divides by 365 and quotes decay per calendar day.
#
# Measured against Kite on 16-Sep-2026 at the 23,200 strike: delta,
# vega and rho agreed to two places, and theta read -21.79 against
# their -13.28. Not an error in either — the same annual figure over a
# different denominator — but a column that disagrees by sixty percent
# with the screen beside it will be read as a bug, and a trader
# comparing the two has no way to know which convention they are
# looking at.
#
# So the conversion happens here, for display, and the model is left
# alone. Changing `greeks()` itself would silently move the theta the
# strategy engine sizes and exits on, which is a trading change wearing
# a formatting change's clothes.
THETA_TRADING_TO_CALENDAR = bs.TRADING_DAYS_PER_YEAR / 365


def _as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d%b%Y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
    return None


def expiry_instant(expiry) -> datetime | None:
    """The moment the contract stops trading, in UTC.

    15:30 IST on the expiry day, not midnight. A weekly read as expiring
    at midnight carries nine and a half hours of life it does not have,
    which on the expiry day itself is most of its remaining theta.
    """
    day = _as_date(expiry)
    if day is None:
        return None
    return datetime.combine(day, MARKET_CLOSE, tzinfo=IST).astimezone(UTC)


def _positive(value) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _one_side(row: dict, prefix: str, kind: str, spot: float,
              years: float) -> None:
    """Fill `<prefix>_iv` and the greeks for one side of a strike."""
    strike = _positive(row.get("strike"))
    if strike is None:
        return

    # A stated IV wins. NSE quotes it in percent; the model wants a
    # fraction, and mixing the two silently is a hundredfold error that
    # still produces plausible-looking greeks.
    stated = _positive(row.get(f"{prefix}_iv"))
    iv = stated / 100 if stated else None

    if iv is None:
        premium = _positive(row.get(f"{prefix}_ltp"))
        if premium is not None:
            iv = bs.implied_volatility(premium, spot, strike, years, kind=kind)

    if not iv or iv <= 0:
        row[f"{prefix}_iv"] = stated            # keep it if the source gave one
        for name in GREEK_FIELDS:
            row[f"{prefix}_{name}"] = None
        return

    row[f"{prefix}_iv"] = round(iv * 100, 2)
    g = bs.greeks(spot, strike, years, iv, kind=kind)
    row[f"{prefix}_delta"] = round(g.delta, 4)
    row[f"{prefix}_gamma"] = round(g.gamma, 6)
    row[f"{prefix}_theta"] = round(g.theta * THETA_TRADING_TO_CALENDAR, 2)
    row[f"{prefix}_vega"] = round(g.vega, 2)
    row[f"{prefix}_rho"] = round(g.rho, 3)


def enrich(strikes: list[dict], spot, expiry, now: datetime | None = None
           ) -> list[dict]:
    """Every strike, with IV and greeks on both sides.

    Returns the rows unchanged — not an error, and not zeroed greeks —
    when the inputs cannot support the maths: no spot, no expiry, or an
    expiry already past. The ladder then renders without those columns,
    which is honest, where a column of 0.00 would not be.
    """
    price = _positive(spot)
    instant = expiry_instant(expiry)
    if not strikes or price is None or instant is None:
        return strikes

    years = bs.years_to_expiry(now or datetime.now(UTC), instant)
    if years <= 0:
        return strikes

    for row in strikes:
        try:
            _one_side(row, "call", "CE", price, years)
            _one_side(row, "put", "PE", price, years)
        except Exception as exc:                          # noqa: BLE001
            # One unusable strike must not cost the whole ladder.
            log.debug("greeks failed for strike %s: %s", row.get("strike"), exc)
    return strikes
