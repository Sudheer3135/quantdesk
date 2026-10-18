"""Reading publisher RSS, and refusing to render what has gone off.

The guard these are mostly about is staleness, and it is not hypothetical.
Moneycontrol's markets feed answers HTTP 200 with well-formed XML and items
dated 2024 — twenty thousand hours old when this was written. Nothing in the
response says so. A desk that trusted the status code would have put
two-year-old headlines beside a live price, and the only thing telling them
apart is a date nobody reads.

So the feed reader ages every item, drops what is past the window, and
reports a source that answered-but-carried-nothing as a *problem* rather
than as an empty success.
"""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app import net  # noqa: E402
from app.news import feeds  # noqa: E402

NOW = datetime(2026, 8, 25, 14, 0, tzinfo=UTC)
SOURCE = feeds.Source("Test Wire", "https://example.test/rss", "markets")


def rss(*items: str) -> bytes:
    body = "".join(items)
    return f"""<?xml version="1.0"?><rss version="2.0"><channel>
        <title>Test</title>{body}</channel></rss>""".encode()


def item(title="Nifty rallies to a record high", *, hours_old=1.0,
         link="https://example.test/a", description="Body text.",
         dated=True) -> str:
    stamp = ""
    if dated:
        when = NOW - timedelta(hours=hours_old)
        stamp = f"<pubDate>{when.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
    return (f"<item><title>{title}</title><link>{link}</link>"
            f"<description>{description}</description>{stamp}</item>")


# ------------------------------------------------------------- staleness

def test_a_stale_item_is_dropped():
    parsed = feeds.parse_feed(rss(item(hours_old=feeds.MAX_ITEM_AGE_HOURS + 5)),
                              SOURCE, now=NOW)
    assert parsed == []


def test_a_fresh_item_survives():
    parsed = feeds.parse_feed(rss(item(hours_old=2)), SOURCE, now=NOW)
    assert len(parsed) == 1
    assert parsed[0].age_hours == pytest.approx(2.0, abs=0.05)


def test_the_moneycontrol_case_yields_nothing_at_all():
    """A healthy 200 carrying 2024 items must produce an empty feed, not a
    page of headlines that look current."""
    ancient = rss(item("Taking Stock: Market ends lower", hours_old=20_500))
    assert feeds.parse_feed(ancient, SOURCE, now=NOW) == []


def test_an_undated_item_is_refused_rather_than_assumed_fresh():
    """Assuming "now" is exactly the assumption the stale feed punishes."""
    assert feeds.parse_feed(rss(item(dated=False)), SOURCE, now=NOW) == []


def test_a_future_dated_item_is_refused():
    """A publisher clock fault, not a scoop."""
    assert feeds.parse_feed(rss(item(hours_old=-6)), SOURCE, now=NOW) == []


def test_a_slightly_future_stamp_is_tolerated():
    """Clock skew of minutes is ordinary and must not empty a feed."""
    assert len(feeds.parse_feed(rss(item(hours_old=-0.2)), SOURCE, now=NOW)) == 1


# ---------------------------------------------------------------- parsing

def test_headlines_are_decoded_and_tags_stripped():
    """Publishers escape their HTML — Moneycontrol leads every description
    with an <img>. Titles arrive double-encoded often enough to unescape
    twice."""
    parsed = feeds.parse_feed(
        rss(item("Market&amp;#39;s gains fade",
                 description="&lt;img src='x'/&gt; Real body.")), SOURCE, now=NOW)
    assert parsed[0].headline == "Market's gains fade"
    assert parsed[0].summary == "Real body."


def test_text_survives_a_feed_that_embeds_real_markup():
    """`findtext` returns only what precedes the first child element, so an
    unescaped tag would silently truncate the field to nothing."""
    embedded = rss("<item><title>Nifty <b>rallies</b> hard</title>"
                   "<link>https://x.test/1</link>"
                   f"<pubDate>{(NOW - timedelta(hours=1))
                       .strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
                   "</item>")
    parsed = feeds.parse_feed(embedded, SOURCE, now=NOW)
    assert parsed[0].headline == "Nifty rallies hard"


def test_each_item_carries_its_tone_and_the_reason_for_it():
    parsed = feeds.parse_feed(rss(item("Sensex surges to record high")),
                              SOURCE, now=NOW)
    assert parsed[0].sentiment == "positive"
    assert "record high" in parsed[0].sentiment_reason


def test_the_id_is_stable_across_refetches():
    """Otherwise the panel re-keys every row on every poll."""
    first = feeds.parse_feed(rss(item()), SOURCE, now=NOW)[0]
    later = feeds.parse_feed(rss(item()), SOURCE, now=NOW + timedelta(minutes=3))[0]
    assert first.id == later.id


def test_an_item_with_no_title_is_skipped_not_rendered_blank():
    assert feeds.parse_feed(rss("<item><link>x</link></item>"),
                            SOURCE, now=NOW) == []


def test_broken_xml_costs_one_source_not_the_panel():
    assert feeds.parse_feed(b"<rss><channel><item>", SOURCE, now=NOW) == []
    assert feeds.parse_feed(b"", SOURCE, now=NOW) == []


def test_dates_parse_in_the_format_rss_actually_uses():
    parsed = feeds.parse_date("Tue, 25 Aug 2026 19:14:01 +0530")
    assert parsed == datetime(2026, 8, 25, 13, 44, 1, tzinfo=UTC)
    assert feeds.parse_date("not a date") is None
    assert feeds.parse_date(None) is None


# --------------------------------------------------------------- collect

class FakeResponse:
    def __init__(self, content=b"", status_code=200):
        self.content, self.status_code = content, status_code


def route(monkeypatch, table):
    """Map each source URL onto a response or an exception."""
    def get(url, **kwargs):
        outcome = table[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(feeds.curl_requests, "get", get)


def test_collect_merges_sources_newest_first(monkeypatch):
    a = feeds.Source("A", "https://a.test", "markets")
    b = feeds.Source("B", "https://b.test", "economy")
    route(monkeypatch, {
        a.url: FakeResponse(rss(item("Older story", hours_old=6,
                                     link="https://a.test/1"))),
        b.url: FakeResponse(rss(item("Newer story", hours_old=1,
                                     link="https://b.test/1"))),
    })
    result = feeds.collect((a, b), deadline=net.Deadline(30), now=NOW)

    assert [i["headline"] for i in result["items"]] == ["Newer story", "Older story"]
    assert result["available"] is True


def test_the_same_story_from_two_feeds_appears_once(monkeypatch):
    """ET's markets and economy feeds overlap. A duplicate would be counted
    twice in the aggregate as well as shown twice."""
    a = feeds.Source("A", "https://a.test", "markets")
    b = feeds.Source("B", "https://b.test", "economy")
    shared = "RBI cuts rates by 25 bps"
    route(monkeypatch, {
        a.url: FakeResponse(rss(item(shared, link="https://a.test/1"))),
        b.url: FakeResponse(rss(item(shared, link="https://b.test/1"))),
    })
    result = feeds.collect((a, b), deadline=net.Deadline(30), now=NOW)
    assert len(result["items"]) == 1


def test_one_dead_source_does_not_empty_the_panel(monkeypatch):
    a = feeds.Source("A", "https://a.test", "markets")
    b = feeds.Source("B", "https://b.test", "economy")
    route(monkeypatch, {
        a.url: FakeResponse(rss(item("Live story", link="https://a.test/1"))),
        b.url: ConnectionError("refused"),
    })
    result = feeds.collect((a, b), deadline=net.Deadline(30), now=NOW)

    assert len(result["items"]) == 1
    problems = {s["source"]: s["problem"] for s in result["sources"]}
    assert problems["A"] is None
    assert "ConnectionError" in problems["B"]


def test_a_source_that_answers_with_nothing_current_is_named_stale(monkeypatch):
    """The distinction the Moneycontrol feed makes necessary: answered but
    carried nothing fresh is a problem, not an empty success."""
    a = feeds.Source("A", "https://a.test", "markets")
    route(monkeypatch, {a.url: FakeResponse(rss(item(hours_old=20_500)))})
    result = feeds.collect((a,), deadline=net.Deadline(30), now=NOW)

    assert result["available"] is False
    assert "no item inside the freshness window" in result["sources"][0]["problem"]


def test_a_non_200_is_reported_with_its_status(monkeypatch):
    a = feeds.Source("A", "https://a.test", "markets")
    route(monkeypatch, {a.url: FakeResponse(b"", status_code=503)})
    result = feeds.collect((a,), deadline=net.Deadline(30), now=NOW)
    assert result["sources"][0]["problem"] == "HTTP 503"


def test_an_exhausted_budget_skips_the_remaining_feeds(monkeypatch):
    """Four feeds behind one API request must not become four hung sockets."""
    a = feeds.Source("A", "https://a.test", "markets")
    b = feeds.Source("B", "https://b.test", "economy")
    route(monkeypatch, {a.url: FakeResponse(rss(item())), b.url: FakeResponse(b"")})

    spent = net.Deadline(0.001)
    result = feeds.collect((a, b), deadline=spent, now=NOW)
    assert all("skipped" in s["problem"] for s in result["sources"])


def test_the_panel_is_capped_so_it_stays_readable(monkeypatch):
    a = feeds.Source("A", "https://a.test", "markets")
    many = rss(*[item(f"Story number {i}", link=f"https://a.test/{i}")
                 for i in range(feeds.MAX_ITEMS + 20)])
    route(monkeypatch, {a.url: FakeResponse(many)})
    result = feeds.collect((a,), deadline=net.Deadline(30), now=NOW)
    assert len(result["items"]) == feeds.MAX_ITEMS


def test_moneycontrol_is_not_among_the_configured_sources():
    """It serves 2024 items behind a healthy 200. A source that cannot be
    trusted to be current has no place on a live desk."""
    assert not any("moneycontrol" in s.url for s in feeds.SOURCES)
    assert len(feeds.SOURCES) >= 3
