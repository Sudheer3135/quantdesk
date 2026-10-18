"""Market news and its tone.

Read-only, cached, and deliberately walled off from everything that makes a
trading decision. The signal engine, the regime classifier, the bias layer,
the entry layer and the risk manager do not import this module and must not:
a headline is context for a human reading the screen, not an input to a rule,
and the moment it becomes one the desk has a strategy nobody backtested.

Cached because four publishers do not publish faster than the cache expires,
and an uncached panel would fetch four feeds on every dashboard poll.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from .. import net
from ..cache import get_json, set_json
from ..news import feeds

log = logging.getLogger(__name__)

router = APIRouter(prefix="/news", tags=["news"])

CACHE_KEY = "news:latest"

# Publishers do not move faster than this, and the dashboard polls every
# minute. Three minutes keeps the panel current without making the desk a
# frequent visitor to four RSS endpoints.
CACHE_TTL_SECONDS = 180

# The whole call, four feeds included. Shorter than the dashboard's own poll
# interval so a slow publisher can never stack requests up behind it.
BUDGET_SECONDS = 20.0


@router.get("")
def latest_news() -> dict:
    """Recent market headlines, newest first, each with a tone reading.

    Every item carries the reason its tone was scored the way it was, because
    a sentiment label with no visible evidence is a number to argue with and
    no way to argue.

    On a total fetch failure this answers with `available: false` and the
    per-source reasons rather than an error. The panel is context; it must
    never be the thing that breaks the page.
    """
    cached = get_json(CACHE_KEY)
    if cached:
        return cached | {"cached": True}

    try:
        with net.budget(BUDGET_SECONDS, label="news"):
            payload = feeds.collect()
    except Exception:
        log.exception("news fetch failed")
        return {"items": [], "sentiment": None, "sources": [],
                "available": False,
                "note": "The news fetch failed. This panel is context only — "
                        "nothing else on the desk depends on it."}

    # Only a result worth serving twice is stored. Caching an empty answer
    # would hold the panel dark for the full TTL after one bad minute.
    if payload.get("available"):
        set_json(CACHE_KEY, payload, ttl=CACHE_TTL_SECONDS)
    return payload | {"cached": False}
