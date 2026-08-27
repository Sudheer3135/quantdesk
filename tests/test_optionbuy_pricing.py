"""Evidence labels: the rule that a premium says where it came from.

Three labels, and a fill carries exactly one. The distinction that matters
most is SNAPSHOT_DERIVED against MODELLED — both are worse than a tape, but
a snapshot-derived close is a price that genuinely printed and a modelled
premium is a calculation. A result that blends them has quietly converted
assumptions into evidence, and nothing in the headline number would say so.
"""
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import option_pricing
from app.optionbuy import pricing
from app.optionbuy.chain import ChainStore, ContractKey
from app.optionbuy.pricing import (
    MODELLED,
    MODELLED_ONLY,
    OBSERVED,
    OBSERVED_ONLY,
    PREFER_OBSERVED,
    SNAPSHOT_DERIVED,
    ModelAssumptions,
    UnpriceableContract,
    quote,
)
from optionbuy_fixtures import option_bars, session_stamps, sessions

DAYS = sessions(date(2025, 6, 2), 1)
EXPIRY = date(2025, 6, 10)
STAMPS = session_stamps(DAYS[0])
KEY = ContractKey(EXPIRY, 24_000.0, "CE")
SPOT = 24_000.0
YEARS = option_pricing.years_to_expiry(
    STAMPS[10], datetime(2025, 6, 10, 10, 0, tzinfo=STAMPS[10].tzinfo))


def a_store(**kwargs) -> ChainStore:
    store = ChainStore(option_bars(DAYS, EXPIRY, **kwargs), {})
    store.seek(STAMPS[-1])
    return store


def ask(store, moment=None, policy=PREFER_OBSERVED, spot=SPOT, key=KEY):
    return quote(store, key, moment or STAMPS[10], spot=spot, years=YEARS,
                 policy=policy, model=ModelAssumptions())


# ---- the three labels --------------------------------------------------

def test_a_real_tape_is_labelled_observed():
    store = a_store(bar_kind="ohlc")
    result = ask(store)
    assert result.evidence == OBSERVED
    assert "Traded price from the archive" in result.basis
    assert result.reference["bar_kind"] == "ohlc"


def test_a_folded_snapshot_is_labelled_snapshot_derived():
    """NSE publishes a snapshot, not a tape. The close is a price that
    printed; the range is built from samples and understates the truth."""
    store = a_store(bar_kind="snapshot", samples=4)
    result = ask(store)
    assert result.evidence == SNAPSHOT_DERIVED
    assert "folded chain snapshot" in result.basis
    assert "4 poll(s)" in result.basis


def test_no_stored_quote_is_labelled_modelled():
    store = ChainStore({}, {})
    store.seek(STAMPS[10])
    result = ask(store)
    assert result.evidence == MODELLED
    assert result.reference is None
    assert "Black-Scholes at a constant 13% IV" in result.basis


def test_the_stored_close_is_used_rather_than_recomputed():
    """The whole point of an observed fill. A store whose premium disagrees
    with the model must produce the stored number, or the label is a lie."""
    store = a_store()
    bars = store._bars[KEY]                              # noqa: SLF001
    store._bars[KEY] = [                                 # noqa: SLF001
        type(b)(**{**b.__dict__, "close": 999.0}) for b in bars]

    result = ask(store)
    assert result.premium == 999.0
    assert result.evidence == SNAPSHOT_DERIVED


def test_settlement_at_expiry_is_intrinsic_and_says_so():
    store = ChainStore({}, {})
    store.seek(STAMPS[10])
    result = quote(store, KEY, STAMPS[10], spot=24_180.0, years=0.0,
                   policy=PREFER_OBSERVED, model=ModelAssumptions())
    assert result.premium == pytest.approx(180.0)
    assert result.evidence == MODELLED
    assert "intrinsic value" in result.basis


# ---- policies ----------------------------------------------------------

def test_observed_only_refuses_rather_than_modelling():
    store = ChainStore({}, {})
    store.seek(STAMPS[10])
    with pytest.raises(UnpriceableContract, match="does not permit a modelled"):
        ask(store, policy=OBSERVED_ONLY)


def test_observed_only_accepts_a_snapshot_derived_price():
    """A folded snapshot is evidence. Requiring a tape nobody publishes
    would make the strict policy unusable rather than strict."""
    store = a_store(bar_kind="snapshot")
    assert ask(store, policy=OBSERVED_ONLY).evidence == SNAPSHOT_DERIVED


def test_modelled_only_never_reads_the_archive():
    """Not merely "prefers the model" — a modelled run must be reproducible
    without the archive at all, or its results move when the archive grows."""
    store = a_store()
    bars = store._bars[KEY]                              # noqa: SLF001
    store._bars[KEY] = [                                 # noqa: SLF001
        type(b)(**{**b.__dict__, "close": 999.0}) for b in bars]

    result = ask(store, policy=MODELLED_ONLY)
    assert result.evidence == MODELLED
    assert result.premium != 999.0
    assert result.reference is None


def test_an_unknown_policy_is_rejected_rather_than_defaulted():
    with pytest.raises(ValueError, match="unknown pricing policy"):
        pricing.allowed("whatever_is_cheapest")


# ---- what does not count as a quote -----------------------------------

def test_a_zero_premium_is_not_a_tradable_quote():
    """A stored zero is a contract with no market. Buying one manufactures
    an infinite return out of a row that means "nobody traded this"."""
    store = a_store()
    store._bars[KEY] = [                                 # noqa: SLF001
        type(b)(**{**b.__dict__, "close": 0.0}) for b in store._bars[KEY]]

    assert ask(store).evidence == MODELLED
    with pytest.raises(UnpriceableContract):
        ask(store, policy=OBSERVED_ONLY)


def test_a_stale_quote_falls_through_to_the_model_under_prefer_observed():
    store = a_store()
    store._bars[KEY] = [b for b in store._bars[KEY]      # noqa: SLF001
                        if b.timestamp <= STAMPS[2]]
    store._stamps[KEY] = [b.timestamp for b in store._bars[KEY]]   # noqa: SLF001

    result = ask(store, moment=STAMPS[20])
    assert result.evidence == MODELLED


def test_a_stale_quote_refuses_the_trade_under_observed_only():
    store = a_store()
    store._bars[KEY] = [b for b in store._bars[KEY]      # noqa: SLF001
                        if b.timestamp <= STAMPS[2]]
    store._stamps[KEY] = [b.timestamp for b in store._bars[KEY]]   # noqa: SLF001

    with pytest.raises(UnpriceableContract, match="no stored quote"):
        ask(store, moment=STAMPS[20], policy=OBSERVED_ONLY)


# ---- never silently mixed ----------------------------------------------

def test_two_legs_with_the_same_label_keep_it():
    assert pricing.combine(OBSERVED, OBSERVED) == OBSERVED
    assert pricing.combine(MODELLED, MODELLED) == MODELLED


def test_two_legs_that_disagree_become_mixed_rather_than_the_better_one():
    """Taking the better label would file a half-modelled trade under
    OBSERVED; taking the worse would throw away a real entry price."""
    assert pricing.combine(OBSERVED, MODELLED) == pricing.MIXED
    assert pricing.combine(MODELLED, SNAPSHOT_DERIVED) == pricing.MIXED
    assert pricing.combine(OBSERVED, SNAPSHOT_DERIVED) == pricing.MIXED


def test_the_breakdown_counts_every_label_and_the_observed_share():
    labels = [OBSERVED, SNAPSHOT_DERIVED, SNAPSHOT_DERIVED, MODELLED]
    out = pricing.breakdown(labels)

    assert out["counts"] == {OBSERVED: 1, SNAPSHOT_DERIVED: 2, MODELLED: 1}
    assert out["total"] == 4
    assert out["observed_pct"] == 75.0
    assert out["pct"][MODELLED] == 25.0


def test_the_breakdown_of_nothing_is_empty_rather_than_perfect():
    out = pricing.breakdown([])
    assert out["total"] == 0
    assert out["observed_pct"] == 0.0
    assert out["counts"] == {}


# ---- the model states its own assumptions ------------------------------

def test_the_model_block_names_the_constant_iv_and_why_it_matters():
    block = ModelAssumptions(iv=0.17).to_dict()
    assert block["iv"] == 0.17
    assert block["iv_source"] == "constant"
    assert "volatility surface never moved" in block["note"]


def test_a_stored_iv_travels_with_an_observed_quote():
    """So the premium projected at the stop is anchored to the volatility
    the market was actually pricing rather than to a run-wide constant."""
    store = a_store(iv=0.22)
    assert ask(store).iv_used == pytest.approx(0.22)
