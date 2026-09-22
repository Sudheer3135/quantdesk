"""Replaying stored signals against the candles that followed them.

Phase 3. The desk had produced 797 signals and recorded one trade, so there
was no way to answer whether any signal was right. This evaluates them from
data already in the archive.

Two things are load-bearing and both are tested here rather than assumed.
The evaluation must use the backtest's conventions, or its answer is not
comparable with the engine's — same next-bar entry, same pessimistic
stop-first rule when a bar covers both levels. And it must exclude the
overnight signals: the agent ran around the clock for weeks, refiling the
same closing candle's reading every five minutes, and counting those rows
would be counting one observation hundreds of times.
"""
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.backtest.costs import CostModel, SlippageModel
from app.backtest.feed import HistoricalFeed
from app.data.importer import import_index_candles
from app.evaluation import outcomes as study
from app.market_hours import IST
from app.models import SignalRecord

# A Wednesday, an ordinary trading day well inside the archive's reach.
DAY = date(2026, 6, 17)

# No slippage and no charges in most tests: they would blur an outcome that
# is meant to be exactly a stop or exactly a target, and the cost model has
# its own suite.
FREE = CostModel(brokerage_per_order=0.0, stt_pct_sell=0.0,
                 exchange_txn_pct=0.0, sebi_pct=0.0, ipft_pct=0.0,
                 stamp_duty_pct_buy=0.0, gst_pct=0.0)
NO_SLIP = SlippageModel(index_pct=0.0, ticks=0.0)


def bars(specs, day=DAY, start_hour=9, start_minute=15):
    """Five-minute candles from 09:15 IST, one per (o, h, l, c) tuple."""
    opening = datetime(day.year, day.month, day.day,
                       start_hour, start_minute, tzinfo=IST)
    return pd.DataFrame({
        "timestamp": [(opening + timedelta(minutes=5 * i)).astimezone(UTC)
                      for i in range(len(specs))],
        "open": [s[0] for s in specs],
        "high": [s[1] for s in specs],
        "low": [s[2] for s in specs],
        "close": [s[3] for s in specs],
        "volume": [1000.0] * len(specs),
    })


def flat(n, price=24_000.0, day=DAY):
    """`n` bars that do nothing, for padding a frame out."""
    return [(price, price + 1, price - 1, price)] * n


def signal(at, action="BUY", entry=24_000.0, stop=23_980.0, target=24_040.0,
           confidence=0.6, trend="bullish", sid=1):
    record = SignalRecord(
        symbol="NIFTY", timeframe="5m", action=action, confidence=confidence,
        price=entry, entry=entry, stop_loss=stop, target=target,
        checks=[], context={"trend": trend}, created_at=at.astimezone(UTC))
    record.id = sid
    return record


def ist_at(hour, minute, day=DAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)


def run_one(frame, record, index):
    return study.evaluate_signal(
        HistoricalFeed(frame), index, record, FREE, NO_SLIP, quantity=1)


# ---- entry convention --------------------------------------------------

def test_entry_is_the_next_bars_open_not_the_signal_price():
    """The engine's rule, and the earliest price a decision made on a closed
    bar could actually have been given."""
    frame = bars([(24_000, 24_005, 23_995, 24_000),
                  (24_010, 24_050, 24_005, 24_045)] + flat(6))
    out = run_one(frame, signal(ist_at(9, 15)), index=0)

    assert out.entry == 24_010.0
    assert out.entry_time == frame["timestamp"].iloc[1].isoformat()


def test_levels_travel_with_the_fill():
    """An overnight or intrabar gap must not silently widen the risk: the
    engine shifts stop and target by the same amount as the fill."""
    frame = bars([(24_000, 24_005, 23_995, 24_000),
                  (24_020, 24_025, 24_015, 24_020)] + flat(6))
    out = run_one(frame, signal(ist_at(9, 15)), index=0)

    assert out.entry == 24_020.0
    assert out.stop == 24_000.0        # 23,980 shifted by +20
    assert out.target == 24_060.0      # 24,040 shifted by +20
    assert out.risk_per_unit == 20.0


# ---- resolution --------------------------------------------------------

def test_a_target_hit_first_is_recorded_as_a_win():
    frame = bars([(24_000, 24_005, 23_995, 24_000),
                  (24_000, 24_010, 23_995, 24_005),
                  (24_005, 24_045, 24_000, 24_040)] + flat(5))
    out = run_one(frame, signal(ist_at(9, 15)), index=0)

    assert out.outcome == "target"
    assert out.exit_price == 24_040.0
    assert out.won is True
    assert out.r_multiple == pytest.approx(2.0, abs=0.01)
    assert out.bars_held == 2


def test_a_stop_hit_first_is_recorded_as_a_loss():
    frame = bars([(24_000, 24_005, 23_995, 24_000),
                  (24_000, 24_005, 23_975, 23_980)] + flat(6))
    out = run_one(frame, signal(ist_at(9, 15)), index=0)

    assert out.outcome == "stop"
    assert out.won is False
    assert out.r_multiple == pytest.approx(-1.0, abs=0.01)


def test_when_one_bar_covers_both_the_stop_is_assumed_first():
    """The engine's pessimistic rule. A five-minute bar does not record
    which level was touched first, and guessing favourably would flatter
    every signal whose bar happened to be wide."""
    frame = bars([(24_000, 24_005, 23_995, 24_000),
                  (24_000, 24_050, 23_970, 24_010)] + flat(6))
    out = run_one(frame, signal(ist_at(9, 15)), index=0)

    assert out.outcome == "stop"


def test_a_sell_signal_resolves_the_other_way_up():
    frame = bars([(24_000, 24_005, 23_995, 24_000),
                  (24_000, 24_005, 23_955, 23_960)] + flat(6))
    record = signal(ist_at(9, 15), action="SELL",
                    entry=24_000.0, stop=24_020.0, target=23_960.0)
    out = run_one(frame, record, index=0)

    assert out.outcome == "target"
    assert out.won is True


def test_the_session_close_ends_an_unresolved_trade():
    """The desk is intraday; the engine flattens at 15:15."""
    frame = bars(flat(2) + [(24_000, 24_002, 23_998, 24_001)],
                 start_hour=15, start_minute=5)
    out = run_one(frame, signal(ist_at(15, 5)), index=0)

    assert out.outcome == "session_end"
    assert out.resolved is True


def test_running_out_of_candles_is_unresolved_not_a_loss():
    """The archive does not reach past the newest bar. Counting that as a
    loss would bias the study toward whichever way the last days went."""
    frame = bars([(24_000, 24_005, 23_995, 24_000),
                  (24_000, 24_005, 23_995, 24_000),
                  (24_000, 24_005, 23_995, 24_000)])
    out = run_one(frame, signal(ist_at(9, 15)), index=0)

    assert out.outcome == "unresolved"
    assert out.resolved is False
    assert out.won is None
    assert out.r_multiple is None


# ---- excursions --------------------------------------------------------

def test_excursions_record_the_best_and_worst_the_trade_ever_looked():
    frame = bars([(24_000, 24_005, 23_995, 24_000),
                  (24_000, 24_030, 23_990, 24_020),
                  (24_020, 24_045, 24_010, 24_040)] + flat(5))
    out = run_one(frame, signal(ist_at(9, 15)), index=0)

    assert out.outcome == "target"
    assert out.mfe_points == pytest.approx(45.0, abs=0.01)   # high 24,045
    assert out.mae_points == pytest.approx(10.0, abs=0.01)   # low  23,990
    assert out.mfe_r == pytest.approx(2.25, abs=0.01)
    assert out.mae_r == pytest.approx(0.5, abs=0.01)


def test_excursions_are_mirrored_for_a_sell():
    frame = bars([(24_000, 24_005, 23_995, 24_000),
                  (24_000, 24_010, 23_960, 23_965)] + flat(6))
    record = signal(ist_at(9, 15), action="SELL",
                    entry=24_000.0, stop=24_020.0, target=23_960.0)
    out = run_one(frame, record, index=0)

    assert out.mfe_points == pytest.approx(40.0, abs=0.01)   # low  23,960
    assert out.mae_points == pytest.approx(10.0, abs=0.01)   # high 24,010


# ---- costs -------------------------------------------------------------

def test_costs_are_charged_and_reduce_the_result():
    """The engine's cost convention. A gross win can still be a net loss,
    which is the entire reason for measuring net."""
    frame = bars([(24_000, 24_005, 23_995, 24_000),
                  (24_000, 24_010, 23_995, 24_005),
                  (24_005, 24_045, 24_000, 24_040)] + flat(5))
    charged = study.evaluate_signal(
        HistoricalFeed(frame), 0, signal(ist_at(9, 15)),
        CostModel(), SlippageModel(index_pct=0.0, ticks=0.0), quantity=75)

    assert charged.charges > 0
    assert charged.net_pnl < charged.gross_points * 75
    # Gross is untouched by costs — that is what makes it the signal reading.
    assert charged.r_multiple == pytest.approx(2.0, abs=0.01)
    assert charged.r_multiple_net < charged.r_multiple


# ---- which signals count ------------------------------------------------

def test_only_signals_inside_a_real_session_are_evaluated():
    """Half the stored signal table was written overnight: the agent ran
    around the clock and refiled the same closing candle's reading every
    five minutes. Those are one observation, not hundreds."""
    assert study.is_in_session(ist_at(10, 0)) is True
    assert study.is_in_session(ist_at(9, 15)) is True
    assert study.is_in_session(ist_at(0, 49)) is False     # the overnight case
    assert study.is_in_session(ist_at(8, 50)) is False     # pre-open
    assert study.is_in_session(ist_at(16, 0)) is False


def test_an_exchange_holiday_is_not_a_session():
    """Reuses the platform's calendar rather than restating weekday rules."""
    assert study.is_in_session(ist_at(11, 0, day=date(2026, 6, 26))) is False


def test_a_hold_is_not_actionable():
    assert study.is_actionable(signal(ist_at(10, 0), action="HOLD")) is False


def test_a_signal_without_levels_is_not_actionable():
    record = signal(ist_at(10, 0))
    record.target = None
    assert study.is_actionable(record) is False


def test_a_zero_width_stop_is_not_actionable():
    """Risk of zero makes every R multiple infinite."""
    assert study.is_actionable(
        signal(ist_at(10, 0), entry=24_000.0, stop=24_000.0)) is False


# ---- the whole study ----------------------------------------------------

def seed(db, specs, signals):
    import_index_candles(db, bars(specs), "NIFTY", "5m", "test")
    for record in signals:
        # These fixtures describe a decision on the supplied bar. Write it
        # when that bar closes, rather than before its close existed.
        record.created_at += timedelta(minutes=5)
        record.id = None
        db.add(record)
    db.commit()


def test_the_study_counts_every_exclusion(db):
    """A study that reports only what it kept is unauditable."""
    seed(db, [(24_000, 24_005, 23_995, 24_000),
              (24_000, 24_050, 23_995, 24_045)] + flat(6),
         [signal(ist_at(9, 15)),                              # counted
          signal(ist_at(9, 15), action="HOLD"),               # a hold
          signal(ist_at(2, 0)),                               # overnight
          signal(ist_at(9, 20), target=None)])                # no levels

    report = study.evaluate(db, "NIFTY", "5m")
    picked = report.selection

    assert picked["stored"] == 4
    assert picked["holds"] == 1
    assert picked["out_of_session"] == 1
    assert picked["incomplete_levels"] == 1
    assert picked["selected"] == 1
    assert report.overall["n"] == 1


def test_an_overnight_signal_is_excluded_but_not_deleted(db):
    """History stays intact; the filter is derived at evaluation time, so it
    self-corrects if the calendar is ever amended."""
    seed(db, [(24_000, 24_005, 23_995, 24_000)] + flat(7),
         [signal(ist_at(2, 0))])

    report = study.evaluate(db, "NIFTY", "5m")

    assert report.selection["out_of_session"] == 1
    assert report.selection["selected"] == 0
    assert db.query(SignalRecord).count() == 1, "the row was deleted"


def test_a_signal_with_no_candle_behind_it_is_counted_as_missing_data(db):
    seed(db, [(24_000, 24_005, 23_995, 24_000)] + flat(7),
         [signal(ist_at(10, 0, day=date(2026, 7, 15)))])

    report = study.evaluate(db, "NIFTY", "5m")
    assert report.selection["no_candle"] == 1
    assert report.selection["selected"] == 0


def test_the_study_reports_distinct_bars_alongside_the_count(db):
    """Consecutive signals on one move are correlated observations. The bar
    count is how you see that a sample of forty is really a sample of two."""
    seed(db, [(24_000, 24_005, 23_995, 24_000)] + flat(7),
         [signal(ist_at(9, 15), sid=1), signal(ist_at(9, 17), sid=2)])

    report = study.evaluate(db, "NIFTY", "5m")
    assert report.selection["selected"] == 1
    assert report.selection["duplicate_bars"] == 1
    assert report.selection["distinct_bars"] == 1


def test_an_empty_archive_produces_an_empty_study(db):
    report = study.evaluate(db, "NIFTY", "5m")
    assert report.overall["n"] == 0
    assert report.confidence["measurable"] is False


# ---- the breakdowns -----------------------------------------------------

def test_the_study_breaks_the_sample_down_every_way_asked_for(db):
    seed(db, [(24_000, 24_005, 23_995, 24_000),
              (24_000, 24_050, 23_995, 24_045)] + flat(6),
         [signal(ist_at(9, 15), confidence=0.40, trend="bullish"),
          signal(ist_at(9, 20), action="SELL", entry=24_000.0,
                 stop=24_020.0, target=23_960.0, confidence=0.70,
                 trend="bearish")])

    report = study.evaluate(db, "NIFTY", "5m")

    assert {b["label"] for b in report.by_direction} == {"BUY", "SELL"}
    # The signal engine's own trend reading. Named `by_trend_label` since a
    # real market regime exists — `evaluation.regime_report` — and two
    # different things under one name in one report get read as one.
    assert {b["label"] for b in report.by_trend_label} == {"bullish", "bearish"}
    assert len(report.by_confidence) == 2
    assert all("–" in b["label"] for b in report.by_confidence)
    assert all(b["label"].endswith(":59") for b in report.by_hour)
    assert report.overall["target_first"] + report.overall["stop_first"] \
        + report.overall["session_end"] + report.overall["time_cap"] \
        + report.overall["unresolved"] == report.overall["n"]


def test_every_bucket_leads_with_its_sample_size(db):
    seed(db, [(24_000, 24_005, 23_995, 24_000),
              (24_000, 24_050, 23_995, 24_045)] + flat(6),
         [signal(ist_at(9, 15))])

    report = study.evaluate(db, "NIFTY", "5m")
    for group in (report.by_confidence, report.by_direction,
                  report.by_hour, report.by_trend_label):
        for bucket in group:
            assert bucket["n"] >= 1


# ---- confidence is measured, never assumed ------------------------------

def test_confidence_is_not_called_a_probability(db):
    seed(db, [(24_000, 24_005, 23_995, 24_000),
              (24_000, 24_050, 23_995, 24_045)] + flat(6),
         [signal(ist_at(9, 15))])

    report = study.evaluate(db, "NIFTY", "5m")
    wording = report.confidence.get("interpretation", "")
    assert "not calibrated" in wording
    assert "must not be read as a" in wording


def test_too_small_a_sample_says_so_rather_than_reporting_a_number():
    tiny = study.confidence_relationship([])
    assert tiny["measurable"] is False
    assert "too few" in tiny["reason"]


def test_the_correlation_is_computed_when_there_is_something_to_correlate():
    """Built by hand rather than through the database: what is under test is
    the statistic, and a rank correlation needs a spread of inputs."""
    rows = []
    for i, (conf, r) in enumerate(
            [(0.4, -1.0), (0.5, -1.0), (0.6, 2.0), (0.7, 2.0), (0.8, 2.0)]):
        rows.append(study.Outcome(
            signal_id=i, signal_time="", action="BUY", confidence=conf,
            trend="bullish", signal_bar_time="2026-06-17T03:55:00+00:00",
            entry_time="2026-06-17T04:00:00+00:00",
            entry=24_000.0, stop=23_980.0, target=24_040.0,
            risk_per_unit=20.0, outcome="target", net_pnl=r, r_multiple=r))

    result = study.confidence_relationship(rows)
    assert result["measurable"] is True
    assert result["spearman_confidence_vs_r"] > 0.8
    assert result["resolved"] == 5


def test_a_flat_confidence_column_cannot_be_correlated():
    """Every signal at the same confidence says nothing about ordering, and
    the honest answer is None rather than a spurious zero."""
    rows = [study.Outcome(
        signal_id=i, signal_time="", action="BUY", confidence=0.6,
        trend="bullish", signal_bar_time="2026-06-17T03:55:00+00:00",
        entry_time="2026-06-17T04:00:00+00:00",
        entry=24_000.0, stop=23_980.0, target=24_040.0, risk_per_unit=20.0,
        outcome="target", net_pnl=float(i), r_multiple=float(i))
        for i in range(5)]

    assert study.confidence_relationship(rows)["spearman_confidence_vs_r"] is None


def test_the_study_states_its_caveats(db):
    report = study.evaluate(db, "NIFTY", "5m")
    joined = " ".join(report.caveats).lower()
    assert "hypothetical" in joined
    assert "not independent" in joined
    assert "stop is assumed" in joined


# ---- the agent no longer runs all night ---------------------------------

def test_the_agent_is_gated_on_the_session_in_every_environment(monkeypatch):
    """Development used to run the agent around the clock, which is why 320
    of 375 stored BUY signals were generated outside market hours — the same
    closing candle re-read every five minutes."""
    from app.workers import agent

    published = []
    monkeypatch.setattr(agent, "market_is_open", lambda *a, **k: False)
    monkeypatch.setattr(agent, "publish", lambda *a, **k: published.append(a))
    monkeypatch.setattr(
        agent, "build_analysis",
        lambda *a, **k: pytest.fail("built a signal off-session"))

    agent.tick()
    assert published == []


def test_force_still_runs_a_pass_for_diagnostics(db, monkeypatch):
    """The escape hatch the option collector's `capture` already has."""
    from app.api.signals import Analysis
    from app.config import get_settings
    from app.workers import agent
    from test_risk_on_live_path import SessionFactory, a_buy

    published = []
    monkeypatch.setenv("ARCHIVE_CANDLES", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(agent, "market_is_open", lambda *a, **k: False)
    monkeypatch.setattr(agent, "SessionLocal", SessionFactory(db))
    monkeypatch.setattr(agent, "publish", lambda *a, **k: published.append(a))
    monkeypatch.setattr(agent, "build_analysis",
                        lambda *a, **k: Analysis(signal=a_buy(), plan=None))

    agent.tick(force=True)
    assert len(published) == 1


def test_the_headline_r_is_gross_and_the_cost_caveat_says_so(db):
    """At an index level of 24,000 the engine's cost model charges about
    2.3R a round trip, because it computes turnover from the traded price as
    though it were an option premium. Reporting net as the headline would
    have shown every signal losing 2-3R regardless of whether it was right."""
    seed(db, [(24_000, 24_005, 23_995, 24_000),
              (24_000, 24_050, 23_995, 24_045)] + flat(6),
         [signal(ist_at(9, 15))])

    report = study.evaluate(db, "NIFTY", "5m")
    assert report.overall["avg_r"] > report.overall["avg_r_net"]

    joined = " ".join(report.caveats)
    assert "GROSS" in joined
    assert "do not read net as expectancy" in joined


def test_a_win_is_decided_gross_not_after_costs(db):
    """A signal that reached its target was right about the market. Whether
    the charges on an index-priced round trip swallowed it is a separate
    question, and conflating them would report a working signal as a loss."""
    seed(db, [(24_000, 24_005, 23_995, 24_000),
              (24_000, 24_050, 23_995, 24_045)] + flat(6),
         [signal(ist_at(9, 15))])

    report = study.evaluate(db, "NIFTY", "5m", include_outcomes=True)
    only = report.outcomes[0]

    assert only["outcome"] == "target"
    assert only["won"] is True
    assert only["net_pnl"] < 0, "test premise: costs exceed the gross win here"
