"""How much of a result is the strategy and how much is the fill.

A backtest reports one number per metric, computed at one slippage
assumption, and that number carries no indication of how hard it is leaning
on the assumption. Two strategies with identical headline expectancy are not
equally trustworthy if doubling the slippage leaves one unchanged and turns
the other negative — and nothing in either result says which is which.

So the same run is reproduced at zero, one and twice the base slippage and
the metrics are printed side by side. Zero is not a realistic market; it is
the upper bound on what the strategy could ever earn, and the distance from
it to 1x is the size of the execution bill. Twice is not a prediction
either; it is a plausible bad day, and a strategy that only works on the
good ones should say so before it is funded.

If the sign of net P&L or of expectancy changes anywhere across that range,
the run is marked `execution_sensitive`. That flag is not a verdict — plenty
of real strategies are execution sensitive and are traded anyway — but it is
the difference between knowing that and not knowing it, and it is never
hidden because the 1x column happened to look good.

Nothing here tunes anything. The multipliers are fixed, the metrics are read
off whatever the run returns, and no column is preferred to another.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace

import numpy as np

from .costs import SlippageModel

# What the overlapping signal-outcome curve's drawdown is called. Not
# `max_drawdown_pct`, and the difference is not pedantry — see
# `hypothetical_outcome_curve`.
HYPOTHETICAL_DRAWDOWN = "hypothetical_outcome_curve_drawdown_pct"

# Zero, base, double. Fixed rather than configurable-by-default so that two
# sensitivity reports are comparable without checking what each swept.
DEFAULT_MULTIPLIERS: tuple[float, ...] = (0.0, 1.0, 2.0)

# The metrics compared across the sweep. Every one of them is read from the
# run's own `stats`, so this module has no second opinion about how a profit
# factor is computed.
REPORTED = ("trades", "net_pnl", "expectancy_per_trade", "expectancy_r",
            "profit_factor")

# The drawdown, reported under whichever name the run computed it under.
# Two different things have been called "max drawdown" here and they are not
# interchangeable — see `hypothetical_outcome_curve` — so the sweep copies
# the run's own key rather than flattening both into one column that would
# then be compared across runs that meant different things by it.
DRAWDOWN_KEYS = ("max_drawdown_pct", HYPOTHETICAL_DRAWDOWN)

# What each of those numbers means. Carried with the number everywhere it
# goes: a drawdown percentage separated from its basis is a figure two
# different curves could have produced, and the two differ by more than a
# hundred percentage points on this archive.
DRAWDOWN_METADATA = ("drawdown_basis", "hypothetical_outcome_curve")

# The two whose *sign* decides sensitivity. A profit factor crossing 1.0 is
# the same event as expectancy crossing zero, so it is not counted twice.
SIGN_CRITICAL = ("net_pnl", "expectancy_per_trade", "expectancy_r")


# The fields that are execution *assumptions* and therefore scale. Named
# rather than "everything numeric" so that adding a field to SlippageModel
# forces a decision about which of the two it is.
SCALED = ("ticks", "impact_ticks", "index_pct", "estimated_spread_pct")


def scale(model: SlippageModel, multiplier: float) -> SlippageModel:
    """The same slippage model, `multiplier` times as expensive.

    Every execution *assumption* scales. A measured quantity does not, and
    `spread_fraction` is where that line used to be crossed: it was scaled
    here and clipped at 1.0, which looked like a stress on quoted execution
    and was not one. The quoted fill path never reads it — a market order
    lifts the ask and hits the bid in full — so scaling it changed the model
    object and nothing about any fill. A sweep over a book with a real bid
    and ask therefore reported three identical rows and presented them as
    evidence that execution did not matter, when what had happened was that
    the multiplier reached nothing the fill used.

    What a quoted book is stressed through is `impact_ticks`: how far a
    market order is assumed to push the touch. The spread itself stays
    exactly as quoted at every multiplier, which is both correct and what
    stops the spread being counted a second time.
    """
    if multiplier < 0:
        raise ValueError("slippage cannot be scaled by a negative number")
    return replace(model, **{name: getattr(model, name) * multiplier
                             for name in SCALED})


def moves_quoted_fills(model: SlippageModel) -> bool:
    """Would scaling this model change a fill priced off a real bid and ask?

    False when `impact_ticks` is zero, which is the default. It is not a
    fault — it means the quoted fill is the touch and nothing but the touch,
    so a sweep genuinely has no assumption to vary there. It has to be
    *said*, though, because three identical rows otherwise read as a
    finding rather than as an absence of one.
    """
    return model.impact_ticks > 0


def hypothetical_outcome_curve(pnls: Sequence[float],
                               starting_capital: float = 100_000.0) -> dict:
    """The drawdown of a curve that is not a portfolio, named so.

    The signal study replays every stored signal, including the ones that
    overlap: the agent emits a signal every five minutes, so a single move
    produces a dozen of them and this curve holds all twelve at once. No
    capital constrains it, nothing is sized against what is already open,
    and equity is allowed to go below zero and keep trading — which it does
    on this archive, to roughly minus five lakh.

    That makes figures like -389% arithmetically correct and completely
    unlike an account drawdown, which cannot pass -100% because the account
    is empty there. Clipping it to -100% would be worse than leaving it: it
    would put a portfolio-shaped number on something that is not a
    portfolio, and hide the deficit that is the whole reason to distrust it.

    So it keeps its own name, `hypothetical_outcome_curve_drawdown_pct`, and
    carries its assumptions with it. It says how bumpy the sequence of
    hypothetical outcomes was. It does not say what an account would have
    done, and nothing downstream may read it as if it did.
    """
    series = np.asarray(list(pnls), dtype=float)
    equity = float(starting_capital) + np.cumsum(
        series if series.size else np.array([0.0]))
    equity = np.concatenate(([float(starting_capital)], equity))
    peak = np.maximum.accumulate(equity)
    # Peaks are positive here (the curve starts at `starting_capital` and
    # the running maximum never falls), so the division is safe even once
    # equity itself has gone negative.
    drawdown = (equity - peak) / peak

    return {
        HYPOTHETICAL_DRAWDOWN: round(float(drawdown.min()) * 100, 2),
        "hypothetical_outcome_curve": {
            # The assumptions as flags as well as prose. Prose survives a
            # copy only if somebody copies it; a reader checking whether
            # this figure is an account drawdown wants a field to test, and
            # a projection that drops these is visibly dropping them.
            "overlapping_outcomes": True,
            "capital_constrained": False,
            "equity_can_go_negative": True,
            "marked_at_resolution": True,
            "executable_portfolio_curve": False,
            "starting_capital": float(starting_capital),
            "final_equity": round(float(equity[-1]), 2),
            "min_equity": round(float(equity.min()), 2),
            "equity_went_negative": bool(equity.min() < 0),
            "outcomes": int(series.size),
            "assumptions": [
                "Every stored signal is included, and stored signals "
                "overlap: one move produces a signal every five minutes and "
                "all of them are counted.",
                "No capital constraint. Nothing is sized against what is "
                "already open and no position limit applies.",
                "Equity may go negative and the curve keeps trading past "
                "the point an account would have been closed.",
                "Marked only when a hypothetical outcome resolves, not "
                "daily and not to market.",
                "This is NOT an executable portfolio equity curve and this "
                "figure is NOT an account-level maximum drawdown.",
            ],
        },
    }


def _sign(value) -> int | None:
    if value is None:
        return None
    return (value > 0) - (value < 0)


def sweep(run: Callable[[SlippageModel], object],
          base: SlippageModel | None = None,
          multipliers: Sequence[float] = DEFAULT_MULTIPLIERS) -> dict:
    """Reproduce a run at each multiple of its slippage and compare.

    `run` takes a `SlippageModel` and returns anything with a `stats` dict —
    which is both backtest results and anything else shaped like them. It is
    called once per multiplier and nothing else about the run changes, so
    every difference in the table below is the fill and only the fill.
    """
    base = base or SlippageModel()
    rows: list[dict] = []
    for multiplier in multipliers:
        result = run(scale(base, multiplier))
        stats = dict(getattr(result, "stats", None) or {})
        row = {"multiplier": multiplier,
               **{key: stats.get(key) for key in REPORTED}}
        # The drawdown under the run's own name. Copied rather than
        # renamed, so an overlapping hypothetical curve cannot arrive in
        # this table wearing the word "portfolio".
        # The drawdown under the run's own name, and never without what it
        # means. 2B.1 renamed the overlapping metric correctly and then
        # projected the bare percentage here, which made it ambiguous again
        # one layer down: -389% with nothing beside it reads as an account
        # drawdown, which is exactly what it is not.
        for key in DRAWDOWN_KEYS:
            if key in stats:
                row[key] = stats[key]
        for key in DRAWDOWN_METADATA:
            if key in stats:
                row[key] = stats[key]
        # Where the money went, when the run says. Total friction split into
        # what the market charged and what this run assumed keeps the reason
        # a column moved separable from the fact that it moved.
        for key in ("gross_pnl", "execution_friction", "spread_cost",
                    "impact_cost", "fees_taxes", "total_fees",
                    "gap_rejected"):
            if key in stats:
                row[key] = stats[key]
        # A run with no trades has no expectancy to compare. Saying so beats
        # a row of zeroes that reads like a flat result.
        row["measurable"] = bool(stats.get("trades"))
        rows.append(row)

    flips: list[str] = []
    for metric in SIGN_CRITICAL:
        signs = {_sign(row[metric]) for row in rows
                 if row["measurable"] and row[metric] is not None}
        signs.discard(0)
        if len(signs) > 1:
            flips.append(metric)

    return {
        "multipliers": list(multipliers),
        "base_slippage": {"ticks": base.ticks, "tick_size": base.tick_size,
                          "impact_ticks": base.impact_ticks,
                          "index_pct": base.index_pct,
                          "spread_fraction": base.spread_fraction,
                          "estimated_spread_pct": base.estimated_spread_pct,
                          "execution_model": base.execution_model,
                          "scaled_fields": list(SCALED)},
        "rows": rows,
        # Whether the multiplier reaches a fill priced off a stored bid and
        # ask at all. False with the default zero impact, and then the
        # quoted columns are identical because there was no assumption to
        # vary — not because execution was shown not to matter.
        "scales_quoted_fills": moves_quoted_fills(base),
        # True when the answer to "does this make money?" depends on the
        # slippage assumption within the swept range. Reported whether or
        # not the base case looks good, which is the entire point.
        "execution_sensitive": bool(flips),
        "sign_changes": flips,
        "interpretation": (
            "Zero slippage is an upper bound, not a scenario. A result "
            "marked execution_sensitive changes sign somewhere between no "
            "slippage and twice the assumed slippage, so its headline "
            "figure is a statement about the fill assumption as much as "
            "about the strategy. On fills priced off a stored bid and ask "
            "the quoted spread is a measurement and does not scale: the "
            "multiplier moves `impact_ticks` only, so a zero-impact model "
            "produces identical quoted rows and `scales_quoted_fills` is "
            "false."),
    }
