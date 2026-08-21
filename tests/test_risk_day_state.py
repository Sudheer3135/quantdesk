"""Today's risk state, rebuilt from the trade journal.

Audit finding H-1: `/signals/live` constructed a blank DayState on every
request, so `trades_taken`, `realised_pnl`, `consecutive_losses` and
`open_positions` were permanently zero. All four daily limits evaluated
against that blank slate and could never trip. The rules were correct; they
were simply never given today's numbers.

These tests hold the reconstruction to the definition a trader would
recognise: the cap counts trades opened *today* in IST, a position carried
overnight still occupies a slot this morning, and a streak of losses is
broken by anything that is not a loss.
"""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.data import repository
from app.market_hours import IST, trading_date
from app.models import TradeRecord
from app.risk.manager import DayState, RiskConfig, day_state_from_trades, evaluate


def trade(pnl=None, status="closed", created_at=None, **kw):
    return TradeRecord(
        symbol="NIFTY", side="BUY", quantity=75, entry=24_200.0, stop_loss=24_190.0,
        status=status, pnl=pnl,
        created_at=created_at or datetime.now(UTC), **kw,
    )


# ---------------------------------------------------------------------------
# the pure reconstruction
# ---------------------------------------------------------------------------

def test_blank_journal_gives_a_blank_day():
    day = trading_date()
    state = day_state_from_trades(day, [], [])
    assert state == DayState(trading_day=day)


def test_counts_every_trade_opened_today_not_only_closed_ones():
    day = trading_date()
    todays = [trade(pnl=-500), trade(status="open")]
    state = day_state_from_trades(day, todays, [todays[1]])
    assert state.trades_taken == 2          # the cap counts entries, not exits
    assert state.realised_pnl == -500       # only closed trades are realised
    assert state.open_positions == 1


def test_realised_pnl_sums_closed_trades():
    day = trading_date()
    todays = [trade(pnl=1200), trade(pnl=-450.5), trade(pnl=None, status="open")]
    state = day_state_from_trades(day, todays, [])
    assert state.realised_pnl == pytest.approx(749.5)


def test_consecutive_losses_counts_the_trailing_streak_only():
    day = trading_date()
    base = datetime.now(UTC) - timedelta(hours=3)
    # loss, loss, win, loss, loss  -> the streak is the last two
    pnls = [-100, -200, 900, -300, -400]
    todays = [trade(pnl=p, created_at=base + timedelta(minutes=10 * i))
              for i, p in enumerate(pnls)]
    assert day_state_from_trades(day, todays, []).consecutive_losses == 2


def test_a_win_breaks_the_streak():
    day = trading_date()
    base = datetime.now(UTC) - timedelta(hours=2)
    todays = [trade(pnl=-100, created_at=base),
              trade(pnl=-200, created_at=base + timedelta(minutes=5)),
              trade(pnl=50, created_at=base + timedelta(minutes=10))]
    assert day_state_from_trades(day, todays, []).consecutive_losses == 0


def test_break_even_also_breaks_the_streak():
    """A scratch is not a loss. Treating it as one would stop the day early."""
    day = trading_date()
    base = datetime.now(UTC) - timedelta(hours=1)
    todays = [trade(pnl=-100, created_at=base),
              trade(pnl=0.0, created_at=base + timedelta(minutes=5))]
    assert day_state_from_trades(day, todays, []).consecutive_losses == 0


def test_streak_is_chronological_not_insertion_order():
    """Rows arrive newest-first from the database; the streak is not."""
    day = trading_date()
    base = datetime.now(UTC) - timedelta(hours=2)
    newest = trade(pnl=-300, created_at=base + timedelta(minutes=20))
    middle = trade(pnl=800, created_at=base + timedelta(minutes=10))
    oldest = trade(pnl=-100, created_at=base)
    assert day_state_from_trades(day, [newest, middle, oldest], []).consecutive_losses == 1


def test_overnight_position_occupies_a_slot_today():
    """`open_now` is deliberately not filtered by date."""
    day = trading_date()
    yesterday = trade(status="open", created_at=datetime.now(UTC) - timedelta(days=1))
    state = day_state_from_trades(day, [], [yesterday])
    assert state.trades_taken == 0
    assert state.open_positions == 1


# ---------------------------------------------------------------------------
# the database read path
# ---------------------------------------------------------------------------

def test_todays_trades_uses_the_ist_trading_day(db):
    """A trade at 01:00 UTC belongs to that day's IST session (06:30 IST).

    A trade at 19:00 UTC is already the *next* IST day, and must not be
    counted against today's cap.
    """
    today_ist = trading_date()
    early = datetime.combine(today_ist, datetime.min.time(), tzinfo=IST) + timedelta(hours=10)
    next_session = early + timedelta(hours=14)          # rolls past IST midnight

    db.add_all([trade(pnl=-100, created_at=early.astimezone(UTC)),
                trade(pnl=-100, created_at=next_session.astimezone(UTC))])
    db.commit()

    found = repository.todays_trades(db, today_ist)
    assert len(found) == 1
    assert trading_date(found[0].created_at.replace(tzinfo=UTC)
                        if found[0].created_at.tzinfo is None
                        else found[0].created_at) == today_ist


def test_open_trades_ignores_the_date(db):
    db.add_all([trade(status="open", created_at=datetime.now(UTC) - timedelta(days=4)),
                trade(status="closed", pnl=10, created_at=datetime.now(UTC))])
    db.commit()
    assert len(repository.open_trades(db)) == 1


def test_state_survives_a_restart_because_it_lives_in_the_database(db):
    """H-1's durability requirement.

    Nothing is held in memory, so 'restarting' is just building the state
    again from a fresh read. The numbers must be identical.
    """
    day = trading_date()
    db.add_all([trade(pnl=-1500), trade(pnl=-1200)])
    db.commit()

    before = day_state_from_trades(day, repository.todays_trades(db, day),
                                   repository.open_trades(db))
    db.expunge_all()                      # drop every in-memory identity
    after = day_state_from_trades(day, repository.todays_trades(db, day),
                                  repository.open_trades(db))

    assert before == after
    assert after.trades_taken == 2
    assert after.consecutive_losses == 2


# ---------------------------------------------------------------------------
# the limits actually bite now
# ---------------------------------------------------------------------------

@pytest.fixture
def config():
    return RiskConfig(capital=100_000, risk_per_trade_pct=1.0, max_trades_per_day=2,
                      min_risk_reward=2.0, lot_size=75)


TRADE = dict(entry=24_200.0, stop_loss=24_190.0, target=24_230.0)


def test_a_clean_day_is_approved(db, config):
    day = trading_date()
    state = day_state_from_trades(day, repository.todays_trades(db, day),
                                  repository.open_trades(db))
    assert evaluate(config=config, state=state, **TRADE).approved


# Descriptions of rows, not rows.
#
# A `parametrize` list is built once, when the decorator is evaluated at
# import — so ORM instances placed in it are shared by every case that reads
# them, including across the sqlite and postgresql runs of the `db` fixture.
# The first backend inserts them and commits; the objects come back detached
# but still carrying an identity key, and `add()` on the next session treats
# that as an existing row and issues no INSERT. The Postgres run then found
# an empty journal, every limit measured against zero, and the trade was
# approved — which is precisely the bug this file exists to catch, wearing
# the costume of a passing fixture.
#
# Built inside the test instead, so each case gets rows of its own.
@pytest.mark.parametrize("row_specs,expected_reason", [
    ([{"pnl": 50}, {"pnl": 60}], "Daily trade cap"),
    ([{"pnl": -1500}, {"pnl": -1600}], "losses in a row"),
    ([{"status": "open"}], "Already holding"),
])
def test_journal_history_blocks_the_next_trade(db, config, row_specs, expected_reason):
    """The regression itself: these rows exist, so the limit must trip."""
    rows = [trade(**spec) for spec in row_specs]
    db.add_all(rows)
    db.commit()

    # The premise. Without this the assertions below can pass for the wrong
    # reason — or, as they did on Postgres, fail for one.
    assert len(repository.todays_trades(db, trading_date())) == len(rows)

    day = trading_date()
    state = day_state_from_trades(day, repository.todays_trades(db, day),
                                  repository.open_trades(db))
    decision = evaluate(config=config, state=state, **TRADE)

    assert not decision.approved
    assert any(expected_reason in r for r in decision.reasons), decision.reasons


def test_daily_loss_limit_trips_from_journal_pnl(db, config):
    db.add(trade(pnl=-3_100))              # over 3% of 100,000
    db.commit()
    day = trading_date()
    state = day_state_from_trades(day, repository.todays_trades(db, day),
                                  repository.open_trades(db))
    decision = evaluate(config=config, state=state, **TRADE)
    assert not decision.approved
    assert any("Daily loss limit" in r for r in decision.reasons)


def test_kill_switch_is_reachable_from_settings(monkeypatch):
    """It was a RiskConfig default nothing wired up, so the documented
    switch could not be thrown without editing code."""
    from app.config import get_settings
    from app.deps import risk_config

    get_settings.cache_clear()
    monkeypatch.setenv("KILL_SWITCH", "true")
    try:
        assert risk_config().kill_switch is True
        decision = evaluate(config=risk_config(),
                            state=DayState(trading_day=trading_date()), **TRADE)
        assert not decision.approved
        assert "Kill switch" in decision.reasons[0]
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("env,attr,value", [
    ("MAX_DAILY_LOSS_PCT", "max_daily_loss_pct", 5.0),
    ("MAX_CONSECUTIVE_LOSSES", "max_consecutive_losses", 4),
    ("MAX_OPEN_POSITIONS", "max_open_positions", 3),
    ("MAX_CAPITAL_DEPLOYED_PCT", "max_capital_deployed_pct", 35.0),
])
def test_every_risk_limit_is_configurable(monkeypatch, env, attr, value):
    from app.config import get_settings
    from app.deps import risk_config

    get_settings.cache_clear()
    monkeypatch.setenv(env, str(value))
    try:
        assert getattr(risk_config(), attr) == value
    finally:
        get_settings.cache_clear()
