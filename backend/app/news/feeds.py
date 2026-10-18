"""Market news, read from publisher RSS.

RSS rather than a scrape or a paid API: it is what the publishers themselves
offer for this, it needs no key, and it breaks loudly rather than silently
when it changes. The cost is that headlines are all you get — no body text,
no ticker mapping, no analyst tone — and the sentiment module is honest
about scoring only what it can see.

Two guards matter more than the fetching, and both come from what the feeds
actually did when this was written.

**Staleness.** Moneycontrol's markets feed returns HTTP 200, well-formed
XML, and items dated 2024 — twenty thousand hours old at the time of
writing. Nothing about the response says so. A desk that rendered it would
have put two-year-old headlines beside a live price, and the only thing
distinguishing them from today's news is a date nobody reads. So every item
is aged, anything past `MAX_ITEM_AGE_HOURS` is dropped, and a source whose
*newest* item is stale is reported as stale rather than quietly contributing
nothing.

**Budget.** Four feeds fetched from an API request must not become four
hung sockets. Every call spends from the caller's deadline — the same
mechanism the option collector uses — so a slow publisher costs one source,
not the whole panel.
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from curl_cffi import requests as curl_requests

from .. import net
from . import sentiment

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    # What kind of story this feed carries. Shown as the panel's label, so a
    # reader can tell an economy story from a company one without opening it.
    label: str


# Verified reachable and current on 25-Aug-2026. Moneycontrol is deliberately
# absent: its markets feed serves 2024 items behind a healthy 200, and a
# source that cannot be trusted to be current has no place on a live desk.
_ET = "https://economictimes.indiatimes.com"
SOURCES: tuple[Source, ...] = (
    Source("Economic Times", f"{_ET}/markets/rssfeeds/1977021501.cms", "markets"),
    Source("ET Economy", f"{_ET}/news/economy/rssfeeds/1373380680.cms", "economy"),
    Source("Business Standard",
           "https://www.business-standard.com/rss/markets-106.rss", "markets"),
    Source("Livemint", "https://www.livemint.com/rss/markets", "markets"),
)

# One feed. Generous enough for a slow publisher, far short of the budget.
REQUEST_TIMEOUT_SECONDS = 6.0

# What a standalone call gets when nobody upstream set a budget.
DEFAULT_BUDGET_SECONDS = 20.0

# Older than this and it is not news. Two days covers a weekend's carry-over
# without admitting the archive.
MAX_ITEM_AGE_HOURS = 48

# How many headlines the panel is given. More than this and nobody reads any
# of them.
MAX_ITEMS = 24

_TAGS = re.compile(r"<[^>]+>")


@dataclass
class Item:
    id: str
    headline: str
    summary: str
    url: str
    source: str
    label: str
    published_at: str
    age_hours: float
    sentiment: str
    sentiment_score: float
    sentiment_reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _text(node: ET.Element, tag: str) -> str:
    """All the text under a child tag, children included.

    `findtext` returns only the text *before* the first child element, so a
    feed that embeds real markup rather than escaping it loses everything
    after the first tag — silently, as an empty string. Most publishers
    escape or CDATA their HTML and never hit this; the one that does not
    should cost a tidier summary, not the whole item.
    """
    child = node.find(tag)
    if child is None:
        return ""
    return "".join(child.itertext())


def _clean(text: str | None) -> str:
    """Entity-decoded, tag-stripped, whitespace-collapsed text.

    Descriptions arrive as HTML fragments — Moneycontrol leads with an
    `<img>` tag — and titles arrive double-encoded often enough that
    unescaping twice is the pragmatic choice.
    """
    if not text:
        return ""
    stripped = _TAGS.sub(" ", text)
    return " ".join(html.unescape(html.unescape(stripped)).split())


def parse_date(raw: str | None) -> datetime | None:
    """RFC-822, as RSS specifies it. None when the feed will not say.

    A missing date is not treated as "now". An item that cannot be aged
    cannot be checked for staleness, and assuming it is fresh is exactly the
    assumption the Moneycontrol feed punishes.
    """
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_feed(body: bytes, source: Source, now: datetime | None = None) -> list[Item]:
    """Turn one feed's XML into scored items. Never raises on bad XML.

    Kept apart from the network so it can be tested against a fixture, which
    is the only way to pin the behaviour of a feed that is currently healthy
    and may not stay that way.
    """
    now = now or datetime.now(UTC)
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        log.warning("%s returned unparseable XML: %s", source.name, exc)
        return []

    items: list[Item] = []
    for node in root.findall(".//item"):
        headline = _clean(_text(node, "title"))
        if not headline:
            continue

        published = parse_date(_text(node, "pubDate"))
        if published is None:
            # Undated means unageable means untrustworthy on a live screen.
            continue
        age = (now - published).total_seconds() / 3600
        if age > MAX_ITEM_AGE_HOURS or age < -1:
            # Future-dated by more than an hour is a publisher clock fault,
            # not a scoop.
            continue

        url = _text(node, "link").strip()
        reading = sentiment.score(headline)
        items.append(Item(
            # Stable across refetches so the panel does not re-key its rows
            # every minute. The link is the natural identity; the hash keeps
            # it short and safe to use as a DOM key.
            id=hashlib.sha1((url or headline).encode()).hexdigest()[:12],
            headline=headline,
            summary=_clean(_text(node, "description"))[:280],
            url=url,
            source=source.name,
            label=source.label,
            published_at=published.isoformat(),
            age_hours=round(age, 2),
            sentiment=reading.label,
            sentiment_score=round(reading.score, 3),
            sentiment_reason=reading.reason,
        ))
    return items


def fetch_source(source: Source, deadline: net.Deadline,
                 now: datetime | None = None) -> tuple[list[Item], str | None]:
    """One feed. Returns (items, why_it_is_empty)."""
    try:
        timeout = deadline.slice(REQUEST_TIMEOUT_SECONDS)
    except net.BudgetExhausted as exc:
        return [], f"skipped: {exc}"

    try:
        response = curl_requests.get(source.url, impersonate="chrome120",
                                     timeout=timeout)
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"

    if response.status_code != 200:
        return [], f"HTTP {response.status_code}"

    items = parse_feed(response.content, source, now=now)
    if not items:
        # The distinction that matters: a feed that answered but carried
        # nothing current is stale, not merely empty, and the panel should
        # be able to say which.
        return [], "answered but carried no item inside the freshness window"
    return items, None


def collect(sources: tuple[Source, ...] = SOURCES,
            deadline: net.Deadline | None = None,
            now: datetime | None = None) -> dict:
    """Every source, merged newest-first, with an aggregate tone.

    Partial results are the normal case and are returned as such: a panel
    showing three of four feeds with the fourth named as failing is more
    useful than an error, and far more useful than three feeds pretending to
    be all of them.
    """
    deadline = deadline or net.deadline_or(DEFAULT_BUDGET_SECONDS, label="news")
    now = now or datetime.now(UTC)

    items: list[Item] = []
    status: list[dict] = []
    for source in sources:
        found, problem = fetch_source(source, deadline, now=now)
        items.extend(found)
        status.append({"source": source.name, "items": len(found),
                       "problem": problem})

    # One story carried by two feeds is one story. ET's markets and economy
    # feeds overlap, and a duplicated headline would be counted twice in the
    # aggregate as well as shown twice.
    seen: set[str] = set()
    unique: list[Item] = []
    for item in sorted(items, key=lambda i: i.published_at, reverse=True):
        key = item.headline.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    trimmed = unique[:MAX_ITEMS]
    tone = sentiment.aggregate([sentiment.score(i.headline) for i in trimmed])

    return {
        "items": [i.to_dict() for i in trimmed],
        "sentiment": tone,
        "sources": status,
        "fetched_at": now.isoformat(),
        "available": bool(trimmed),
    }
