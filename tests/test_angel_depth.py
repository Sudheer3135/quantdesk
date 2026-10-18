"""Angel market depth, from the SDK's own parser to the v2 selector.

Measured 01-Oct-2026: every one of 640 contract quotes captured as Phase 3B
evidence had an ask and no bid, and all eight contract selections that day
were refused `no_two_sided_quote`. The decoder was filtering depth levels on
`buy_sell_flag`, a key SmartAPI never sends — the SDK calls it `flag` — so
every bid level was discarded while every ask survived (a missing key read
as "not a buy", which is to say "an ask").

These tests build real SNAP_QUOTE binary packets and parse them with the
installed SDK, so the shape under test is the SDK's and not one written
from memory. No raw live frame was retained; the SDK parser is the basis.
"""
import struct
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import option_pricing
from app.brokers import angel as angel_api
from app.data import option_universe as ou
from app.strategy_v2 import rules
from app.strategy_v2.config import V2Config
from app.strategy_v2.paper import PaperTrader
from app.workers.option_chain_live import LiveChain

sdk = pytest.importorskip("SmartApi.smartWebSocketV2")

NOW = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)
STAMP = int(NOW.timestamp() * 1000)
SPOT = 24000.0
YEARS = 5 / 365
IV = 0.12


def packet(token, *, ltp, bids=(), asks=(), stamp=STAMP, oi=1000, volume=500):
    """A SNAP_QUOTE binary frame laid out the way the SDK reads it.

    Ten 20-byte depth levels at bytes 147-347, each <flag:H qty:q price:q
    orders:H>, bids flagged 1 and asks 0, unused levels left zero.
    """
    buf = bytearray(379)
    struct.pack_into("<BB", buf, 0, 3, angel_api.NSE_FO)      # SNAP_QUOTE, NFO
    buf[2:2 + len(token)] = token.encode()
    struct.pack_into("<qqq", buf, 27, 1, stamp, int(round(ltp * 100)))
    struct.pack_into("<q", buf, 67, volume)
    struct.pack_into("<q", buf, 131, oi)
    levels = [(1, p) for p in bids] + [(0, p) for p in asks]
    assert len(levels) <= 10
    for i, (flag, price) in enumerate(levels):
        struct.pack_into("<HqqH", buf, 147 + 20 * i, flag, 75, int(round(price * 100)), 3)
    return bytes(buf)


def sdk_parse(raw: bytes) -> dict:
    """The installed SDK's parse, exactly as its socket hands frames to us."""
    socket = object.__new__(sdk.SmartWebSocketV2)
    return socket._parse_binary_data(raw)


def decode(frame):
    return angel_api.decode_option_tick(frame, now=NOW)


# ---- the SDK contract this decoder relies on -------------------------------

def test_the_sdk_shape_is_flag_keyed_and_split_by_side():
    frame = sdk_parse(packet("40001", ltp=100.0, bids=(99.0, 98.5), asks=(101.0,)))

    for level in frame["best_5_buy_data"] + frame["best_5_sell_data"]:
        assert set(level) == {"flag", "quantity", "price", "no of orders"}
        assert "buy_sell_flag" not in level
    assert {lv["flag"] for lv in frame["best_5_buy_data"]} == {1}
    # The sell list also carries the zero padding: flag 0, price 0.
    assert {lv["flag"] for lv in frame["best_5_sell_data"]} == {0}
    assert sorted(lv["price"] for lv in frame["best_5_buy_data"]) == [9850, 9900]
    assert 10100 in [lv["price"] for lv in frame["best_5_sell_data"]]


# ---- decoding --------------------------------------------------------------

def test_an_sdk_frame_yields_both_sides_of_the_book():
    tick = decode(sdk_parse(packet("40001", ltp=123.45,
                                   bids=(123.05, 122.9, 123.1, 121.0, 122.0),
                                   asks=(124.2, 123.8, 124.0, 125.0, 123.85))))

    assert tick.price == 123.45, "paise converted"
    assert tick.bid == 123.1, "highest bid"
    assert tick.ask == 123.8, "lowest ask"
    assert tick.spread == 0.7


def test_zero_padded_levels_are_absent_not_zero_prices():
    tick = decode(sdk_parse(packet("40001", ltp=100.0, bids=(99.0,), asks=(101.0,))))
    # Eight padded levels (flag 0, price 0) sit in the sell list beside 101.
    assert tick.bid == 99.0 and tick.ask == 101.0

    tick = decode(sdk_parse(packet("40001", ltp=100.0)))
    assert tick.bid is None and tick.ask is None


def test_an_empty_book_is_unavailable():
    frame = sdk_parse(packet("40001", ltp=100.0))
    frame["best_5_buy_data"], frame["best_5_sell_data"] = [], []

    tick = decode(frame)
    assert tick.bid is None and tick.ask is None


@pytest.mark.parametrize("marker", ["missing", None, "1", "0", True, False,
                                    1.0, 0.0, -1])
def test_a_level_without_a_valid_flag_is_on_neither_side(marker):
    """Above all, never an ask by default — the original defect's shape."""
    def level(price):
        lv = {"quantity": 75, "price": int(price * 100), "no of orders": 3}
        if marker != "missing":
            lv["flag"] = marker
        return lv

    frame = sdk_parse(packet("40001", ltp=100.0))
    frame["best_5_buy_data"] = [level(99.0)]
    frame["best_5_sell_data"] = [level(101.0)]

    tick = decode(frame)
    assert tick.bid is None
    assert tick.ask is None


def test_the_retired_buy_sell_flag_shape_is_not_read_as_either_side():
    frame = sdk_parse(packet("40001", ltp=100.0))
    frame["best_5_buy_data"] = [{"buy_sell_flag": 1, "price": 9900}]
    frame["best_5_sell_data"] = [{"buy_sell_flag": 0, "price": 10100}]

    tick = decode(frame)
    assert tick.bid is None and tick.ask is None


def test_a_level_in_the_wrong_list_does_not_leak_across():
    frame = sdk_parse(packet("40001", ltp=100.0, bids=(99.0,), asks=(101.0,)))
    # An ask-flagged level among the bids, a bid-flagged one among the asks,
    # each priced to win if it were counted.
    frame["best_5_buy_data"].append(
        {"flag": 0, "quantity": 75, "price": 15000, "no of orders": 1})
    frame["best_5_sell_data"].append(
        {"flag": 1, "quantity": 75, "price": 5000, "no of orders": 1})

    tick = decode(frame)
    assert tick.bid == 99.0
    assert tick.ask == 101.0

    only_wrong = sdk_parse(packet("40001", ltp=100.0))
    only_wrong["best_5_buy_data"] = [
        {"flag": 0, "quantity": 75, "price": 9900, "no of orders": 1}]
    only_wrong["best_5_sell_data"] = [
        {"flag": 1, "quantity": 75, "price": 10100, "no of orders": 1}]
    tick = decode(only_wrong)
    assert tick.bid is None and tick.ask is None


def test_an_untraded_strike_with_no_usable_book_is_still_refused():
    frame = sdk_parse(packet("40001", ltp=0.0))
    frame["best_5_sell_data"] = [{"price": 10100, "quantity": 75}]   # no flag

    with pytest.raises(angel_api.MalformedTick, match="neither"):
        decode(frame)


def test_an_untraded_strike_is_priced_from_an_sdk_book():
    tick = decode(sdk_parse(packet("40001", ltp=0.0, bids=(10.0,), asks=(12.0,))))
    assert tick.price == 11.0 and tick.bid == 10.0 and tick.ask == 12.0


# ---- decoder -> feed -> live chain -> v2 candidate -> selector -------------

def _master():
    rows, token = [], 40000
    for strike in range(23500, 24600, 50):
        for kind in ("CE", "PE"):
            rows.append({"token": str(token), "symbol": f"NIFTY01SEP26{strike}{kind}",
                         "name": "NIFTY", "exch_seg": "NFO", "instrumenttype": "OPTIDX",
                         "strike": f"{strike * 100:.6f}", "expiry": "01SEP2026",
                         "lotsize": "75"})
            token += 1
    return rows


def _streamed_candidates(monkeypatch, *, half_spread=0.5, spread_scale=None,
                         age=0.0):
    """Push SDK-parsed frames through AngelFeed into a LiveChain and read
    them back the way the v2 paper trader does."""
    from app.workers import angel_feed as af
    from app.workers.angel_feed import AngelFeed

    store = LiveChain(clock=lambda: NOW)
    store.set_universe(ou.build(_master(), spot=SPOT, band=2, on=date(2026, 8, 29)))
    monkeypatch.setattr(af.option_chain_live, "CHAIN", store)
    feed = AngelFeed(login_fn=lambda: None, socket_factory=lambda s: None,
                     publish_fn=lambda *a, **k: {}, clock=lambda: NOW)

    stamp = int((NOW - timedelta(seconds=age)).timestamp() * 1000)
    for c in store.universe.contracts:
        fair = option_pricing.price(SPOT, c.strike, YEARS, IV, kind=c.option_type)
        half = fair * spread_scale if spread_scale else half_spread
        feed._on_data(None, sdk_parse(packet(
            c.token, ltp=round(fair, 2), stamp=stamp,
            bids=(round(fair - half, 2),), asks=(round(fair + half, 2),))))

    assert feed.stats.option_ticks == len(store.universe.contracts)
    return [PaperTrader._candidate(q, NOW) for q in store.quotes(now=NOW)]


def _pick(candidates, option_type="CE"):
    trace = {}
    pick, rejected = rules.pick_contract(candidates, option_type=option_type,
                                         spot=SPOT, years=YEARS, cfg=V2Config(),
                                         trace=trace)
    return pick, rejected, trace


def test_streamed_bids_reach_the_selector_and_a_compliant_contract_is_picked(monkeypatch):
    candidates = _streamed_candidates(monkeypatch)
    assert all(c.bid is not None and c.ask is not None for c in candidates)

    for option_type in ("CE", "PE"):
        pick, rejected, trace = _pick(candidates, option_type)
        assert rejected is None, rejected
        cfg = V2Config()
        assert cfg.min_delta <= abs(pick.delta) <= cfg.max_delta
        assert pick.spread_pct is not None and pick.spread_pct <= cfg.max_spread_pct
        assert pick.candidate.ask >= cfg.min_premium
        chosen = next(a for a in trace["alternatives"]
                      if a["token"] == pick.candidate.token)
        assert chosen["mid_basis"] == "bid_ask_mid"
        assert {c["check"]: c["status"] for c in chosen["liquidity_checks"]} == {
            "quote_age": "passed", "two_sided_quote": "passed",
            "spread": "passed", "premium_floor": "passed"}


def test_the_selector_sees_exactly_the_decoded_values(monkeypatch):
    """Same inputs, same outcome: hand-built candidates carrying the values
    the path delivered are picked identically, so nothing between the
    decoder and the selector reshapes them."""
    streamed = _streamed_candidates(monkeypatch)
    by_hand = [rules.Candidate(strike=c.strike, option_type=c.option_type,
                               ltp=c.ltp, bid=c.bid, ask=c.ask,
                               age_seconds=c.age_seconds, token=c.token,
                               symbol=c.symbol, lot_size=c.lot_size)
               for c in streamed]

    a, ra, _ = _pick(streamed)
    b, rb, _ = _pick(by_hand)
    assert ra is None and rb is None
    assert a.to_dict() == b.to_dict()


def test_without_bids_the_selector_refuses_exactly_as_it_did_on_01_oct(monkeypatch):
    """The pre-fix decoder's output — every bid None — fed to the unchanged
    selector reproduces the recorded refusal."""
    no_bids = [rules.Candidate(**{**c.__dict__, "bid": None})
               for c in _streamed_candidates(monkeypatch)]

    pick, rejected, trace = _pick(no_bids)
    assert pick is None
    assert rejected.code == rules.NO_DEPTH == "no_two_sided_quote"
    assert trace["result"]["code"] == "no_two_sided_quote"


def test_a_wide_book_is_still_refused_spread_too_wide(monkeypatch):
    pick, rejected, _ = _pick(_streamed_candidates(monkeypatch, spread_scale=0.10))
    assert pick is None
    assert rejected.code == rules.WIDE_SPREAD


def test_an_old_book_is_still_refused_quote_too_old(monkeypatch):
    pick, rejected, _ = _pick(_streamed_candidates(
        monkeypatch, age=V2Config().max_quote_age_seconds + 5))
    assert pick is None
    assert rejected.code == rules.STALE_QUOTE
