"""The option archive's look-ahead guard.

Index look-ahead is loud: a strategy reading tomorrow's close usually posts
an absurd equity curve, and somebody notices. Option look-ahead is quiet.
Ask for a strike at 11:05 and get the 11:10 quote back, and the result is a
strategy that buys a few rupees better than it could have — a small,
plausible, permanent edge with nothing in the output to flag it.

So these tests are about refusals, not about arithmetic.
"""
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.optionbuy import chain as chain_module
from app.optionbuy.chain import (
    ChainStore,
    ContractKey,
    OptionLookaheadError,
)
from optionbuy_fixtures import (
    candles,
    option_bars,
    seed_index_rows,
    seed_option_rows,
    session_stamps,
    sessions,
)

DAYS = sessions(date(2025, 6, 2), 2)
EXPIRY = date(2025, 6, 10)
STAMPS = session_stamps(DAYS[0])
KEY = ContractKey(EXPIRY, 24_000.0, "CE")


def a_store(**kwargs) -> ChainStore:
    return ChainStore(option_bars(DAYS, EXPIRY, **kwargs), {})


# ---- the clock -------------------------------------------------------

def test_a_read_before_the_walk_starts_is_refused():
    store = a_store()
    # No clock means no basis for deciding what is past. Answering anyway
    # would make the guard depend on the caller remembering to start it.
    with pytest.raises(OptionLookaheadError, match="clock has not started"):
        store.bar_at(KEY, STAMPS[10])


def test_a_quote_the_walk_has_not_reached_is_refused():
    store = a_store()
    store.advance(STAMPS[10])

    assert store.bar_at(KEY, STAMPS[10]) is not None
    with pytest.raises(OptionLookaheadError, match="had not printed"):
        store.bar_at(KEY, STAMPS[11])


def test_the_clock_cannot_run_backwards():
    store = a_store()
    store.advance(STAMPS[20])
    with pytest.raises(OptionLookaheadError, match="cannot go backwards"):
        store.advance(STAMPS[19])


def test_the_ladder_and_the_expiry_list_are_guarded_too():
    """A selection made from a ladder that did not exist yet is look-ahead
    even when the premium it eventually reads is in the past."""
    store = a_store()
    store.advance(STAMPS[5])

    assert store.strikes(EXPIRY, "CE", STAMPS[5])
    with pytest.raises(OptionLookaheadError):
        store.strikes(EXPIRY, "CE", STAMPS[6])
    with pytest.raises(OptionLookaheadError):
        store.expiries(STAMPS[6])


def test_a_contract_listed_later_is_not_visible_earlier():
    bars = option_bars(DAYS, EXPIRY)
    late = ContractKey(EXPIRY, 99_000.0, "CE")
    # One strike that only starts printing halfway through the session.
    bars[late] = [b for b in bars[ContractKey(EXPIRY, 24_000.0, "CE")][40:]]
    bars[late] = [
        type(b)(**{**b.__dict__, "key": late}) for b in bars[late]
    ]
    store = ChainStore(bars, {})

    store.advance(STAMPS[10])
    assert 99_000.0 not in store.strikes(EXPIRY, "CE", STAMPS[10])
    store.advance(STAMPS[50])
    assert 99_000.0 in store.strikes(EXPIRY, "CE", STAMPS[50])


# ---- what counts as a current quote ----------------------------------

def test_the_last_quote_at_or_before_the_moment_is_used():
    store = a_store()
    store.advance(STAMPS[10])
    # Halfway through the bucket, the 11:00 bar is still the current quote.
    midway = STAMPS[10] + timedelta(minutes=3)
    store.advance(midway)
    assert store.bar_at(KEY, midway).timestamp == STAMPS[10]


def test_a_stale_quote_is_no_quote():
    """A price from forty minutes ago is a fact about forty minutes ago."""
    bars = option_bars(DAYS, EXPIRY)
    thinned = {KEY: [b for b in bars[KEY] if b.timestamp <= STAMPS[3]]}
    store = ChainStore(thinned, {})

    store.advance(STAMPS[4])
    assert store.bar_at(KEY, STAMPS[4]) is not None      # 5 minutes old

    store.advance(STAMPS[12])
    assert store.bar_at(KEY, STAMPS[12]) is None         # 45 minutes old
    # The bar is still there; it is the currency of it that failed.
    assert store.bar_at(KEY, STAMPS[12], allow_stale=True) is not None


def test_yesterdays_close_is_never_this_mornings_quote():
    """Age alone would let an overnight gap be priced off the wrong session."""
    bars = option_bars(DAYS[:1], EXPIRY)
    store = ChainStore(bars, {})

    second_day_open = session_stamps(DAYS[1])[0]
    store.advance(second_day_open)
    # Well inside the staleness window by wall clock — the previous session
    # closed 17 hours earlier, but the guard is the session, not the hours.
    assert store.bar_at(KEY, second_day_open) is None


# ---- what the archive says about itself -------------------------------

def test_the_fingerprint_changes_when_a_premium_changes():
    a = a_store()
    b = a_store()
    assert a.fingerprint()["hash"] == b.fingerprint()["hash"]

    moved = option_bars(DAYS, EXPIRY)
    first = moved[KEY][0]
    moved[KEY][0] = type(first)(**{**first.__dict__, "close": first.close + 1})
    assert ChainStore(moved, {}).fingerprint()["hash"] != a.fingerprint()["hash"]


def test_the_fingerprint_changes_when_the_archive_grows():
    one_day = ChainStore(option_bars(DAYS[:1], EXPIRY), {})
    two_days = ChainStore(option_bars(DAYS, EXPIRY), {})
    assert one_day.fingerprint()["hash"] != two_days.fingerprint()["hash"]
    assert two_days.fingerprint()["sessions"] == 2


def test_polls_per_bucket_counts_one_poll_once_across_the_chain():
    """Every contract in a snapshot is written by the same poll.

    Summing samples across strikes would report a session with ten contracts
    as a thousand percent covered, which is how a coverage check stops being
    one.
    """
    store = a_store(samples=5)
    polls = store.polls_by_bucket(DAYS[0])
    assert set(polls.values()) == {5}
    assert len(polls) == len(session_stamps(DAYS[0]))


def test_an_empty_store_is_empty_rather_than_absent():
    store = chain_module.empty_store()
    assert store.empty and len(store) == 0
    store.seek(datetime(2025, 6, 2, 4, 0, tzinfo=UTC))
    assert store.bar_at(KEY, datetime(2025, 6, 2, 4, 0, tzinfo=UTC)) is None


# ---- loading from the database ---------------------------------------

def test_the_database_path_produces_the_same_shape(db):
    frame = candles(DAYS)
    seed_index_rows(db, frame)
    seed_option_rows(db, DAYS, EXPIRY, strikes=[24_000.0, 24_050.0])

    store = chain_module.load(db, underlying="NIFTY", timeframe="5m")

    assert not store.empty
    assert store.sessions() == set(DAYS)
    store.advance(STAMPS[10])
    assert store.expiries(STAMPS[10]) == [EXPIRY]
    assert store.strikes(EXPIRY, "CE", STAMPS[10]) == [24_000.0, 24_050.0]
    bar = store.bar_at(ContractKey(EXPIRY, 24_000.0, "CE"), STAMPS[10])
    assert bar is not None
    assert bar.bar_kind == "snapshot"
    assert bar.samples == 5
    # The citation has to be enough to find the row again.
    assert bar.reference()["option_candle_id"] == bar.row_id
    assert bar.reference()["contract"].startswith("24000 CE")


def test_loading_respects_the_requested_window(db):
    seed_option_rows(db, DAYS, EXPIRY, strikes=[24_000.0])

    only_first = chain_module.load(db, start=DAYS[0], end=DAYS[0])
    assert only_first.sessions() == {DAYS[0]}
