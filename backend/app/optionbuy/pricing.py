"""Where every simulated premium came from, on the record.

The platform already refuses to print a number it cannot justify. This is
that rule applied to the one number an option backtest is entirely made of.

Three labels, and a fill carries exactly one:

  ``OBSERVED``          A real tape. The contract traded at this price, and
                        the bar's high and low are the session's true
                        extremes.
  ``SNAPSHOT_DERIVED``  A real *price*, sampled. NSE's public chain is a
                        snapshot rather than a tape, so the collector folds
                        polls into buckets: the close is a last-traded price
                        that genuinely printed, but the range is understated
                        because it is built from samples.
  ``MODELLED``          Nothing was observed. Black-Scholes at an assumed
                        constant IV — a calculation, not a market.

The distinction that matters most is the second against the third. Both are
worse than a tape, but a snapshot-derived close is *evidence* and a modelled
premium is *an assumption*, and a result that averages them into one number
called "premium" has quietly converted assumptions into evidence. So they
are never blended: a leg is one label, a trade names the label on each leg,
and the report counts them separately. A trade whose legs disagree is
labelled ``MIXED`` rather than being filed under the flattering half.

`policy` decides what may be used at all. `OBSERVED_ONLY` refuses to open a
trade it cannot price from the archive — the right setting once enough
history exists, and the reason the rejection is counted rather than skipped.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..analytics import option_pricing
from .chain import ChainStore, ContractKey, OptionBar

OBSERVED = "OBSERVED"
SNAPSHOT_DERIVED = "SNAPSHOT_DERIVED"
MODELLED = "MODELLED"
MIXED = "MIXED"

# Best evidence first. Used for ordering report rows, never for averaging —
# there is no arithmetic mean of "a tape" and "a guess".
EVIDENCE_ORDER = (OBSERVED, SNAPSHOT_DERIVED, MODELLED, MIXED)

OBSERVED_ONLY = "observed_only"
PREFER_OBSERVED = "prefer_observed"
MODELLED_ONLY = "modelled_only"
POLICIES = (OBSERVED_ONLY, PREFER_OBSERVED, MODELLED_ONLY)

# Evidence each policy is willing to accept, in preference order.
_ALLOWED: dict[str, tuple[str, ...]] = {
    OBSERVED_ONLY: (OBSERVED, SNAPSHOT_DERIVED),
    PREFER_OBSERVED: (OBSERVED, SNAPSHOT_DERIVED, MODELLED),
    MODELLED_ONLY: (MODELLED,),
}

# A quote at or below this is not a tradable price. Deep out-of-the-money
# weeklies print at 0.05 and are real; a stored zero is a contract with no
# market, and buying one in a backtest manufactures an infinite return.
MIN_TRADABLE_PREMIUM = 0.05


class UnpriceableContract(RuntimeError):
    """No premium this policy will accept. Never a reason to invent one.

    `ineligible` separates two refusals a caller has to count differently:
    a contract the archive cannot price at all, and one whose only stored
    quotes became available before the order could have existed. The second
    is the execution clock doing its job, not a hole in the data, and
    folding them into one rejection code would hide which it was.
    """

    def __init__(self, reason: str, *, ineligible: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.ineligible = ineligible


@dataclass
class Quote:
    """One premium, and the complete account of where it came from."""
    premium: float
    evidence: str
    basis: str
    reference: dict | None = None      # the archive row, when there was one
    bid: float | None = None
    ask: float | None = None
    iv_used: float | None = None
    open_interest: float | None = None
    volume: float | None = None
    bar_kind: str | None = None

    @property
    def modelled(self) -> bool:
        return self.evidence == MODELLED

    def to_dict(self) -> dict:
        return {
            "premium": round(self.premium, 2),
            "evidence": self.evidence,
            "basis": self.basis,
            "reference": self.reference,
            "iv_used": round(self.iv_used, 4) if self.iv_used is not None else None,
            "bar_kind": self.bar_kind,
        }


@dataclass
class ModelAssumptions:
    """Everything the Black-Scholes fallback assumes, stated once.

    Written into the result rather than left in the source. A modelled
    premium is only defensible alongside the inputs that produced it, and
    "we used Black-Scholes" is not those inputs.
    """
    iv: float = option_pricing.DEFAULT_IV
    rate: float = option_pricing.DEFAULT_RATE
    iv_source: str = "constant"
    note: str = (
        "Volatility is a single constant for the whole run. Real IV rises "
        "into events and collapses after them, so modelled premiums are "
        "smooth where the market was not, and every modelled trade is "
        "priced as though the volatility surface never moved."
    )
    trading_days_per_year: int = option_pricing.TRADING_DAYS_PER_YEAR
    settlement: str = "European, cash-settled at intrinsic value on expiry"

    def to_dict(self) -> dict:
        return {
            "iv": self.iv,
            "iv_source": self.iv_source,
            "rate": self.rate,
            "trading_days_per_year": self.trading_days_per_year,
            "settlement": self.settlement,
            "note": self.note,
        }


def evidence_for(bar: OptionBar) -> str:
    """Which label a stored bar earns. Its own `bar_kind` decides, not us."""
    return OBSERVED if bar.observed_tape else SNAPSHOT_DERIVED


def allowed(policy: str) -> tuple[str, ...]:
    if policy not in _ALLOWED:
        raise ValueError(f"unknown pricing policy {policy!r}; expected one of "
                         f"{', '.join(POLICIES)}")
    return _ALLOWED[policy]


def _from_bar(bar: OptionBar, moment: datetime,
              available_from: datetime | None = None) -> Quote:
    age = int((moment - bar.timestamp).total_seconds() // 60)
    freshness = "at this bar" if age <= 0 else f"{age} min old"
    if bar.observed_tape:
        basis = f"Traded price from the archive, {freshness}."
    else:
        basis = (f"Last-traded price from a folded chain snapshot, "
                 f"{freshness}, {bar.samples or 1} poll(s) in the bucket.")
    return Quote(
        premium=float(bar.close),
        evidence=evidence_for(bar),
        basis=basis,
        reference=bar.reference() | (
            {"available_from": available_from.isoformat()}
            if available_from is not None else {}),
        bid=bar.bid, ask=bar.ask, iv_used=bar.iv,
        open_interest=bar.open_interest, volume=bar.volume,
        bar_kind=bar.bar_kind,
    )


def modelled_quote(spot: float, key: ContractKey, years: float,
                   model: ModelAssumptions) -> Quote:
    """A Black-Scholes premium, labelled as one.

    Public because an open position has to be closable even when the policy
    would refuse to *open* one at this bar — see `strategy._maybe_exit`.
    Holding a trade because the collector missed a bucket would be a
    position kept open by a data gap.
    """
    premium = option_pricing.price(
        spot, key.strike, years, model.iv, model.rate, kind=key.option_type)
    if years <= 0:
        basis = (f"Settled at intrinsic value against an index level of "
                 f"{spot:.2f}. No premium was observed.")
    else:
        basis = (f"Black-Scholes at a constant {model.iv:.0%} IV — no quote "
                 f"was stored for this contract at this bar.")
    return Quote(premium=float(premium), evidence=MODELLED, basis=basis,
                 reference=None, iv_used=model.iv, bar_kind=None)


def quote(
    store: ChainStore,
    key: ContractKey,
    moment: datetime,
    *,
    spot: float,
    years: float,
    policy: str = PREFER_OBSERVED,
    model: ModelAssumptions | None = None,
    eligible_from: datetime | None = None,
) -> Quote:
    """The premium this policy is willing to use, with its label attached.

    Raises rather than returning a fallback the policy forbids. A caller
    that wants a modelled price has to ask for a policy that permits one,
    which keeps the choice at the top of the run where it is visible instead
    of buried at the fill.
    """
    model = model or ModelAssumptions()
    permitted = allowed(policy)

    if policy != MODELLED_ONLY:
        # `eligible_from` is the execution clock. A quote that became
        # available before the order could exist is not an observation this
        # fill may use, so the store refuses it and the policy below decides
        # what to do instead — exactly as it would for a contract the
        # collector never priced at all.
        bar = store.bar_at(key, moment, eligible_from=eligible_from)
        if bar is not None and bar.close >= MIN_TRADABLE_PREMIUM:
            found = _from_bar(bar, moment, store.available_from(bar))
            if found.evidence in permitted:
                return found

    if MODELLED not in permitted:
        # Was it the eligibility floor that blocked this, or is there simply
        # no usable quote? Asked by dropping the floor and looking again, so
        # the answer is the store's and not an inference from the message.
        ineligible = bool(
            eligible_from is not None
            and store.bar_at(key, moment, eligible_from=eligible_from) is None
            and store.bar_at(key, moment) is not None)
        since = (f" available at or after {eligible_from.isoformat()}"
                 if eligible_from is not None else "")
        raise UnpriceableContract(
            f"no stored quote for {key.label()} at {moment.isoformat()}"
            f"{since}, and the {policy} policy does not permit a modelled "
            "premium", ineligible=ineligible)

    return modelled_quote(spot, key, years, model)


def combine(entry: str, exit_: str) -> str:
    """The evidence label for a whole trade.

    Two legs, and they can differ: an entry priced off the archive can exit
    on a bar the collector missed. Reporting the pair as the better of the
    two would make a half-modelled trade look observed, and taking the worse
    would discard a real entry price. So a mismatch gets its own label and
    both legs stay named on the trade.
    """
    return entry if entry == exit_ else MIXED


def breakdown(labels: list[str]) -> dict:
    """Counts and shares per label, in evidence order.

    Every result carries this. A headline expectancy computed mostly from
    modelled fills is a statement about Black-Scholes, and the only way to
    know that is to be told the mix.
    """
    total = len(labels)
    counts = {name: labels.count(name) for name in EVIDENCE_ORDER}
    counts = {k: v for k, v in counts.items() if v}
    return {
        "counts": counts,
        "pct": {k: round(v / total * 100, 1) for k, v in counts.items()} if total else {},
        "total": total,
        "observed_pct": round(
            sum(v for k, v in counts.items() if k in (OBSERVED, SNAPSHOT_DERIVED))
            / total * 100, 1) if total else 0.0,
    }
