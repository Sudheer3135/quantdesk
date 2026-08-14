"""Risk management.

This module has veto power. The signal engine can only *propose* a trade;
nothing reaches a broker unless `evaluate` returns approved=True.

Defaults follow the rulebook:
  - risk 1% of capital per trade
  - at most 2 trades a day
  - minimum reward:risk of 1:2
  - stop the day after 2 losses or a 3% drawdown
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from math import floor


@dataclass
class RiskConfig:
    capital: float
    risk_per_trade_pct: float = 1.0
    max_trades_per_day: int = 2
    min_risk_reward: float = 2.0
    max_daily_loss_pct: float = 3.0
    max_consecutive_losses: int = 2
    max_open_positions: int = 1
    lot_size: int = 75          # NIFTY lot size — verify with the exchange circular
    kill_switch: bool = False   # flip to True to block all new entries

    # An option buyer can lose the entire premium. A stop protects you only
    # if it fills — a gap straight through it does not. So cap how much
    # capital may sit in open premium regardless of what the stop implies.
    max_capital_deployed_pct: float = 20.0


@dataclass
class DayState:
    trading_day: date
    trades_taken: int = 0
    realised_pnl: float = 0.0
    consecutive_losses: int = 0
    open_positions: int = 0

    def record_fill(self) -> None:
        self.trades_taken += 1
        self.open_positions += 1

    def record_close(self, pnl: float) -> None:
        self.realised_pnl += pnl
        self.open_positions = max(0, self.open_positions - 1)
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0


@dataclass
class RiskDecision:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    quantity: int = 0
    lots: int = 0
    risk_amount: float = 0.0
    risk_per_unit: float = 0.0
    risk_reward: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate(
    *,
    config: RiskConfig,
    state: DayState,
    entry: float,
    stop_loss: float,
    target: float,
    instrument: str = "option",
    unit_cost: float | None = None,
) -> RiskDecision:
    """Decide whether this trade may be taken, and for how many units."""
    reasons: list[str] = []

    if config.kill_switch:
        return RiskDecision(False, ["Kill switch is on — no new entries."])

    if state.trades_taken >= config.max_trades_per_day:
        reasons.append(f"Daily trade cap reached ({config.max_trades_per_day}).")

    if state.open_positions >= config.max_open_positions:
        reasons.append(f"Already holding {state.open_positions} position(s).")

    if state.consecutive_losses >= config.max_consecutive_losses:
        reasons.append(f"{state.consecutive_losses} losses in a row — done for the day.")

    max_daily_loss = -config.capital * config.max_daily_loss_pct / 100
    if state.realised_pnl <= max_daily_loss:
        reasons.append(f"Daily loss limit hit ({state.realised_pnl:.0f}).")

    size_note: str | None = None
    risk_per_unit = abs(entry - stop_loss)
    reward_per_unit = abs(target - entry)
    if risk_per_unit <= 0:
        reasons.append("Stop loss equals entry — no defined risk.")
        return RiskDecision(False, reasons)

    rr = reward_per_unit / risk_per_unit
    # Compare with a tolerance. Entry, stop and target are rounded to two
    # decimals upstream, so an intended 1:2 often lands at 1.9999999 and a
    # bare `<` would reject a trade that meets the rule exactly.
    if rr < config.min_risk_reward - 1e-6:
        reasons.append(
            f"Reward:risk is 1:{rr:.2f}, below the "
            f"1:{config.min_risk_reward:.1f} floor."
        )

    risk_amount = config.capital * config.risk_per_trade_pct / 100
    raw_units = risk_amount / risk_per_unit

    if instrument == "option":
        lots = floor(raw_units / config.lot_size)
        quantity = lots * config.lot_size
        if lots < 1:
            # Say what would actually fix it. One lot at this stop distance
            # risks a fixed amount; that number divided by the risk
            # percentage is the capital the rule requires.
            one_lot_risk = risk_per_unit * config.lot_size
            needed = one_lot_risk / (config.risk_per_trade_pct / 100)
            reasons.append(
                f"One lot ({config.lot_size}) at a {risk_per_unit:.2f} stop risks "
                f"{one_lot_risk:.0f}, which is more than {config.risk_per_trade_pct}% "
                f"of {config.capital:.0f}. This rule needs about {needed:.0f} capital, "
                f"or a tighter stop."
            )
    else:
        lots = 0
        quantity = floor(raw_units)
        if quantity < 1:
            reasons.append("Position size rounds down to zero units.")

    # `unit_cost` is what one unit actually costs to buy — the option
    # premium. Stop-based sizing assumes the stop fills; this does not.
    if unit_cost and quantity:
        ceiling = config.capital * config.max_capital_deployed_pct / 100
        outlay = unit_cost * quantity
        if outlay > ceiling:
            affordable = int(ceiling / unit_cost)
            if instrument == "option":
                lots = affordable // config.lot_size
                quantity = lots * config.lot_size
            else:
                quantity = affordable
            if quantity < (config.lot_size if instrument == "option" else 1):
                reasons.append(
                    f"One lot costs {unit_cost * config.lot_size:.0f}, over the "
                    f"{config.max_capital_deployed_pct}% deployment cap "
                    f"({ceiling:.0f})."
                )
            else:
                size_note = (
                    f"Size cut to {quantity} so premium outlay stays under "
                    f"{config.max_capital_deployed_pct}% of capital."
                )
                risk_amount = risk_per_unit * quantity

    if reasons:
        return RiskDecision(False, reasons, quantity, lots,
                            risk_amount, risk_per_unit, round(rr, 2))

    return RiskDecision(
        approved=True,
        reasons=[
            f"Risking {config.risk_per_trade_pct}% ({risk_amount:.0f}) at "
            f"{risk_per_unit:.2f} per unit.",
            f"Reward:risk 1:{rr:.2f}.",
            f"Trade {state.trades_taken + 1} of {config.max_trades_per_day} today.",
        ] + ([size_note] if size_note else []),
        quantity=quantity,
        lots=lots,
        risk_amount=risk_amount,
        risk_per_unit=risk_per_unit,
        risk_reward=round(rr, 2),
    )
