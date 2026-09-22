"""The option-buying backtest.

The index backtest asks whether the direction was right. This asks the
question that decides the account: would buying the option have made money?
They are different questions, and the gap between them is where most retail
option strategies quietly die — a trade can be directionally correct, reach
its index target, and still close red because decay took more than delta
gave.

What this engine does *not* do is as important as what it does:

  It **does not generate signals.** `signal_engine.generate` produces the
  BUY/SELL/HOLD and the levels, exactly as it does live. `plan.build`
  produces the bias and the entry state. This engine reads both and buys an
  option; it decides nothing about direction or timing.

  It **does not have its own risk rules.** `risk.manager.evaluate` holds the
  veto, sized in premium terms, against a `DayState` rebuilt per trading day.
  The 1% rule, the trade cap, the loss limit, the consecutive-loss rule and
  the deployment cap all apply because they are the same code the desk runs.

  It **does not invent a premium.** Every fill carries one of three evidence
  labels, and the pricing policy decides which are permitted. A contract the
  policy cannot price is a trade that does not happen, counted under a named
  rejection code.

  It **does not look forward.** `HistoricalFeed` guards the candles and
  `ChainStore` guards the option quotes, both by refusing rather than by
  convention.

Two honest limitations, stated here because they change how the numbers
should be read, and repeated in the result:

  **Intrabar option prices do not exist in this archive.** The chain is
  captured at bucket resolution, so an observed exit fills at the close of
  the bar that triggered it, not at the trigger level itself. Modelled exits
  fill at the level. Both are recorded, per trade, in `exit_basis`.

  **Sizing is always modelled.** The premium the option would carry at the
  stop is a counterfactual — no archive holds the price of a level that was
  never reached. It is projected with Black-Scholes, at the contract's own
  stored IV where the archive has one.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import date, time

import pandas as pd

from ..analytics import option_pricing, signal_engine
from ..analytics import plan as plan_builder
from ..backtest.costs import (
    CostModel,
    FlatCostModel,
    SlippageModel,
    buy_fill,
    describe,
    sell_fill,
)
from ..backtest.feed import HistoricalFeed
from ..backtest.measurement import PositionLedger, provenance
from ..risk.manager import DayState, RiskConfig, evaluate
from . import contracts as contract_module
from . import pricing as pricing_module
from . import report as report_module
from .chain import ChainStore, empty_store
from .contracts import SelectionConfig
from .coverage import EvidenceGate
from .pricing import MODELLED, MODELLED_ONLY, ModelAssumptions

log = logging.getLogger(__name__)

STRATEGY_NAME = "option_buying"
STRATEGY_VERSION = "1.0"

# Exits, named so a result can be grouped by them.
STOP, TARGET, TIME, SESSION_END, SESSION_BOUNDARY, EXPIRY = (
    "stop", "target", "time", "session_end", "session_boundary", "expiry")

# Why a signal did not become a trade. Contract-selection codes come from
# `contracts.py`; these are the ones this engine owns.
HOLD_SIGNAL = "no_directional_signal"
ENTRY_STATE_BLOCKED = "entry_state_not_ready"
BIAS_DISAGREES = "bias_disagrees_with_action"
PLAN_FAILED = "plan_could_not_be_built"
GAP_PAST_STOP = "gap_past_stop_before_fill"
FILL_CROSSES_SESSION = "fill_lands_in_a_later_session"
UNPRICEABLE = "no_permitted_premium"
RISK_VETO = "risk_manager_vetoed"
NO_DEFINED_RISK = "premium_risk_not_defined"
ALREADY_IN_TRADE = "position_already_open"

# The stop must be far enough below the entry premium for the division that
# sizes the position to mean anything. Inherited from the existing option
# engine, where clamping this instead turned one trade into 654 lots.
MIN_PREMIUM_RISK_FRACTION = 0.02

# An archived IV outside this band is a bad row, not a volatility. Using one
# to project the premium at the stop produces a position size from noise.
IV_SANE = (0.01, 3.0)


@dataclass
class OptionBuyConfig:
    """Everything this run assumes, in one object that lands in the result."""
    starting_capital: float = 200_000.0
    lot_size: int = 75

    pricing_policy: str = pricing_module.PREFER_OBSERVED
    min_observed_pct: float = 0.0

    # Which decision outputs must agree before a trade is taken. These are
    # the desk's live convention, not a new rule: the two-layer model exists
    # because direction and timing fail independently.
    require_entry_states: tuple[str, ...] = (plan_builder.ENTER_NOW,)
    require_bias_agreement: bool = True

    warmup: int = 60
    analysis_window: int = 300
    max_bars_in_trade: int = 24
    session_exit_ist: time = time(15, 15)
    hold_overnight: bool = False

    def to_dict(self) -> dict:
        out = asdict(self)
        out["session_exit_ist"] = self.session_exit_ist.strftime("%H:%M")
        out["require_entry_states"] = list(self.require_entry_states)
        return out


@dataclass
class OptionTrade:
    """One round trip, with its complete provenance attached."""
    strategy: str
    underlying: str
    option_type: str
    strike: float
    expiry: str
    contract: str

    entry_time: str
    exit_time: str
    direction: str                    # BUY | SELL on the index

    index_entry: float
    index_stop: float
    index_target: float
    index_exit: float
    trigger_level: float | None

    premium_entry: float
    premium_stop: float
    premium_target: float
    premium_exit: float

    lots: int
    quantity: int
    gross_pnl: float
    costs: dict
    pnl: float
    r_multiple: float
    risk_amount: float
    decay_cost: float
    exit_reason: str
    entry_basis: str
    exit_basis: str
    bars_held: int

    # The decision this trade came from, recomputed at its own bar from the
    # prefix alone. Kept so a result can be argued about without re-running.
    confidence: float
    bias: str | None
    entry_state: str | None
    regime_day: str | None
    regime_hour: str | None
    days_to_expiry: float
    moneyness: str
    checks: dict = field(default_factory=dict)

    # Evidence.
    evidence: str = MODELLED
    entry_evidence: str = MODELLED
    exit_evidence: str = MODELLED
    execution_friction: float = 0.0
    fees: float = 0.0
    timing: dict = field(default_factory=dict)
    execution_accuracy: str = "estimated_bar_resolution"
    entry_quote: dict = field(default_factory=dict)
    exit_quote: dict = field(default_factory=dict)
    sizing_basis: str = "modelled"
    sizing_iv: float | None = None

    selection: dict = field(default_factory=dict)
    risk: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OptionBuyResult:
    trades: list[OptionTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    breakdowns: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    rejections: dict = field(default_factory=dict)
    assumptions: dict = field(default_factory=dict)
    dataset: dict = field(default_factory=dict)
    coverage: dict = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    refused: dict | None = None

    def to_dict(self) -> dict:
        out = {
            "strategy": STRATEGY_NAME,
            "version": STRATEGY_VERSION,
            "trades": [t.to_dict() for t in self.trades],
            "equity_curve": self.equity_curve,
            "stats": self.stats,
            "breakdowns": self.breakdowns,
            "evidence": self.evidence,
            "rejections": self.rejections,
            "assumptions": self.assumptions,
            "dataset": self.dataset,
            "coverage": self.coverage,
            "limitations": self.limitations,
        }
        if self.refused is not None:
            out["refused"] = self.refused
        return out


class Rejections:
    """A tally with examples. Every skipped signal lands here."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.examples: dict[str, str] = {}

    def add(self, code: str, detail: str) -> None:
        self.counts[code] = self.counts.get(code, 0) + 1
        self.examples.setdefault(code, detail)

    def to_dict(self) -> dict:
        return {
            "total": sum(self.counts.values()),
            "counts": dict(sorted(self.counts.items(),
                                  key=lambda kv: -kv[1])),
            "examples": self.examples,
        }


def _sane_iv(value: float | None) -> float | None:
    if value is None:
        return None
    low, high = IV_SANE
    return float(value) if low <= float(value) <= high else None


def _bias_agrees(bias: str, action: str) -> bool:
    return ((action == "BUY" and bias == plan_builder.BULLISH)
            or (action == "SELL" and bias == plan_builder.BEARISH))


def run(
    candles: pd.DataFrame,
    *,
    store: ChainStore | None = None,
    config: OptionBuyConfig | None = None,
    risk_config: RiskConfig | None = None,
    selection: SelectionConfig | None = None,
    model: ModelAssumptions | None = None,
    cost_model: CostModel | FlatCostModel | None = None,
    slippage_model: SlippageModel | None = None,
    signal_fn: Callable[[pd.DataFrame], signal_engine.Signal] | None = None,
    plan_fn: Callable[[pd.DataFrame], plan_builder.Plan] | None = None,
    dataset: dict | None = None,
    coverage: dict | None = None,
) -> OptionBuyResult:
    """Walk the candles forward, buying options on approved decisions."""
    cfg = config or OptionBuyConfig()
    sel = selection or SelectionConfig()
    sel.validate()
    model = model or ModelAssumptions()
    store = store if store is not None else empty_store()
    use_archive = cfg.pricing_policy != MODELLED_ONLY

    risk = replace(risk_config) if risk_config else RiskConfig(capital=cfg.starting_capital,
                                     lot_size=cfg.lot_size)
    costs = cost_model or CostModel()
    slippage = slippage_model or SlippageModel()
    # Both are seams, not alternatives. The defaults are the engines the
    # desk runs live; overriding them is how a test states a decision
    # exactly, and how a sweep varies one parameter of the existing engine.
    # Neither is a place to put a second definition of a signal or a bias.
    signal_fn = signal_fn or (lambda frame: signal_engine.generate(frame))
    plan_fn = plan_fn or (lambda frame: plan_builder.build(frame))

    feed = HistoricalFeed(candles, analysis_window=cfg.analysis_window)
    store.seek(None)

    equity = cfg.starting_capital
    curve: list[float] = [equity]
    trades: list[OptionTrade] = []
    rejections = Rejections()
    day_states: dict[date, DayState] = {}
    open_trade: dict | None = None
    ledger = PositionLedger()
    initial_risk = asdict(risk)

    for i in feed.walk(cfg.warmup, reserve=0):
        bar = feed.bar(i)
        stamp = feed.close_time(i)
        ist = stamp.tz_convert("Asia/Kolkata").to_pydatetime()
        store.advance(stamp.to_pydatetime())
        state = day_states.setdefault(ist.date(), DayState(trading_day=ist.date()))

        if open_trade is not None:
            closed = _maybe_exit(open_trade, feed, i, bar, ist, store, cfg, sel,
                                 model, costs, slippage)
            if closed is not None:
                trade, pnl = closed
                equity = cfg.starting_capital + sum(t.pnl for t in trades) + trade.pnl
                ledger.close(stamp, trade.exit_reason, trade.pnl)
                curve.append(equity)
                trades.append(trade)
                # The same state `record_fill` incremented: entries whose
                # fill would cross a session are refused, so the decision
                # day and the fill day are always the same day.
                day_states.setdefault(
                    open_trade["entry_day"],
                    DayState(trading_day=open_trade["entry_day"])).record_close(pnl)
                open_trade = None

        if open_trade is not None:
            continue
        if not feed.can_enter(i, cfg.session_exit_ist.hour * 60 + cfg.session_exit_ist.minute):
            rejections.add(FILL_CROSSES_SESSION, "No contiguous executable bar before the session cutoff.")
            continue

        opened = _maybe_enter(feed, i, ist, store, cfg, sel, model, risk, state,
                              equity, costs, slippage, signal_fn, plan_fn,
                              rejections, use_archive)
        if opened is not None:
            open_trade = opened
            ledger.enter(stamp, opened["entry_time"], opened["quantity"])
            state.record_fill()

    labels = [t.evidence for t in trades]
    evidence = pricing_module.breakdown(labels)
    gate = EvidenceGate(required_pct=cfg.min_observed_pct,
                        achieved_pct=evidence["observed_pct"],
                        trades=len(trades))

    result = OptionBuyResult(
        trades=trades,
        equity_curve=[round(v, 2) for v in curve],
        stats=report_module.stats(trades, curve, cfg.starting_capital),
        breakdowns=report_module.breakdowns(trades),
        evidence=evidence | {"gate": gate.to_dict()},
        rejections=rejections.to_dict(),
        assumptions=_assumptions(cfg, sel, model, costs, slippage),
        dataset=(dataset or {}) | {"reproducibility": provenance(candles,
            {"strategy": cfg.to_dict(), "selection": sel.to_dict(), "model": model.to_dict(),
             "risk": initial_risk, "execution": describe(costs, slippage),
             "option_data": store.fingerprint()}, signal_fn),
            "positions": ledger.finish(trades, cfg.starting_capital, equity)},
        coverage=coverage or {},
        limitations=_limitations(cfg, evidence),
    )
    if not gate.ok:
        result.refused = gate.refusal()
    return result


# --------------------------------------------------------------------------
# entry
# --------------------------------------------------------------------------

def _maybe_enter(feed, i, ist, store, cfg, sel, model, risk, state, equity,
                 costs, slippage, signal_fn, plan_fn, rejections, use_archive):
    """One bar's worth of "should this become a trade?"."""
    window = feed.view(i)
    try:
        signal = signal_fn(window)
    except Exception as exc:                    # noqa: BLE001
        rejections.add(PLAN_FAILED, f"signal engine failed at {ist}: {exc}")
        return None

    if signal.action == "HOLD" or signal.stop_loss is None or signal.target is None:
        rejections.add(HOLD_SIGNAL, f"{ist.isoformat()} — {signal.action}")
        return None

    # The two-layer decision, rebuilt from the prefix alone. `plan.build` is
    # causal by construction, which is what makes replaying it legitimate.
    try:
        built = plan_fn(window)
    except Exception as exc:                    # noqa: BLE001
        rejections.add(PLAN_FAILED, f"plan failed at {ist}: {exc}")
        return None

    bias = built.bias["label"]
    entry_state = built.entry["state"]

    if cfg.require_entry_states and entry_state not in cfg.require_entry_states:
        rejections.add(ENTRY_STATE_BLOCKED,
                       f"{ist.isoformat()} — entry state {entry_state}, this "
                       f"run requires {', '.join(cfg.require_entry_states)}")
        return None
    if cfg.require_bias_agreement and not _bias_agrees(bias, signal.action):
        rejections.add(BIAS_DISAGREES,
                       f"{ist.isoformat()} — {signal.action} against a "
                       f"{bias} bias")
        return None

    # The existing entry convention, unchanged: filled at the next bar's
    # open, which is the earliest price a decision on a closed bar could
    # have been executed at.
    index_entry = feed.next_open(i)
    fill_stamp = feed.next_timestamp(i)
    fill_ist = fill_stamp.tz_convert("Asia/Kolkata").to_pydatetime()

    # A decision on the last bar of a session would otherwise fill at the
    # next morning's open — seventeen hours later, through an overnight gap,
    # against levels computed from a bar that is no longer the last one. The
    # index engine tolerates that because it holds index points; an option
    # buyer also pays a night of decay for a signal nobody could have acted
    # on. Skipped, and counted.
    if fill_ist.date() != ist.date():
        rejections.add(
            FILL_CROSSES_SESSION,
            f"{ist.isoformat()} is the last bar of its session; the fill "
            f"would land at {fill_ist.isoformat()}, after an overnight gap "
            "the signal's levels know nothing about")
        return None

    # A gap that opens past the stop is not a trade with tiny risk — its
    # premise is already invalid. Skipped rather than sized.
    if signal.action == "BUY" and index_entry <= signal.stop_loss:
        rejections.add(GAP_PAST_STOP,
                       f"{fill_ist.isoformat()} opened at {index_entry:.2f}, "
                       f"already past the {signal.stop_loss:.2f} stop")
        return None
    if signal.action == "SELL" and index_entry >= signal.stop_loss:
        rejections.add(GAP_PAST_STOP,
                       f"{fill_ist.isoformat()} opened at {index_entry:.2f}, "
                       f"already past the {signal.stop_loss:.2f} stop")
        return None

    chosen, rejected = contract_module.select(
        action=signal.action, spot=float(feed.bar(i)["close"]), moment=ist, store=store,
        config=sel, use_archive=use_archive, iv=model.iv)
    if chosen is None:
        rejections.add(rejected.code, rejected.detail)
        return None

    # Two tenors, and conflating them manufactures risk out of decay.
    #
    #   `hold_years`  — from the fill. The position is held from the moment
    #                   it opens, so this is the decay baseline.
    #   `quote_years` — from the decision bar, which is when an observed
    #                   quote was printed.
    #
    # The sizing projection has to use whichever of the two the entry
    # premium itself belongs to. Projecting the stop at the fill tenor while
    # the entry price came from a bucket five minutes earlier makes the
    # difference between them five minutes of time value — and a one-point
    # stop then reports a ten-rupee risk that is entirely decay.
    quote_years = option_pricing.years_to_expiry(ist, chosen.expiry)
    hold_years = option_pricing.years_to_expiry(fill_ist, chosen.expiry)
    years = hold_years
    if years <= 0:
        rejections.add(contract_module.EXPIRY_TOO_NEAR,
                       f"{chosen.key.label()} has already expired at {ist}")
        return None

    try:
        quote = pricing_module.quote(
            store, chosen.key, ist, spot=index_entry, years=years,
            policy=cfg.pricing_policy, model=model)
    except pricing_module.UnpriceableContract as exc:
        rejections.add(UNPRICEABLE, exc.reason)
        return None

    fill = buy_fill(quote.premium, slippage, bid=quote.bid, ask=quote.ask)
    premium_entry = fill.filled
    # The index fills at the next bar's open — the existing convention, kept.
    # The premium cannot: the archive holds a folded bucket, not a tick, so
    # the last quote available at the decision bar is the most recent price
    # that provably existed before the fill. The pairing is therefore not
    # simultaneous, and it is named rather than left to be discovered.
    entry_basis = ("decision_bar_quote_observed" if not quote.modelled
                   else "next_open_modelled")
    if premium_entry < pricing_module.MIN_TRADABLE_PREMIUM:
        rejections.add(contract_module.PREMIUM_TOO_LOW,
                       f"{chosen.key.label()} filled at {premium_entry:.2f}")
        return None

    # Sizing is a counterfactual and therefore always modelled: no archive
    # stores the premium at a level the index never reached. The contract's
    # own stored IV is preferred over the constant, so the projection is at
    # least anchored to the volatility the market was pricing.
    sizing_iv = _sane_iv(quote.iv_used) or model.iv
    projection_years = hold_years if quote.modelled else quote_years
    premium_stop = option_pricing.price(
        signal.stop_loss, chosen.strike, projection_years, sizing_iv,
        model.rate, kind=chosen.option_type)
    premium_target = option_pricing.price(
        signal.target, chosen.strike, projection_years, sizing_iv,
        model.rate, kind=chosen.option_type)
    premium_risk = premium_entry - premium_stop

    if premium_risk < premium_entry * MIN_PREMIUM_RISK_FRACTION:
        rejections.add(
            NO_DEFINED_RISK,
            f"{chosen.key.label()} at {premium_entry:.2f} is worth "
            f"{premium_stop:.2f} at the stop — a {premium_risk:.2f} risk, too "
            "small a fraction of the premium to size against")
        return None

    risk.capital = equity
    decision = evaluate(
        config=risk, state=state,
        entry=premium_entry,
        stop_loss=premium_entry - premium_risk,
        target=premium_entry + (premium_target - premium_entry),
        unit_cost=premium_entry)
    if not decision.approved:
        rejections.add(RISK_VETO, f"{fill_ist.isoformat()} — "
                                  + "; ".join(decision.reasons))
        return None

    return {
        "signal": signal, "selection": chosen, "quote": quote,
        "entry_reference": fill.requested, "entry_friction": fill.slippage,
        "timing": {"bar_open_time": feed.timestamp(i).isoformat(), "bar_close_time": feed.close_time(i).isoformat(), "signal_time": ist.isoformat(), "earliest_execution_time": fill_stamp.isoformat()},
        "entry_basis": entry_basis,
        "direction": signal.action,
        "index_entry": index_entry,
        "index_stop": signal.stop_loss, "index_target": signal.target,
        "premium_entry": premium_entry,
        "premium_stop": premium_stop, "premium_target": premium_target,
        "premium_risk": premium_risk,
        "entry_years": years, "sizing_iv": sizing_iv,
        "quantity": decision.quantity, "lots": decision.lots,
        "risk_amount": decision.risk_amount,
        "risk": decision.to_dict(),
        "entry_index": i + 1,
        "entry_time": fill_stamp.isoformat(),
        "entry_day": fill_ist.date(),
        "entry_session": fill_ist.date(),
        "bias": built.bias["label"],
        "entry_state": built.entry["state"],
        "regime_day": built.entry.get("regime_day"),
        "regime_hour": built.entry.get("regime_hour"),
        "confidence": signal.confidence,
        "checks": {c.name: round(c.contribution, 4) for c in signal.checks},
    }


# --------------------------------------------------------------------------
# exit
# --------------------------------------------------------------------------

def _exit_trigger(trade, bar, ist, i, cfg):
    """Which rule closes this trade on this bar, if any.

    Order matters and is deliberately pessimistic. Expiry first because an
    expired contract cannot be held whatever else happened; then the stop,
    because when one bar touches both the stop and the target there is no
    way to know which came first and assuming the good one is how a backtest
    flatters itself.
    """
    if ist >= trade["selection"].expiry:
        return float(bar["close"]), EXPIRY, None

    if not cfg.hold_overnight and ist.date() != trade["entry_session"]:
        return float(bar["open"]), SESSION_BOUNDARY, None

    long_index = trade["direction"] == "BUY"
    stop, target = trade["index_stop"], trade["index_target"]

    hit_stop = bar["low"] <= stop if long_index else bar["high"] >= stop
    hit_target = bar["high"] >= target if long_index else bar["low"] <= target

    if hit_stop:
        fill = min(stop, float(bar["open"])) if long_index else max(stop, float(bar["open"]))
        return fill, ("stop_gap" if fill != stop else STOP), stop
    if hit_target:
        return target, TARGET, target
    if i - trade["entry_index"] >= cfg.max_bars_in_trade:
        return float(bar["close"]), TIME, None
    if ist.time() >= cfg.session_exit_ist:
        return float(bar["close"]), SESSION_END, None
    return None, "", None


def _maybe_exit(trade, feed, i, bar, ist, store, cfg, sel, model, costs,
                slippage):
    """Close the trade if a rule fires, and price the exit honestly."""
    index_exit, reason, trigger = _exit_trigger(trade, bar, ist, i, cfg)
    if index_exit is None and (i == len(feed) - 1 or (not cfg.hold_overnight and not feed.can_enter(i, cfg.session_exit_ist.hour * 60 + cfg.session_exit_ist.minute))):
        index_exit = float(bar["close"])
        reason = "end_of_data" if i == len(feed)-1 else "session_or_data_boundary"
    if index_exit is None:
        return None

    key = trade["selection"].key
    years = option_pricing.years_to_expiry(ist, trade["selection"].expiry)

    try:
        quote = pricing_module.quote(
            store, key, ist, spot=index_exit, years=years,
            policy=cfg.pricing_policy, model=model)
    except pricing_module.UnpriceableContract:
        # A trade that cannot be exited on evidence still has to be exited —
        # holding it because the collector missed a bar would be a position
        # kept open by a data gap. Priced by the model and labelled as such,
        # which makes the whole trade MIXED rather than quietly OBSERVED.
        quote = pricing_module.modelled_quote(index_exit, key, years, model)

    if quote.modelled:
        # The model prices the level the rule fired at.
        exit_basis = "trigger_level" if trigger is not None else "bar_close"
    else:
        # The archive prices the bar, not the level. Recording the bar's
        # close as the index exit keeps the premium and the index reference
        # describing the same instant instead of two different ones.
        exit_basis = "bar_close_observed"
        index_exit = float(bar["close"])

    fill = sell_fill(quote.premium, slippage, bid=quote.bid, ask=quote.ask)
    premium_exit = fill.filled
    quantity = trade["quantity"]

    gross = (premium_exit - trade["premium_entry"]) * quantity
    charges = costs.round_trip(buy_price=trade["premium_entry"],
                               sell_price=premium_exit, quantity=quantity)
    pnl = gross - charges.total

    # The decay bill: what the passage of time cost, holding the index level
    # and the volatility fixed. Both sides are modelled *at the same spot and
    # the same IV*, so the only difference between them is the clock.
    #
    # Comparing a modelled "no decay" price against the observed exit instead
    # would fold model-versus-market error into the same number, and on an
    # observed run that error is the larger of the two. A decay figure that
    # silently contains it would send somebody tuning the wrong thing.
    frozen = option_pricing.price(
        index_exit, key.strike, trade["entry_years"], trade["sizing_iv"],
        model.rate, kind=key.option_type)
    decayed = option_pricing.price(
        index_exit, key.strike, years, trade["sizing_iv"],
        model.rate, kind=key.option_type)
    decay_cost = round((frozen - decayed) * quantity, 2)

    risk_amount = trade["premium_risk"] * quantity
    evidence = pricing_module.combine(trade["quote"].evidence, quote.evidence)
    chosen = trade["selection"]

    built = OptionTrade(
        strategy=STRATEGY_NAME,
        underlying=store.underlying,
        option_type=key.option_type,
        strike=key.strike,
        expiry=key.expiry.isoformat(),
        contract=key.label(),
        entry_time=trade["entry_time"],
        exit_time=feed.close_time(i).isoformat(),
        direction=trade["direction"],
        index_entry=round(trade["index_entry"], 2),
        index_stop=round(trade["index_stop"], 2),
        index_target=round(trade["index_target"], 2),
        index_exit=round(index_exit, 2),
        trigger_level=round(trigger, 2) if trigger is not None else None,
        premium_entry=round(trade["premium_entry"], 2),
        premium_stop=round(trade["premium_stop"], 2),
        premium_target=round(trade["premium_target"], 2),
        premium_exit=round(premium_exit, 2),
        lots=trade["lots"], quantity=quantity,
        gross_pnl=round((fill.requested - trade["entry_reference"]) * quantity, 2),
        execution_friction=round((fill.slippage + trade["entry_friction"]) * quantity, 2),
        fees=round(charges.total, 2), timing=trade["timing"],
        costs=charges.to_dict(),
        pnl=round(pnl, 2),
        r_multiple=round(pnl / risk_amount, 3) if risk_amount else 0.0,
        risk_amount=round(risk_amount, 2),
        decay_cost=decay_cost,
        exit_reason=reason,
        entry_basis=trade["entry_basis"],
        exit_basis=exit_basis,
        bars_held=i - trade["entry_index"] + 1,
        confidence=trade["confidence"],
        bias=trade["bias"], entry_state=trade["entry_state"],
        regime_day=trade["regime_day"], regime_hour=trade["regime_hour"],
        days_to_expiry=round(chosen.days_to_expiry, 2),
        moneyness=chosen.moneyness,
        checks=trade["checks"],
        evidence=evidence,
        entry_evidence=trade["quote"].evidence,
        exit_evidence=quote.evidence,
        entry_quote=trade["quote"].to_dict(),
        exit_quote=quote.to_dict(),
        sizing_basis="modelled_projection",
        sizing_iv=round(trade["sizing_iv"], 4),
        selection=chosen.to_dict(),
        risk=trade["risk"],
    )
    return built, pnl


# --------------------------------------------------------------------------
# the run's own account of itself
# --------------------------------------------------------------------------

def _assumptions(cfg, sel, model, costs, slippage) -> dict:
    return {
        "strategy": STRATEGY_NAME,
        "version": STRATEGY_VERSION,
        "run": cfg.to_dict(),
        "selection": sel.to_dict(),
        "model": model.to_dict(),
        **describe(costs, slippage),
    }


def _limitations(cfg, evidence: dict) -> list[str]:
    """What this result cannot tell you. Always present, never a footnote."""
    out = [
        "Option premiums are stored at bucket resolution, not as a tick tape. "
        "An OBSERVED entry therefore pays the last quote available at the "
        "decision bar while the index fills at the next bar's open, and an "
        "OBSERVED exit fills at the close of the bar that triggered rather "
        "than at the trigger level. Per trade, `entry_basis` and `exit_basis` "
        "say which pairing was used.",
        "Position sizing is always modelled: no archive holds the premium at "
        "a level the index never reached, so the stop premium is projected "
        "with Black-Scholes.",
        "When one bar touches both the stop and the target, the stop is "
        "assumed to have filled first.",
        "An open position is always closable. If the archive has no quote "
        "at the exit bar the premium is modelled even under observed_only, "
        "because holding a trade on a collector outage would be a position "
        "kept open by a data gap. Those trades are labelled MIXED.",
        "`decay_cost` is a modelled attribution, not a measured one: both "
        "sides of it are Black-Scholes at the same index level and the same "
        "IV, differing only in time remaining. It says what the clock cost, "
        "not what the trade lost.",
    ]
    if cfg.pricing_policy == MODELLED_ONLY:
        out.append(
            "Every premium in this run is Black-Scholes at a constant IV. "
            "Nothing here is evidence about traded prices.")
    elif evidence.get("counts", {}).get(MODELLED):
        out.append(
            f"{evidence['pct'].get(MODELLED, 0)}% of trades were priced by the "
            "model rather than from the archive. Read the evidence breakdown "
            "before quoting the headline number.")
    if evidence.get("counts", {}).get(pricing_module.SNAPSHOT_DERIVED):
        out.append(
            "SNAPSHOT_DERIVED fills are real last-traded prices folded from "
            "polls. The close is a price that printed; the bar's high and low "
            "understate the true range.")
    return out
