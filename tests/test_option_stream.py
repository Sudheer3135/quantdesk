"""The streamed option chain.

The polled chain is a snapshot up to sixty seconds old; this one is
assembled from SNAP_QUOTE ticks arriving in roughly four hundred
milliseconds. Most of these tests exist to hold that speed to the same
honesty standard as the slow path — a fast chain that quietly reports a
stale strike is worse than a slow one that admits its age.

The tests that matter most:

  * `test_a_quiet_strike_is_priced_from_the_book_not_rejected` — an option
    with no trade still has a quote, and the index decoder's "zero LTP is
    not a price" rule would throw away half the chain.
  * `test_a_stale_contract_is_dropped_rather_than_reported` — the chain is
    a composite of prints of different ages, and an old premium in an OI
    table is invisible.
  * `test_option_ticks_never_mark_the_price_feed_healthy` — both ride one
    socket, and a busy chain must not disguise a dead index.
"""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.brokers import angel as angel_api
from app.data import option_universe as ou
from app.workers.option_chain_live import LiveChain

NOW = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)
STAMP = int(NOW.timestamp() * 1000)


def master_row(token, strike, kind, expiry="01SEP2026"):
    return {"token": str(token), "symbol": f"NIFTY01SEP26{int(strike)}{kind}",
            "name": "NIFTY", "exch_seg": "NFO", "instrumenttype": "OPTIDX",
            "strike": f"{strike * 100:.6f}", "expiry": expiry, "lotsize": "75"}


def master(strikes=range(23500, 24600, 50), expiry="01SEP2026"):
    rows = []
    token = 40000
    for strike in strikes:
        for kind in ("CE", "PE"):
            rows.append(master_row(token, strike, kind, expiry))
            token += 1
    # A little noise, so the filters have something to exclude.
    rows.append({"token": "99926000", "symbol": "Nifty 50", "name": "NIFTY",
                 "exch_seg": "NSE", "instrumenttype": "AMXIDX",
                 "strike": "-1.000000", "expiry": ""})
    rows.append({**master_row(90001, 24000, "CE"), "name": "BANKNIFTY"})
    return rows


def option_frame(token, *, ltp=100.0, oi=1000, volume=500,
                 bid=99.0, ask=101.0, stamp=STAMP):
    """A SNAP_QUOTE frame in the SDK's parsed shape."""
    frame = {
        "subscription_mode": 3, "subscription_mode_val": "SNAP_QUOTE",
        "exchange_type": angel_api.NSE_FO, "token": str(token),
        "sequence_number": 1, "exchange_timestamp": stamp,
        "last_traded_price": int(ltp * 100),
        "open_interest": oi, "volume_trade_for_the_day": volume,
        "best_5_buy_data": [], "best_5_sell_data": [],
    }
    if bid is not None:
        frame["best_5_buy_data"] = [{"buy_sell_flag": 1, "price": int(bid * 100),
                                     "quantity": 50}]
    if ask is not None:
        frame["best_5_sell_data"] = [{"buy_sell_flag": 0, "price": int(ask * 100),
                                      "quantity": 50}]
    return frame


# ---- the universe -----------------------------------------------------

def test_the_universe_is_a_band_around_spot_on_the_nearest_expiry():
    universe = ou.build(master(), spot=24000.0, band=4,
                        on=datetime(2026, 8, 29).date())

    assert universe.expiry.isoformat() == "2026-09-01"
    assert universe.strikes == [23800.0, 23850.0, 23900.0, 23950.0, 24000.0,
                                24050.0, 24100.0, 24150.0, 24200.0]
    assert len(universe.contracts) == 18, "both sides of every strike"
    assert all(c.option_type in ("CE", "PE") for c in universe.contracts)


def test_other_underlyings_and_the_index_itself_are_excluded():
    universe = ou.build(master(), spot=24000.0, band=40,
                        on=datetime(2026, 8, 29).date())

    tokens = set(universe.tokens)
    assert "99926000" not in tokens, "the index is not an option"
    assert "90001" not in tokens, "BANKNIFTY is not NIFTY"


def test_strikes_are_converted_out_of_paise():
    universe = ou.build(master(), spot=24000.0, band=1,
                        on=datetime(2026, 8, 29).date())
    assert 24000.0 in universe.strikes
    assert 2400000.0 not in universe.strikes


def test_an_empty_master_yields_an_empty_universe_rather_than_raising():
    """A feed that cannot find contracts falls back; it does not fail."""
    universe = ou.build([], spot=24000.0)

    assert universe.contracts == []
    assert universe.tokens == []
    assert universe.needs_refresh(24000.0)


def test_the_band_is_refreshed_only_once_spot_nears_its_edge():
    universe = ou.build(master(), spot=24000.0, band=10,
                        on=datetime(2026, 8, 29).date())

    # Pinned to a day inside the contracts' life so this tests drift and
    # only drift. Left unpinned it silently became a calendar test: once
    # the fixture's expiry fell into the past every assertion here was
    # answered by the expiry check before drift was ever consulted.
    live = universe.expiry

    assert not universe.needs_refresh(24000.0, margin=5, on=live)
    assert not universe.needs_refresh(24200.0, margin=5, on=live)
    assert universe.needs_refresh(24300.0, margin=5, on=live), "spot at the edge"
    assert universe.needs_refresh(23600.0, margin=5, on=live)


def test_a_universe_is_rebuilt_once_its_expiry_has_passed():
    """The morning-after bug, and why drift could never catch it.

    Measured 16-Sep-2026: the desk was still subscribed to eighty
    15-Sep contracts the day after they expired. Those tokens never
    print again, so the live chain sat at 0 of 80 quoted for the whole
    session and every option silently fell back to the 60-second NSE
    poll — roughly 150x slower than the stream it replaced.

    Drift cannot see this. The strikes are still perfectly centred on
    spot; they are simply dead. An expiry is a fact about the calendar,
    so it has to be asked about separately or it is never asked at all.
    """
    universe = ou.build(master(), spot=24000.0, band=10,
                        on=datetime(2026, 8, 29).date())
    expiry = universe.expiry

    # Perfectly centred, and on any day up to the expiry that is enough.
    assert not universe.needs_refresh(24000.0, margin=5, on=expiry)

    # The next morning the same well-centred band is worthless.
    assert universe.needs_refresh(
        24000.0, margin=5, on=expiry + timedelta(days=1)), (
        "a universe whose contracts have expired must be rebuilt even "
        "though spot has not moved")


def test_expiry_day_itself_is_still_a_trading_day():
    """`<` and not `<=`.

    Contracts settle at the close, so they trade all through their own
    expiry — and that is their heaviest session. Rebuilding at 09:15 on
    the expiry would throw away the most active day the universe has.
    """
    universe = ou.build(master(), spot=24000.0, band=10,
                        on=datetime(2026, 8, 29).date())

    assert not universe.needs_refresh(24000.0, margin=5, on=universe.expiry)


# ---- decoding ---------------------------------------------------------

def test_open_interest_and_the_top_of_book_are_decoded():
    tick = angel_api.decode_option_tick(
        option_frame("40001", ltp=123.45, oi=98765, bid=123.0, ask=124.0),
        now=NOW)

    assert tick.price == 123.45, "paise must be converted"
    assert tick.open_interest == 98765
    assert tick.bid == 123.0
    assert tick.ask == 124.0
    assert tick.spread == 1.0


def test_a_quiet_strike_is_priced_from_the_book_not_rejected():
    """The rule that differs from the index decoder.

    An option that has not traded still has a two-sided quote, and rejecting
    a zero LTP the way the index feed does would silently drop every quiet
    strike — which is most of the wings.
    """
    tick = angel_api.decode_option_tick(
        option_frame("40001", ltp=0.0, bid=10.0, ask=12.0), now=NOW)

    assert tick.price == 11.0, "mid of the book"
    assert tick.bid == 10.0 and tick.ask == 12.0


def test_a_contract_with_neither_trade_nor_quote_is_refused():
    with pytest.raises(angel_api.MalformedTick, match="neither"):
        angel_api.decode_option_tick(
            option_frame("40001", ltp=0.0, bid=None, ask=None), now=NOW)


def test_the_best_bid_is_the_highest_and_the_best_ask_the_lowest():
    frame = option_frame("40001", ltp=100.0)
    frame["best_5_buy_data"] = [
        {"buy_sell_flag": 1, "price": 9800}, {"buy_sell_flag": 1, "price": 9900},
        {"buy_sell_flag": 1, "price": 0}]
    frame["best_5_sell_data"] = [
        {"buy_sell_flag": 0, "price": 10200}, {"buy_sell_flag": 0, "price": 10100},
        {"buy_sell_flag": 0, "price": 0}]

    tick = angel_api.decode_option_tick(frame, now=NOW)

    assert tick.bid == 99.0, "highest bid"
    assert tick.ask == 101.0, "lowest ask"


def test_a_future_timestamp_is_refused_by_the_shared_guard():
    """The option decoder must inherit the index decoder's clock guard."""
    ahead = int((NOW + timedelta(hours=5, minutes=30)).timestamp() * 1000)

    with pytest.raises(angel_api.MalformedTick, match="future"):
        angel_api.decode_option_tick(option_frame("40001", stamp=ahead), now=NOW)


def test_a_multi_segment_subscription_groups_by_exchange():
    payload = angel_api.token_lists({angel_api.NSE_FO: ["1", "2"],
                                     angel_api.NSE_CM: ["99926000"]})

    assert payload == [{"exchangeType": 1, "tokens": ["99926000"]},
                       {"exchangeType": 2, "tokens": ["1", "2"]}]


# ---- the chain --------------------------------------------------------

@pytest.fixture
def chain():
    store = LiveChain(clock=lambda: NOW)
    store.set_universe(ou.build(master(), spot=24000.0, band=2,
                                on=datetime(2026, 8, 29).date()))
    return store


def feed(store, universe, *, strikes=None, **kwargs):
    """Push one tick per contract for the given strikes."""
    for contract in universe.contracts:
        if strikes and contract.strike not in strikes:
            continue
        store.update(angel_api.decode_option_tick(
            option_frame(contract.token, **kwargs), now=NOW))


def test_a_chain_is_built_once_both_sides_of_a_strike_have_quoted(chain):
    feed(chain, chain.universe)

    snap = chain.snapshot(now=NOW)
    assert snap["strikes"] == 5
    assert list(snap["frame"].columns)[:3] == ["strike", "call_oi", "put_oi"]
    assert snap["one_sided"] == 0


def test_a_one_sided_strike_is_not_reported_as_a_chain_row(chain):
    """Half a strike is not a chain row.

    `summarise` reads call against put open interest, so a row with one
    side would report a put/call ratio of infinity.
    """
    call = next(c for c in chain.universe.contracts if c.option_type == "CE")
    chain.update(angel_api.decode_option_tick(option_frame(call.token), now=NOW))

    snap = chain.snapshot(now=NOW)
    assert snap["strikes"] == 0
    assert snap["one_sided"] == 1
    assert snap["frame"].empty


def test_a_stale_contract_is_dropped_rather_than_reported(chain):
    feed(chain, chain.universe)
    much_later = NOW + timedelta(seconds=chain.max_age_seconds + 60)

    snap = chain.snapshot(now=much_later)

    assert snap["strikes"] == 0
    assert snap["dropped_stale"] == len(chain.universe.contracts)


def test_the_snapshot_reports_the_age_of_its_oldest_print(chain):
    feed(chain, chain.universe)
    later = NOW + timedelta(seconds=42)

    snap = chain.snapshot(now=later)

    assert snap["oldest_age_seconds"] == pytest.approx(42.0)
    assert snap["newest_age_seconds"] == pytest.approx(42.0)


def test_a_tick_without_open_interest_does_not_erase_the_last_one(chain):
    """Absent is not zero.

    An OI table that blinks to zero would move max-pain to a strike nobody
    is at, and it would do so silently.
    """
    contract = chain.universe.contracts[0]
    chain.update(angel_api.decode_option_tick(
        option_frame(contract.token, oi=5000), now=NOW))

    frame = option_frame(contract.token, oi=None)
    frame.pop("open_interest")
    chain.update(angel_api.decode_option_tick(frame, now=NOW))

    quote = chain._quotes[contract.token]
    assert quote.open_interest == 5000


def test_a_tick_for_an_unsubscribed_token_is_counted_and_dropped(chain):
    applied = chain.update(angel_api.decode_option_tick(
        option_frame("11111111"), now=NOW))

    assert applied is False
    assert chain.stats.unknown_token == 1
    assert chain.snapshot(now=NOW)["contracts"] == 0


def test_re_centring_discards_quotes_that_left_the_band(chain):
    """Otherwise an unsubscribed wing ages quietly, looking live."""
    feed(chain, chain.universe)
    assert chain.snapshot(now=NOW)["contracts"] == 10

    chain.set_universe(ou.build(master(), spot=24400.0, band=2,
                                on=datetime(2026, 8, 29).date()))

    snap = chain.snapshot(now=NOW)
    assert snap["contracts"] == 0, "the old band's quotes must not persist"
    assert chain.stats.rebuilds == 2


def test_the_chain_is_not_ready_until_three_strikes_are_two_sided(chain):
    assert not chain.ready

    two = chain.universe.strikes[:2]
    feed(chain, chain.universe, strikes=set(two))
    assert not chain.ready

    feed(chain, chain.universe)
    assert chain.ready


def test_the_frame_carries_the_expiry_the_engine_needs(chain):
    feed(chain, chain.universe)
    snap = chain.snapshot(now=NOW)

    assert snap["frame"].attrs["expiry"] == "01-Sep-2026"
    assert snap["expiry"] == "2026-09-01"


def test_the_chain_summarises_without_error(chain):
    """The whole point: this frame must satisfy the existing analytics."""
    from app.analytics import options

    feed(chain, chain.universe)
    frame = chain.snapshot(now=NOW)["frame"]

    summary = options.summarise(frame, 24000.0)
    assert summary.to_dict()["pcr_oi"] is not None
    assert summary.max_pain is not None


# ---- routing on the shared socket -------------------------------------

def test_option_frames_are_routed_away_from_the_index_decoder():
    from app.workers.angel_feed import AngelFeed

    assert AngelFeed._is_option_frame(option_frame("40001")) is True
    assert AngelFeed._is_option_frame(
        {"exchange_type": angel_api.NSE_CM, "token": "99926000"}) is False
    assert AngelFeed._is_option_frame(b"not a dict") is False


def test_option_ticks_never_mark_the_price_feed_healthy(monkeypatch):
    """Both ride one socket. A busy chain must not hide a dead index."""
    from app.workers import angel_feed as af
    from app.workers.angel_feed import AngelFeed

    store = LiveChain(clock=lambda: NOW)
    store.set_universe(ou.build(master(), spot=24000.0, band=2,
                                on=datetime(2026, 8, 29).date()))
    monkeypatch.setattr(af, "CHAIN", store, raising=False)
    monkeypatch.setattr(af.option_chain_live, "CHAIN", store)

    feed_ = AngelFeed(login_fn=lambda: None, socket_factory=lambda s: None,
                      publish_fn=lambda *a, **k: {}, clock=lambda: NOW)
    contract = store.universe.contracts[0]

    feed_._on_data(None, option_frame(contract.token))

    assert feed_.stats.option_ticks == 1
    assert feed_.stats.ticks == 0, "an option tick is not an index tick"
    assert feed_.stats.last_price is None
    assert feed_.stats.last_tick_at is None
    assert feed_.healthy is False


def test_a_malformed_option_frame_is_counted_not_raised(monkeypatch):
    from app.workers import angel_feed as af
    from app.workers.angel_feed import AngelFeed

    store = LiveChain(clock=lambda: NOW)
    monkeypatch.setattr(af.option_chain_live, "CHAIN", store)
    feed_ = AngelFeed(login_fn=lambda: None, socket_factory=lambda s: None,
                      publish_fn=lambda *a, **k: {}, clock=lambda: NOW)

    feed_._on_data(None, {"exchange_type": angel_api.NSE_FO, "token": "1",
                          "last_traded_price": 0, "exchange_timestamp": STAMP})

    assert feed_.stats.option_malformed == 1
    assert feed_.stats.ticks == 0
