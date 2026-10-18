"""Paging the archive backwards, for a chart being panned.

`/market/candles` answers "what is happening now" from the broker and is
capped at 500 bars. That cap is correct there and fatal for a chart the user
can scroll: panning left has to keep finding older data, and the broker does
not serve years of it. This endpoint reads the archive instead.

Three things it must get right, each of which fails silently rather than
loudly if it does not: the seam between pages (a duplicate bar makes
lightweight-charts throw), the indicator warm-up (an EMA200 served from a
cold start is the mean of whatever happened to be loaded), and the end of
history (without `has_more` the chart asks for what is not there once per
pan event, forever).
"""
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.api import market as market_api
from app.brokers.base import UnknownSymbol
from app.data.importer import import_index_candles
from app.db import get_db
from test_importer import session_bars

# Weekdays in June 2026, avoiding 26-Jun (Muharram) which the validation
# gate rejects.
TRADING_DAYS = [16, 17, 18, 19, 22, 23, 24, 25]
BARS_PER_DAY = 75


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(market_api.router)
    app.dependency_overrides[get_db] = lambda: db

    # Mirrors the handler main.py registers centrally, so this harness
    # answers an unsupported symbol the way the running app does.
    @app.exception_handler(UnknownSymbol)
    async def _unknown(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    return TestClient(app)


@pytest.fixture
def seeded(db):
    for day in TRADING_DAYS:
        import_index_candles(db, session_bars(date(2026, 6, day),
                                              count=BARS_PER_DAY),
                             "NIFTY", "5m", "test")
    return len(TRADING_DAYS) * BARS_PER_DAY


def get(client, **params):
    r = client.get("/market/candles/history", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_serves_the_newest_window_when_no_cursor_is_given(client, seeded):
    body = get(client, limit=50)
    assert len(body["candles"]) == 50
    # The newest 50, not the oldest 50 — a chart opening on 16-Jun when the
    # archive runs to 25-Jun would look like a broken feed.
    assert body["candles"][-1]["timestamp"].startswith("2026-06-25")


def test_rows_come_back_oldest_first(client, seeded):
    stamps = [c["timestamp"] for c in get(client, limit=40)["candles"]]
    assert stamps == sorted(stamps)


def test_the_cursor_is_exclusive_so_pages_do_not_overlap(client, seeded):
    """A duplicate timestamp makes lightweight-charts throw."""
    first = get(client, limit=50)
    oldest = first["candles"][0]["timestamp"]

    second = get(client, limit=50, before=oldest)
    assert all(c["timestamp"] < oldest for c in second["candles"])

    stamps = [c["timestamp"] for c in second["candles"] + first["candles"]]
    assert len(stamps) == len(set(stamps))


def test_paging_walks_backwards_through_the_archive(client, seeded):
    page, seen, cursor = None, [], None
    for _ in range(20):
        page = get(client, limit=100, before=cursor)
        rows = page["candles"]
        if not rows:
            break
        seen = rows + seen
        cursor = page["oldest"]
        if not page["has_more"]:
            break

    stamps = [c["timestamp"] for c in seen]
    assert len(stamps) == len(set(stamps))
    assert stamps == sorted(stamps)
    assert len(stamps) == seeded


def test_has_more_goes_false_at_the_start_of_the_archive(client, seeded):
    """Without this the chart asks for what is not there on every pan."""
    body = get(client, limit=seeded + 500)
    assert body["has_more"] is False


def test_has_more_is_true_while_older_bars_remain(client, seeded):
    assert get(client, limit=20)["has_more"] is True


def test_an_empty_page_past_the_start_is_not_an_error(client, seeded):
    body = get(client, limit=50, before="2026-01-01T00:00:00+00:00")
    assert body["candles"] == []
    assert body["has_more"] is False
    assert body["oldest"] is None


def test_indicators_are_warmed_up_at_the_left_edge(client, seeded):
    """An EMA200 computed from a cold start is not slightly wrong.

    It is the mean of however many bars happened to be loaded — which, on a
    scrolled window, is whatever the user just panned into.
    """
    body = get(client, limit=50, before="2026-06-25T00:00:00+00:00")
    first = body["candles"][0]
    assert first["ema200"] is not None
    assert first["ema20"] is not None


def test_the_warm_up_bars_are_not_returned(client, seeded):
    """They exist to make the window's indicators right, not to be drawn."""
    body = get(client, limit=30, before="2026-06-25T00:00:00+00:00")
    assert len(body["candles"]) == 30


def test_the_same_bar_reads_the_same_from_either_page(client, seeded):
    """A bar's EMA must not depend on which request fetched it."""
    wide = get(client, limit=200)
    narrow = get(client, limit=20)
    by_stamp = {c["timestamp"]: c for c in wide["candles"]}
    for row in narrow["candles"]:
        # Not exact, and cannot be: `adjust=False` makes the EMA recursive,
        # so its seed's influence decays rather than vanishing. The warm-up
        # is sized to put that residue far below a tick — this asserts it is
        # there, not that it is zero.
        assert row["ema200"] == pytest.approx(
            by_stamp[row["timestamp"]]["ema200"], abs=0.01)


def test_only_the_window_is_read_from_the_database(client, seeded, monkeypatch):
    """The limit has to reach SQL, not be applied after the fact.

    Loading from the start of the archive and discarding the front is
    O(all history) per pan. It is invisible against a few thousand rows and
    becomes the dominant cost the moment a multi-year backfill lands —
    which is exactly when a chart that can scroll starts being used.
    """
    seen = {}
    real = market_api.repository.load_index_candles

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(market_api.repository, "load_index_candles", spy)
    get(client, limit=50)

    assert seen.get("newest") is True
    assert seen.get("limit") is not None
    assert seen["limit"] <= 50 + market_api.HISTORY_WARMUP_BARS + 1


def test_oldest_is_the_cursor_for_the_next_page(client, seeded):
    body = get(client, limit=40)
    assert body["oldest"] == body["candles"][0]["timestamp"]


def test_a_malformed_cursor_is_rejected_rather_than_ignored(client, seeded):
    """Ignoring it would silently serve the newest window instead, which
    reads as the chart refusing to scroll."""
    r = client.get("/market/candles/history", params={"before": "last tuesday"})
    assert r.status_code == 422


def test_the_page_size_is_bounded(client, seeded):
    r = client.get("/market/candles/history",
                   params={"limit": market_api.HISTORY_MAX_BARS + 1})
    assert r.status_code == 422


def test_an_empty_archive_answers_rather_than_failing(client):
    body = get(client, limit=50)
    assert body["candles"] == []
    assert body["has_more"] is False
    assert body["oldest"] is None


def test_an_unknown_symbol_is_rejected(client, seeded):
    r = client.get("/market/candles/history", params={"symbol": "NOTREAL"})
    assert r.status_code in (400, 404, 422)
