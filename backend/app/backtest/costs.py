"""Transaction costs and slippage for Indian index options.

Both engines used to charge a flat `cost_per_round_trip = 120.0`. That
number is not wrong so much as structurally misleading: real F&O charges
scale with *premium turnover*, so a flat figure overcharges a cheap
out-of-the-money trade and undercharges a large in-the-money one. On a
strategy whose edge is a fraction of an R, getting that shape wrong is
enough to flip the sign of the expectancy.

Worse, a flat number hides which charge dominates. Broken out, the answer
for a retail option buyer is usually STT on the sell leg — which is why an
itemised breakdown is returned rather than a single total.

⚠️ **These rates change by circular.** The defaults below reflect the
Indian F&O schedule as of **October 2024** and are correct only until SEBI
or the exchange revises them, which happens without warning and has happened
repeatedly. Verify them against a real contract note before believing any
number that comes out of here, and override them in settings rather than
editing this file. A rate that quietly goes stale flatters or punishes every
backtest run after it, and nothing in the output would look wrong.

Sources to check against: your broker's contract note (the authoritative
one), the NSE circular on transaction charges, and the SEBI turnover fee
notification.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

# The tick a NIFTY option premium moves in.
TICK_SIZE = 0.05


@dataclass(frozen=True)
class CostModel:
    """Statutory and brokerage charges, as percentages of premium turnover.

    Percentages are expressed as percent, not fractions: 0.1 means 0.1%.
    That matches how every circular and contract note states them, which is
    what you will be comparing against when one of these numbers is wrong.
    """

    # Flat per executed order. ₹20 is the discount-broker standard; a
    # percentage-brokerage account needs this replaced, not adjusted.
    brokerage_per_order: float = 20.0

    # Securities Transaction Tax — charged on the SELL leg only, on premium.
    # Raised from 0.0625% to 0.1% effective 1 October 2024.
    stt_pct_sell: float = 0.10

    # NSE transaction charge on premium turnover, both legs.
    exchange_txn_pct: float = 0.03503

    # SEBI turnover fee: ₹10 per crore of premium turnover.
    sebi_pct: float = 0.0001

    # NSE investor protection fund trust levy on premium turnover.
    ipft_pct: float = 0.0005

    # Stamp duty, BUY leg only.
    stamp_duty_pct_buy: float = 0.003

    # GST on brokerage plus exchange and SEBI charges. Not on STT or stamp.
    gst_pct: float = 18.0

    orders_per_round_trip: int = 2

    # What the percentages above are percentages *of*. Every rate in this
    # schedule is levied on premium turnover, so this model is only correct
    # when it is handed a premium. Handing it an index level charges option
    # rates on 24,000 a point instead of a hundred-odd rupees of premium and
    # produces a bill roughly two hundred times too large; the index
    # backtest uses `FlatCostModel` for exactly that reason. The field
    # exists so a caller can check rather than assume.
    turnover_basis: str = "option_premium"

    # Whether anyone has checked these against a real contract note on a
    # known date. "assumed" is the honest default: the numbers above were
    # read off circulars, not off a settlement, and no date-stamped source
    # document is stored alongside them. A run that reports its costs as
    # verified when nothing verified them is worse than one that says
    # nothing, because the claim travels further than the caveat.
    schedule_status: str = "assumed"

    def round_trip(self, buy_price: float, sell_price: float,
                   quantity: int) -> CostBreakdown:
        """Every charge on one complete long-option round trip.

        `quantity` is contracts, not lots — a NIFTY lot is 75 contracts and
        the charges scale with the contract count.

        `buy_price` and `sell_price` are the prices of the *buy leg* and the
        *sell leg*, decided by the side each order was sent on. They are not
        the lower and the higher of the two prices: STT falls on the sale
        and stamp duty on the purchase, so inferring the legs from price
        order misplaces both on every losing long. `execution.legs` does
        this correctly and is what the engines call.
        """
        buy_turnover = max(0.0, buy_price) * quantity
        sell_turnover = max(0.0, sell_price) * quantity
        turnover = buy_turnover + sell_turnover

        brokerage = self.brokerage_per_order * self.orders_per_round_trip
        stt = sell_turnover * self.stt_pct_sell / 100
        exchange = turnover * self.exchange_txn_pct / 100
        sebi = turnover * self.sebi_pct / 100
        ipft = turnover * self.ipft_pct / 100
        stamp = buy_turnover * self.stamp_duty_pct_buy / 100
        gst = (brokerage + exchange + sebi + ipft) * self.gst_pct / 100

        return CostBreakdown(
            brokerage=brokerage, stt=stt, exchange=exchange, sebi=sebi,
            ipft=ipft, stamp_duty=stamp, gst=gst,
        )


@dataclass
class CostBreakdown:
    """What each charge cost, so you can see which one is eating the edge."""
    brokerage: float = 0.0
    stt: float = 0.0
    exchange: float = 0.0
    sebi: float = 0.0
    ipft: float = 0.0
    stamp_duty: float = 0.0
    gst: float = 0.0

    @property
    def total(self) -> float:
        return (self.brokerage + self.stt + self.exchange + self.sebi
                + self.ipft + self.stamp_duty + self.gst)

    @property
    def largest(self) -> str:
        """Which single charge dominated. Usually STT, and worth knowing."""
        items = {k: v for k, v in asdict(self).items()}
        return max(items, key=items.get) if items else ""

    def to_dict(self) -> dict:
        out = {k: round(v, 2) for k, v in asdict(self).items()}
        out["total"] = round(self.total, 2)
        out["largest"] = self.largest
        return out


@dataclass(frozen=True)
class SlippageModel:
    """What you actually get filled at, versus what you asked for.

    Two regimes, and the difference matters:

      - When a bid and ask are stored, slippage is *measured* — you cross
        some fraction of a real spread. This is why `option_candles` carries
        bid and ask columns even though the free NSE feed leaves them null.
      - Otherwise it is *assumed*, as a number of ticks. An assumption is
        fine as long as nothing pretends it was a measurement, so the fill
        reports which one it used.
    """
    ticks: float = 2.0
    tick_size: float = TICK_SIZE

    # How much of the quoted spread a market order gives up, used by
    # `per_unit` to estimate a per-contract cost from a book.
    #
    # It deliberately does *not* reach the quoted fill path below. When a
    # real bid and ask are stored the spread is a measurement, not an
    # assumption, and a market order crosses it in full: a buy lifts the
    # ask, a sell hits the bid. Scaling a measured spread would be inventing
    # a different market rather than stressing the execution assumption, and
    # shrinking it would manufacture fills better than the touch. What a
    # sensitivity sweep varies on a quoted book is `impact_ticks` — the part
    # that genuinely is an assumption.
    spread_fraction: float = 0.5
    execution_model: str = "ltp_slippage"  # or conservative_spread
    estimated_spread_pct: float = 1.0
    impact_ticks: float = 0.0

    # Slippage on the index itself, as a percentage. Used by the index
    # backtest, where there is no premium and no book.
    index_pct: float = 0.02

    def per_unit(self, bid: float | None = None,
                 ask: float | None = None) -> tuple[float, str]:
        """Premium given up per contract, and how it was arrived at."""
        if bid is not None and ask is not None and ask > bid >= 0:
            return (ask - bid) * self.spread_fraction, "measured_spread"
        return self.ticks * self.tick_size, "assumed_ticks"

    def index_points(self, price: float) -> float:
        return price * self.index_pct / 100


@dataclass
class Fill:
    """One executed leg, priced honestly.

    `slippage` is the whole distance from the reference to the fill, and it
    is split because the two halves answer different questions. `spread_cost`
    is what the market charged — on a quoted book it is the measured
    half-spread, and no assumption of this platform's can make it smaller.
    `impact_cost` is what this run *assumed* on top of that, and it is the
    only part a sensitivity sweep is entitled to vary. Reporting one number
    made a sweep over a quoted book look flat when it was in fact measuring
    a spread it could not move.
    """
    requested: float
    filled: float
    slippage: float
    basis: str
    spread_cost: float = 0.0
    impact_cost: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def buy_fill(price: float, model: SlippageModel,
             bid: float | None = None, ask: float | None = None) -> Fill:
    """A buy fills worse than quoted — higher."""
    return _execution_fill(price, model, bid, ask, buying=True)


def sell_fill(price: float, model: SlippageModel,
              bid: float | None = None, ask: float | None = None) -> Fill:
    """A sell fills worse than quoted — lower, and never below zero.

    An option cannot be sold for a negative premium. Without the floor, a
    near-worthless contract plus slippage produces a negative exit price,
    which shows up as a *profit* on a losing trade.
    """
    return _execution_fill(price, model, bid, ask, buying=False)


def _execution_fill(price, model, bid, ask, *, buying):
    import math
    if not math.isfinite(price) or price < 0:
        raise ValueError("invalid reference premium")
    if model.execution_model not in ("ltp_slippage", "conservative_spread"):
        raise ValueError("unknown execution model")
    if min(model.ticks, model.estimated_spread_pct, model.impact_ticks, model.tick_size) < 0:
        raise ValueError("execution friction cannot be negative")
    impact = model.impact_ticks * model.tick_size
    if (bid is not None and ask is not None and math.isfinite(bid) and math.isfinite(ask)
            and 0 < bid <= ask):
        # Reference is midpoint, never arbitrary LTP. Crossing the spread is
        # friction, and the two components are kept apart:
        #
        #   spread_cost  the measured half-spread. The market's number. A
        #                sweep at 0x does not erase it, because a zero
        #                *slippage assumption* is not a claim that the book
        #                was one tick wide.
        #   impact_cost  what this run assumes a market order moves the
        #                touch by. The assumption, and the only part a
        #                multiplier scales.
        #
        # Adding the two once, here, is also what stops the spread being
        # counted twice: the fill is the touch plus impact, never the touch
        # plus a re-derived spread plus impact.
        reference = (bid + ask) / 2
        spread_cost = (ask - bid) / 2
        filled = ask + impact if buying else max(0, bid - impact)
        basis = "quoted_touch_estimated_impact"
        # A sell floored at zero gives up less than the model asked for, so
        # the components are read back off the fill rather than asserted.
        realised = abs(filled - reference)
        impact_cost = max(0.0, realised - spread_cost)
        spread_cost = min(spread_cost, realised)
    else:
        reference = price
        spread_estimate = (price * model.estimated_spread_pct / 200
                           if model.execution_model == "conservative_spread"
                           else 0.0)
        slip = model.ticks * model.tick_size + impact + spread_estimate
        filled = price + slip if buying else max(0, price - slip)
        basis = "estimated_" + model.execution_model
        # No book, so nothing here is measured: the whole distance is an
        # assumption. The estimated spread is still reported under
        # `spread_cost` so the two branches read the same way, but it is an
        # estimate and the basis string says so.
        realised = abs(filled - reference)
        spread_cost = min(spread_estimate, realised)
        impact_cost = max(0.0, realised - spread_cost)
    return Fill(requested=reference, filled=filled,
                slippage=abs(filled-reference), basis=basis,
                spread_cost=spread_cost, impact_cost=impact_cost)


@dataclass(frozen=True)
class FlatCostModel:
    """A single rupee figure for the whole round trip.

    Kept for the index backtest, which trades index points rather than a
    premium and so has no turnover to charge against. Also useful for
    reproducing an old result: pass the number the previous run used and the
    comparison stays honest.
    """
    per_round_trip: float = 120.0

    # Charged per round trip regardless of what was traded, so there is no
    # turnover to get wrong. Stated in the same words as `CostModel` so a
    # caller can read the basis off either without knowing which it has.
    turnover_basis: str = "flat_per_round_trip"
    schedule_status: str = "configured"

    def round_trip(self, buy_price: float = 0.0, sell_price: float = 0.0,
                   quantity: int = 0) -> CostBreakdown:
        return CostBreakdown(brokerage=self.per_round_trip)


DEFAULT_COSTS = CostModel()
DEFAULT_SLIPPAGE = SlippageModel()


def describe(model: CostModel | FlatCostModel,
             slippage: SlippageModel) -> dict:
    """The cost assumptions, for the result block.

    A backtest that does not state its cost assumptions cannot be compared
    with one that used different ones, and after a month you will not
    remember which was which.
    """
    if isinstance(model, FlatCostModel):
        costs: dict = {"kind": "flat", "per_round_trip": model.per_round_trip}
    else:
        costs = {"kind": "itemised", "rates_as_of": "2024-10",
                 **{k: v for k, v in asdict(model).items()}}
    # Stated on every result, not only the itemised one. "assumed" here
    # means exactly what it says: nobody has reconciled these rates against
    # a dated contract note, and until somebody does, a net figure computed
    # with them carries that uncertainty wherever it is quoted. The rates
    # themselves are preserved above so an old result stays reproducible
    # after the schedule changes.
    costs.setdefault("cost_schedule_status",
                     getattr(model, "schedule_status", "assumed"))
    costs.setdefault("turnover_basis",
                     getattr(model, "turnover_basis", "unstated"))
    return {
        "costs": costs,
        "slippage": asdict(slippage),
        "caveat": "Rates change by circular. Verify against a contract note.",
    }
