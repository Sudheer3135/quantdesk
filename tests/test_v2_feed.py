"""India VIX and strategy v2's contracts on the shared Angel socket.

The test that matters most is the first: VIX and NIFTY arrive on the same
segment in the same LTP shape, so a routing mistake would not raise — it
would draw the index at fifteen.
"""
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import get_settings
from app.data import option_universe as ou
from app.workers import angel_feed as af
from app.workers import option_chain_live, vix_live
from app.workers.option_chain_live import LiveChain
from test_option_stream import master_row, option_frame

NOW = datetime(2026, 9, 21, 5, 0, tzinfo=UTC)        # Mon 10:30 IST
STAMP = int(NOW.timestamp() * 1000)


def ltp(token, paise):
    return {"token": token, "sequence_number": 1, "exchange_timestamp": STAMP,
            "last_traded_price": paise, "subscription_mode": 1}


class Socket:
    def __init__(self):
        self.subscriptions = []

    def subscribe(self, correlation_id, mode, token_list):
        self.subscriptions.append((correlation_id, mode, token_list))


@pytest.fixture
def stores(monkeypatch):
    published_vix = []
    vix = vix_live.VixStore(clock=lambda: NOW, publish=published_vix.append)
    main, v2 = LiveChain(clock=lambda: NOW), LiveChain(clock=lambda: NOW)
    monkeypatch.setattr(vix_live, "VIX", vix)
    monkeypatch.setattr(option_chain_live, "CHAIN", main)
    monkeypatch.setattr(option_chain_live, "V2_CHAIN", v2)
    monkeypatch.setattr(option_chain_live, "LISTED", option_chain_live.ListedExpiries())
    return vix, main, v2, published_vix


def a_feed(published):
    return af.AngelFeed(login_fn=lambda: None, socket_factory=lambda s: None,
                        publish_fn=lambda *a, **k: published.append((a, k)) or {},
                        clock=lambda: NOW)


def test_a_vix_print_is_never_published_as_the_index(stores):
    vix, _, _, published_vix = stores
    published = []
    feed = a_feed(published)

    feed._on_data(None, ltp("99926017", 1432))

    assert published == [], "VIX reached the price channel"
    assert feed.stats.ticks == 0 and feed.stats.last_price is None
    assert feed.stats.vix_ticks == 1
    assert vix.current() == 14.32
    assert published_vix[0]["value"] == 14.32


def test_the_index_still_publishes_beside_it(stores):
    published = []
    feed = a_feed(published)

    feed._on_data(None, ltp("99926017", 1432))
    feed._on_data(None, ltp("99926000", 2433455))

    assert len(published) == 1
    assert published[0][0][1] == 24334.55
    assert feed.stats.ticks == 1 and feed.stats.vix_ticks == 1


def test_a_stale_vix_is_not_offered_as_current(stores):
    vix, *_ = stores
    old = datetime(2026, 9, 21, 4, 0, tzinfo=UTC)
    vix.update(type("T", (), {"price": 14.0, "source_time": old})())
    assert vix.current() is None
    assert vix.last().value == 14.0


def test_switching_vix_off_leaves_only_the_index_subscribed(stores, monkeypatch):
    monkeypatch.setenv("ANGEL_VIX_TOKEN", "")
    get_settings.cache_clear()
    feed = a_feed([])
    feed._socket = Socket()
    feed._on_open()
    assert feed._socket.subscriptions[0][2] == [{"exchangeType": 1, "tokens": ["99926000"]}]
    get_settings.cache_clear()


def two_expiry_master():
    rows, token = [], 50000
    for expiry in ("22SEP2026", "29SEP2026"):
        for strike in range(23500, 24550, 50):
            for kind in ("CE", "PE"):
                row = master_row(token, strike, kind, expiry)
                row["lotsize"] = "65"
                rows.append(row)
                token += 1
    return rows


def test_v2_ticks_go_to_the_v2_store_without_counting_as_unknown(stores):
    _, main, v2, _ = stores
    rows = two_expiry_master()
    main.set_universe(ou.build(rows, 24000.0, band=2, expiry=date(2026, 9, 22)))
    v2.set_universe(ou.build(rows, 24000.0, band=2, expiry=date(2026, 9, 29)))
    feed = a_feed([])

    token = v2.universe.contracts[0].token
    feed._on_data(None, option_frame(token, stamp=STAMP))

    assert feed.stats.option_ticks == 1
    assert v2.stats.ticks == 1
    assert main.stats.ticks == 0 and main.stats.unknown_token == 0
    assert option_chain_live.chain_for(date(2026, 9, 29)) is v2
    assert option_chain_live.chain_for(date(2026, 9, 22)) is main


def test_the_day_before_expiry_v2_streams_next_week(stores, monkeypatch):
    _, main, v2, _ = stores
    monkeypatch.setenv("V2_PAPER_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr("app.market_hours.trading_date", lambda *a: date(2026, 9, 21))
    rows = two_expiry_master()
    main.set_universe(ou.build(rows, 24000.0, band=20, expiry=date(2026, 9, 22)))
    feed = a_feed([])
    feed._socket = Socket()

    feed._maintain_v2_universe(rows, 24000.0, get_settings())

    assert v2.universe.expiry == date(2026, 9, 29)
    assert {c.lot_size for c in v2.universe.contracts} == {65}
    ids = [s[0] for s in feed._socket.subscriptions]
    assert ids == ["quantdesk-options-v2"]
    assert option_chain_live.LISTED.get() == [date(2026, 9, 22), date(2026, 9, 29)]
    get_settings.cache_clear()


def test_when_v2_wants_the_nearest_expiry_nothing_extra_is_subscribed(stores, monkeypatch):
    _, main, v2, _ = stores
    monkeypatch.setattr("app.market_hours.trading_date", lambda *a: date(2026, 9, 17))
    rows = two_expiry_master()
    main.set_universe(ou.build(rows, 24000.0, band=20, expiry=date(2026, 9, 22)))
    feed = a_feed([])
    feed._socket = Socket()

    feed._maintain_v2_universe(rows, 24000.0, get_settings())

    assert feed._socket.subscriptions == []
    assert option_chain_live.chain_for(date(2026, 9, 22)) is main


def test_a_quote_list_keeps_one_sided_contracts(stores):
    _, main, _, _ = stores
    rows = two_expiry_master()
    main.set_universe(ou.build(rows, 24000.0, band=1, expiry=date(2026, 9, 22)))
    call = [c for c in main.universe.contracts if c.option_type == "CE"][0]
    feed = a_feed([])
    feed._on_data(None, option_frame(call.token, stamp=STAMP))

    assert main.snapshot()["strikes"] == 0            # the chain frame needs both sides
    assert [q.contract.token for q in main.quotes()] == [call.token]
