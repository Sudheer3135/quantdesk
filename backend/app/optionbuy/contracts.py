"""Which contract a direction actually buys, and when to refuse to buy one.

An index signal says "up". It does not say 24,350 CE expiring Tuesday, and
the distance between those two statements is most of an option buyer's
result. The same correct call is a good trade at one strike and a donation
at another.

Four decisions live here, in this order, because each one narrows the next:

  **Side.** BUY takes a call, SELL takes a put. There is nothing clever to
  do here and nothing configurable about it.

  **Expiry.** The nearest listed expiry inside a tenor window. Both ends of
  that window are real constraints: too far out and the position is paying
  for time the strategy never intends to use; too near and gamma makes the
  premium a coin flip that has nothing to do with the signal. Expiry day
  itself is excluded by default.

  **Strike.** At the money, a fixed number of steps away, or the strike
  whose delta is closest to a target. When the archive is available the
  choice is snapped to the ladder that actually existed — NIFTY lists 50-
  point strikes near the money and 100-point strikes further out, so a
  computed strike is regularly a contract nobody could have traded.

  **Liquidity.** Open interest, volume, and the quoted spread. A backtest
  that fills at the close of a contract with no open interest is measuring
  a market that was not there.

Every refusal is a named code, returned rather than logged. A strategy that
silently skips the trades it cannot price reports the win rate of the subset
it happened to like, and nothing in the output would show the selection.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from ..analytics import option_pricing
from ..market_hours import IST
from .chain import ChainStore, ContractKey

CALL, PUT = "CE", "PE"

# Strike-selection policies.
ATM = "atm"
OFFSET = "offset"
DELTA = "delta"
STRIKE_POLICIES = (ATM, OFFSET, DELTA)

# Expiry-selection policies. `nearest` takes the front expiry that satisfies
# the tenor window; `next` deliberately skips it, which is how you ask
# whether the front-week result was really a gamma artefact.
NEAREST = "nearest"
NEXT = "next"
EXPIRY_POLICIES = (NEAREST, NEXT)

# Rejection codes. Named, counted, and reported — never a bare `continue`.
NO_EXPIRY = "no_expiry_available"
EXPIRY_TOO_NEAR = "expiry_too_near"
EXPIRY_TOO_FAR = "expiry_too_far"
STRIKE_NOT_LISTED = "strike_not_listed"
NO_QUOTE = "no_quote_at_decision_bar"
THIN_OPEN_INTEREST = "open_interest_below_floor"
THIN_VOLUME = "volume_below_floor"
WIDE_SPREAD = "quoted_spread_too_wide"
PREMIUM_TOO_LOW = "premium_below_floor"

# NIFTY weeklies expire on Tuesday at the time of writing. NSE has moved
# this before — it was Thursday until 2025 — so it is a parameter, and a
# long backtest spanning a change needs the archive rather than this.
DEFAULT_EXPIRY_WEEKDAY = 1
EXPIRY_TIME = (15, 30)


@dataclass
class SelectionConfig:
    """The contract-selection policy for a run, stated in one place."""

    # ---- expiry
    expiry_policy: str = NEAREST
    min_days_to_expiry: float = 1.0
    max_days_to_expiry: float = 10.0
    expiry_weekday: int = DEFAULT_EXPIRY_WEEKDAY   # only used with no archive

    # ---- strike
    strike_policy: str = ATM
    strike_offset: int = 0        # steps from ATM; negative goes in the money
    target_delta: float = 0.5     # only used by the delta policy
    strike_step: int = 50         # fallback ladder when the archive has none

    # ---- liquidity
    min_open_interest: float = 0.0
    min_volume: float = 0.0
    max_spread_pct: float = 25.0  # of premium, only when bid and ask exist
    min_premium: float = 5.0      # a 2-rupee lottery ticket is not this strategy

    def validate(self) -> None:
        if self.strike_policy not in STRIKE_POLICIES:
            raise ValueError(f"unknown strike policy {self.strike_policy!r}")
        if self.expiry_policy not in EXPIRY_POLICIES:
            raise ValueError(f"unknown expiry policy {self.expiry_policy!r}")
        if self.min_days_to_expiry > self.max_days_to_expiry:
            raise ValueError("min_days_to_expiry is beyond max_days_to_expiry — "
                             "no expiry can satisfy this window")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Rejection:
    """Why no contract was chosen. Counted in the result, never swallowed."""
    code: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Liquidity:
    """What the archive said about tradability at the decision bar."""
    checked: bool
    open_interest: float | None = None
    volume: float | None = None
    spread: float | None = None
    spread_pct: float | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Selection:
    """The contract this signal buys, and the reasoning that reached it."""
    key: ContractKey
    expiry: datetime                  # IST-aware, at the close on expiry day
    days_to_expiry: float
    moneyness: str                    # ATM | ITM | OTM
    steps_from_atm: int
    delta: float | None
    liquidity: Liquidity
    reasons: list[str] = field(default_factory=list)

    @property
    def strike(self) -> float:
        return self.key.strike

    @property
    def option_type(self) -> str:
        return self.key.option_type

    def to_dict(self) -> dict:
        return {
            "contract": self.key.label(),
            "strike": self.key.strike,
            "option_type": self.key.option_type,
            "expiry": self.key.expiry.isoformat(),
            "days_to_expiry": round(self.days_to_expiry, 2),
            "moneyness": self.moneyness,
            "steps_from_atm": self.steps_from_atm,
            "delta": round(self.delta, 4) if self.delta is not None else None,
            "liquidity": self.liquidity.to_dict(),
            "reasons": self.reasons,
        }


def side_for(action: str) -> str:
    """BUY buys a call, SELL buys a put. This strategy only ever buys."""
    if action == "BUY":
        return CALL
    if action == "SELL":
        return PUT
    raise ValueError(f"{action!r} is not a directional action; there is no "
                     "contract to buy for a HOLD")


def expiry_moment(day: date) -> datetime:
    """Expiry as an instant: the close on expiry day, IST."""
    hour, minute = EXPIRY_TIME
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def synthetic_expiries(moment: datetime, weekday: int, count: int = 6) -> list[date]:
    """The weekly expiry calendar, when the archive cannot supply one.

    Only reachable on a modelled run. With option history loaded the listed
    expiries are read from the archive instead, because a computed calendar
    cannot know about a settlement holiday or an exchange rescheduling — and
    a backtest that trades an expiry which never existed is fiction whatever
    else it gets right.
    """
    ahead = (weekday - moment.weekday()) % 7
    first = (moment + timedelta(days=ahead)).date()
    if expiry_moment(first) <= moment:
        first = first + timedelta(days=7)
    return [first + timedelta(days=7 * n) for n in range(count)]


def days_to_expiry(moment: datetime, expiry: datetime) -> float:
    """Calendar days remaining, as a float. Tenor, not decay.

    Decay is measured in trading time by `option_pricing.years_to_expiry`.
    This is the number the tenor window is expressed in and the number a
    result is bucketed by, and for both of those a human means calendar days.
    """
    return (expiry - moment).total_seconds() / 86_400


def choose_expiry(
    moment: datetime,
    store: ChainStore,
    config: SelectionConfig,
    *,
    use_archive: bool,
) -> tuple[date | None, Rejection | None]:
    """The expiry to trade, or why none of them qualified."""
    if use_archive:
        listed = [e for e in store.expiries(moment)
                  if expiry_moment(e) > moment]
    else:
        listed = synthetic_expiries(moment, config.expiry_weekday)

    if not listed:
        return None, Rejection(
            NO_EXPIRY,
            f"no expiry listed after {moment.isoformat()}"
            + (" in the option archive" if use_archive else ""))

    if config.expiry_policy == NEXT:
        listed = listed[1:]
        if not listed:
            return None, Rejection(
                NO_EXPIRY, "the 'next' policy skips the front expiry and "
                           "there is no second one listed")

    # Report the *nearest* failure rather than the first. Told that an
    # expiry 47 days out was too far, nobody learns that the front weekly
    # was one day away and excluded on purpose.
    too_near: Rejection | None = None
    for day in listed:
        tenor = days_to_expiry(moment, expiry_moment(day))
        if tenor < config.min_days_to_expiry:
            if too_near is None:
                too_near = Rejection(
                    EXPIRY_TOO_NEAR,
                    f"{day.isoformat()} is {tenor:.2f} days out, inside the "
                    f"{config.min_days_to_expiry} day floor — expiry-day gamma "
                    "prices the option off the clock rather than off the signal")
            continue
        if tenor > config.max_days_to_expiry:
            return None, too_near or Rejection(
                EXPIRY_TOO_FAR,
                f"the nearest qualifying expiry {day.isoformat()} is "
                f"{tenor:.2f} days out, beyond the {config.max_days_to_expiry} "
                "day ceiling")
        return day, None

    return None, too_near or Rejection(
        NO_EXPIRY, "no listed expiry fell inside the tenor window")


def _ladder(store: ChainStore, expiry: date, option_type: str,
            moment: datetime, use_archive: bool, spot: float,
            config: SelectionConfig) -> list[float]:
    """Strikes that were actually listed, or a synthetic ladder around spot."""
    if use_archive:
        return store.strikes(expiry, option_type, moment)
    atm = option_pricing.atm_strike(spot, config.strike_step)
    return [atm + n * config.strike_step for n in range(-20, 21)]


def _moneyness(strike: float, spot: float, option_type: str,
               step: int) -> tuple[str, int]:
    """Where the strike sits, and how many steps away it is."""
    steps = int(round((strike - spot) / step)) if step else 0
    if abs(strike - spot) <= step / 2:
        return "ATM", steps
    if option_type == CALL:
        return ("OTM" if strike > spot else "ITM"), steps
    return ("OTM" if strike < spot else "ITM"), steps


def _by_delta(ladder: list[float], spot: float, years: float, iv: float,
              option_type: str, target: float) -> float:
    """The listed strike whose Black-Scholes delta is nearest the target.

    Delta targeting is a model choice even on an observed run: the archive
    stores IV per contract but not delta, and recomputing it from every
    stored quote to pick a strike would price the whole ladder before
    choosing from it. The chosen contract is still priced from the archive
    afterwards — only the *choice* is modelled, which the reasons say.
    """
    want = abs(target)

    def distance(strike: float) -> float:
        greeks = option_pricing.greeks(spot, strike, years, iv, kind=option_type)
        return abs(abs(greeks.delta) - want)

    return min(ladder, key=distance)


def select(
    *,
    action: str,
    spot: float,
    moment: datetime,
    store: ChainStore,
    config: SelectionConfig,
    use_archive: bool,
    iv: float = option_pricing.DEFAULT_IV,
) -> tuple[Selection | None, Rejection | None]:
    """Pick the contract, or say why this signal cannot be traded.

    `moment` is the decision bar in IST. Every archive read is made through
    the store, which refuses anything the walk has not reached — so a
    selection cannot be made using a ladder or an open-interest figure that
    only existed later.
    """
    config.validate()
    option_type = side_for(action)
    reasons: list[str] = []

    expiry_day, rejected = choose_expiry(moment, store, config,
                                         use_archive=use_archive)
    if expiry_day is None:
        return None, rejected

    expiry_at = expiry_moment(expiry_day)
    tenor = days_to_expiry(moment, expiry_at)
    # Both aware, both IST. Mixing an aware moment with a naive expiry
    # raises, and silently stripping one zone is how a 5.5-hour error gets
    # into a decay calculation.
    years = option_pricing.years_to_expiry(moment, expiry_at)
    reasons.append(
        f"{expiry_day.isoformat()} expiry, {tenor:.1f} days out"
        + (" (listed in the archive)." if use_archive else " (weekly calendar)."))

    ladder = _ladder(store, expiry_day, option_type, moment, use_archive,
                     spot, config)
    if not ladder:
        return None, Rejection(
            STRIKE_NOT_LISTED,
            f"no {option_type} strikes stored for the {expiry_day.isoformat()} "
            "expiry at this bar")

    step = config.strike_step
    if config.strike_policy == DELTA:
        strike = _by_delta(ladder, spot, years, iv, option_type,
                           config.target_delta)
        reasons.append(f"Strike {strike:.0f} chosen for a delta near "
                       f"{config.target_delta:.2f} (modelled).")
    else:
        wanted = option_pricing.atm_strike(spot, step)
        if config.strike_policy == OFFSET and config.strike_offset:
            direction = 1 if option_type == CALL else -1
            wanted += direction * config.strike_offset * step
        # Snap to the ladder. An ATM calculation lands between listed
        # strikes as soon as the step widens away from the money, and a
        # contract that was never listed cannot have been bought.
        strike = min(ladder, key=lambda s: (abs(s - wanted), s))
        if strike != wanted:
            reasons.append(f"Wanted {wanted:.0f}; nearest listed strike is "
                           f"{strike:.0f}.")
        else:
            reasons.append(f"Strike {strike:.0f}.")

    key = ContractKey(expiry=expiry_day, strike=float(strike),
                      option_type=option_type)
    moneyness, steps = _moneyness(strike, spot, option_type, step)
    reasons.append(f"{moneyness} with the index at {spot:.2f}.")

    liquidity, rejected = check_liquidity(store, key, moment, config,
                                          use_archive=use_archive)
    if rejected is not None:
        return None, rejected
    reasons.append(liquidity.note)

    delta = option_pricing.greeks(spot, strike, years, iv,
                                  kind=option_type).delta if years > 0 else None
    return Selection(key=key, expiry=expiry_at, days_to_expiry=tenor,
                     moneyness=moneyness, steps_from_atm=steps, delta=delta,
                     liquidity=liquidity, reasons=reasons), None


def check_liquidity(
    store: ChainStore,
    key: ContractKey,
    moment: datetime,
    config: SelectionConfig,
    *,
    use_archive: bool,
) -> tuple[Liquidity, Rejection | None]:
    """Was this contract tradable at the decision bar?

    On a modelled run there is nothing to check, and the answer says so
    rather than passing. An unchecked filter reported as a passed one is how
    a backtest ends up claiming it avoided illiquid strikes when it never
    looked at one.
    """
    if not use_archive:
        return Liquidity(
            checked=False,
            note="Liquidity unchecked — this run reads no option archive, so "
                 "open interest, volume and spread are unknown.",
        ), None

    bar = store.bar_at(key, moment)
    if bar is None:
        return Liquidity(checked=True), Rejection(
            NO_QUOTE,
            f"no quote stored for {key.label()} at {moment.isoformat()} — the "
            "collector had nothing for this contract on this bar")

    if bar.close < config.min_premium:
        return Liquidity(checked=True), Rejection(
            PREMIUM_TOO_LOW,
            f"{key.label()} quoted {bar.close:.2f}, below the "
            f"{config.min_premium:.2f} floor")

    oi = bar.open_interest
    if config.min_open_interest > 0:
        if oi is None:
            return Liquidity(checked=True), Rejection(
                THIN_OPEN_INTEREST,
                f"{key.label()} stores no open interest, and this run requires "
                f"at least {config.min_open_interest:.0f}")
        if oi < config.min_open_interest:
            return Liquidity(checked=True, open_interest=oi), Rejection(
                THIN_OPEN_INTEREST,
                f"{key.label()} held {oi:.0f} open interest, below the "
                f"{config.min_open_interest:.0f} floor")

    volume = bar.volume
    if config.min_volume > 0:
        if volume is None:
            return Liquidity(checked=True, open_interest=oi), Rejection(
                THIN_VOLUME,
                f"{key.label()} stores no volume, and this run requires at "
                f"least {config.min_volume:.0f}")
        if volume < config.min_volume:
            return Liquidity(checked=True, open_interest=oi, volume=volume), Rejection(
                THIN_VOLUME,
                f"{key.label()} traded {volume:.0f}, below the "
                f"{config.min_volume:.0f} floor")

    spread = spread_pct = None
    if bar.bid is not None and bar.ask is not None and bar.ask > bar.bid >= 0:
        spread = bar.ask - bar.bid
        mid = (bar.ask + bar.bid) / 2
        spread_pct = (spread / mid * 100) if mid > 0 else None
        if spread_pct is not None and spread_pct > config.max_spread_pct:
            return Liquidity(checked=True, open_interest=oi, volume=volume,
                             spread=spread, spread_pct=spread_pct), Rejection(
                WIDE_SPREAD,
                f"{key.label()} quoted {bar.bid:.2f} / {bar.ask:.2f}, a "
                f"{spread_pct:.1f}% spread against the "
                f"{config.max_spread_pct:.1f}% ceiling")

    parts = [f"quoted {bar.close:.2f}"]
    parts.append(f"OI {oi:.0f}" if oi is not None else "OI not stored")
    parts.append(f"volume {volume:.0f}" if volume is not None else "volume not stored")
    parts.append(f"spread {spread_pct:.1f}%" if spread_pct is not None
                 else "no depth published")
    return Liquidity(checked=True, open_interest=oi, volume=volume,
                     spread=spread, spread_pct=spread_pct,
                     note="Liquidity: " + ", ".join(parts) + "."), None
