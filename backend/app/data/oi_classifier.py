"""Telling a busy option market apart from a broken data feed.

The first version of the open-interest check compared each bar's OI change
against that contract's own median change and flagged anything ten times
larger. On real NSE data it produced 229 warnings, essentially all of them
at at-the-money strikes on the day before expiry, with one to three *million*
contracts of volume behind them. That is not corruption; that is the market
doing the most normal thing it does. Meanwhile the median for a far
out-of-the-money strike is near zero, so the same rule flagged trivial
noise there.

Raising the threshold would only move the line. The real problem was that
the rule had no idea what open interest *is*.

**The invariant this module is built on.** Open interest is a stock, not a
flow. It changes only when contracts are opened or closed, and every opening
or closing is a trade. So across any interval:

    |ΔOI|  ≤  contracts traded in that interval

An OI move larger than the volume that could have produced it is not a big
move. It is arithmetically impossible, and no threshold is needed to say so.

NSE gives us both halves. `openInterest` is a level, and `totalTradedVolume`
is cumulative for the session — so the interval volume is simply
`volume[t] - volume[t-1]`, and the comparison is available on data already
stored. That is what separates a genuine anomaly from heavy trading, and it
is a physical test rather than a tuned one.

Three verdicts, because "suspicious" and "unknown" are different answers:

    ANOMALY               the numbers contradict each other
    HIGH_ACTIVITY         a large move, fully supported by trading
    INSUFFICIENT_EVIDENCE too little history or liquidity to judge

A check that cannot say "I don't know" ends up guessing, and its guesses
are indistinguishable from its findings.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

ANOMALY = "ANOMALY"
HIGH_ACTIVITY = "HIGH_ACTIVITY"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
NORMAL = None

# Below this many bars for a contract, its own history cannot say what is
# typical for it, so nothing about the size of a move is judgeable.
MIN_OBSERVATIONS = 5

# Volume and OI are sampled a moment apart, and NSE's OI figure lags its
# volume figure slightly. A move exceeding traded volume by a hair is that
# skew, not corruption; exceeding it by a third is not explainable that way.
VOLUME_TOLERANCE = 1.33

# One NIFTY lot. Moves smaller than this are below the resolution at which
# any of this can be argued about, whichever direction they break.
NEGLIGIBLE = 75

# How far past a contract's own typical move counts as "large" and therefore
# worth reporting as activity. This only splits NORMAL from HIGH_ACTIVITY —
# it never decides whether something is an anomaly, so mis-tuning it costs
# noise, not a false corruption report.
LARGE_MOVE_MULTIPLE = 10.0


@dataclass
class OIChange:
    """One consecutive pair of observations for a single contract."""
    strike: float
    option_type: str
    timestamp: str
    oi: float
    previous_oi: float
    delta_oi: float
    interval_volume: float | None      # None when it cannot be derived
    typical_move: float                # this contract's own median |ΔOI|
    observations: int
    same_session: bool
    expiring: bool                     # this bar sits on the expiry date
    moneyness_pct: float | None = None  # (strike - spot) / spot * 100

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Verdict:
    classification: str | None
    reason: str

    @property
    def reportable(self) -> bool:
        return self.classification is not None


def classify(change: OIChange) -> Verdict:
    """Judge one OI change. See the module docstring for the reasoning.

    Order matters. Contradictions are checked before magnitude, because a
    figure that cannot be true should never be excused as "a big day", and
    evidence is checked before size, because a move can only be called
    unusual relative to a history that exists.
    """
    d_oi = change.delta_oi
    magnitude = abs(d_oi)

    # ---- 1. contradictions, in order of how certain they are ----------

    if change.oi < 0 or change.previous_oi < 0:
        return Verdict(ANOMALY, "open interest is negative, which cannot occur")

    # Overnight is not an interval we can reason about: OI legitimately
    # resets its daily change, positions expire, and cumulative volume
    # starts again from zero. Nothing here applies across a session break.
    if not change.same_session:
        return Verdict(NORMAL, "spans a session boundary; not comparable")

    volume = change.interval_volume
    if volume is not None and volume < 0:
        return Verdict(
            ANOMALY,
            f"cumulative volume fell by {abs(volume):,.0f} within a session, "
            "which cannot happen — the feed restated or reordered a bar")

    # ---- 2. is the move big enough to reason about? -------------------

    if magnitude == 0:
        return Verdict(NORMAL, "open interest did not change")

    if magnitude < NEGLIGIBLE:
        return Verdict(
            INSUFFICIENT_EVIDENCE,
            f"a move of {magnitude:,.0f} contracts is under one lot — below "
            "the resolution at which this can be argued either way")

    # ---- 3. the physical test ------------------------------------------
    #
    # This runs before the history check on purpose. Whether a move is
    # *unusual* depends on what is usual for the contract, so it needs a
    # history. Whether a move is *possible* does not: open interest cannot
    # exceed the contracts traded no matter how new the contract is, and
    # gating that behind an observation count would excuse contradictions
    # in exactly the young, thinly-observed series where a broken feed is
    # most likely to show up first.

    if volume is None:
        return Verdict(
            INSUFFICIENT_EVIDENCE,
            "no interval volume available, so the move cannot be checked "
            "against the trading that would have had to produce it")

    if volume <= 0:
        return Verdict(
            ANOMALY,
            f"open interest moved by {magnitude:,.0f} contracts with no "
            "trading in the interval — every change in open interest "
            "requires a trade")

    if magnitude > volume * VOLUME_TOLERANCE:
        return Verdict(
            ANOMALY,
            f"open interest moved by {magnitude:,.0f} contracts on "
            f"{volume:,.0f} traded — a change larger than the volume that "
            "could have produced it")

    # OI vanishing mid-session is checked *after* the volume test, because a
    # contract genuinely closed out by heavy trading will pass that test and
    # is not corrupt. Reaching here means the drop was supported by volume.
    if change.oi == 0 and change.previous_oi > 0 and not change.expiring:
        return Verdict(
            ANOMALY,
            f"open interest fell from {change.previous_oi:,.0f} to exactly "
            "zero mid-session on a contract that is not expiring")

    # ---- 4. supported by volume, so how notable is it? ----------------
    #
    # Everything from here is a judgement about size, which is the part
    # that genuinely needs the contract's own history behind it.

    if change.observations < MIN_OBSERVATIONS:
        return Verdict(
            INSUFFICIENT_EVIDENCE,
            f"only {change.observations} observation(s) for this contract; "
            f"{MIN_OBSERVATIONS} are needed before a move can be called "
            "unusual. The move is consistent with the volume traded.")

    if change.typical_move > 0 and magnitude > change.typical_move * LARGE_MOVE_MULTIPLE:
        where = _describe_context(change)
        return Verdict(
            HIGH_ACTIVITY,
            f"open interest moved {magnitude:,.0f} contracts on "
            f"{volume:,.0f} traded{where} — a large move, fully supported "
            "by trading")

    return Verdict(NORMAL, "within the contract's normal range and supported by volume")


def _describe_context(change: OIChange) -> str:
    """Why a large move here is unsurprising, when the data can say so.

    Not used to decide anything — the volume test already did that. It is
    here so a reader of the report can see at a glance that the flagged
    activity sat at the money on expiry day rather than somewhere odd.
    """
    parts = []
    if change.moneyness_pct is not None and abs(change.moneyness_pct) <= 1.0:
        parts.append("at the money")
    if change.expiring:
        parts.append("on expiry day")
    return f" ({', '.join(parts)})" if parts else ""


def summarise(verdicts: list[tuple[OIChange, Verdict]]) -> dict[str, int]:
    counts = {ANOMALY: 0, HIGH_ACTIVITY: 0, INSUFFICIENT_EVIDENCE: 0}
    for _, verdict in verdicts:
        if verdict.classification in counts:
            counts[verdict.classification] += 1
    return counts
