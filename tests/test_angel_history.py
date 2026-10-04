"""The Angel historical backfill.

Three of these tests exist because of a measured API behaviour rather than
an imagined one, and they are the ones that matter:

  * `test_a_window_longer_than_the_cap_is_never_requested` — Angel truncates
    an over-long range to its recent end and answers SUCCESS, so a 2-year
    request returns a hundred days and looks fine.
  * `test_an_unknown_token_is_refused_before_any_fetch` — an unknown token
    answers SUCCESS with no rows, which is indistinguishable from a quiet
    market unless the token is checked first.
  * `test_the_throttle_is_caught_by_type_not_by_status_code` — the rate
    limiter arrives as a JSON parse failure, not an HTTP 429.

Every network call here is a fake. The one test that touches the real API
is skipped unless ANGEL_LIVE_TEST=1.
"""
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.data import angel_history as ah

IST_OFFSET = "+05:30"


def bar(ts: str, close: float = 100.0) -> list:
    return [f"{ts}{IST_OFFSET}", close, close + 1, close - 1, close, 0]


def session(day: date, bars: int = 76, close: float = 100.0) -> list[list]:
    """A day of five-minute bars from 09:15 IST."""
    start = datetime(day.year, day.month, day.day, 9, 15)
    return [bar((start + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M"),
                close + i * 0.05)
            for i in range(bars)]


MASTER = [{"token": "99926000", "symbol": "Nifty 50", "exch_seg": "NSE"},
          {"token": "12345", "symbol": "OTHER", "exch_seg": "NSE"}]


class FakeClient:
    """Records every request and replays scripted responses."""

    def __init__(self, responses=None, default=None):
        self.requests: list[dict] = []
        self.responses = list(responses or [])
        self.default = default if default is not None else {
            "status": True, "message": "SUCCESS", "data": []}

    def getCandleData(self, params):                      # noqa: N802
        self.requests.append(dict(params))
        if self.responses:
            nxt = self.responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return self.default


def no_sleep(_seconds):
    return None


# ---- pagination -------------------------------------------------------

def test_windows_walk_backwards_from_the_most_recent_day():
    """Backwards, because a truncated response keeps the recent end.

    Walking forwards, an over-long window would silently drop its oldest
    rows — which is exactly the era a backfill exists to collect.
    """
    windows = ah.plan_windows(datetime(2024, 1, 1, 9, 15),
                              datetime(2024, 12, 31, 15, 30), max_days=100)

    assert windows[0][1] == datetime(2024, 12, 31, 15, 30)
    assert windows[-1][0] == datetime(2024, 1, 1, 9, 15)
    starts = [w[0] for w in windows]
    assert starts == sorted(starts, reverse=True), "windows must descend"


def test_a_window_longer_than_the_cap_is_never_requested():
    windows = ah.plan_windows(datetime(2022, 1, 1), datetime(2026, 1, 1),
                              max_days=100)

    for start, end in windows:
        assert (end - start).days < 100, (
            f"window {start}..{end} exceeds Angel's cap; the response would "
            "be silently truncated and still report SUCCESS")


def test_windows_do_not_overlap_or_leave_holes():
    windows = sorted(ah.plan_windows(datetime(2025, 1, 1),
                                     datetime(2025, 6, 30), max_days=30))
    for (_, earlier_end), (later_start, _) in zip(windows, windows[1:],
                                                  strict=False):
        assert later_start > earlier_end, "windows overlap"
        assert (later_start - earlier_end).days <= 1, "gap between windows"


def test_a_single_short_range_is_one_window():
    windows = ah.plan_windows(datetime(2025, 1, 1), datetime(2025, 1, 5),
                              max_days=100)
    assert len(windows) == 1


def test_an_inverted_range_is_rejected():
    with pytest.raises(ValueError, match="before"):
        ah.plan_windows(datetime(2025, 6, 1), datetime(2025, 1, 1))


# ---- token validation -------------------------------------------------

def test_an_unknown_token_is_refused_before_any_fetch():
    client = FakeClient()

    with pytest.raises(ah.UnvalidatedToken, match="instrument master"):
        ah.fetch_index_candles(client, "99999999", date(2025, 1, 1),
                               date(2025, 1, 5), master=MASTER, sleep=no_sleep)

    assert client.requests == [], (
        "an unvalidated token must never reach the API — its empty response "
        "is indistinguishable from a real quiet window")


def test_a_validated_token_that_returns_nothing_is_no_trades_not_an_error():
    """The other half of the disambiguation.

    Same empty payload, opposite meaning: because the token was checked
    against the master, an empty window is a real quiet window.
    """
    client = FakeClient(default={"status": True, "message": "SUCCESS",
                                 "data": []})

    result = ah.fetch_index_candles(client, "99926000", date(2025, 1, 1),
                                    date(2025, 1, 5), master=MASTER,
                                    sleep=no_sleep)

    assert result.candles == 0
    assert result.empty_windows == 1
    assert result.windows[0].no_trades is True
    assert result.clean, "a genuinely quiet window is not a data-quality fault"


# ---- truncation detection --------------------------------------------

def test_a_response_short_of_its_window_raises_a_warning():
    """The regression that motivated the whole module.

    The fake answers a 100-day request with three days of bars, which is
    what a silently truncated response looks like from the caller's side.
    """
    client = FakeClient(default={
        "status": True, "message": "SUCCESS",
        "data": session(date(2025, 3, 28)) + session(date(2025, 3, 31))})

    result = ah.fetch_index_candles(client, "99926000", date(2025, 1, 1),
                                    date(2025, 3, 31), master=MASTER,
                                    sleep=no_sleep)

    assert result.truncated_windows >= 1
    assert not result.clean
    assert any("asked" in w for w in result.warnings)
    note = result.windows[0].note
    assert "2025-01-01" in note and "2025-03-28" in note, (
        "the warning must name the exact dates so the gap is actionable")


def test_a_complete_window_raises_no_warning():
    days = [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)]
    client = FakeClient(default={
        "status": True, "message": "SUCCESS",
        "data": [b for d in days for b in session(d)]})

    result = ah.fetch_index_candles(client, "99926000", date(2025, 1, 1),
                                    date(2025, 1, 7), master=MASTER,
                                    sleep=no_sleep)

    assert result.clean
    assert result.candles == 76 * 3


# ---- throttling -------------------------------------------------------

def throttle():
    from SmartApi.smartExceptions import DataException
    return DataException("Couldn't parse the JSON response received from "
                         "the server")


def test_the_throttle_is_caught_by_type_not_by_status_code():
    """Angel's rate limiter is a parse failure, not an HTTP 429."""
    good = {"status": True, "message": "SUCCESS", "data": session(date(2025, 1, 2))}
    client = FakeClient(responses=[throttle(), throttle(), good])

    result = ah.fetch_index_candles(client, "99926000", date(2025, 1, 2),
                                    date(2025, 1, 2), master=MASTER,
                                    sleep=no_sleep)

    assert result.candles == 76
    assert result.windows[0].retries == 2
    assert len(client.requests) == 3


def test_retries_are_bounded_and_then_it_fails_loudly(monkeypatch):
    client = FakeClient(responses=[throttle()] * 10)

    with pytest.raises(ah.AngelHistoryError, match="throttled"):
        ah.fetch_index_candles(client, "99926000", date(2025, 1, 2),
                               date(2025, 1, 2), master=MASTER, sleep=no_sleep)

    settings = ah.get_settings()
    assert len(client.requests) == settings.angel_history_max_retries + 1


def test_backoff_grows_between_attempts():
    waits = []
    client = FakeClient(responses=[throttle(), throttle(),
                                   {"status": True, "data": []}])

    ah.fetch_index_candles(client, "99926000", date(2025, 1, 2),
                           date(2025, 1, 2), master=MASTER,
                           sleep=waits.append)

    backoffs = [w for w in waits if w > ah.get_settings().angel_history_pace_seconds]
    assert len(backoffs) == 2
    assert backoffs[1] > backoffs[0], "backoff must grow"


def test_a_refused_request_fails_rather_than_returning_nothing():
    client = FakeClient(default={"status": False, "message": "Invalid token",
                                 "errorcode": "AB1004"})

    with pytest.raises(ah.AngelHistoryError, match="refused"):
        ah.fetch_index_candles(client, "99926000", date(2025, 1, 2),
                               date(2025, 1, 2), master=MASTER, sleep=no_sleep)


# ---- the merge policy -------------------------------------------------

def test_a_backfill_into_occupied_range_is_refused():
    """The whole merge policy, in one assertion.

    uq_candle cannot hold two sources for one bar, and the importer upserts
    on conflict — so writing here would overwrite Yahoo's rows rather than
    sit beside them.
    """
    plan = ah.plan_backfill(date(2026, 6, 1), date(2026, 8, 1),
                            date(2026, 5, 15), date(2026, 8, 28))

    assert not plan.writable
    assert "refusing" in plan.reason.lower()


def test_a_backfill_is_clipped_to_end_before_the_archive_begins():
    plan = ah.plan_backfill(date(2024, 9, 1), date(2026, 7, 1),
                            date(2026, 5, 15), date(2026, 8, 28))

    assert plan.writable
    assert plan.write_start == date(2024, 9, 1)
    assert plan.write_end == date(2026, 5, 14), "must stop one day short"
    assert plan.clipped_days > 0


def test_an_empty_archive_takes_the_whole_range():
    plan = ah.plan_backfill(date(2024, 9, 1), date(2026, 5, 1), None, None)

    assert plan.writable
    assert (plan.write_start, plan.write_end) == (date(2024, 9, 1),
                                                  date(2026, 5, 1))


def test_a_range_entirely_before_the_archive_is_untouched():
    plan = ah.plan_backfill(date(2024, 1, 1), date(2024, 12, 31),
                            date(2026, 5, 15), date(2026, 8, 28))

    assert (plan.write_start, plan.write_end) == (date(2024, 1, 1),
                                                  date(2024, 12, 31))
    assert plan.clipped_days == 0


# ---- the overlap measurement -----------------------------------------

def test_the_overlap_comparison_stores_nothing_and_reports_differences():
    ts = pd.date_range("2026-06-01 09:15", periods=5, freq="5min", tz="UTC")
    angel = pd.DataFrame({"timestamp": ts, "close": [100, 101, 102, 103, 104.0]})
    archive = pd.DataFrame({"timestamp": ts,
                            "close": [100, 101, 102.5, 103, 199.0]})

    report = ah.compare_overlap(angel, archive)

    assert report.bars_compared == 5
    assert report.close_matches == 3
    assert report.max_close_diff == pytest.approx(95.0)
    assert report.worst[0]["diff"] == pytest.approx(95.0)


def test_the_overlap_reports_bars_only_one_vendor_has():
    a = pd.DataFrame({"timestamp": pd.date_range("2026-06-01", periods=3,
                                                 freq="5min", tz="UTC"),
                      "close": [1.0, 2.0, 3.0]})
    b = pd.DataFrame({"timestamp": pd.date_range("2026-06-01", periods=5,
                                                 freq="5min", tz="UTC"),
                      "close": [1.0, 2.0, 3.0, 4.0, 5.0]})

    report = ah.compare_overlap(a, b)

    assert report.bars_compared == 3
    assert report.only_in_archive == 2
    assert report.only_in_angel == 0


# ---- session accounting ----------------------------------------------

def test_short_sessions_are_visible_in_the_per_day_report():
    frame = ah._to_frame(session(date(2025, 1, 2)) + session(date(2025, 1, 3), bars=40))

    per_day = ah.session_counts(frame)
    gaps = ah.find_gaps(per_day)

    assert [d["bars"] for d in per_day] == [76, 40]
    assert len(gaps) == 1
    assert gaps[0]["day"] == "2025-01-03"
    assert gaps[0]["short_by"] == 36


def test_a_full_session_is_not_reported_as_a_gap():
    frame = ah._to_frame(session(date(2025, 1, 2)))
    assert ah.find_gaps(ah.session_counts(frame)) == []


# ---- the live probe ---------------------------------------------------

@pytest.mark.skipif(os.getenv("ANGEL_LIVE_TEST") != "1",
                    reason="set ANGEL_LIVE_TEST=1 to hit the real API")
def test_one_real_window_against_the_live_api():
    from app.brokers import angel

    session_, client = angel.login_with_client()
    assert session_.auth_token

    end = date.today() - timedelta(days=7)
    result = ah.fetch_index_candles(client, "99926000",
                                    end - timedelta(days=3), end)

    assert result.candles > 0
    assert result.clean
    assert set(result.frame.columns) >= set(ah.CANDLE_FIELDS)


# ---- session plumbing (phase 1) --------------------------------------

FAKE_API_KEY = "AbCd1234ZzTopSecretApiKey"
FAKE_CLIENT = "S9988771"
FAKE_MPIN = "445566"
FAKE_TOTP = "JBSWY3DPEHPK3PXPTOTPSEED"
FAKE_SECRETS = (FAKE_API_KEY, FAKE_CLIENT, FAKE_MPIN, FAKE_TOTP)


class FakeConnect:
    """Stands in for SmartConnect, recording what it was asked."""

    def __init__(self, api_key):
        self.api_key = api_key
        self.calls = []

    def generateSession(self, client_code, secret, otp):   # noqa: N802
        self.calls.append((client_code, secret, otp))
        return {"status": True, "data": {
            "jwtToken": "jwt-123", "refreshToken": "refresh-456",
            "feedToken": "feed-789"}}

    def getfeedToken(self):                                # noqa: N802
        return "feed-789"


@pytest.fixture
def angel_env(monkeypatch):
    monkeypatch.setenv("ANGEL_ENABLED", "true")
    monkeypatch.setenv("ANGEL_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("ANGEL_CLIENT_CODE", FAKE_CLIENT)
    monkeypatch.setenv("ANGEL_MPIN", FAKE_MPIN)
    monkeypatch.setenv("ANGEL_TOTP_SECRET", FAKE_TOTP)
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_login_with_client_hands_back_the_object_that_holds_getcandledata(
        angel_env):
    """The reason this variant exists.

    `login` throws the client away because the websocket needs only tokens,
    but `getCandleData` is a method on the client and there is no supported
    way to rebuild an authenticated one from a JWT.
    """
    from app.brokers import angel

    made = []

    def factory(api_key):
        made.append(FakeConnect(api_key))
        return made[-1]

    session, client = angel.login_with_client(connect_factory=factory)

    assert session.auth_token == "jwt-123"
    assert client is made[0], "the caller must get the same client back"
    assert hasattr(client, "generateSession")


def test_the_original_login_signature_still_returns_only_a_session(angel_env):
    from app.brokers import angel

    result = angel.login(connect_factory=lambda k: FakeConnect(k))

    assert isinstance(result, angel.AngelSession)
    assert not isinstance(result, tuple), "login's contract must not change"


def test_the_history_path_installs_redaction_before_the_first_call(angel_env):
    """The scrubber must be armed before anything can log a request body."""
    import logging

    from app.brokers import angel
    from app.brokers.angel import CredentialFilter

    logging.getLogger().filters = [
        f for f in logging.getLogger().filters
        if not isinstance(f, CredentialFilter)]

    angel.login_with_client(connect_factory=lambda k: FakeConnect(k))

    assert any(isinstance(f, CredentialFilter)
               for f in logging.getLogger().filters), (
        "login_with_client must go through install_log_redaction")


def test_a_backfill_cannot_run_without_credentials(monkeypatch):
    from app.brokers import angel
    from app.config import get_settings

    # Emptied rather than deleted: pydantic-settings still reads the repo's
    # .env, and on a developer machine that file holds real credentials.
    # An environment variable outranks the file; an absent one does not.
    for key in ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_MPIN",
                "ANGEL_PASSWORD", "ANGEL_TOTP_SECRET"):
        monkeypatch.setenv(key, "")
    get_settings.cache_clear()

    with pytest.raises(angel.AngelNotConfigured):
        angel.login_with_client()
    get_settings.cache_clear()


def test_a_refused_login_leaks_nothing(angel_env, caplog):
    import logging

    from app.brokers import angel

    class Refusing(FakeConnect):
        def generateSession(self, client_code, secret, otp):  # noqa: N802
            raise RuntimeError(
                f"login failed for {client_code} mpin={secret} totp={otp}")

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(angel.AngelError):
            angel.login_with_client(connect_factory=lambda k: Refusing(k))

    for secret in FAKE_SECRETS:
        assert secret not in caplog.text, "a credential reached the log"


# ---- ingest (phase 3), against a real database -----------------------

def stored_rows(db):
    from app.models import CandleRecord
    return db.query(CandleRecord).order_by(CandleRecord.timestamp).all()


def run(db, client, **kwargs):
    kwargs.setdefault("start", date(2025, 1, 1))
    kwargs.setdefault("end", date(2025, 1, 3))
    kwargs.setdefault("master", MASTER)
    kwargs.setdefault("sleep", no_sleep)
    return ah.run_backfill(db, client, **kwargs)


def two_sessions():
    return {"status": True, "message": "SUCCESS",
            "data": session(date(2025, 1, 2)) + session(date(2025, 1, 3))}


def test_stored_bars_carry_the_angel_source_tag(db):
    client = FakeClient(default=two_sessions())

    report = run(db, client)

    rows = stored_rows(db)
    assert rows, "nothing was written"
    assert {r.source for r in rows} == {ah.SOURCE}
    assert ah.SOURCE == "angel_hist"
    assert len(ah.SOURCE) <= 16, "source column is varchar(16)"
    assert report.stored["write"]["inserted"] == len(rows)


def test_every_stored_bar_went_through_the_validation_gate(db):
    """Provenance is not decoration: these rows must be indistinguishable
    from live ones in every respect except the vendor."""
    client = FakeClient(default=two_sessions())

    report = run(db, client)

    assert "validation" in report.stored
    for row in stored_rows(db):
        assert row.session_date is not None
        assert row.ingested_at is not None
        assert row.timeframe == "5m"


def test_re_running_the_import_does_not_duplicate_rows(db):
    first = run(db, FakeClient(default=two_sessions()))
    count_after_first = len(stored_rows(db))

    second = run(db, FakeClient(default=two_sessions()))

    assert len(stored_rows(db)) == count_after_first, "re-import duplicated rows"
    assert first.stored["write"]["inserted"] == count_after_first
    assert second.stored["write"]["inserted"] == 0
    # Pass 2C: identical bars are recognised and left alone, not rewritten.
    assert second.stored["write"]["updated"] == 0
    assert second.stored["unchanged"] == count_after_first


def test_the_report_names_short_sessions(db):
    client = FakeClient(default={
        "status": True, "message": "SUCCESS",
        "data": session(date(2025, 1, 2)) + session(date(2025, 1, 3), bars=30)})

    report = run(db, client)

    assert len(report.per_day) == 2
    assert len(report.gaps) == 1
    assert report.gaps[0]["day"] == "2025-01-03"


def test_a_dry_run_fetches_but_writes_nothing(db):
    client = FakeClient(default=two_sessions())

    report = run(db, client, dry_run=True)

    assert report.fetch.candles == 152
    assert stored_rows(db) == []
    assert report.stored == {}


def test_a_run_into_occupied_range_never_reaches_the_network(db):
    from app.data.importer import import_index_candles

    existing = ah._to_frame(session(date(2025, 1, 2)))
    import_index_candles(db, existing, "NIFTY", "5m", "free")
    db.commit()

    client = FakeClient(default=two_sessions())
    report = run(db, client, start=date(2025, 1, 2), end=date(2025, 1, 10))

    assert client.requests == [], "an occupied range must not be fetched"
    assert not report.plan.writable
    assert {r.source for r in stored_rows(db)} == {"free"}, (
        "the existing rows must be untouched")


def test_a_run_is_clipped_to_stop_before_existing_data(db):
    from app.data.importer import import_index_candles

    existing = ah._to_frame(session(date(2025, 1, 6)))
    import_index_candles(db, existing, "NIFTY", "5m", "free")
    db.commit()

    client = FakeClient(default=two_sessions())
    report = run(db, client, start=date(2025, 1, 1), end=date(2025, 1, 10))

    assert report.plan.write_end == date(2025, 1, 5)
    sources = {r.source for r in stored_rows(db)}
    assert sources == {"free", ah.SOURCE}
    # And crucially, one row per bar — the feed refuses duplicates.
    stamps = [r.timestamp for r in stored_rows(db)]
    assert len(stamps) == len(set(stamps))


# ---- source selection (phase 3.2, relocated to the repository) --------

def test_candles_can_be_read_by_source(db):
    from app.data import repository
    from app.data.importer import import_index_candles

    import_index_candles(db, ah._to_frame(session(date(2025, 1, 2))),
                         "NIFTY", "5m", "free")
    import_index_candles(db, ah._to_frame(session(date(2025, 1, 6))),
                         "NIFTY", "5m", ah.SOURCE)
    db.commit()

    everything = repository.load_index_candles(db)
    angel_only = repository.load_index_candles(db, sources=[ah.SOURCE])
    yahoo_only = repository.load_index_candles(db, sources=["free"])

    assert len(everything) == 152
    assert len(angel_only) == 76
    assert len(yahoo_only) == 76
    assert repository.provenance_of(angel_only)["sources"] == {ah.SOURCE: 76}


def test_an_unfiltered_read_still_has_one_row_per_bar(db):
    """The invariant HistoricalFeed depends on.

    `HistoricalFeed.__init__` raises on duplicate timestamps, so as long as
    uq_candle excludes `source` this must hold for every read.
    """
    from app.backtest.feed import HistoricalFeed
    from app.data import repository
    from app.data.importer import import_index_candles

    import_index_candles(db, ah._to_frame(session(date(2025, 1, 2))),
                         "NIFTY", "5m", "free")
    import_index_candles(db, ah._to_frame(session(date(2025, 1, 6))),
                         "NIFTY", "5m", ah.SOURCE)
    db.commit()

    frame = repository.load_index_candles(db)

    assert not frame["timestamp"].duplicated().any()
    HistoricalFeed(frame, analysis_window=10)     # must not raise


# ---- filling holes inside the archive ---------------------------------

def angel_days(*days, close=200.0):
    rows = []
    for d in days:
        rows += session(d, close=close)
    return {"status": True, "message": "SUCCESS", "data": rows}


def seed(db, day, bars=76, close=100.0):
    from app.data.importer import import_index_candles
    import_index_candles(db, ah._to_frame(session(day, bars=bars, close=close)),
                         "NIFTY", "5m", "free")
    db.commit()


def fill(db, client, **kwargs):
    kwargs.setdefault("start", date(2025, 1, 6))
    kwargs.setdefault("end", date(2025, 1, 10))
    kwargs.setdefault("today", date(2025, 1, 10))
    kwargs.setdefault("master", MASTER)
    kwargs.setdefault("sleep", no_sleep)
    return ah.fill_gaps(db, client, **kwargs)


def test_gap_fill_finds_absent_and_short_sessions_only(db):
    seed(db, date(2025, 1, 6))              # full
    seed(db, date(2025, 1, 7), bars=40)     # short

    missing, skipped = ah.find_missing_sessions(
        db, symbol="NIFTY", timeframe="5m", start=date(2025, 1, 4),
        end=date(2025, 1, 10), today=date(2025, 1, 10))

    assert [m["day"] for m in missing] == ["2025-01-07", "2025-01-08", "2025-01-09"]
    assert missing[0]["stored_bars"] == 40
    assert [s["day"] for s in skipped] == ["2025-01-10"], "today belongs to the live feed"


def test_gap_fill_never_restates_an_existing_bar(db):
    seed(db, date(2025, 1, 6))
    seed(db, date(2025, 1, 7), bars=40)
    before = {r.timestamp: (r.close, r.source, r.revision) for r in stored_rows(db)}

    client = FakeClient(default=angel_days(
        date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8), date(2025, 1, 9)))
    report = fill(db, client)

    rows = stored_rows(db)
    for r in rows:
        if r.timestamp in before:
            assert (r.close, r.source, r.revision) == before[r.timestamp]
    assert len(rows) == 76 * 4
    assert report.new_bars == 36 + 76 + 76
    assert report.per_day_added == {"2025-01-07": 36, "2025-01-08": 76, "2025-01-09": 76}
    assert report.stored["write"]["updated"] == 0
    stamps = [r.timestamp for r in rows]
    assert len(stamps) == len(set(stamps))


def test_gap_fill_measures_the_seam_on_a_mixed_day(db):
    seed(db, date(2025, 1, 7), bars=40, close=100.0)
    client = FakeClient(default=angel_days(date(2025, 1, 7), date(2025, 1, 8),
                                           date(2025, 1, 9), close=100.5))

    report = fill(db, client, start=date(2025, 1, 7))

    assert report.overlap.bars_compared == 40
    assert report.overlap.max_close_diff == pytest.approx(0.5)


def test_gap_fill_dry_run_writes_nothing(db):
    client = FakeClient(default=angel_days(date(2025, 1, 8)))
    report = fill(db, client, start=date(2025, 1, 8), end=date(2025, 1, 8), dry_run=True)

    assert report.new_bars == 76
    assert stored_rows(db) == []


def test_gap_fill_with_nothing_missing_reaches_no_network(db):
    for d in (6, 7, 8, 9):
        seed(db, date(2025, 1, d))
    client = FakeClient(default=angel_days(date(2025, 1, 8)))

    report = fill(db, client)

    assert client.requests == []
    assert report.missing == [] and report.fetch is None


def test_gap_fill_ignores_bars_on_days_that_were_not_short(db):
    seed(db, date(2025, 1, 6))
    seed(db, date(2025, 1, 8))
    client = FakeClient(default=angel_days(date(2025, 1, 6), date(2025, 1, 7),
                                           date(2025, 1, 8), date(2025, 1, 9)))

    report = fill(db, client, end=date(2025, 1, 9))

    assert set(report.per_day_added) == {"2025-01-07", "2025-01-09"}
    assert {r.source for r in stored_rows(db)
            if ah._session_date(r.timestamp) in (date(2025, 1, 6), date(2025, 1, 8))} == {"free"}


# ---- the instrument master, and why it is cached ---------------------------
#
# Measured on 15-Sep-2026. Angel's instrument master is a single ~34MB JSON
# over one connection, and it truncated twice that afternoon — at 23.1MB and
# at 8.4MB — each time as a clean 200 whose body simply stopped. There was no
# retry and no cache, so each failure left the desk with no option universe
# at all for the rest of the session.
#
# That is not a cosmetic outage. With no universe the live option chain never
# subscribes, and the desk silently serves the polled NSE snapshot instead:
# roughly 60 seconds behind the market where the stream is roughly 400ms. The
# only trace was one WARNING in the log. These tests hold the three
# behaviours that close that hole.

MASTER_ROWS = [{"token": "1", "name": "NIFTY", "exch_seg": "NFO",
                "instrumenttype": "OPTIDX", "expiry": "22SEP2026",
                "strike": "2320000", "symbol": "NIFTY22SEP2623200CE"}]


class FlakyMaster:
    """Angel's download: truncates a few times, then completes."""

    def __init__(self, fail_times, body=None):
        self.fail_times = fail_times
        self.calls = 0
        self.body = json.dumps(body if body is not None else MASTER_ROWS)

    def __call__(self, url, timeout=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(
                "peer closed connection without sending complete message "
                "body (received 8415974 bytes, expected 34576518)")
        return SimpleNamespace(text=self.body, raise_for_status=lambda: None)


@pytest.fixture
def instant_retries(monkeypatch):
    """The retry pause, skipped — the backoff is tested by call count."""
    monkeypatch.setattr(ah.time, "sleep", lambda _s: None)


def test_todays_cached_master_is_used_without_touching_the_network(tmp_path):
    """A restart must not spend 34MB re-fetching this morning's file.

    This is what made the outage so easy to hit: every restart paid the
    download again, so every restart was another chance to truncate.
    """
    day = date(2026, 9, 15)
    cached = tmp_path / f"instrument-master-{day.isoformat()}.json"
    cached.write_text(json.dumps(MASTER_ROWS))

    def explode(*a, **k):                       # pragma: no cover
        raise AssertionError("the network was touched despite a fresh cache")

    import httpx
    original = httpx.get
    httpx.get = explode
    try:
        rows = ah.load_master(cache_dir=tmp_path, on=day)
    finally:
        httpx.get = original

    assert rows == MASTER_ROWS


def test_a_truncated_download_is_retried_rather_than_abandoned(
        tmp_path, instant_retries, monkeypatch):
    """Two truncations then a good body must still produce a master."""
    import httpx
    flaky = FlakyMaster(fail_times=2)
    monkeypatch.setattr(httpx, "get", flaky)

    rows = ah.load_master(cache_dir=tmp_path, on=date(2026, 9, 15))

    assert rows == MASTER_ROWS
    assert flaky.calls == 3, "the download must be retried, not abandoned"


def test_a_completed_download_is_cached_for_the_day(
        tmp_path, instant_retries, monkeypatch):
    import httpx
    flaky = FlakyMaster(fail_times=0)
    monkeypatch.setattr(httpx, "get", flaky)
    day = date(2026, 9, 15)

    ah.load_master(cache_dir=tmp_path, on=day)
    ah.load_master(cache_dir=tmp_path, on=day)

    assert flaky.calls == 1, "the second call must be served from the cache"
    assert (tmp_path / f"instrument-master-{day.isoformat()}.json").exists()


def test_a_master_that_will_not_download_falls_back_to_yesterdays(
        tmp_path, instant_retries, monkeypatch):
    """The judgement call, stated plainly.

    Yesterday's tokens are still today's tokens — an expiry does not move
    and a contract does not get renumbered. The only thing an old master
    can lack is something listed this morning. Serving the live option
    chain from slightly old contract list beats dropping every option to a
    sixty-second poll, so long as it says so.
    """
    import httpx
    yesterday = date(2026, 9, 14)
    (tmp_path / f"instrument-master-{yesterday.isoformat()}.json").write_text(
        json.dumps(MASTER_ROWS))
    monkeypatch.setattr(httpx, "get", FlakyMaster(fail_times=99))

    rows = ah.load_master(cache_dir=tmp_path, on=date(2026, 9, 15))

    assert rows == MASTER_ROWS


def test_a_master_that_will_not_download_with_no_cache_raises(
        tmp_path, instant_retries, monkeypatch):
    """With nothing to fall back on it must fail loudly, not return []."""
    import httpx
    monkeypatch.setattr(httpx, "get", FlakyMaster(fail_times=99))

    with pytest.raises(ah.AngelHistoryError):
        ah.load_master(cache_dir=tmp_path, on=date(2026, 9, 15))


def test_a_corrupt_cache_is_discarded_rather_than_served(
        tmp_path, instant_retries, monkeypatch):
    """A half-written file must not poison the whole session."""
    import httpx
    day = date(2026, 9, 15)
    corrupt = tmp_path / f"instrument-master-{day.isoformat()}.json"
    corrupt.write_text('[{"token": "1", "na')        # truncated mid-row
    flaky = FlakyMaster(fail_times=0)
    monkeypatch.setattr(httpx, "get", flaky)

    rows = ah.load_master(cache_dir=tmp_path, on=day)

    assert rows == MASTER_ROWS
    assert flaky.calls == 1


def test_the_fetch_seam_still_bypasses_all_of_it(tmp_path):
    """Existing callers that inject their own fetch are untouched."""
    assert ah.load_master(fetch=lambda: MASTER_ROWS) == MASTER_ROWS
