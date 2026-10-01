"""The dashboard's freshness lines are the backend's, not its own.

`prices.LIVE_SECONDS` / `prices.DELAYED_SECONDS` decide what the API calls a
live, delayed or stale price. The dashboard classifies the same price again
in the browser (`classifyAge` in App.jsx) and keeps its own copy of the two
numbers, because it must keep re-classifying between pushes as the age
climbs. This pins the copy to the original, so the two can never disagree
about what "stale" means.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.workers import prices  # noqa: E402


def _frontend_constant(name: str) -> int:
    source = (ROOT / "frontend" / "src" / "App.jsx").read_text()
    found = re.findall(rf"^const {name} = (\d+);", source, re.MULTILINE)
    assert len(found) == 1, f"expected one {name} in App.jsx, found {found}"
    return int(found[0])


def test_the_dashboard_uses_the_backends_live_line():
    assert _frontend_constant("LIVE_SECONDS") == prices.LIVE_SECONDS


def test_the_dashboard_uses_the_backends_delayed_line():
    assert _frontend_constant("DELAYED_SECONDS") == prices.DELAYED_SECONDS
