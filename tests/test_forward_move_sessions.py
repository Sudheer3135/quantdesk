"""A forward return must not be measured across an overnight gap.

Found during the wide-replay preparation and reported before it was fixed.

The candle frame is contiguous in *rows*, not in time. Bar N is 15:30 and
bar N+1 is 09:15 the next morning, so counting twelve rows forward from
14:00 measured a real hour while counting twelve forward from 15:00 measured
an hour plus a night — including the gap open, which on this instrument is
where a large share of the movement lives. Nineteen of the 167 replayed
signals (11.4%) were being scored that way, one of them spanning Friday to
Monday, and their returns were averaged in with intraday ones as though they
were the same measurement.

The three cases the fix is pinned on, and why each is the interesting one:

  15:25 → 15:30   the last horizon that is still inside the session. It has
                  to resolve, or the fix has quietly truncated the sample at
                  the close instead of at the boundary.
  15:30 → 09:15   the boundary itself, in its smallest form: one row apart,
                  a night in between.
  intraday        the ordinary case, which must be completely unaffected.

Unresolved is `None`, never zero and never a shortened horizon. Zero would
score a bias flat on evidence that does not exist; a shortened horizon would
quietly judge late-session bars on a different question than the rest.
"""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.backtest.feed import HistoricalFeed
from app.evaluation import two_layer
from app.market_hours import IST

# 75 bars, stamped with their *open* time — 09:15 through 15:25, the last of
# them being the bar that covers 15:25 to 15:30. That is this project's
# convention throughout (see `data.quality.SESSION_MINUTES`), so a case named
# "15:25 → 15:30" below is the step onto the final bar, and "15:30 → 09:15"
# is the step off it.
BARS_PER_SESSION = 75
LAST_BAR = BARS_PER_SESSION - 1          # stamped 15:25, closes at 15:30

# 5m, 15m, 30m and 1h expressed in five-minute bars.
HORIZONS = {"5m": 1, "15m": 3, "30m": 6, "60m": 12}


def sessions(days=(1, 2, 3), seed=11):
    """A frame of whole NSE sessions, contiguous in rows and not in time.

    June 2026: the 1st is a Monday, so (5, 8) below is a genuine Friday to
    Monday jump with a weekend in the middle.
    """
    rng = np.random.default_rng(seed)
    stamps, closes = [], []
    price = 24_000.0
    for day in days:
        opening = datetime(2026, 6, day, 9, 15, tzinfo=IST)
        for bar in range(BARS_PER_SESSION):
            stamps.append((opening + timedelta(minutes=5 * bar)).astimezone(UTC))
            price += float(rng.normal(0, 6))
            closes.append(price)
    close = np.array(closes)
    return pd.DataFrame({
        "timestamp": stamps,
        "open": close, "high": close + 12, "low": close - 12,
        "close": close, "volume": [1000.0] * len(close)})


@pytest.fixture
def feed():
    return HistoricalFeed(sessions())


def ist_at(feed, index):
    feed.seek(len(feed) - 1)
    return feed.ist(index)


def move(feed, index, horizon):
    feed.seek(len(feed) - 1)          # the walk has seen everything
    return two_layer._forward_move(feed, index, horizon=horizon)


# ---------------------------------------------------------------------------
# the three pinned cases
# ---------------------------------------------------------------------------

def test_1525_to_1530_still_resolves(feed):
    """The last horizon inside the session. If this came back unresolved the
    fix would have truncated the sample at the close, not at the boundary."""
    penultimate = LAST_BAR - 1
    assert ist_at(feed, penultimate).strftime("%H:%M") == "15:20"
    assert ist_at(feed, LAST_BAR).strftime("%H:%M") == "15:25"

    points, atrs = move(feed, penultimate, horizon=1)
    assert points is not None
    assert atrs is not None


def test_1530_to_the_next_mornings_0915_is_unresolved(feed):
    """The boundary in its smallest form: one row apart, a night between."""
    assert ist_at(feed, LAST_BAR).strftime("%H:%M") == "15:25"
    assert ist_at(feed, LAST_BAR + 1).strftime("%H:%M") == "09:15"
    assert ist_at(feed, LAST_BAR).date() != ist_at(feed, LAST_BAR + 1).date()

    assert move(feed, LAST_BAR, horizon=1) == (None, None)


def test_an_ordinary_intraday_horizon_is_untouched(feed):
    """The fix must change nothing about the case it is not about."""
    midday = 30                                     # 11:45
    for horizon in HORIZONS.values():
        points, atrs = move(feed, midday, horizon)
        assert points is not None, horizon
        assert atrs is not None, horizon


# ---------------------------------------------------------------------------
# every horizon, at the boundary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,horizon", sorted(HORIZONS.items()))
def test_every_horizon_stops_at_the_close(feed, name, horizon):
    """A bar `horizon` rows before the close resolves; the next one does not."""
    assert move(feed, LAST_BAR - horizon, horizon)[0] is not None, name
    assert move(feed, LAST_BAR - horizon + 1, horizon) == (None, None), name


def test_exactly_the_last_h_bars_of_a_session_are_unresolved(feed):
    """Stated as a count, so a fix that is merely close cannot pass."""
    for horizon in HORIZONS.values():
        unresolved = [i for i in range(BARS_PER_SESSION)
                      if move(feed, i, horizon) == (None, None)]
        assert unresolved == list(range(BARS_PER_SESSION - horizon,
                                        BARS_PER_SESSION)), horizon


def test_a_weekend_is_the_same_boundary_as_a_night():
    """Friday 05-Jun-2026 to Monday the 8th. No calendar lookup is involved —
    a gap of any length shows up as two different trading dates."""
    feed = HistoricalFeed(sessions(days=(4, 5, 8)))
    friday_close = BARS_PER_SESSION + LAST_BAR
    feed.seek(len(feed) - 1)
    assert feed.ist(friday_close).strftime("%a %H:%M") == "Fri 15:25"
    assert feed.ist(friday_close + 1).strftime("%a %H:%M") == "Mon 09:15"

    assert move(feed, friday_close, horizon=1) == (None, None)


# ---------------------------------------------------------------------------
# unresolved, not zero
# ---------------------------------------------------------------------------

def test_an_unresolved_move_makes_no_directional_claim(feed):
    """`None` propagates to NO_CLAIM, which `_accuracy` excludes from both
    the numerator and the base rate. Zero would have scored the bias FLAT on
    evidence that does not exist."""
    _points, atrs = move(feed, LAST_BAR, horizon=1)
    assert atrs is None
    assert two_layer._bias_verdict("BULLISH", atrs) == two_layer.NO_CLAIM


def test_the_horizon_is_never_silently_shortened(feed):
    """A truncated horizon would judge late-session bars on a different
    question than the rest of the sample, and say nothing about it."""
    inside = move(feed, LAST_BAR - 12, horizon=12)[0]
    crossing = move(feed, LAST_BAR - 6, horizon=12)[0]
    assert inside is not None
    assert crossing is None, "it measured something shorter and did not say so"


def test_the_last_session_still_ends_at_the_frame(feed):
    """Running off the end of the archive was already handled and still is —
    the session check is an addition to that guard, not a replacement."""
    assert move(feed, len(feed) - 1, horizon=1) == (None, None)
