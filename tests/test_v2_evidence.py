"""Phase 3B: what strategy v2 saw when it decided, kept with the decision.

The evidence is an envelope around the decision, not part of it. So most of
these tests are about two things at once: the record says what happened —
which observation was consumed, which gates were reached, what every
alternative contract looked like and why the selector passed over it — and
the decision itself is exactly what it would have been without the record.
"""
# The `desk` fixture is imported from test_v2_paper; each test taking it as an
# argument reads to ruff as a redefinition.
# ruff: noqa: F811
from __future__ import annotations

import json
import random
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.analytics import option_pricing
from app.brokers.angel import OptionTick
from app.db import get_db
from app.market_hours import IST
from app.models import PaperDecision, PaperPosition, SignalRecord
from app.optionbuy.contracts import expiry_moment
from app.strategy_v2 import evidence, rules
from app.strategy_v2.config import V2Config
from test_v2_paper import EXPIRY, buy_signal, desk  # noqa: F401  (the fixture)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---- helpers ------------------------------------------------------------------

def observed(signal=None, *, observation_id="obs-test-1", signal_id=41, **timing):
    """A published signal as the agent now sends it: with its observation."""
    sig = dict(signal or buy_signal())
    sig["observation"] = {"schema": "signal_observation/1", "observation_id": observation_id,
                          "signal_id": signal_id, "row_provenance": "persisted",
                          "code_id": "abc123", "code_id_basis": "test",
                          "data_source": "test", "published_at": "2026-09-17T04:55:10+00:00"}
    sig["context"] = {
        # All before the desk clock (10:30 IST = 05:00 UTC), as a real
        # signal's clocks are before the moment v2 consumes it.
        "timing": {"bar_open_time": "2026-09-17T04:50:00+00:00",
                   "bar_close_time": "2026-09-17T04:55:00+00:00",
                   "received_at": "2026-09-17T04:55:04+00:00",
                   "available_at": "2026-09-17T04:55:04+00:00",
                   "available_basis": "received_at",
                   "decision_at": "2026-09-17T04:55:08+00:00",
                   "earliest_execution_time": "2026-09-17T04:55:08+00:00",
                   "execution_latency_seconds": 0.0, **timing},
        "provenance": {"strategy_version": "nifty-signal-engine/1",
                       "parameter_hash": "p" * 64, "input_fingerprint": "f" * 64},
        "atr14": 21.5, "trend": "bullish",
    }
    return sig


def last_row(desk):
    with desk.session() as db:
        return db.query(PaperDecision).order_by(PaperDecision.id.desc()).first()


def last_evidence(desk):
    return last_row(desk).detail["evidence"]


def gates(ev):
    return {g["gate"]: g["status"] for g in ev["vector"]["decision_context"]["gate_trace"]}


def rank_order(desk, option_type="CE"):
    """The tokens the selector would try, nearest delta first."""
    now = desk.clock()
    cands = [rules.Candidate(strike=q.contract.strike, option_type=q.contract.option_type,
                             ltp=q.price, bid=q.bid, ask=q.ask, age_seconds=q.age_seconds(now),
                             token=q.contract.token, symbol=q.contract.symbol,
                             lot_size=q.contract.lot_size) for q in desk.chain.quotes(now=now)]
    years = option_pricing.years_to_expiry(now.astimezone(IST), expiry_moment(EXPIRY))
    trace = {}
    rules.pick_contract(cands, option_type=option_type, spot=desk.spot, years=years,
                        cfg=V2Config(), trace=trace)
    ranked = sorted((a for a in trace["alternatives"] if a.get("band_rank")),
                    key=lambda a: a["band_rank"])
    return [a["token"] for a in ranked]


def one_sided(desk, token):
    q = next(q for q in desk.chain.quotes(now=desk.clock()) if q.contract.token == token)
    desk.chain.update(OptionTick(token=token, price=q.price, source_time=desk.clock(),
                                 open_interest=None, volume=None, bid=None, ask=None))


# ---- observation identity and linkage --------------------------------------------

def test_the_decision_names_the_observation_it_consumed(desk):
    trader = desk.trader()
    desk.signal = observed()
    trader.step()

    ev = last_evidence(desk)
    ident = ev["vector"]["identity"]
    assert ident["observation_id"] == "obs-test-1" and ident["signal_id"] == 41
    assert ident["linkage"] == "linked" and ev["status"] == evidence.COMPLETE
    # Deliberately not an opportunity or a thesis.
    assert ident["opportunity_id"] is None and ident["opportunity_id_status"] == "unset"
    assert ident["strategy_family"] is None and ident["strategy_family_status"] == "unknown"


def test_a_signal_without_an_observation_is_recorded_as_unlinked(desk):
    trader = desk.trader()
    desk.signal = buy_signal()                     # an older publisher: no observation
    trader.step()

    ev = last_evidence(desk)
    assert ev["vector"]["identity"]["linkage"] == "missing"
    assert ev["vector"]["identity"]["observation_id"] is None
    assert ev["status"] == evidence.PARTIAL
    assert "identity.observation_id" in ev["missing"]


def test_duplicate_delivery_is_visible_and_changes_nothing(desk):
    """The trader acts on a changed fingerprint, exactly as before. The same
    observation arriving again after another one is considered again — that
    is unchanged behaviour — and the record now shows it was seen before."""
    trader = desk.trader()
    desk.kill = True                                # keep every decision a cheap refusal
    a = observed(observation_id="obs-A", signal_id=1)
    b = observed(buy_signal(stop=23940.0), observation_id="obs-B", signal_id=2)

    desk.signal = a
    trader.step()
    trader.step()                                   # the same payload again: no new decision
    with desk.session() as db:
        assert db.query(PaperDecision).count() == 1

    desk.signal = b
    trader.step()
    desk.signal = a
    trader.step()

    with desk.session() as db:
        rows = db.query(PaperDecision).order_by(PaperDecision.id).all()
    ids = [r.detail["evidence"]["vector"]["identity"] for r in rows]
    assert [i["observation_id"] for i in ids] == ["obs-A", "obs-B", "obs-A"]
    assert [i["seen_in_recent_history"] for i in ids] == [False, False, True]
    assert ids[0]["duplicate_check"]["kind"] == "bounded_recent_history"
    assert ids[0]["duplicate_check"]["capacity"] == evidence.DUPLICATE_CAPACITY
    assert all(r.code == rules.KILL_SWITCH for r in rows)


def test_the_agent_publishes_the_row_it_stored_and_v2_links_to_it(db, monkeypatch):
    """End to end: agent tick → stored row → published observation."""
    from app.analytics.signal_engine import Signal
    from app.api.signals import Analysis
    from app.config import get_settings
    from app.workers import agent
    from test_risk_on_live_path import SessionFactory

    sent = {}
    monkeypatch.setenv("ARCHIVE_CANDLES", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(agent, "SessionLocal", SessionFactory(db))
    monkeypatch.setattr(agent, "publish",
                        lambda ch, blob, **k: sent.setdefault("p", json.loads(blob)) or True)
    now = datetime.now(UTC).isoformat()
    sig = Signal(symbol="NIFTY", timeframe="5m", timestamp=now, action="BUY",
                 confidence=0.6, price=24200.0, entry=24200.0, stop_loss=24190.0,
                 target=24225.0, risk_reward=2.5, checks=[],
                 context={"timing": {"decision_at": now, "bar_open_time": now},
                          "provenance": {"strategy_version": "s", "parameter_hash": "p",
                                         "input_fingerprint": "f"}})
    monkeypatch.setattr(agent, "build_analysis", lambda *a, **k: Analysis(signal=sig, plan=None))
    agent.tick(force=True)

    obs = sent["p"]["observation"]
    row = db.get(SignalRecord, obs["signal_id"])
    assert row is not None and row.provenance["observation_id"] == obs["observation_id"]
    assert obs["row_provenance"] == "persisted" and obs["published_at"]
    # Signal values are untouched by the identity.
    assert (row.action, row.entry, row.stop_loss, row.target) == ("BUY", 24200.0, 24190.0,
                                                                 24225.0)
    assert sent["p"]["action"] == "BUY" and sent["p"]["stop_loss"] == 24190.0


# ---- gate trace and early rejection -----------------------------------------------

def test_an_early_rejection_reaches_no_contract_selection(desk):
    trader = desk.trader()
    hold = observed({"timestamp": "2026-09-17T10:25:00+05:30", "action": "HOLD",
                     "price": 24000.0, "confidence": 0.2})
    desk.signal = hold
    trader.step()

    ev = last_evidence(desk)
    g = gates(ev)
    assert g["signal.direction"] == "failed"
    assert all(s == "not_reached" for k, s in g.items() if k != "signal.direction")
    assert ev["contract_evidence"]["status"] == "selection_not_reached"
    assert "universe" not in ev["contract_evidence"]         # nothing was fetched to fill it


def test_a_rejection_before_selection_passes_earlier_gates_only(desk):
    desk.clock.at(15, 0)                                      # after the 14:30 entry window
    trader = desk.trader()
    desk.signal = observed()
    trader.step()

    row = last_row(desk)
    assert row.code == rules.OUTSIDE_WINDOW
    g = gates(row.detail["evidence"])
    order = list(evidence.GATES)
    cut = order.index("entry_window")
    assert all(g[k] == "passed" for k in order[:cut])
    assert g["entry_window"] == "failed"
    assert all(g[k] == "not_reached" for k in order[cut + 1:])
    assert row.detail["evidence"]["contract_evidence"]["status"] == "selection_not_reached"
    # The feed was never asked, so the record does not claim it was healthy.
    assert row.detail["evidence"]["vector"]["data_status"]["feed_healthy_at_decision"] is None


def test_a_gate_switched_off_by_config_is_disabled_not_passed(desk):
    trader = desk.trader(cfg=V2Config(require_bias_agreement=False))
    desk.kill = True
    desk.signal = observed()
    trader.step()
    assert gates(last_evidence(desk))["signal.bias"] == "disabled"


# ---- contract evidence --------------------------------------------------------------

def test_selection_keeps_the_whole_universe_and_every_alternatives_fate(desk):
    trader = desk.trader()
    desk.signal = observed()
    trader.step()

    row = last_row(desk)
    assert row.outcome == "entered"
    ce = row.detail["evidence"]["contract_evidence"]
    assert ce["status"] == "selection_reached"
    supplied = desk.chain.quotes(now=desk.clock())
    assert len(ce["universe"]["quotes"]) == len(supplied)
    assert ce["result"]["status"] == "selected"
    chosen = [a for a in ce["alternatives"] if a["status"] == "selected"]
    assert len(chosen) == 1 and chosen[0]["token"] == ce["result"]["token"]
    assert ce["pick"]["token"] == chosen[0]["token"]
    # The other side of the chain was supplied but never in the pool.
    assert all(a["status"] == "excluded" and a["code"] == "other_option_type"
               for a in ce["alternatives"] if a["option_type"] == "PE")
    # Ranked behind the selected one: never assessed, not refused.
    behind = [a for a in ce["alternatives"]
              if a.get("band_rank", 0) > chosen[0]["band_rank"]]
    assert behind and all(a["status"] == "not_assessed" for a in behind)
    assert ce["result"]["code"] is None
    assert row.detail["evidence"]["vector"]["decision_context"]["position_id"] is not None


def test_missing_quotes_stay_missing_and_the_refusal_is_explained(desk):
    for token in rank_order(desk):
        one_sided(desk, token)                    # every in-band call loses its book
    trader = desk.trader()
    desk.signal = observed()
    trader.step()

    row = last_row(desk)
    assert row.code == rules.NO_DEPTH and row.outcome == "rejected"
    ce = row.detail["evidence"]["contract_evidence"]
    assert ce["result"] == {"status": "refused", "token": None, "code": rules.NO_DEPTH}
    by_token = {q["token"]: q for q in ce["universe"]["quotes"]}
    for alt in (a for a in ce["alternatives"] if a.get("band_rank")):
        q = by_token[alt["token"]]
        assert q["bid"] is None and q["ask"] is None           # never filled in
        assert q["bid_status"] == "missing" and q["ask_status"] == "missing"
        assert alt["mid_basis"] == "ltp_fallback"
        assert alt["status"] == "failed" and alt["code"] == rules.NO_DEPTH
        assert [c["status"] for c in alt["liquidity_checks"]] == [
            "passed", "failed", "not_assessed", "not_assessed"]
    assert gates(row.detail["evidence"])["contract_selection"] == "failed"


def test_stale_quotes_are_marked_stale_and_refused_as_before(desk):
    desk.clock.advance(seconds=30)                # every quote now 30s old; limit is 10s
    trader = desk.trader()
    desk.signal = observed()
    trader.step()

    row = last_row(desk)
    assert row.code == rules.STALE_QUOTE
    ce = row.detail["evidence"]["contract_evidence"]
    assert all(q["freshness"] == "stale" and q["age_seconds"] >= 30
               for q in ce["universe"]["quotes"])
    first = min((a for a in ce["alternatives"] if a.get("band_rank")),
                key=lambda a: a["band_rank"])
    assert first["status"] == "failed" and first["code"] == rules.STALE_QUOTE
    assert first["liquidity_checks"][0] == {"check": "quote_age", "status": "failed"}


def test_the_nearest_strike_failing_moves_selection_to_the_next(desk):
    ranked = rank_order(desk)
    one_sided(desk, ranked[0])
    trader = desk.trader()
    desk.signal = observed()
    trader.step()

    row = last_row(desk)
    ce = row.detail["evidence"]["contract_evidence"]
    fates = {a["token"]: a for a in ce["alternatives"]}
    assert fates[ranked[0]]["status"] == "failed" and fates[ranked[0]]["code"] == rules.NO_DEPTH
    assert fates[ranked[1]]["status"] == "selected"
    assert all(fates[t]["status"] == "not_assessed" for t in ranked[2:])
    assert row.outcome == "entered" and ce["result"]["token"] == ranked[1]
    assert ce["pick"]["token"] == ranked[1]


def test_the_traced_selection_agrees_with_the_decision(desk):
    trader = desk.trader()
    desk.signal = observed()
    trader.step()
    ev = last_evidence(desk)
    assert trader._evidence.failures == 0
    assert ev["contract_evidence"]["trace_consistent_with_decision"] is True
    assert ev["issues"] == []
    assert ev["status"] == evidence.COMPLETE


def test_a_later_quote_cannot_rewrite_the_evidence(desk):
    trader = desk.trader()
    desk.signal = observed()
    trader.step()
    before = json.dumps(last_evidence(desk), sort_keys=True)

    desk.clock.advance(seconds=5)
    desk.quote_all(spread=3.0, spot=24100.0)    # the whole chain reprices afterwards
    trader.step()                                # monitoring the open position

    assert json.dumps(last_evidence(desk), sort_keys=True) == before


# ---- clocks, provenance, predictor separation ------------------------------------

def test_clocks_are_recorded_with_their_bases_and_missing_ones_stay_missing(desk):
    trader = desk.trader()
    sig = observed()
    del sig["context"]["timing"]["available_at"]
    desk.signal = sig
    trader.step()

    ev = last_evidence(desk)
    c = ev["vector"]["causality"]
    assert c["bar_close_time"] == "2026-09-17T04:55:00+00:00"
    assert c["signal_decision_at"] == "2026-09-17T04:55:08+00:00"
    assert c["available_at"] is None and "causality.available_at" in ev["missing"]
    assert c["generated_at_basis"].startswith("stamped by v2")   # the agent sends none
    assert "cannot refuse it on age" in c["signal_age_note"]
    assert "not enumerated" in ev["missing_scope"]
    assert c["v2_decided_at"] == desk.clock().isoformat()
    v = ev["vector"]["versions"]
    assert v["parameter_hash"] == "p" * 64 and v["code_id"] == "abc123"
    assert v["v2_config_hash"] and v["evidence_schema"] == evidence.EVIDENCE_SCHEMA


def test_no_future_or_realised_field_enters_the_contemporaneous_vector(desk):
    trader = desk.trader()
    desk.signal = observed()
    trader.step()
    ev = last_evidence(desk)
    evidence.assert_no_future_fields(ev["vector"])          # does not raise
    assert ev["outcome_label"]["status"] == "not_implemented"
    assert ev["vector"]["setup"]["score_calibrated"] is False
    assert "role" in ev["vector"]["decision_context"]

    with pytest.raises(ValueError, match="mfe"):
        evidence.assert_no_future_fields({"market_state": {"mfe": 1.0}})
    with pytest.raises(ValueError, match="revision"):
        evidence.assert_no_future_fields({"evidence": {"checks": [{"revision": 2}]}})


def test_an_undeclared_nested_field_never_reaches_the_vector(desk):
    """Codex's reproduction: a next-day quote nested inside a copied structure."""
    trader = desk.trader()
    sig = observed()
    sig["context"]["last_structure_event"] = {
        "kind": "BOS", "direction": "bullish", "timestamp": "2026-09-17T04:45:00+00:00",
        "broken_level": 23990.0, "close": 23995.0, "index": 7,
        "future_quote": {"at": "2026-09-18T04:00:00+00:00", "bid": 190.0}, "mfe": 3.0}
    sig["checks"] = [{"name": "vwap", "score": 1.0, "reason": "above",
                      "later": {"r_multiple": 2.0}}]
    desk.signal = sig
    trader.step()
    ev = last_evidence(desk)
    blob = json.dumps(ev["vector"])
    assert "future_quote" not in blob and "2026-09-18" not in blob and "mfe" not in blob
    assert "r_multiple" not in blob and "later" not in blob
    lse = ev["vector"]["market_state"]["last_structure_event"]
    assert lse["kind"] == "BOS" and lse["broken_level"] == 23990.0      # declared fields kept
    diag = ev["diagnostics"]["rejected_predictors"]
    assert any(d.get("undeclared_fields") == ["future_quote", "mfe"] for d in diag)
    assert any(d.get("undeclared_fields") == ["later"] for d in diag)
    assert last_row(desk).outcome == "entered"               # the decision is unchanged


def test_a_structure_event_dated_after_the_signal_is_excluded(desk):
    trader = desk.trader()
    sig = observed()
    sig["context"]["last_structure_event"] = {"kind": "BOS", "direction": "bullish",
                                              "timestamp": "2026-09-18T04:00:00+00:00"}
    desk.signal = sig
    trader.step()
    ev = last_evidence(desk)
    assert ev["vector"]["market_state"]["last_structure_event"] is None
    assert ev["status"] == evidence.INCONSISTENT
    assert [i["kind"] for i in ev["issues"]] == [evidence.FUTURE_PREDICTOR]
    assert ev["diagnostics"]["rejected_predictors"][0]["value"]["timestamp"].startswith(
        "2026-09-18")


def test_modelled_and_observed_values_carry_their_basis(desk):
    trader = desk.trader()
    desk.signal = observed()
    trader.step()
    ev = last_evidence(desk)
    assert ev["bases"]["premium_levels"].startswith("modelled")
    assert ev["bases"]["mid_iv_delta_spread"].startswith("derived")
    assert ev["bases"]["paper_fill"].startswith("simulated")
    assert ev["bases"]["open_interest_volume"].startswith("last known")
    q = ev["contract_evidence"]["universe"]["quotes"][0]
    assert q["open_interest_status"] == "last_known" and q["lot_size_status"] == "instrument_master"


def test_a_non_finite_value_is_stored_as_missing_and_says_so(desk):
    trader = desk.trader()
    sig = observed()
    sig["context"]["atr14"] = float("nan")
    desk.signal = sig
    trader.step()
    ev = last_evidence(desk)
    assert ev["vector"]["market_state"]["atr14"] is None
    assert "evidence.vector.market_state.atr14" in ev["non_finite_fields"]
    json.dumps(ev, allow_nan=False)                            # the column can take it


# ---- persistence and failure ----------------------------------------------------------

def test_evidence_survives_a_restart_and_is_served_apart_from_the_list(desk):
    from app.api import strategy_v2 as api
    trader = desk.trader()
    desk.signal = observed()
    trader.step()
    stored = last_evidence(desk)

    fresh = desk.trader()                         # a new process: nothing held in memory
    fresh.recover()
    assert last_evidence(desk) == stored

    app = FastAPI()
    app.include_router(api.router)

    def session():
        with desk.session() as db:
            yield db
    app.dependency_overrides[get_db] = session
    client = TestClient(app)
    listing = client.get(f"{api.router.prefix}/decisions").json()["decisions"]
    assert "evidence" not in listing[0]["detail"] and listing[0]["evidence_status"] == "complete"
    served = client.get(f"{api.router.prefix}/decisions/{listing[0]['id']}/evidence").json()
    assert served["evidence"] == stored
    assert client.get(f"{api.router.prefix}/decisions/999999/evidence").status_code == 404


def test_the_published_state_does_not_carry_the_evidence(desk):
    trader = desk.trader()
    desk.signal = observed()
    trader.step()
    assert "evidence" not in trader.last_decision
    assert trader.status()["evidence_capture_failures"] == 0


@pytest.mark.parametrize("scenario", ["entered", "rejected_early", "rejected_at_selection"])
def test_an_evidence_failure_is_visible_and_decides_nothing(desk, monkeypatch, scenario):
    def boom(self, *a, **k):
        raise RuntimeError("recorder broke")
    if scenario == "rejected_at_selection":
        for token in rank_order(desk):
            one_sided(desk, token)
    signal = observed() if scenario != "rejected_early" else observed(
        {"timestamp": "t", "action": "HOLD", "price": 1.0, "confidence": 0.1})

    monkeypatch.setattr(evidence.Recorder, "build", boom)
    broken = desk.trader()
    desk.signal = signal
    broken.step()
    row = last_row(desk)
    assert row.detail["evidence"]["status"] == evidence.CAPTURE_FAILED
    assert "recorder broke" in row.detail["evidence"]["error"]
    assert broken._evidence.failures == 1 and broken.status()["evidence_capture_failures"] == 1
    expected = {"entered": ("entered", rules.ENTERED),
                "rejected_early": ("rejected", rules.HOLD),
                "rejected_at_selection": ("rejected", rules.NO_DEPTH)}[scenario]
    assert (row.outcome, row.code) == expected
    with desk.session() as db:
        opened = db.query(PaperPosition).count()
    assert opened == (1 if scenario == "entered" else 0)


# ---- behavioural equivalence of the selector -------------------------------------------

def _random_candidates(rng, spot=24000.0):
    out = []
    for strike in range(23500, 24550, 50):
        for kind in ("CE", "PE"):
            years = 4 / 365
            fair = option_pricing.price(spot, strike, years, rng.uniform(0.10, 0.18), kind=kind)
            fair = max(fair, rng.choice([0.05, 0.5, 5.0, fair]))
            spread = rng.choice([0.1, 0.5, 2.0, 8.0, fair * 0.2])
            shape = rng.random()
            bid = None if shape < 0.15 else max(fair - spread / 2, 0.0)
            ask = None if shape < 0.10 else fair + spread / 2
            if 0.10 <= shape < 0.13:
                bid, ask = ask, bid                           # crossed or half-empty
            out.append(rules.Candidate(strike=float(strike), option_type=kind,
                                       ltp=round(fair, 2), bid=bid, ask=ask,
                                       age_seconds=rng.choice([0.5, 3.0, 9.9, 10.1, 45.0]),
                                       token=f"{strike}{kind}", symbol=f"N{strike}{kind}",
                                       lot_size=rng.choice([65, 65, None])))
    return out


def _outcome(result):
    pick, rej = result
    return ((pick.to_dict() if pick else None),
            (rej.code, rej.detail) if rej else None)


def test_the_trace_never_changes_what_the_selector_picks():
    rng = random.Random(3_2026)
    cfg = V2Config()
    kinds = set()
    for _ in range(400):
        cands = _random_candidates(rng)
        option_type = rng.choice(["CE", "PE"])
        spot = rng.uniform(23700, 24300)
        years = rng.choice([4 / 365, 1 / 365, 0.0])
        plain = rules.pick_contract(cands, option_type=option_type, spot=spot,
                                    years=years, cfg=cfg)
        trace = {}
        traced = rules.pick_contract(cands, option_type=option_type, spot=spot,
                                     years=years, cfg=cfg, trace=trace)
        assert _outcome(plain) == _outcome(traced)
        res = trace["result"]
        kinds.add(res["code"] or "selected")
        assert res["status"] == ("selected" if plain[0] else "refused")
        assert res["code"] == (plain[1].code if plain[1] else None)
        assert len(trace["alternatives"]) == len(cands)
        assert sum(a["status"] == "selected" for a in trace["alternatives"]) == (
            1 if plain[0] else 0)
    # The sample covered the refusals the evidence has to explain.
    assert {"selected", rules.STALE_QUOTE, rules.NO_DEPTH, rules.CHAIN_NOT_READY} <= kinds


def test_a_generator_of_candidates_is_consumed_once_as_before():
    rng = random.Random(7)
    cands = _random_candidates(rng)
    cfg = V2Config()
    a = rules.pick_contract(iter(cands), option_type="CE", spot=24000.0, years=4 / 365, cfg=cfg)
    b = rules.pick_contract(iter(cands), option_type="CE", spot=24000.0, years=4 / 365, cfg=cfg,
                            trace={})
    assert _outcome(a) == _outcome(b)


# ---- correction pass: capture failures, clocks, quote validity, gates -------------

def test_a_failed_quote_snapshot_is_counted_logged_and_keeps_the_rest(desk, monkeypatch, caplog):
    def broken(*a, **k):
        raise RuntimeError("snapshot broke")
    monkeypatch.setattr(evidence, "quote_snapshot", broken)
    trader = desk.trader()
    desk.signal = observed()
    with caplog.at_level("ERROR", logger="app.strategy_v2.evidence"):
        trader.step()

    row = last_row(desk)
    assert (row.outcome, row.code) == ("entered", rules.ENTERED)        # decision unchanged
    ev = row.detail["evidence"]
    assert ev["status"] == evidence.CAPTURE_INCOMPLETE
    assert [(i["kind"], i["stage"]) for i in ev["issues"]] == [
        (evidence.CAPTURE_ERROR, "quote_snapshot")]
    ce = ev["contract_evidence"]
    assert ce["status"] == "selection_reached" and ce["universe"]["quotes"] is None
    assert ce["alternatives"] and ce["trace_consistent_with_decision"] is True  # kept
    assert trader._evidence.failures == 1 and trader._evidence.trace_disagreements == 0
    assert trader.status()["evidence_capture_failures"] == 1
    assert any("incomplete" in r.getMessage() for r in caplog.records)


def test_a_failed_selector_trace_is_a_capture_error_not_a_disagreement(desk, monkeypatch):
    real = rules.pick_contract

    def traced_breaks(*a, **k):
        if "trace" in k:
            raise RuntimeError("trace broke")
        return real(*a, **k)
    monkeypatch.setattr(rules, "pick_contract", traced_breaks)
    trader = desk.trader()
    desk.signal = observed()
    trader.step()

    row = last_row(desk)
    assert row.outcome == "entered"
    ev = row.detail["evidence"]
    assert ev["status"] == evidence.CAPTURE_INCOMPLETE
    assert [(i["kind"], i["stage"]) for i in ev["issues"]] == [
        (evidence.CAPTURE_ERROR, "selector_trace")]
    assert ev["contract_evidence"]["universe"]["quotes"]                  # snapshot kept
    assert ev["contract_evidence"]["trace_consistent_with_decision"] is None
    assert trader._evidence.failures == 1 and trader._evidence.trace_disagreements == 0


def test_a_disagreeing_trace_is_flagged_as_such_and_not_as_a_capture_failure(desk, monkeypatch,
                                                                             caplog):
    real = rules.pick_contract

    def traced_refuses(*a, **k):
        if "trace" in k:
            k["trace"]["alternatives"], k["trace"]["result"] = [], {"status": "refused"}
            return None, rules.Rejection(rules.NO_DEPTH, "invented")
        return real(*a, **k)
    monkeypatch.setattr(rules, "pick_contract", traced_refuses)
    trader = desk.trader()
    desk.signal = observed()
    with caplog.at_level("ERROR", logger="app.strategy_v2.evidence"):
        trader.step()

    row = last_row(desk)
    assert row.outcome == "entered"
    ev = row.detail["evidence"]
    assert ev["status"] == evidence.INCONSISTENT
    assert [i["kind"] for i in ev["issues"]] == [evidence.TRACE_DISAGREEMENT]
    assert ev["contract_evidence"]["trace_consistent_with_decision"] is False
    assert trader._evidence.trace_disagreements == 1 and trader._evidence.failures == 0
    assert any("disagrees" in r.getMessage() for r in caplog.records)


def test_an_available_at_in_the_future_is_inconsistent_not_complete(desk):
    trader = desk.trader()
    desk.signal = observed(available_at="2099-01-01T00:00:00+00:00")
    trader.step()
    row = last_row(desk)
    ev = row.detail["evidence"]
    assert ev["status"] == evidence.INCONSISTENT
    flagged = {i["clock"] for i in ev["issues"] if i["kind"] == evidence.CLOCK}
    assert "available_at" in flagged and "available_at<=signal_decision_at" in flagged
    assert row.outcome == "entered"                   # trading clocks decided, as before


def test_an_unparseable_clock_is_flagged(desk):
    trader = desk.trader()
    desk.signal = observed(bar_close_time="yesterday-ish")
    trader.step()
    ev = last_evidence(desk)
    assert ev["status"] == evidence.INCONSISTENT
    assert any(i["clock"] == "bar_close_time" for i in ev["issues"])


def test_a_quote_stamped_after_the_decision_is_not_fresh(desk):
    token = rank_order(desk)[0]
    q = next(q for q in desk.chain.quotes(now=desk.clock()) if q.contract.token == token)
    desk.chain.update(OptionTick(token=token, price=q.price,
                                 source_time=desk.clock() + timedelta(minutes=1),
                                 open_interest=1000, volume=500, bid=q.bid, ask=q.ask))
    trader = desk.trader()
    desk.signal = observed()
    trader.step()

    row = last_row(desk)
    assert row.outcome == "entered"                   # the selector saw age 0, as before
    ev = row.detail["evidence"]
    snap = {x["token"]: x for x in ev["contract_evidence"]["universe"]["quotes"]}[token]
    assert snap["freshness"] == "future_timestamp"
    assert snap["age_seconds"] == 0.0 and snap["raw_age_seconds"] == -60.0
    assert ev["status"] == evidence.INCONSISTENT
    assert any(i["clock"] == f"quote {token}" for i in ev["issues"])


def test_non_finite_and_zero_quote_fields_are_classified_before_serialising(desk):
    ranked = rank_order(desk)
    nan_tok, inf_tok, zero_tok = ranked[0], ranked[1], ranked[2]
    now = desk.clock()
    by = {q.contract.token: q for q in desk.chain.quotes(now=now)}
    desk.chain.update(OptionTick(token=nan_tok, price=float("nan"), source_time=now,
                                 open_interest=0, volume=0, bid=float("nan"),
                                 ask=by[nan_tok].ask))
    desk.chain.update(OptionTick(token=inf_tok, price=by[inf_tok].price, source_time=now,
                                 open_interest=float("inf"), volume=None,
                                 bid=by[inf_tok].bid, ask=float("inf")))
    desk.chain.update(OptionTick(token=zero_tok, price=by[zero_tok].price, source_time=now,
                                 open_interest=1000, volume=500, bid=0.0,
                                 ask=by[zero_tok].ask))
    trader = desk.trader()
    desk.signal = observed()
    trader.step()

    ev = last_evidence(desk)
    snap = {x["token"]: x for x in ev["contract_evidence"]["universe"]["quotes"]}
    n, i, z = snap[nan_tok], snap[inf_tok], snap[zero_tok]
    assert (n["bid"], n["bid_status"]) == (None, "invalid_non_finite")
    assert (n["ltp"], n["ltp_status"]) == (None, "invalid_non_finite")
    assert n["open_interest_status"] == "last_known" and n["open_interest"] == 0   # a real zero
    assert (i["ask"], i["ask_status"]) == (None, "invalid_non_finite")
    assert i["open_interest_status"] == "invalid_non_finite" and i["open_interest"] is None
    assert (z["bid"], z["bid_status"]) == (0.0, "not_positive")
    assert all(x["ltp_status"] == "observed" for t, x in snap.items()
               if t not in (nan_tok,))
    assert any(p.endswith(".bid") for p in ev["non_finite_fields"])
    json.dumps(ev, allow_nan=False)


def test_an_unrecognised_rejection_is_unreconciled_not_a_clean_trace(desk, monkeypatch):
    monkeypatch.setattr(rules, "signal_rejection",
                        lambda *a, **k: rules.Rejection("brand_new_code", "new"))
    trader = desk.trader()
    desk.signal = observed()
    trader.step()

    row = last_row(desk)
    assert (row.outcome, row.code) == ("rejected", "brand_new_code")   # decision as made
    ev = row.detail["evidence"]
    assert ev["status"] == evidence.CAPTURE_INCOMPLETE
    assert [i["kind"] for i in ev["issues"]] == [evidence.GATE_UNRECONCILED]
    assert {g["status"] for g in ev["vector"]["decision_context"]["gate_trace"]} == {"unknown"}
    assert trader._evidence.failures == 1


@pytest.mark.parametrize("gate,code", [("feed", rules.KILL_SWITCH), ("nonsense", rules.HOLD),
                                       (None, rules.HOLD)])
def test_a_gate_and_code_that_do_not_belong_together_are_unreconciled(gate, code):
    cap = evidence.Capture(failed_gate=gate)
    trace, problem = evidence.gate_trace(cap, code=code, outcome="rejected")
    assert problem and {g["status"] for g in trace} == {"unknown"}
    trace, problem = evidence.gate_trace(evidence.Capture(), code=rules.ENTERED,
                                         outcome="entered")
    assert problem is None and {g["status"] for g in trace} == {"passed"}


def test_a_disabled_gate_after_the_rejection_is_not_reached(desk):
    trader = desk.trader(cfg=V2Config(require_bias_agreement=False))
    desk.signal = observed({"timestamp": "2026-09-17T10:25:00+05:30", "action": "HOLD",
                            "price": 24000.0, "confidence": 0.2})
    trader.step()
    bias = next(g for g in gates_full(last_evidence(desk)) if g["gate"] == "signal.bias")
    assert bias == {"gate": "signal.bias", "enabled": False, "status": "not_reached"}


def test_a_disabled_gate_that_execution_passes_is_disabled_and_says_so(desk):
    trader = desk.trader(cfg=V2Config(require_bias_agreement=False))
    desk.kill = True
    desk.signal = observed()
    trader.step()
    bias = next(g for g in gates_full(last_evidence(desk)) if g["gate"] == "signal.bias")
    assert bias == {"gate": "signal.bias", "enabled": False, "status": "disabled"}


def gates_full(ev):
    return ev["vector"]["decision_context"]["gate_trace"]


def test_the_recent_history_window_is_bounded_and_says_so():
    rec = evidence.Recorder(V2Config(), strategy="v2", version="t", remember=2)
    seen = [rec._seen(x) for x in ("a", "b", "a", "c", "d", "a")]
    # "a" is remembered while in the window, forgotten once two newer ids push it out.
    assert seen == [False, False, True, False, False, False]


class SpyChain:
    """Counts every contract read the trader makes."""

    def __init__(self, chain):
        self.chain, self.lookups, self.reads = chain, 0, 0

    def lookup(self, expiry):
        self.lookups += 1
        return self if expiry == EXPIRY else None

    def quotes(self, *, now=None):
        self.reads += 1
        return self.chain.quotes(now=now)

    @property
    def max_age_seconds(self):
        return self.chain.max_age_seconds


@pytest.mark.parametrize("setup", ["hold", "window", "kill", "feed", "vix", "past_stop"])
def test_no_contract_is_read_before_selection_is_reached(desk, setup):
    spy = SpyChain(desk.chain)
    trader = desk.trader()
    trader._chain_for = spy.lookup
    signal = observed()
    if setup == "hold":
        signal = observed({"timestamp": "t", "action": "HOLD", "price": 1.0, "confidence": 0.1})
    elif setup == "window":
        desk.clock.at(15, 0)
    elif setup == "kill":
        desk.kill = True
    elif setup == "feed":
        desk.feed_ok = False
    elif setup == "vix":
        desk.vix = 30.0
    elif setup == "past_stop":
        desk.spot = 23900.0
    # `consider` alone: `step` also publishes the dashboard state, whose
    # gauge reads the chain on its own schedule (unchanged by this pass).
    trader.consider(signal, desk.clock())
    assert last_row(desk).outcome == "rejected"
    assert (spy.lookups, spy.reads) == (0, 0)
    assert last_evidence(desk)["contract_evidence"]["status"] == "selection_not_reached"


def test_reaching_selection_reads_the_chain_exactly_once(desk):
    spy = SpyChain(desk.chain)
    trader = desk.trader()
    trader._chain_for = spy.lookup
    trader.consider(observed(), desk.clock())
    assert last_row(desk).outcome == "entered"
    assert (spy.lookups, spy.reads) == (1, 1)


# ---- every gate, one scenario each --------------------------------------------------

def _strip_lot_sizes(desk):
    import dataclasses
    for token, q in list(desk.chain._quotes.items()):
        desk.chain._quotes[token] = dataclasses.replace(
            q, contract=dataclasses.replace(q.contract, lot_size=None))


GATE_SCENARIOS = {
    "signal.direction": (lambda d: None, {"action": "HOLD"}, rules.HOLD),
    "signal.levels": (lambda d: None, {"stop_loss": None}, rules.NO_LEVELS),
    "signal.age": (lambda d: None, {"generated_at": "2026-09-17T04:30:00+00:00"},
                   rules.STALE_SIGNAL),
    "signal.entry_state": (lambda d: None,
                           {"plan": {"bias": {"label": "BULLISH"},
                                     "entry": {"state": "WAIT_PULLBACK"}}}, rules.ENTRY_STATE),
    "signal.bias": (lambda d: None, {"plan": {"bias": {"label": "BEARISH"},
                                              "entry": {"state": "ENTER_NOW"}}}, rules.BIAS),
    "kill_switch": (lambda d: setattr(d, "kill", True), {}, rules.KILL_SWITCH),
    "entry_window": (lambda d: d.clock.at(15, 0), {}, rules.OUTSIDE_WINDOW),
    "instrument_master": (lambda d: setattr(d, "listed", []), {}, rules.CHAIN_NOT_READY),
    "expiry_day": (lambda d: setattr(d, "listed", [date(2026, 9, 17), EXPIRY]), {},
                   rules.EXPIRY_DAY),
    "feed": (lambda d: setattr(d, "feed_ok", False), {}, rules.FEED_DOWN),
    "spot": (lambda d: setattr(d, "spot", None), {}, rules.NO_SPOT),
    "levels_vs_spot": (lambda d: setattr(d, "spot", 23900.0), {}, rules.PAST_STOP),
    "vix": (lambda d: setattr(d, "vix", 30.0), {}, rules.VIX_HIGH),
    "expiry_choice": (lambda d: setattr(d, "listed", [date(2026, 9, 18)]), {}, rules.NO_EXPIRY),
    "chain": (lambda d: setattr(d, "listed", [date(2026, 9, 29)]), {}, rules.CHAIN_NOT_READY),
    "contract_selection": (lambda d: d.clock.advance(seconds=30), {}, rules.STALE_QUOTE),
    "lot_size": (_strip_lot_sizes, {}, rules.CHAIN_NOT_READY),
    "premium_risk": (lambda d: d.quote_all(spread=5.0), {"target": 24001.0},
                     rules.NO_DEFINED_RISK),
}


@pytest.mark.parametrize("gate", sorted(GATE_SCENARIOS))
def test_each_gate_is_recorded_where_it_refused(desk, gate):
    setup, overrides, code = GATE_SCENARIOS[gate]
    setup(desk)
    trader = desk.trader()
    sig = observed()
    sig.update(overrides)
    desk.signal = sig
    trader.step()

    row = last_row(desk)
    assert (row.outcome, row.code) == ("rejected", code)
    trace = gates_full(row.detail["evidence"])
    order = [g["gate"] for g in trace]
    cut = order.index(gate)
    assert [g["status"] for g in trace[:cut]] == ["passed"] * cut
    assert trace[cut]["status"] == "failed" and trace[cut]["code"] == code
    assert {g["status"] for g in trace[cut + 1:]} <= {"not_reached"}
    assert row.detail["evidence"]["issues"] == [] or gate == "signal.age"


def _assert_refused_at(row, gate, code):
    assert (row.outcome, row.code) == ("rejected", code)
    trace = gates_full(row.detail["evidence"])
    cut = [g["gate"] for g in trace].index(gate)
    assert [g["status"] for g in trace[:cut]] == ["passed"] * cut
    assert trace[cut]["status"] == "failed" and trace[cut]["code"] == code
    assert {g["status"] for g in trace[cut + 1:]} <= {"not_reached"}


def test_the_position_gate_is_recorded_where_it_refused(desk):
    trader = desk.trader()
    desk.signal = observed()
    trader.step()                                          # opens the one position
    desk.clock.advance(seconds=1)
    desk.signal = observed(buy_signal(stop=23940.0), observation_id="obs-2", signal_id=42)
    trader.step()
    _assert_refused_at(last_row(desk), "position", rules.POSITION_OPEN)


def test_the_risk_manager_gate_is_recorded_where_it_refused(desk):
    trader = desk.trader(cfg=V2Config(paper_capital=10_000.0))
    desk.signal = observed()
    trader.step()
    row = last_row(desk)
    _assert_refused_at(row, "risk_manager", rules.RISK_VETO)
    ev = row.detail["evidence"]
    assert ev["contract_evidence"]["result"]["status"] == "selected"   # selection passed
    assert ev["vector"]["decision_context"]["risk"]["approved"] is False
