"""IV and greeks, added to a chain at the API boundary.

The desk carries two chains — Angel's stream and NSE's poll — and only
one of them states an implied volatility. These tests hold the rule that
follows: a stated IV is believed, an absent one is solved for, and a
premium that can yield neither produces an absence rather than a zero.

That last one is the whole point. On a ladder a plotted 0.00 delta and a
missing delta look identical and mean completely different things — one
says "this option barely moves", the other says "we could not tell".
"""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import chain_greeks as cg

# Mid-session, six days before a weekly expiry.
NOW = datetime(2026, 9, 16, 4, 30, tzinfo=UTC)      # 10:00 IST
EXPIRY = "2026-09-22"
SPOT = 23_155.75

GREEKS = ("delta", "gamma", "theta", "vega", "rho")


def row(strike=23_200.0, **kw):
    base = {"strike": strike, "call_ltp": 157.5, "put_ltp": 175.65}
    base.update(kw)
    return base


def enriched(*rows, spot=SPOT, expiry=EXPIRY, now=NOW):
    return cg.enrich([dict(r) for r in rows], spot, expiry, now=now)


class TestTheMaths:
    def test_an_atm_call_has_a_delta_near_a_half(self):
        """The one number anybody sanity-checks a chain against."""
        out = enriched(row(23_150.0))[0]
        assert 0.40 < out["call_delta"] < 0.60
        # And the put on the same strike is its mirror, less one.
        assert out["put_delta"] == pytest.approx(out["call_delta"] - 1, abs=0.12)

    def test_delta_falls_away_from_the_money(self):
        deep_itm, atm, deep_otm = enriched(
            row(22_000.0, call_ltp=1180.0),
            row(23_150.0, call_ltp=189.25),
            row(24_500.0, call_ltp=1.05))
        assert deep_itm["call_delta"] > atm["call_delta"] > deep_otm["call_delta"]
        assert deep_itm["call_delta"] > 0.85
        assert deep_otm["call_delta"] < 0.15

    def test_a_put_delta_is_negative_and_a_call_delta_is_not(self):
        out = enriched(row())[0]
        assert out["call_delta"] > 0
        assert out["put_delta"] < 0

    def test_theta_is_a_cost_to_the_holder_on_both_sides(self):
        """A long option decays whichever way it points."""
        out = enriched(row())[0]
        assert out["call_theta"] < 0
        assert out["put_theta"] < 0

    def test_theta_is_quoted_per_calendar_day_like_every_other_chain(self):
        """The unit, pinned, because two are defensible and only one is
        published.

        `option_pricing` returns decay per *trading* day — right for the
        strategy engine, which reasons in sessions — while every broker
        chain divides by 365. Measured against Kite on 16-Sep-2026 at
        the 23,200 strike: delta, vega and rho agreed to two places and
        theta read -21.79 against their -13.28. Same annual figure,
        different denominator.

        A column that disagrees by sixty percent with the screen beside
        it gets read as a bug, so the ladder publishes the common unit.
        The model is deliberately untouched: changing it would move the
        theta the strategy sizes and exits on.
        """
        from app.analytics.option_pricing import (
            TRADING_DAYS_PER_YEAR, greeks, years_to_expiry)

        out = enriched(row(23_200.0, call_iv=13.70))[0]
        years = years_to_expiry(NOW, cg.expiry_instant(EXPIRY))
        model = greeks(SPOT, 23_200.0, years, 0.1370, kind="CE")

        # Per calendar day, not per trading day.
        assert out["call_theta"] == pytest.approx(
            model.theta * TRADING_DAYS_PER_YEAR / 365, abs=0.01)
        assert out["call_theta"] != pytest.approx(model.theta, abs=0.01)
        # And within touching distance of the published figure.
        assert -17.0 < out["call_theta"] < -12.0

    def test_gamma_and_vega_match_when_both_sides_share_one_iv(self):
        """Put-call parity, stated precisely.

        A call and a put on the same strike and expiry have identical
        convexity and identical volatility exposure *at the same IV* —
        the two greeks are literally the same formula, with no term that
        knows which side it is. So when a source states one IV for both,
        gamma and vega must come back equal.
        """
        out = enriched(row(23_150.0, call_iv=14.0, put_iv=14.0))[0]
        assert out["call_gamma"] == pytest.approx(out["put_gamma"], rel=1e-6)
        assert out["call_vega"] == pytest.approx(out["put_vega"], rel=1e-6)

    def test_each_side_keeps_its_own_solved_iv(self):
        """And the other half of that, which is not a bug.

        Where no IV is stated each side is solved from its own premium,
        and a call and a put on one strike rarely trade at the same
        implied volatility — that difference is the skew, and it is real
        information. Averaging the two sides into one number to make the
        greeks agree would be inventing a price nobody quoted.
        """
        out = enriched(row(23_150.0, call_ltp=189.25, put_ltp=150.05))[0]
        assert out["call_iv"] != out["put_iv"]
        assert out["call_gamma"] != out["put_gamma"]


class TestWhereTheIVComesFrom:
    def test_a_stated_iv_is_believed_rather_than_recomputed(self):
        """NSE publishes its own. Solving on top of it would put two
        different volatilities on one desk with no way to tell which a
        row was using."""
        out = enriched(row(call_iv=18.25))[0]
        assert out["call_iv"] == 18.25

    def test_a_stated_iv_is_read_as_percent_not_as_a_fraction(self):
        """The hundredfold error that still produces plausible greeks.

        NSE quotes 18.25 meaning 18.25%; Black-Scholes wants 0.1825. Fed
        the raw number the model prices at 1825% volatility, and the
        delta it returns is a perfectly reasonable-looking 0.99.
        """
        out = enriched(row(call_iv=18.25))[0]
        # At 18% vol a 23,200 call on a 23,155 spot is near the money.
        assert 0.40 < out["call_delta"] < 0.65, (
            "a percent read as a fraction prices at 1825% vol")

    def test_an_absent_iv_is_solved_from_the_traded_premium(self):
        out = enriched(row())[0]
        assert out["call_iv"] > 0
        # And it round-trips: the solved IV reprices to the premium given.
        from app.analytics.option_pricing import price, years_to_expiry
        years = years_to_expiry(NOW, cg.expiry_instant(EXPIRY))
        assert price(SPOT, 23_200.0, years, out["call_iv"] / 100,
                     kind="CE") == pytest.approx(157.5, rel=0.02)


class TestWhatIsDeliberatelyAbsent:
    def test_an_untraded_strike_gets_no_greeks_rather_than_zeros(self):
        """A plotted 0.00 and a missing value look the same on a ladder
        and mean opposite things."""
        out = enriched(row(call_ltp=0.0, put_ltp=0.0))[0]
        for side in ("call", "put"):
            for name in GREEKS:
                assert out[f"{side}_{name}"] is None

    def test_an_expired_series_is_left_alone(self):
        """Past expiry there is no time value to differentiate, and a
        greek computed on negative time is meaningless rather than
        merely small."""
        out = cg.enrich([row()], SPOT, "2026-09-15", now=NOW)[0]
        assert "call_delta" not in out

    def test_no_spot_means_no_greeks_rather_than_a_guess(self):
        out = cg.enrich([row()], None, EXPIRY, now=NOW)[0]
        assert "call_delta" not in out

    def test_one_unusable_strike_does_not_cost_the_whole_ladder(self):
        rows = cg.enrich(
            [row(23_150.0), {"strike": "nonsense"}, row(23_250.0)],
            SPOT, EXPIRY, now=NOW)
        assert rows[0]["call_delta"] is not None
        assert rows[2]["call_delta"] is not None


class TestTheExpiryInstant:
    def test_a_weekly_expires_at_the_close_not_at_midnight(self):
        """Nine and a half hours of phantom life.

        Read as midnight, a contract on its own expiry day carries most
        of a session of theta it does not have — which on the day that
        matters most is the largest error in the whole table.
        """
        at = cg.expiry_instant("2026-09-22")
        assert at == datetime(2026, 9, 22, 10, 0, tzinfo=UTC)   # 15:30 IST

    @pytest.mark.parametrize("text", ["2026-09-22", "22-Sep-2026", "22SEP2026"])
    def test_it_reads_the_formats_the_two_sources_actually_send(self, text):
        assert cg.expiry_instant(text) is not None

    def test_an_unreadable_expiry_is_none_rather_than_a_default(self):
        assert cg.expiry_instant("next Thursday") is None
        assert cg.expiry_instant(None) is None


def test_the_whole_chain_costs_almost_nothing():
    """Forty strikes, both sides, solved and differentiated.

    Measured at 1.2ms on 16-Sep-2026 — about half a percent of one core
    at four publishes a second, which is why there is no cache here. If
    this ever stops being true a cache is the fix, not dropping columns.
    """
    import time
    rows = [row(22_250.0 + 50 * i, call_ltp=max(1.0, SPOT - (22_250 + 50 * i) + 150),
                put_ltp=max(1.0, (22_250 + 50 * i) - SPOT + 150))
            for i in range(40)]
    started = time.perf_counter()
    cg.enrich(rows, SPOT, EXPIRY, now=NOW)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.10, f"a chain took {elapsed * 1000:.0f}ms"
