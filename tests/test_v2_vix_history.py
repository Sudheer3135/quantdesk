"""India VIX history for strategy v2's gate."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.models import VixDaily
from app.strategy_v2 import vix
from test_angel_history import FakeClient, no_sleep

MASTER = [{"token": "99926017", "symbol": "India VIX", "exch_seg": "NSE"}]


def day_bar(day: date, close: float):
    stamp = f"{day.isoformat()}T00:00:00+05:30"
    return [stamp, close, close + 0.5, close - 0.5, close, 0]


def test_daily_bars_are_keyed_by_their_ist_session(db):
    client = FakeClient(default={"status": True, "data": [
        day_bar(date(2026, 9, 10), 13.1), day_bar(date(2026, 9, 11), 13.4)]})

    frame = vix.fetch_daily(client, "99926017", date(2026, 9, 10), date(2026, 9, 11),
                            master=MASTER, sleep=no_sleep)

    assert frame["session_date"].tolist() == [date(2026, 9, 10), date(2026, 9, 11)]
    assert client.requests[0]["interval"] == "ONE_DAY"
    assert client.requests[0]["symboltoken"] == "99926017"


def test_a_long_range_is_fetched_a_year_at_a_time(db):
    client = FakeClient(default={"status": True, "data": []})
    vix.fetch_daily(client, "99926017", date(2024, 1, 1), date(2026, 9, 11),
                    master=MASTER, sleep=no_sleep)
    assert len(client.requests) == 3


def test_an_unknown_token_is_refused_before_any_request(db):
    import pytest
    from app.data.angel_history import UnvalidatedToken
    client = FakeClient()
    with pytest.raises(UnvalidatedToken):
        vix.fetch_daily(client, "123", date(2026, 1, 1), date(2026, 2, 1),
                        master=MASTER, sleep=no_sleep)
    assert client.requests == []


def test_storing_never_restates_a_session(db):
    first = vix.to_daily(vix.ah._to_frame([day_bar(date(2026, 9, 10), 13.1)]))
    again = vix.to_daily(vix.ah._to_frame([day_bar(date(2026, 9, 10), 99.0),
                                           day_bar(date(2026, 9, 11), 13.4)]))

    assert vix.store(db, first, vix.SOURCE_HISTORY)["inserted"] == 1
    result = vix.store(db, again, vix.SOURCE_HISTORY)

    assert result == {"inserted": 1, "skipped_existing": 1}
    closes = {r.session_date: r.close for r in db.query(VixDaily).all()}
    assert closes == {date(2026, 9, 10): 13.1, date(2026, 9, 11): 13.4}


def test_history_ends_before_the_day_being_judged(db):
    frame = vix.to_daily(vix.ah._to_frame([day_bar(date(2026, 9, d), 12 + d / 10)
                                           for d in (8, 9, 10, 11)]))
    vix.store(db, frame, vix.SOURCE_HISTORY)

    assert vix.load_closes(db, before=date(2026, 9, 11)) == [12.8, 12.9, 13.0]
    assert vix.coverage(db)["sessions"] == 4


def test_the_live_close_is_filed_once(db):
    assert vix.record_close(db, date(2026, 9, 15), 14.2) is True
    assert vix.record_close(db, date(2026, 9, 15), 15.0) is False
    assert vix.record_close(db, date(2026, 9, 16), None) is False
    row = db.query(VixDaily).one()
    assert (row.close, row.source) == (14.2, vix.SOURCE_LIVE)
