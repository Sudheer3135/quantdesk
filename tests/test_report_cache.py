"""Reports kept until the data under them changes — and not a moment longer.

The dashboard recomputed the quality report (1.36s) and the outcome study
(0.47s) every minute. These pin down when a kept answer may be served and,
more importantly, every way it must not be.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.data import quality, report_cache
from app.data.importer import import_index_candles
from test_importer import session_bars


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def counting(value="report"):
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return {"value": value, "n": calls["n"], "nested": {"list": [1, 2]}}

    return calls, compute


def seed(db, day=16):
    import_index_candles(db, session_bars(date(2026, 6, day), count=75),
                         "NIFTY", "5m", "test")


def test_an_unchanged_archive_is_not_recomputed(db):
    seed(db)
    calls, compute = counting()
    clock = Clock()
    for _ in range(5):
        report_cache.memoise("k", db, compute, clock=clock)
    assert calls["n"] == 1


def test_a_new_row_recomputes_at_once(db):
    """An insert is never served stale — not even for a second."""
    seed(db, 16)
    calls, compute = counting()
    clock = Clock()
    report_cache.memoise("k", db, compute, clock=clock)
    seed(db, 17)
    report_cache.memoise("k", db, compute, clock=clock)
    assert calls["n"] == 2


def test_nothing_is_kept_longer_than_one_bar(db):
    """Upserts widen the forming bar without adding a row, which the version
    cannot see. The age cap is what bounds that."""
    seed(db)
    calls, compute = counting()
    clock = Clock()
    report_cache.memoise("k", db, compute, clock=clock)
    clock.t += report_cache.MAX_AGE_SECONDS - 1
    report_cache.memoise("k", db, compute, clock=clock)
    assert calls["n"] == 1
    clock.t += 2
    report_cache.memoise("k", db, compute, clock=clock)
    assert calls["n"] == 2


def test_a_caller_cannot_edit_the_kept_answer(db):
    seed(db)
    _, compute = counting()
    clock = Clock()
    first = report_cache.memoise("k", db, compute, clock=clock)
    first["nested"]["list"].append("vandalised")
    first["value"] = "vandalised"
    second = report_cache.memoise("k", db, compute, clock=clock)
    assert second["value"] == "report"
    assert second["nested"]["list"] == [1, 2]


def test_different_questions_are_kept_apart(db):
    seed(db)
    a_calls, a = counting("a")
    b_calls, b = counting("b")
    clock = Clock()
    assert report_cache.memoise(("quality", True), db, a, clock=clock)["value"] == "a"
    assert report_cache.memoise(("quality", False), db, b, clock=clock)["value"] == "b"
    assert a_calls["n"] == b_calls["n"] == 1


def test_the_quality_report_matches_an_uncached_one(db):
    """Caching must change the cost, never the answer."""
    seed(db, 16)
    seed(db, 18)                       # 17-Jun missing: a real finding
    cold = quality.report(db, "NIFTY", "5m")
    warm = quality.report(db, "NIFTY", "5m")
    assert warm == cold
    assert any(f["check"] == "missing_sessions" for f in warm["findings"])


def test_collector_liveness_is_asked_every_time(db, monkeypatch):
    """The one clock-dependent check. A kept "the collector is fine" would
    be an alarm switched off for up to five minutes."""
    seed(db)
    asked = {"n": 0}
    real = quality.option_collector_liveness

    def spy(*args, **kwargs):
        asked["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(quality, "option_collector_liveness", spy)
    stored = {"n": 0}
    real_stored = quality._stored_findings

    def stored_spy(*args, **kwargs):
        stored["n"] += 1
        return real_stored(*args, **kwargs)

    monkeypatch.setattr(quality, "_stored_findings", stored_spy)

    for _ in range(3):
        quality.report(db, "NIFTY", "5m")
    assert asked["n"] == 3
    assert stored["n"] == 1


def test_a_stalled_collector_appears_on_a_warm_cache(db, monkeypatch):
    seed(db)
    quality.report(db, "NIFTY", "5m")          # warm the cache, collector fine

    from app.data.quality import Finding
    monkeypatch.setattr(quality, "option_collector_liveness", lambda *a, **k: [
        Finding("option_collector_stalled", "error", "stalled", count=1)])
    warm = quality.report(db, "NIFTY", "5m")
    assert any(f["check"] == "option_collector_stalled" for f in warm["findings"])
    assert warm["errors"] >= 1
