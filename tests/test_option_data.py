"""Tests for the option snapshot importer.

Option history cannot be backfilled — NSE publishes a snapshot, not a tape —
so every bar is assembled from live polls and the merge rules are the whole
game. Get them wrong and the archive fills with plausible-looking premiums
that nobody can tell are wrong six months later, when it is far too late to
recapture the real ones.
"""
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.data.importer import (
    bucket_start,
    import_option_snapshot,
    parse_expiry,
)
from app.models import OptionCandle, OptionContract

EXPIRY = "18-Jun-2026"

# A Wednesday inside market hours, in the past.
MOMENT = datetime(2026, 6, 17, 5, 22, 30, tzinfo=UTC)     # 10:52:30 IST


def chain(strikes=(24_400, 24_450, 24_500), call_ltp=120.0, put_ltp=95.0,
          call_iv=13.5, oi=900_000.0, volume=400_000.0):
    """A chain in the shape `parse_option_chain` returns."""
    return pd.DataFrame([
        {
            "strike": float(s),
            "call_oi": oi, "put_oi": oi * 0.8,
            "call_oi_change": 12_000.0, "put_oi_change": -8_000.0,
            "call_volume": volume, "put_volume": volume * 0.9,
            "call_iv": call_iv, "put_iv": 15.2,
            "call_ltp": call_ltp, "put_ltp": put_ltp,
        }
        for s in strikes
    ])


def store(db, **kwargs):
    return import_option_snapshot(
        db, kwargs.pop("chain", chain()), underlying="NIFTY", expiry=EXPIRY,
        spot=24_450.0, source="free", captured_at=kwargs.pop("at", MOMENT),
        **kwargs)


# ---- shape ------------------------------------------------------------

def test_a_snapshot_creates_contracts_and_bars(db):
    report = store(db)

    contracts = db.scalars(select(OptionContract)).all()
    assert len(contracts) == 6                      # 3 strikes × CE and PE
    assert {c.option_type for c in contracts} == {"CE", "PE"}
    assert all(c.expiry_date == date(2026, 6, 18) for c in contracts)

    bars = db.scalars(select(OptionCandle)).all()
    assert len(bars) == 6
    assert all(b.bar_kind == "snapshot" for b in bars)
    assert all(b.samples == 1 for b in bars)
    assert report.candles.inserted == 6


def test_the_spot_is_stored_beside_the_premium(db):
    """Without it, greeks and implied volatility cannot be recomputed later:
    the spot series and the option series drift apart at the first gap."""
    store(db)
    bar = db.scalars(select(OptionCandle)).first()
    assert bar.underlying_close == 24_450.0


def test_iv_is_stored_as_a_fraction_not_a_percentage(db):
    """NSE publishes 13.5 meaning 13.5%; `option_pricing` takes 0.135. The
    mismatch raises nothing and produces premiums a hundred times too
    large."""
    store(db, chain=chain(call_iv=13.5))
    bar = db.scalars(
        select(OptionCandle).join(OptionContract)
        .where(OptionContract.option_type == "CE")).first()
    assert bar.iv == pytest.approx(0.135)


def test_an_untraded_strike_is_skipped_rather_than_stored_as_zero(db):
    """A zero last price is 'nobody traded this', not 'this is free'. Stored
    as a premium, a backtest would happily buy it."""
    payload = chain()
    payload.loc[1, "call_ltp"] = 0.0
    report = store(db, chain=payload)

    assert report.skipped["no_price"] == 1
    assert len(db.scalars(select(OptionCandle)).all()) == 5


# ---- bucketing and merging -------------------------------------------

def test_polls_inside_one_bucket_merge_into_a_single_bar(db):
    """The agent polls every few minutes; five-minute bars must not multiply
    with every poll."""
    store(db, at=MOMENT)
    store(db, at=MOMENT + timedelta(seconds=90))

    bars = db.scalars(select(OptionCandle)).all()
    assert len(bars) == 6
    assert all(b.samples == 2 for b in bars)


def test_the_bar_tracks_its_extremes_across_polls(db):
    """The open belongs to the first poll and must not be rewritten; the
    high and low track everything seen; the close is the latest."""
    store(db, chain=chain(call_ltp=100.0), at=MOMENT)
    store(db, chain=chain(call_ltp=130.0), at=MOMENT + timedelta(seconds=60))
    store(db, chain=chain(call_ltp=90.0), at=MOMENT + timedelta(seconds=120))

    bar = db.scalars(
        select(OptionCandle).join(OptionContract)
        .where(OptionContract.option_type == "CE",
               OptionContract.strike == 24_400)).first()

    assert bar.open == 100.0, "a later poll must not rewrite the open"
    assert bar.high == 130.0
    assert bar.low == 90.0
    assert bar.close == 90.0
    assert bar.samples == 3


def test_a_new_bucket_starts_a_new_bar(db):
    store(db, at=MOMENT)
    store(db, at=MOMENT + timedelta(minutes=6))
    assert len(db.scalars(select(OptionCandle)).all()) == 12


def test_bucket_start_floors_to_the_timeframe():
    assert bucket_start(datetime(2026, 6, 17, 5, 22, 30, tzinfo=UTC), "5m") \
        == datetime(2026, 6, 17, 5, 20, tzinfo=UTC)
    assert bucket_start(datetime(2026, 6, 17, 5, 20, 0, tzinfo=UTC), "5m") \
        == datetime(2026, 6, 17, 5, 20, tzinfo=UTC)
    assert bucket_start(datetime(2026, 6, 17, 5, 22, tzinfo=UTC), "15m") \
        == datetime(2026, 6, 17, 5, 15, tzinfo=UTC)


def test_repeating_a_snapshot_does_not_duplicate_contracts(db):
    store(db, at=MOMENT)
    store(db, at=MOMENT + timedelta(minutes=6))
    assert len(db.scalars(select(OptionContract)).all()) == 6


def test_first_seen_is_never_overwritten(db):
    """It records when a contract entered the archive. A later poll updating
    it would erase the only evidence of how long you have watched it."""
    store(db, at=MOMENT)
    original = db.scalars(select(OptionContract)).first().first_seen

    later = MOMENT + timedelta(minutes=30)
    store(db, at=later)

    contract = db.scalars(select(OptionContract)).first()
    assert contract.first_seen == original
    assert contract.last_seen != original


# ---- guards -----------------------------------------------------------

def test_a_future_dated_snapshot_is_refused(db):
    """Requirement 5, on the option side. A historical table must never hold
    a row for a moment that has not happened."""
    with pytest.raises(ValueError, match="future"):
        store(db, at=datetime.now(UTC) + timedelta(hours=2))


def test_an_out_of_hours_capture_is_stored_but_flagged(db):
    """The chain does not change when the market is shut, so these bars
    repeat the closing state. Worth keeping, not worth mistaking for
    activity."""
    saturday = datetime(2026, 6, 20, 6, 0, tzinfo=UTC)
    report = store(db, at=saturday)
    assert any("outside market hours" in w for w in report.warnings)
    assert len(db.scalars(select(OptionCandle)).all()) == 6


def test_an_empty_chain_is_not_an_error(db):
    report = store(db, chain=pd.DataFrame())
    assert report.candles.written == 0
    assert any("Empty chain" in w for w in report.warnings)


def test_expiry_parsing_accepts_nse_format_and_real_dates():
    assert parse_expiry("18-Jun-2026") == date(2026, 6, 18)
    assert parse_expiry(date(2026, 6, 18)) == date(2026, 6, 18)
    assert parse_expiry(datetime(2026, 6, 18, 15, 30)) == date(2026, 6, 18)
    with pytest.raises(ValueError):
        parse_expiry("2026-06-18")


def test_two_expiries_are_stored_as_separate_contracts(db):
    """The failure this guards against: filing a snapshot under a guessed
    expiry silently merges two different contracts into one series, and the
    resulting premium history looks entirely plausible."""
    store(db)
    import_option_snapshot(
        db, chain(), underlying="NIFTY", expiry="25-Jun-2026", spot=24_450.0,
        source="free", captured_at=MOMENT)

    contracts = db.scalars(select(OptionContract)).all()
    assert len({c.expiry_date for c in contracts}) == 2
    assert len(contracts) == 12


def test_absurd_iv_is_dropped_rather_than_stored(db):
    """A 900% implied volatility is a bad quote, not a volatile strike."""
    store(db, chain=chain(call_iv=900.0))
    bar = db.scalars(
        select(OptionCandle).join(OptionContract)
        .where(OptionContract.option_type == "CE")).first()
    assert bar.iv is None
