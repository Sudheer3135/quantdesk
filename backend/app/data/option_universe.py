"""Which option contracts the live feed should subscribe to.

A NIFTY chain lists well over a thousand contracts across eighteen
expiries. Subscribing to all of them would spend the socket's budget on
strikes nobody will ever trade and bury the ones that matter, so this picks
a band around the money on the nearest expiries and nothing else.

Two decisions here are worth stating because they are not obvious:

**The band is in strikes, not rupees.** NIFTY strikes are 50 apart, so a
band of 20 is ±1000 points. Expressing it in strikes keeps the subscription
the same size whatever the index level, which is what the socket cares
about; expressing it in points would silently double the contract count
over a few years of a rising index.

**The band is deliberately wider than the money.** Spot moves during a
session and re-subscribing costs a round trip and a gap in the series, so
the universe is chosen once with room to drift. `needs_refresh` says when
the drift has finally outrun it.

Expired contracts are absent from Angel's master, which is why this can
only ever build a *live* universe. There is no historical equivalent.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

log = logging.getLogger(__name__)

# NIFTY strikes are 50 apart. Used only to convert a band in strikes into a
# band in points for the drift check — never to invent a strike that is not
# in the master.
STRIKE_STEP = 50.0

# Angel reports strikes in paise, like every other price it sends.
PAISE = 100.0

CALL, PUT = "CE", "PE"


@dataclass(frozen=True)
class Contract:
    """One subscribable option contract."""
    token: str
    symbol: str
    strike: float
    option_type: str
    expiry: date
    lot_size: int | None = None

    @property
    def key(self) -> tuple[float, str]:
        return (self.strike, self.option_type)

    def to_dict(self) -> dict:
        return {"token": self.token, "symbol": self.symbol,
                "strike": self.strike, "option_type": self.option_type,
                "expiry": self.expiry.isoformat(), "lot_size": self.lot_size}


@dataclass
class Universe:
    """The contracts to subscribe to, and the spot they were chosen around."""
    contracts: list[Contract]
    expiry: date | None
    centre: float
    band: int

    @property
    def tokens(self) -> list[str]:
        return [c.token for c in self.contracts]

    def by_token(self) -> dict[str, Contract]:
        return {c.token: c for c in self.contracts}

    @property
    def strikes(self) -> list[float]:
        return sorted({c.strike for c in self.contracts})

    def needs_refresh(self, spot: float, *, margin: int = 5,
                      on: date | None = None) -> bool:
        """Should this universe be rebuilt?

        Two ways it goes stale, and only one of them used to be checked.

        **Its expiry has passed.** Measured on 16-Sep-2026: the desk was
        still subscribed to eighty 15-Sep contracts the morning after they
        expired. Those tokens never print again, so the live chain sat at
        0 of 80 quoted for the whole session and every option on the desk
        silently fell back to the 60-second NSE poll — a chain roughly
        150x slower than the stream it replaced, with nothing on the
        dashboard saying why beyond a "POLL" badge.

        Drift alone could never catch that: the strikes stay perfectly
        well centred on spot, they are simply dead. An expiry is a
        property of the calendar, not of the price, so it has to be asked
        about separately or it is never asked at all.

        **Spot has drifted off the band.** `margin` is how many strikes of
        cover must remain between spot and the edge. Below that the chain
        stops describing the money, which is the only part of it the
        signal engine reads.
        """
        if not self.contracts:
            return True
        # Expiry day itself is a trading day — contracts settle at the
        # close, so `<` rather than `<=`. Rebuilding at 09:15 on the
        # expiry would throw away the most active session they have.
        if self.expiry and self.expiry < (on or date.today()):
            return True
        edge = margin * STRIKE_STEP
        return spot < min(self.strikes) + edge or spot > max(self.strikes) - edge

    def to_dict(self) -> dict:
        return {
            "expiry": self.expiry.isoformat() if self.expiry else None,
            "centre": self.centre,
            "band": self.band,
            "contracts": len(self.contracts),
            "strikes": len(self.strikes),
            "range": [min(self.strikes), max(self.strikes)] if self.contracts else [],
        }


def parse_expiry(value: str) -> date | None:
    """Angel writes expiries as 01SEP2026."""
    try:
        return datetime.strptime(str(value).strip(), "%d%b%Y").date()
    except (ValueError, TypeError):
        return None


def parse_strike(value) -> float | None:
    """Strikes arrive in paise, as a float string: '2440000.000000'."""
    try:
        return round(float(value) / PAISE, 2)
    except (TypeError, ValueError):
        return None


def index_options(master: list[dict], underlying: str = "NIFTY") -> list[dict]:
    """Every listed index-option row for one underlying."""
    return [
        row for row in master
        if row.get("name") == underlying
        and row.get("exch_seg") == "NFO"
        and row.get("instrumenttype") == "OPTIDX"
    ]


def expiries(rows: list[dict], *, on: date | None = None) -> list[date]:
    """Listed expiries that have not passed, soonest first."""
    today = on or date.today()
    found = {parse_expiry(r.get("expiry")) for r in rows}
    return sorted(d for d in found if d and d >= today)


def build(master: list[dict], spot: float, *, underlying: str = "NIFTY",
          band: int = 20, expiry: date | None = None,
          on: date | None = None) -> Universe:
    """The contracts within `band` strikes of `spot` on the nearest expiry.

    Returns an empty universe rather than raising when nothing matches: a
    feed that cannot find its contracts should fall back to the polled
    chain, not fail to start.
    """
    rows = index_options(master, underlying)
    if not rows:
        log.warning("no %s index options in the instrument master", underlying)
        return Universe(contracts=[], expiry=None, centre=spot, band=band)

    if expiry is None:
        upcoming = expiries(rows, on=on)
        if not upcoming:
            log.warning("no unexpired %s expiries in the master", underlying)
            return Universe(contracts=[], expiry=None, centre=spot, band=band)
        expiry = upcoming[0]

    reach = band * STRIKE_STEP
    contracts: list[Contract] = []
    for row in rows:
        if parse_expiry(row.get("expiry")) != expiry:
            continue
        strike = parse_strike(row.get("strike"))
        if strike is None or abs(strike - spot) > reach:
            continue
        symbol = str(row.get("symbol") or "")
        kind = CALL if symbol.endswith(CALL) else PUT if symbol.endswith(PUT) else None
        if kind is None:
            continue
        try:
            lot = int(row.get("lotsize")) if row.get("lotsize") else None
        except (TypeError, ValueError):
            lot = None
        contracts.append(Contract(token=str(row.get("token")), symbol=symbol,
                                  strike=strike, option_type=kind,
                                  expiry=expiry, lot_size=lot))

    contracts.sort(key=lambda c: (c.strike, c.option_type))
    log.info("option universe: %d contracts, expiry %s, %.0f ± %.0f",
             len(contracts), expiry, spot, reach)
    return Universe(contracts=contracts, expiry=expiry, centre=spot, band=band)
