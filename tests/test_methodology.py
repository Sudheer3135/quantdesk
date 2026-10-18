"""Repair Pass 2D — research methodology.

Seen-data registry and prospective holdout (OS-1), session folds and
embargo (OS-2/3), the trial registry (OS-4), frozen benchmarks (OS-5),
sample adequacy and session bootstrap (OS-6), and the daily MTM ledger
(OS-7). Every holdout here is synthetic: no test evaluates the strategy on
real prospective data.
"""
import dataclasses
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app import market_hours
from app.data import clock_grid, research
from app.methodology import (
    benchmarks,
    events,
    folds,
    mtm,
    protection,
    readiness,
    registry,
    sample,
    trials,
)
from app.models import CandleRecord, ResearchEvent

IST = timezone(timedelta(hours=5, minutes=30))


def trading_days(start: date, n: int) -> list[date]:
    out, day = [], start
    while len(out) < n:
        if market_hours.is_trading_date(day):
            out.append(day)
        day += timedelta(days=1)
    return out


DAYS = trading_days(date(2026, 6, 1), 16)
ARCHIVE, FUTURE = DAYS[:6], DAYS[6:]


def ist(day, hh, mm):
    return pd.Timestamp(datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST))


def session_bars(day, price=24_000.0, drop=None):
    stamps = [s.tz_convert("UTC") for s in
              pd.date_range(ist(day, 9, 15), ist(day, 15, 25), freq="5min")]
    if drop:
        stamps = [s for s in stamps if s != ist(day, *drop).tz_convert("UTC")]
    return pd.DataFrame({"timestamp": stamps, "open": price, "high": price + 5,
                         "low": price - 5, "close": price + 1, "volume": 1000.0})


def frame(days, faulty=()):
    return pd.concat([session_bars(d, drop=(9, 20) if d in faulty else None)
                      for d in days], ignore_index=True)


def after_close(day):
    return ist(day, 16, 0).to_pydatetime()


def lock(db, *, target=3, archive=ARCHIVE):
    out = registry.create_lock(db, study_id="study-1", archive=archive,
                               dataset_fingerprint="fp-archive",
                               created_at=after_close(archive[-1]),
                               target_sessions=target)
    db.commit()
    return out


def trusted(purpose=registry.COLLECTION):
    """A grant for this test module, as a trusted internal path would hold.

    The module is added to the trusted holders only for the call: no test
    relies on it being trusted in general, and the spoofing tests run
    without it.
    """
    registry.TRUSTED_HOLDERS[__name__] = frozenset({purpose})
    try:
        return registry.trusted_access(purpose)
    finally:
        del registry.TRUSTED_HOLDERS[__name__]


def certified(raw, as_of=None):
    """Raw in-memory bars certified as a trusted raw fixture path would."""
    return protection.certify_raw_frame(
        raw, source=protection.MEMORY, as_of=as_of,
        certification=trusted(registry.RAW_CERTIFICATION))


def collected(db, candles, as_of=None):
    """In-memory raw candles certified, then protected as collection sees
    them: protected sessions visible, every row stamped with its raw
    session verdict."""
    return protection.protect_research_frame(db, certified(candles, as_of),
                                             source=protection.MEMORY, access=trusted())


def observe(db, days, faulty=(), as_of=None):
    rows = registry.observe_sessions(db, collected(db, frame(days, faulty)),
                                     as_of=as_of or after_close(days[-1]))
    db.commit()
    return rows


def category(db, day):
    return registry.state(db).category(day.isoformat())


# ===========================================================================
# OS-1  seen data
# ===========================================================================

def test_every_unregistered_session_is_seen_pre_registry(db):
    rows = registry.classify_sessions(db, ARCHIVE)
    assert {r.category for r in rows} == {registry.SEEN_PRE_REGISTRY}
    assert all(r.reason == registry.PRE_REGISTRY_REASON for r in rows)
    # With no event log at all (the read-only audit snapshot), the same.
    assert {r.category for r in registry.classify_sessions(None, ARCHIVE)} \
        == {registry.SEEN_PRE_REGISTRY}


def test_a_seen_session_can_never_become_holdout(db):
    registry.register_usage(db, sessions=ARCHIVE, category=registry.SEEN_DEVELOPMENT,
                            study_id="study-0", reason="audit", code_id="abc",
                            dataset_fingerprint="fp")
    db.commit()
    created = lock(db)
    # The boundary is computed from availability, not chosen: the last
    # archive session. There is no argument through which to pick it.
    assert created["seen_through_session"] == ARCHIVE[-1].isoformat()
    observe(db, ARCHIVE)                    # re-observing seen data changes nothing
    assert {category(db, d) for d in ARCHIVE} == {registry.SEEN_DEVELOPMENT}
    first = registry.classify_sessions(db, [ARCHIVE[0]])[0]
    assert first.first_registered_usage is not None and first.study_id == "study-0"


def test_a_future_clean_session_enters_the_candidate_pool(db):
    lock(db)
    day = FUTURE[0]
    # Still trading: pending, and protected already.
    registry.observe_sessions(db, collected(db, frame([day])),
                              as_of=ist(day, 14, 0).to_pydatetime())
    assert category(db, day) == registry.HOLDOUT_PENDING_QUALITY
    observe(db, [day])
    assert category(db, day) == registry.HOLDOUT_CANDIDATE


def test_a_quarantined_session_never_counts_toward_the_target(db):
    lock(db, target=3)
    days = FUTURE[:4]
    observe(db, days, faulty={days[1]})
    assert category(db, days[1]) == registry.EXCLUDED_DATA_QUALITY
    sealed = registry.state(db).locked_sessions()
    assert sealed == [days[0].isoformat(), days[2].isoformat(), days[3].isoformat()]


def test_the_pool_seals_only_at_the_target(db):
    lock(db, target=3)
    observe(db, FUTURE[:2])
    assert registry.state(db).locked_sessions() == []
    assert {category(db, d) for d in FUTURE[:2]} == {registry.HOLDOUT_CANDIDATE}
    observe(db, FUTURE[2:3])
    assert {category(db, d) for d in FUTURE[:3]} == {registry.HOLDOUT_LOCKED}


def test_an_exposed_candidate_is_contaminated_for_good(db):
    lock(db, target=2)
    observe(db, FUTURE[:3])
    assert category(db, FUTURE[0]) == registry.HOLDOUT_LOCKED

    hit = registry.record_exposure(db, sessions=[FUTURE[0]], channel="manual_backtest",
                                   detail="looked at P&L")
    db.commit()
    assert hit == [FUTURE[0].isoformat()]
    assert category(db, FUTURE[0]) == registry.SEEN_CONTAMINATED
    # The pool continues with the next clean session.
    assert registry.state(db).locked_sessions() == [FUTURE[1].isoformat(),
                                                    FUTURE[2].isoformat()]

    # No way back: re-observing, re-locking or registering changes nothing.
    observe(db, FUTURE[:3])
    with pytest.raises(registry.HoldoutError):
        lock(db, target=2, archive=ARCHIVE + FUTURE[:3])   # sealed pool is live
    assert category(db, FUTURE[0]) == registry.SEEN_CONTAMINATED
    assert registry.record_exposure(db, sessions=[FUTURE[0]], channel="x",
                                    detail="again") == []


def test_registering_a_protected_session_as_used_is_refused(db):
    lock(db)
    observe(db, FUTURE[:1])
    with pytest.raises(registry.HoldoutAccessDenied):
        registry.register_usage(db, sessions=FUTURE[:1],
                                category=registry.SEEN_DEVELOPMENT, study_id="s",
                                reason="r", code_id=None, dataset_fingerprint=None)


def store(db, candles):
    for r in candles.itertuples():
        db.add(CandleRecord(symbol="NIFTY", timeframe="5m",
                            timestamp=r.timestamp.to_pydatetime(), open=r.open,
                            high=r.high, low=r.low, close=r.close, volume=r.volume,
                            source="test", session_date=r.timestamp.tz_convert(IST).date(),
                            ingested_at=datetime(2026, 6, 1, tzinfo=UTC)))
    db.commit()


def test_ordinary_research_cannot_read_or_evaluate_a_locked_holdout(db):
    from app.evaluation import outcomes

    store(db, frame(ARCHIVE))
    lock(db, target=2)
    store(db, frame(FUTURE[:3]))            # arrives after the lock
    observe(db, FUTURE[:3])
    protected = {d.isoformat() for d in FUTURE[:3]}

    served = research.load_research_candles(db)
    days = set(registry.archive_sessions(served))
    assert not days & protected
    assert set(served.attrs["holdout"]["withheld_sessions"]) == protected

    # As-known reads are withheld too.
    as_known = research.load_research_candles(db, as_known_at=datetime.now(UTC))
    assert not set(registry.archive_sessions(as_known)) & protected

    # Collection and validation still see everything.
    raw = research.load_research_candles(db, access=trusted(registry.DATA_QUALITY))
    assert protected <= set(registry.archive_sessions(raw))

    # The evaluator's candles come from the same guarded loader.
    assert not set(registry.archive_sessions(outcomes.collect(db).candles)) & protected

    with pytest.raises(registry.HoldoutAccessDenied):
        registry.assert_accessible(db, FUTURE[:1])
    with pytest.raises(registry.HoldoutAccessDenied):
        folds.build(db, {d.isoformat(): {"quality": "clean"} for d in ARCHIVE + FUTURE[:3]},
                    n_folds=1, min_train_sessions=2, validation_sessions=1)


def test_a_live_signal_on_a_protected_session_contaminates_it(db):
    lock(db)
    observe(db, FUTURE[:1])
    hit = registry.note_strategy_output(db, moment=ist(FUTURE[0], 10, 2).to_pydatetime(),
                                        channel="strategy_signal:agent", detail="BUY")
    assert hit == [FUTURE[0].isoformat()]
    assert category(db, FUTURE[0]) == registry.SEEN_CONTAMINATED
    exposure = registry.state(db).exposed[FUTURE[0].isoformat()][0]
    assert exposure["channel"] == "strategy_signal:agent"
    assert exposure["was"] == registry.HOLDOUT_CANDIDATE


# ---- one-time final evaluation ---------------------------------------------

def study(db, study_id="study-1"):
    trials.register_study(db, {"study_id": study_id,
                               "primary_metric": benchmarks.PRIMARY_METRIC})
    db.commit()


def spec(trial_id, *, mode=trials.CONFIRMATORY, parameter_hash="p1", study_id="study-1",
         family="family-a"):
    return {"trial_id": trial_id, "study_id": study_id, "family_id": family,
            "mode": mode, "hypothesis": "h", "rationale": "r",
            "strategy_version": "nifty-signal-engine/1", "code_id": "09c6ffc2eb1d",
            "parameter_hash": parameter_hash, "changed_parameters": {},
            "dataset_ids": ["fp"], "fold_manifest_hash": "fm",
            "embargo_policy": folds.EmbargoPolicy().to_dict(),
            "execution_policy_hash": "e", "cost_model_hash": "c",
            "primary_metric": benchmarks.PRIMARY_METRIC,
            "secondary_metrics": list(benchmarks.SECONDARY_METRICS),
            "benchmark_ids": benchmarks.ids()}


def candidate(s):
    return {k: s[k] for k in registry.FROZEN_FIELDS}


def factory(db):
    """Independent sessions on the same database, as final_evaluation needs."""
    from sqlalchemy.orm import sessionmaker
    return sessionmaker(bind=db.get_bind(), expire_on_commit=False)


def sealed_holdout(db, target=2):
    study(db)
    lock(db, target=target)
    observe(db, FUTURE[:target])


def test_final_evaluation_consumes_the_holdout_exactly_once(db):
    sealed_holdout(db)
    s = trials.preregister(db, spec("t-final"))
    db.commit()
    seen = []
    out = registry.final_evaluation(factory(db), candidate(s),
                                    lambda sessions: seen.append(sessions) or {"r": 0.1})
    db.commit()
    assert seen == [[d.isoformat() for d in FUTURE[:2]]]
    assert {category(db, d) for d in FUTURE[:2]} == {registry.HOLDOUT_CONSUMED}
    assert out["result_fingerprint"] == events.digest({"r": 0.1})
    assert trials.state(db).trials["t-final"].results[0]["inspected"] is True

    with pytest.raises(registry.HoldoutError, match="already consumed"):
        registry.final_evaluation(factory(db), candidate(s), lambda _: {"r": 9})


def test_a_changed_strategy_cannot_reuse_a_consumed_holdout(db):
    sealed_holdout(db)
    first = trials.preregister(db, spec("t-1"))
    db.commit()
    registry.final_evaluation(factory(db), candidate(first), lambda _: {"r": 0.1})
    db.commit()

    changed = trials.preregister(db, spec("t-2", parameter_hash="p2"))
    db.commit()
    with pytest.raises(registry.HoldoutError, match="already consumed"):
        registry.final_evaluation(factory(db), candidate(changed), lambda _: {"r": 0.2})

    # A new generation starts after the consumed sessions; they stay consumed.
    nxt = lock(db, target=2, archive=ARCHIVE + FUTURE[:2])
    assert nxt["seen_through_session"] >= FUTURE[1].isoformat()
    observe(db, FUTURE[2:4])
    assert {category(db, d) for d in FUTURE[:2]} == {registry.HOLDOUT_CONSUMED}
    assert registry.state(db).locked_sessions() == [d.isoformat() for d in FUTURE[2:4]]


def test_a_crash_after_consumption_cannot_be_rolled_back_into_a_second_try(db):
    """The real failure sequence: consumption is committed, the evaluator
    throws, the caller rolls back its own work, and a new session tries
    again. The holdout stays consumed and the retry is refused."""
    sealed_holdout(db)
    s = trials.preregister(db, spec("t-fail"))
    db.commit()

    def boom(_):
        raise RuntimeError("crashed after reading")

    with pytest.raises(RuntimeError):
        registry.final_evaluation(factory(db), candidate(s), boom)
    db.rollback()                                   # the caller's cleanup

    with factory(db)() as fresh:
        kinds = [e.event_type for e in events.read(fresh)]
        assert kinds.count("holdout_consumed") == 1
        assert "final_evaluation_failed" in kinds
        assert "final_evaluation_result" not in kinds
        assert {registry.state(fresh).category(d.isoformat())
                for d in FUTURE[:2]} == {registry.HOLDOUT_CONSUMED}
    retry = trials.preregister(db, spec("t-retry"))
    db.commit()
    with pytest.raises(registry.HoldoutError, match="already consumed"):
        registry.final_evaluation(factory(db), candidate(retry), lambda _: {"r": 1})


def test_two_contenders_cannot_both_consume(db):
    """Both read an unconsumed holdout; only the first write lands. The loser
    fails at the write, before anything is evaluated."""
    sealed_holdout(db)
    a = trials.preregister(db, spec("t-a"))
    b = trials.preregister(db, spec("t-b"))
    db.commit()
    make = factory(db)
    with make() as first, make() as second:
        plan_a = registry.plan_consumption(first, candidate(a))
        first.rollback()
        plan_b = registry.plan_consumption(second, candidate(b))
        second.rollback()
        registry.commit_consumption(first, plan_a)
        with pytest.raises(registry.HoldoutError, match="refused"):
            registry.commit_consumption(second, plan_b)
    with make() as check:
        assert [e.event_type for e in events.read(check)].count("holdout_consumed") == 1


def forge(db, *, subject, consumed_lock_id, event_type="holdout_consumed",
          lock_id=None, well_hashed=False):
    """Insert a research event with raw SQL: no ORM, no `events.append`, no
    registry — exactly what a buggy or hostile writer could do."""
    last = db.scalars(select(ResearchEvent).order_by(ResearchEvent.seq.desc())).first()
    seq, prev = (last.seq + 1, last.event_hash) if last else (1, events.GENESIS)
    created = datetime.now(UTC)
    payload = {"lock_id": lock_id or subject, "sessions": [FUTURE[0].isoformat()]}
    digest = (events._hash(seq=seq, stream="holdout", event_type=event_type,  # noqa: SLF001
                           subject=subject, payload=payload, created_at=created,
                           prev_hash=prev, consumed_lock_id=consumed_lock_id)
              if well_hashed else f"{seq:064x}")
    db.execute(text(
        "INSERT INTO research_events (seq, stream, event_type, subject, payload, "
        "created_at, prev_hash, event_hash, consumed_lock_id) VALUES (:seq, 'holdout', "
        ":event_type, :subject, :payload, :created, :prev, :hash, :consumed)"),
        {"seq": seq, "event_type": event_type, "subject": subject,
         "payload": events.canonical(payload), "created": created, "prev": prev,
         "hash": digest, "consumed": consumed_lock_id})
    db.flush()


def test_the_schema_admits_one_consumption_per_generation_whatever_writes_it(db):
    """Pass 2D.2: the invariant lives in the table, not in the application.
    Every forged second consumption of the same generation is refused by
    the database; a different generation is not."""
    from sqlalchemy.exc import IntegrityError

    sealed_holdout(db)
    a = trials.preregister(db, spec("t-a"))
    db.commit()
    registry.final_evaluation(factory(db), candidate(a), lambda _: {"r": 0})   # first: ok
    lock_id = registry.state(db).lock["lock_id"]

    attempts = {
        "same generation, id set": dict(subject=lock_id, consumed_lock_id=lock_id),
        "same generation, id NULL": dict(subject=lock_id, consumed_lock_id=None),
        "same generation, forged id": dict(subject=lock_id,
                                           consumed_lock_id=f"holdout_consumed:{lock_id}"),
        "same generation, other id": dict(subject=lock_id, consumed_lock_id="study-1:holdout:9"),
        "id on a non-consumption event": dict(subject=lock_id, consumed_lock_id=lock_id,
                                              event_type="final_evaluation_result"),
    }
    for name, forged in attempts.items():
        with pytest.raises(IntegrityError):
            forge(db, **forged)
            pytest.fail(f"accepted: {name}")
        db.rollback()

    # The ORM, bypassing `events.append`, is refused the same way.
    last = db.scalars(select(ResearchEvent).order_by(ResearchEvent.seq.desc())).first()
    db.add(ResearchEvent(seq=last.seq + 1, stream="holdout", event_type="holdout_consumed",
                         subject=lock_id, payload={"lock_id": lock_id, "sessions": []},
                         created_at=datetime.now(UTC), prev_hash=last.event_hash,
                         event_hash="e" * 64, consumed_lock_id=None))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()

    with factory(db)() as fresh:
        assert [e.event_type for e in events.read(fresh)].count("holdout_consumed") == 1

    # A different generation is a different consumption: it succeeds.
    lock(db, target=2, archive=ARCHIVE + FUTURE[:2])
    observe(db, FUTURE[2:4])
    b = trials.preregister(db, spec("t-b"))
    db.commit()
    registry.final_evaluation(factory(db), candidate(b), lambda _: {"r": 1})
    with factory(db)() as fresh:
        consumed = [e.subject for e in events.read(fresh) if e.event_type == "holdout_consumed"]
        assert consumed == [lock_id, registry.state(fresh).lock["lock_id"]] \
            and len(set(consumed)) == 2


def test_a_consumption_row_cannot_spend_one_generation_under_another_name(db):
    """A row the schema accepts — its own, unused generation id — whose
    payload points at a real generation is not read as consuming it."""
    sealed_holdout(db)
    lock_id = registry.state(db).lock["lock_id"]
    forge(db, subject="decoy", consumed_lock_id="decoy", lock_id=lock_id, well_hashed=True)
    db.commit()
    with pytest.raises(events.TamperedLog, match="spends 'decoy'"):
        registry.state(db)


@pytest.mark.parametrize("change,message", [
    (lambda c: c.pop("cost_model_hash"), "not frozen"),
    (lambda c: c.update(code_id="09c6ffc2eb1d+dirty.abc"), "dirty"),
    (lambda c: c.update(parameter_hash="other"), "differs from its preregistration"),
    (lambda c: c.update(trial_id="never-registered"), "not preregistered"),
])
def test_an_unfrozen_or_mismatched_candidate_cannot_consume(db, change, message):
    sealed_holdout(db)
    s = trials.preregister(db, spec("t-x"))
    db.commit()
    c = candidate(s)
    change(c)
    with pytest.raises(registry.HoldoutError, match=message):
        registry.final_evaluation(factory(db), c, lambda _: {})
    assert registry.state(db).consumed == {}


def test_an_exploratory_trial_cannot_consume_and_the_pool_must_be_sealed(db):
    study(db)
    lock(db, target=3)
    observe(db, FUTURE[:2])
    s = trials.preregister(db, spec("t-c"))
    db.commit()
    with pytest.raises(registry.HoldoutError, match="not sealed"):
        registry.final_evaluation(factory(db), candidate(s), lambda _: {})
    observe(db, FUTURE[2:3])
    e = trials.preregister(db, spec("t-e", mode=trials.EXPLORATORY))
    db.commit()
    with pytest.raises(registry.HoldoutError, match="confirmatory"):
        registry.final_evaluation(factory(db), candidate(e), lambda _: {})


# ---- tamper evidence --------------------------------------------------------

def test_the_event_log_is_append_only_and_tamper_evident(db):
    lock(db)
    row = db.scalars(select(ResearchEvent)).first()
    row.subject = "edited"
    with pytest.raises(events.ImmutableEvent):
        db.flush()
    db.rollback()
    with pytest.raises(events.ImmutableEvent):
        db.delete(db.scalars(select(ResearchEvent)).first())
        db.flush()
    db.rollback()

    db.execute(text("UPDATE research_events SET payload = :p WHERE seq = 1"),
               {"p": '{"seen_through_session": "2020-01-01"}'})
    db.commit()
    db.expire_all()
    with pytest.raises(events.TamperedLog, match="altered"):
        registry.state(db)


# ===========================================================================
# OS-2 / OS-3  folds and embargo
# ===========================================================================

def table(days, faulty=()):
    return {d.isoformat(): {"quality": "faulty" if d in faulty else "clean",
                            "dataset_fingerprint": f"fp-{d}"} for d in days}


def test_folds_split_whole_sessions_chronologically_with_an_embargo(db):
    days = DAYS[:14]
    m = folds.build(db, table(days, faulty={days[4]}), n_folds=3, min_train_sessions=4,
                    validation_sessions=2)
    assert m.excluded == (days[4].isoformat(),)
    for f in m.folds:
        assert not set(f.train) & set(f.validation)
        assert not set(f.embargo) & set(f.validation)
        assert not set(f.train) & set(f.embargo)
        assert max(f.train) < min(f.embargo) < min(f.validation)
        assert len(f.embargo) == 1
        assert days[4].isoformat() not in f.train + f.embargo + f.validation
    # Expanding: every fold trains on all of the previous fold's sessions.
    for a, b in zip(m.folds, m.folds[1:]):
        assert set(a.train) < set(b.train)
        assert max(a.validation) < min(b.validation)
    assert m.label == folds.SEEN_DATA_LABEL


def test_folds_are_deterministic_and_never_shuffled(db):
    days = DAYS[:12]
    t = table(days)
    reversed_input = dict(reversed(list(t.items())))
    a = folds.build(db, t, n_folds=2, min_train_sessions=4, validation_sessions=2)
    b = folds.build(db, reversed_input, n_folds=2, min_train_sessions=4, validation_sessions=2)
    assert a.manifest_hash == b.manifest_hash
    assert a.to_dict() == b.to_dict()
    assert list(a.folds[0].train) == sorted(a.folds[0].train)
    # A changed fingerprint is a different manifest.
    t2 = t | {days[0].isoformat(): {"quality": "clean", "dataset_fingerprint": "other"}}
    assert folds.build(db, t2, n_folds=2, min_train_sessions=4,
                       validation_sessions=2).manifest_hash != a.manifest_hash


def test_a_longer_holding_horizon_expands_the_embargo(db):
    policy = folds.EmbargoPolicy(embargo_sessions=1, label_horizon_sessions=3)
    assert policy.required == 3
    m = folds.build(db, table(DAYS[:14]), n_folds=2, min_train_sessions=3,
                    validation_sessions=2, policy=policy)
    assert all(len(f.embargo) == 3 for f in m.folds)


def test_warmup_is_past_only_and_only_validation_is_scored(db):
    days = DAYS[:10]
    candles = frame(days)
    m = folds.build(db, table(days, faulty={days[1]}), n_folds=1, min_train_sessions=4,
                    validation_sessions=2)
    f = m.folds[0]
    out = folds.validation_frame(db, candles, f, m.excluded)
    scored_days = set(registry.archive_sessions(out[out["scored"]]))
    context = out[~out["scored"]]
    assert scored_days == set(f.validation)
    assert not set(f.embargo) & scored_days
    assert pd.to_datetime(context["timestamp"], utc=True).max() < \
        pd.to_datetime(out.loc[out["scored"], "timestamp"], utc=True).min()
    assert days[1].isoformat() not in registry.archive_sessions(out)
    assert max(registry.archive_sessions(out)) == max(f.validation)   # nothing after


def test_a_trade_spanning_the_embargo_is_not_scored(db):
    m = folds.build(db, table(DAYS[:10]), n_folds=1, min_train_sessions=4,
                    validation_sessions=2)
    f = m.folds[0]
    embargo, val = date.fromisoformat(f.embargo[0]), date.fromisoformat(f.validation[0])
    inside = SimpleNamespace(entry_time=ist(val, 10, 0).isoformat(),
                             exit_time=ist(val, 11, 0).isoformat())
    spanning = SimpleNamespace(entry_time=ist(embargo, 15, 0).isoformat(),
                               exit_time=ist(val, 9, 20).isoformat())
    assert folds.scored_trades([inside, spanning], f) == [inside]


# ===========================================================================
# OS-4  trials
# ===========================================================================

def test_a_result_needs_a_prior_preregistration(db):
    study(db)
    with pytest.raises(trials.TrialError, match="not preregistered"):
        trials.record_result(db, trial_id="ghost", result={"x": 1}, inspected=True,
                             partition="dev")


def test_a_result_does_not_touch_the_preregistration(db):
    study(db)
    registered = trials.preregister(db, spec("t-1", mode=trials.EXPLORATORY))
    db.commit()
    before = [e for e in events.read(db) if e.event_type == "trial_preregistered"]
    trials.record_result(db, trial_id="t-1", result={"net_expectancy_r": -0.1},
                         inspected=True, partition="fold-1")
    db.commit()
    after = [e for e in events.read(db) if e.event_type == "trial_preregistered"]
    assert before == after
    assert after[0].payload == registered
    assert events.verify(db) == 3          # study, preregistration, result — chain intact


def test_repeats_count_as_attempts_and_modes_are_preserved(db):
    study(db)
    for n, mode in enumerate([trials.EXPLORATORY, trials.EXPLORATORY,
                              trials.CONFIRMATORY], start=1):
        trials.preregister(db, spec(f"t-{n}", mode=mode))
        trials.record_result(db, trial_id=f"t-{n}", result={"r": n}, inspected=True,
                             partition="fold-1")
    trials.preregister(db, spec("t-4", mode=trials.EXPLORATORY))   # never run
    db.commit()
    with pytest.raises(trials.TrialError, match="another attempt"):
        trials.record_result(db, trial_id="t-1", result={"r": 1}, inspected=True,
                             partition="fold-1")

    fam = trials.summary(trials.state(db))["families"]["family-a"]
    assert fam["registered_attempts"] == 4
    assert fam["results_inspected"] == 3
    assert fam["completed"] == 3
    assert (fam["exploratory"], fam["confirmatory"]) == (3, 1)
    # All four specify the same experiment: repeats are counted, not merged.
    assert fam["distinct_specifications"] == 1


def test_the_result_fingerprint_is_deterministic():
    assert trials.result_fingerprint({"a": 1, "b": [1, 2]}) == \
        trials.result_fingerprint({"b": [1, 2], "a": 1})
    assert trials.result_fingerprint({"a": 1}) != trials.result_fingerprint({"a": 2})


def test_the_pre_registry_trial_count_is_unknown_not_zero(db):
    s = trials.summary(trials.state(db))
    assert s["pre_registry_exploration"] is True
    assert s["pre_registry_trial_count"] is None
    assert s["pre_registry_trial_count_status"] == "unknown"
    assert s["known_registered_trials"] == 0


def test_a_study_primary_metric_is_frozen_once_results_exist(db):
    study(db)
    trials.register_study(db, {"study_id": "study-1", "primary_metric": "net_pnl"})
    trials.register_study(db, {"study_id": "study-1",
                               "primary_metric": benchmarks.PRIMARY_METRIC})
    trials.preregister(db, spec("t-1"))
    trials.record_result(db, trial_id="t-1", result={}, inspected=True, partition="dev")
    db.commit()
    with pytest.raises(trials.TrialError, match="frozen"):
        trials.register_study(db, {"study_id": "study-1", "primary_metric": "win_rate"})


# ===========================================================================
# OS-5 / OS-6  benchmarks, sample, bootstrap
# ===========================================================================

def test_benchmark_definitions_are_frozen():
    benchmarks.verify_frozen()
    with pytest.raises(dataclasses.FrozenInstanceError):
        benchmarks.CASH.definition = "something else"
    edited = dataclasses.replace(benchmarks.PASSIVE_NIFTY, parameters=(("entry_bar", "09:20"),))
    assert edited.definition_hash != benchmarks.DEFINITION_HASHES[edited.benchmark_id]
    baseline = dict(benchmarks.PRE_OPTIMISATION_BASELINE.parameters)
    assert baseline["git_commit"] == "09c6ffc2eb1d0ce99da55ba1f2d659a93e8a673e"


def test_the_study_manifest_names_the_benchmarks_and_metric(db):
    m = folds.build(db, table(DAYS[:10]), n_folds=1, min_train_sessions=4,
                    validation_sessions=2)
    manifest = readiness.study_manifest(study_id="s", fold_manifest=m,
                                        dataset_fingerprint="fp")
    assert manifest["benchmark_ids"] == benchmarks.ids()
    assert manifest["primary_metric"] == "net_expectancy_r"
    assert manifest["fold_manifest_hash"] == m.manifest_hash
    assert manifest["embargo_policy"]["required_embargo_sessions"] == 1
    assert manifest["fold_label"] == folds.SEEN_DATA_LABEL


def test_the_passive_comparator_never_turns_an_excluded_session_into_zero():
    rows = benchmarks.passive_nifty(frame(DAYS[:2]), excluded={DAYS[1].isoformat()})
    assert rows[0]["status"] == "ok"
    assert rows[0]["session_return"] == pytest.approx(24_001 / 24_000 - 1)
    assert rows[1]["session_return"] is None and rows[1]["status"] == "unavailable"


def outcomes_for(n_sessions, trades_each=1):
    return [sample.SessionOutcome(session=f"2026-06-{i + 1:02d}", net_pnl=float(i - 3),
                                  trade_r=tuple([0.5 if i % 2 else -1.0] * trades_each),
                                  trade_pnl=tuple([50.0 if i % 2 else -100.0] * trades_each))
            for i in range(n_sessions)]


def test_small_samples_are_labelled_and_sessions_are_not_trades():
    rows = outcomes_for(5, trades_each=10) + [
        sample.SessionOutcome(session="2026-07-01", net_pnl=0.0)]
    report = sample.adequacy(rows, excluded_sessions=2)
    assert report["sample_status"] == sample.INSUFFICIENT
    assert (report["independent_sessions"], report["closed_trades"]) == (6, 50)
    assert report["no_trade_sessions"] == 1 and report["active_trading_sessions"] == 5
    assert report["positive_sessions"] + report["negative_sessions"] \
        + report["flat_sessions"] == 6
    big = sample.adequacy(outcomes_for(30, trades_each=4))
    assert big["sample_status"] == sample.MEETS_POLICY


def test_the_session_bootstrap_is_seeded_and_leaves_the_data_alone():
    rows = outcomes_for(12)
    original = list(rows)
    a = sample.session_bootstrap(rows, seed=7, resamples=300)
    b = sample.session_bootstrap(rows, seed=7, resamples=300)
    c = sample.session_bootstrap(rows, seed=8, resamples=300)
    assert a == b
    assert c["provenance"]["draws_fingerprint"] != a["provenance"]["draws_fingerprint"]
    assert rows == original
    assert a["provenance"]["cluster_basis"] == sample.CLUSTER_BASIS
    assert a["provenance"]["resample_unit_count"] == 12          # sessions, not trades
    assert "no significance" in a["provenance"]["note"]


def test_there_is_no_trade_level_bootstrap_path():
    with pytest.raises(TypeError, match="IID"):
        sample.session_bootstrap([0.5, -1.0, 0.5], seed=1)
    with pytest.raises(TypeError):
        sample.adequacy([SimpleNamespace(r=1.0)])


def test_an_undefined_profit_factor_stays_unavailable():
    winners = [sample.SessionOutcome(session=f"d{i}", net_pnl=10.0, trade_r=(1.0,),
                                     trade_pnl=(10.0,)) for i in range(5)]
    out = sample.session_bootstrap(winners, seed=1, resamples=50)
    assert out["point"]["profit_factor"] is None
    assert out["intervals"]["profit_factor"]["status"] == "unavailable"


# ===========================================================================
# OS-7  daily MTM
# ===========================================================================

SESSIONS = {d.isoformat(): mtm.CLEAN for d in DAYS[:3]}


def fill(pid, kind, day, hh, mm, price, **kw):
    return mtm.Fill(position_id=pid, kind=kind, time=ist(day, hh, mm).to_pydatetime(),
                    side=kw.pop("side", "BUY"), quantity=kw.pop("quantity", 10),
                    price=price, **kw)


def test_a_no_trade_session_is_flat_and_present():
    rows = mtm.build([], sessions=SESSIONS, starting_equity=100_000)
    assert len(rows) == 3
    for row in rows:
        assert (row["continuity_starting_equity"], row["continuity_ending_equity"]) \
            == (100_000, 100_000)
        assert (row["clean_starting_equity"], row["clean_ending_equity"]) == (100_000, 100_000)
        assert row["scored"] is True
        assert (row["realised_pnl"], row["fees"], row["session_return"]) == (0, 0, 0)
        assert row["mark_basis"] == mtm.FLAT_BASIS and row["status"] == mtm.OK


def test_a_closed_profitable_trade_reconciles_exactly():
    d = DAYS[0]
    rows = mtm.build([fill("p1", "open", d, 10, 0, 24_000.0, friction=5.0),
                      fill("p1", "close", d, 11, 0, 24_050.0, friction=5.0, fees=40.0)],
                     sessions=SESSIONS, starting_equity=100_000)
    day = rows[0]
    assert day["realised_pnl"] == 500.0            # (24050 - 24000) * 10, friction inside
    assert day["execution_friction"] == 10.0
    assert day["fees"] == 40.0
    assert day["continuity_ending_equity"] == day["clean_ending_equity"] == 100_460.0
    assert day["session_pnl"] == day["realised_pnl"] - day["fees"]
    assert day["session_return"] == pytest.approx(460 / 100_000)
    assert rows[1]["clean_starting_equity"] == 100_460.0 and rows[1]["session_pnl"] == 0


def marks_at(price, observed, available):
    def source(instrument, pid, at):
        return mtm.Mark(price=price, observed_at=observed, available_at=available,
                        basis="test_mark")
    return source


def test_an_open_position_is_marked_from_a_permissible_mark_only():
    d = DAYS[0]
    opened = [fill("p1", "open", d, 14, 0, 24_000.0)]
    ok = mtm.build(opened, sessions={d.isoformat(): mtm.CLEAN}, starting_equity=100_000,
                   marks=marks_at(24_030.0, ist(d, 15, 25).to_pydatetime(),
                                  ist(d, 15, 30).to_pydatetime()))[0]
    assert ok["unrealised_pnl"] == 300.0 and ok["continuity_ending_equity"] == 100_300.0
    assert ok["open_position_count"] == 1 and ok["mark_basis"] == "test_mark"
    assert ok["exposure"] == 240_300.0

    future = mtm.build(opened, sessions={d.isoformat(): mtm.CLEAN}, starting_equity=100_000,
                       marks=marks_at(24_100.0, ist(d, 15, 25).to_pydatetime(),
                                      ist(d, 15, 35).to_pydatetime()))[0]
    assert future["status"] == mtm.MTM_UNAVAILABLE
    assert "future" in future["marks"][0]["reason"]

    prior = DAYS[0] - timedelta(days=3)
    stale = mtm.build(opened, sessions={d.isoformat(): mtm.CLEAN}, starting_equity=100_000,
                      marks=marks_at(23_900.0, ist(prior, 15, 25).to_pydatetime(),
                                     ist(prior, 15, 30).to_pydatetime()))[0]
    assert stale["status"] == mtm.MTM_UNAVAILABLE
    assert "forward-fill" in stale["marks"][0]["reason"]


def test_a_missing_mark_is_unavailable_not_fabricated():
    d = DAYS[0]
    row = mtm.build([fill("p1", "open", d, 14, 0, 24_000.0)], sessions=SESSIONS,
                    starting_equity=100_000, marks=lambda *a: None)[0]
    assert row["status"] == mtm.MTM_UNAVAILABLE
    assert row["unrealised_pnl"] is None and row["continuity_ending_equity"] is None
    assert row["scored"] is False and row["clean_ending_equity"] is None
    assert row["session_return"] is None and row["drawdown"] is None


def test_a_quarantined_session_is_excluded_not_a_zero_day():
    sessions = SESSIONS | {DAYS[1].isoformat(): mtm.EXCLUDED}
    rows = mtm.build([], sessions=sessions, starting_equity=100_000)
    excluded = rows[1]
    assert excluded["status"] == mtm.EXCLUDED
    for field in ("clean_starting_equity", "realised_pnl", "unrealised_pnl", "fees",
                  "execution_friction", "clean_ending_equity", "session_pnl",
                  "session_return", "drawdown"):
        assert excluded[field] is None, field
    assert excluded["scored"] is False
    assert rows[2]["clean_starting_equity"] == 100_000

    # The account traded on the excluded session anyway: booked for
    # continuity, never reported as a performance observation.
    traded = mtm.build([fill("p", "open", DAYS[1], 10, 0, 100.0),
                        fill("p", "close", DAYS[1], 11, 0, 110.0, fees=20.0)],
                       sessions=sessions, starting_equity=100_000)
    row = traded[1]
    assert row["status"] == mtm.EXCLUDED
    assert row["executions_on_excluded_session"] == 2
    assert (row["session_pnl"], row["session_return"], row["drawdown"]) == (None, None, None)
    assert row["continuity_ending_equity"] == 100_080.0          # booked for continuity
    assert traded[2]["continuity_starting_equity"] == 100_080.0
    assert traded[2]["clean_starting_equity"] == 100_000           # but never scored
    assert traded[2]["session_pnl"] == 0


def test_overlapping_outcomes_are_refused():
    evaluator_row = SimpleNamespace(signal_id=48, entry_time="x", exit_time="y")
    with pytest.raises(mtm.LedgerError, match="hypothetical"):
        mtm.build([evaluator_row], sessions=SESSIONS, starting_equity=100_000)
    with pytest.raises(mtm.LedgerError, match="hypothetical|position ledger"):
        mtm.fills_from_engine([evaluator_row])
    d = DAYS[0]
    with pytest.raises(mtm.LedgerError, match="overlapping"):
        mtm.build([fill("a", "open", d, 10, 0, 1.0), fill("b", "open", d, 10, 5, 1.0),
                   fill("a", "close", d, 11, 0, 1.0), fill("b", "close", d, 11, 5, 1.0)],
                  sessions=SESSIONS, starting_equity=100_000)


def test_engine_trades_become_a_reconciled_fill_stream():
    from app.backtest.engine import Trade

    d = DAYS[0]
    trade = Trade(entry_time=ist(d, 10, 5).isoformat(), exit_time=ist(d, 11, 0).isoformat(),
                  side="SELL", entry=24_000.0, exit=23_960.0, quantity=5,
                  stop_loss=24_040.0, target=23_920.0, pnl=80.0, r_multiple=0.4,
                  exit_reason="time", confidence=0.7, gross_pnl=206.0,
                  execution_friction=6.0, fees=120.0, brokerage=120.0, statutory_fees=0.0,
                  net_pnl=80.0,
                  entry_side="SELL", exit_side="BUY")
    rows = mtm.build(mtm.fills_from_engine([trade]), sessions=SESSIONS,
                     starting_equity=100_000)
    assert rows[0]["continuity_ending_equity"] == 100_080.0
    assert rows[0]["fees"] == 120.0 and rows[0]["realised_pnl"] == 200.0


# ===========================================================================
# readiness
# ===========================================================================

def test_readiness_keeps_each_answer_separate(db):
    out = readiness.summary(db, frame(ARCHIVE))
    assert out["current_historical_data"]["status"] == "SEEN / DEVELOPMENT ONLY"
    assert out["current_historical_data"]["classification"] == {
        registry.SEEN_PRE_REGISTRY: len(ARCHIVE)}
    assert out["seen_data_cross_validation"]["status"] == readiness.AVAILABLE
    assert out["genuine_untouched_holdout"]["status"].startswith("NOT YET AVAILABLE")
    assert out["prospective_holdout_mechanism"]["status"] == readiness.READY
    assert out["option_historical_executable_research"]["status"] == readiness.BLOCKED
    assert out["trial_registry"]["pre_registry_trial_count"] is None
    # Without the table (an unmigrated or read-only archive): not ready, said so.
    bare = readiness.summary(None, frame(ARCHIVE))
    assert bare["trial_registry"]["status"].startswith(readiness.NOT_READY)


def test_the_methodology_endpoint_reports_the_archive_as_seen(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import data as data_api
    from app.db import get_db

    store(db, frame(ARCHIVE))
    app = FastAPI()
    app.include_router(data_api.router)
    app.dependency_overrides[get_db] = lambda: db
    body = TestClient(app).get("/data/methodology").json()
    assert body["current_historical_data"]["classification"] == {
        registry.SEEN_PRE_REGISTRY: len(ARCHIVE)}
    assert body["genuine_untouched_holdout"]["status"].startswith("NOT YET AVAILABLE")
    assert body["option_historical_executable_research"]["status"] == "BLOCKED_BY_DATA"


def engine_trade(**change):
    """gross 510, friction 10, fees 40, net 460: a consistent long.

    Pass 2D.1's brief gave "gross 500, friction 10, fees 40, net 460" as the
    passing case, but under its own identity (gross − friction − fees = net)
    that is 450, not 460 — see test_the_brief_example_is_checked_by_arithmetic.
    """
    from app.backtest.engine import Trade

    d = DAYS[0]
    base = dict(entry_time=ist(d, 10, 5).isoformat(), exit_time=ist(d, 11, 0).isoformat(),
                side="BUY", entry=24_000.0, exit=24_050.0, quantity=10,
                stop_loss=23_950.0, target=24_100.0, pnl=460.0, r_multiple=0.9,
                exit_reason="time", confidence=0.7, gross_pnl=510.0,
                execution_friction=10.0, fees=40.0, brokerage=30.0, statutory_fees=10.0,
                net_pnl=460.0, entry_side="BUY", exit_side="SELL",
                entry_fill_exact=24_000.0, exit_fill_exact=24_050.0)
    return Trade(**(base | change))


def test_a_consistent_trade_reconciles_on_its_unrounded_fills():
    fills = mtm.fills_from_engine([engine_trade()])
    assert [f.price_basis for f in fills] == [mtm.EXACT_FILL, mtm.EXACT_FILL]
    row = mtm.build(fills, sessions=SESSIONS, starting_equity=100_000)[0]
    assert row["continuity_ending_equity"] == 100_460.0 and row["fees"] == 40.0
    assert row["execution_friction"] == 10.0 and row["realised_pnl"] == 500.0


def test_unrounded_source_prices_are_used_exactly():
    """True fills 24000.004 / 24050.003 display as 24000.0 / 24050.0; the
    ledger takes the true ones, not the display."""
    t = engine_trade(entry_fill_exact=24_000.004, exit_fill_exact=24_050.003,
                     gross_pnl=509.99, net_pnl=459.99, pnl=459.99)
    open_fill, close_fill = mtm.fills_from_engine([t])
    assert (open_fill.price, close_fill.price) == (24_000.004, 24_050.003)


@pytest.mark.parametrize("change,why", [
    (dict(gross_pnl=600.0), "≠ net"),                               # contradictory gross
    (dict(brokerage=35.0), "brokerage"),                            # fee components
    (dict(side="SELL", entry_side="SELL", exit_side="BUY"), "move"),  # direction flipped
    (dict(exit_side="BUY"), "sides"),                               # leg mismatch
    (dict(quantity=0), "quantity"),
    (dict(quantity=2.5), "quantity"),
    (dict(quantity=11), "move"),                                    # size disagrees with money
    (dict(exit_time=ist(DAYS[0], 9, 30).isoformat()), "exit before entry"),
    (dict(exit_fill_exact=24_050.5), "unrounded fills disagree"),
])
def test_a_contradictory_trade_is_refused(change, why):
    with pytest.raises(mtm.LedgerError, match=why):
        mtm.fills_from_engine([engine_trade(**change)])


@pytest.mark.parametrize("gross,accepted", [(510.0, True), (500.0, False), (600.0, False)])
def test_the_brief_example_is_checked_by_arithmetic(gross, accepted):
    """friction 10, fees 40, net 460: only gross 510 satisfies the identity.
    The brief's 500 is refused like its 600 — the rule is applied, not the
    example."""
    trade = engine_trade(gross_pnl=gross)
    if accepted:
        mtm.fills_from_engine([trade])
    else:
        with pytest.raises(mtm.LedgerError, match="≠ net"):
            mtm.fills_from_engine([trade])


def test_a_price_is_rebuilt_only_without_evidence_and_is_labelled():
    t = engine_trade(entry_fill_exact=None, exit_fill_exact=None)
    open_fill, close_fill = mtm.fills_from_engine([t])
    assert close_fill.price_basis == mtm.RECONSTRUCTED
    assert open_fill.price_basis == "engine_displayed_fill"
    row = mtm.build([open_fill, close_fill], sessions=SESSIONS, starting_equity=100_000)[0]
    assert row["continuity_ending_equity"] == 100_460.0
    assert mtm.RECONSTRUCTED in row["fill_price_bases"]
    # The fallback does not rescue inconsistent money...
    with pytest.raises(mtm.LedgerError):
        mtm.fills_from_engine([engine_trade(entry_fill_exact=None, exit_fill_exact=None,
                                            gross_pnl=600.0)])
    # ...nor displayed prices that disagree with self-consistent money: the
    # rebuild would otherwise quietly replace the 24060 with 24050.
    with pytest.raises(mtm.LedgerError, match="displayed fills move"):
        mtm.fills_from_engine([engine_trade(entry_fill_exact=None, exit_fill_exact=None,
                                            exit=24_060.0)])


# ===========================================================================
# Pass 2D.1 — permanence, protected access, quality propagation
# ===========================================================================

def test_the_eligibility_predicate_itself_never_admits_a_seen_session():
    """Codex's reproduction, at the predicate: a session after the active
    boundary that the registry has ever seen is never eligible, whatever the
    archive said when the lock was made."""
    f = [d.isoformat() for d in FUTURE[:6]]
    s = registry.State(
        usage={f[0]: [{"seq": 1, "at": "x", "category": registry.SEEN_DEVELOPMENT}]},
        exposed={f[1]: [{"seq": 2, "at": "x", "channel": "manual"}]},
        consumed={f[2]: "old-lock"},
        locks=[{"lock_id": "old", "seen_through_session": ARCHIVE[-1].isoformat(),
                "known_sessions": [f[3]], "target_sessions": 2},
               {"lock_id": "new", "seen_through_session": ARCHIVE[-1].isoformat(),
                "known_sessions": [], "target_sessions": 2}],
        observed={d: {"seq": 9, "at": "x", "quality": "clean"} for d in f})
    assert [s.holdout_eligible(d) for d in f] == [False, False, False, False, True, True]
    assert s.category(f[0]) == registry.SEEN_DEVELOPMENT
    assert s.category(f[1]) == registry.SEEN_CONTAMINATED
    assert s.category(f[2]) == registry.HOLDOUT_CONSUMED
    assert s.category(f[3]) == registry.SEEN_PRE_REGISTRY
    assert s.locked_sessions() == f[4:6]


def test_a_seen_development_session_missing_from_the_archive_stays_seen(db):
    registry.register_usage(db, sessions=[FUTURE[0]], category=registry.SEEN_DEVELOPMENT,
                            study_id="s0", reason="looked at it", code_id=None,
                            dataset_fingerprint=None)
    db.commit()
    created = lock(db, archive=ARCHIVE)             # the archive omits FUTURE[0]
    assert created["seen_through_session"] >= FUTURE[0].isoformat()
    observe(db, FUTURE[:3])
    assert category(db, FUTURE[0]) == registry.SEEN_DEVELOPMENT
    assert FUTURE[0].isoformat() not in registry.state(db)._pool()     # noqa: SLF001


def test_a_pre_registry_session_stays_seen_when_omitted_or_deleted(db):
    store(db, frame(ARCHIVE))
    created = lock(db, archive=ARCHIVE[:2])          # caller passes a partial list
    assert created["seen_through_session"] == ARCHIVE[-1].isoformat()
    assert ARCHIVE[-1].isoformat() in created["known_sessions"]
    db.execute(text("DELETE FROM candles"))          # the archive is later truncated
    db.commit()
    observe(db, ARCHIVE + FUTURE[:1])
    assert category(db, ARCHIVE[-1]) == registry.SEEN_PRE_REGISTRY
    assert category(db, FUTURE[0]) == registry.HOLDOUT_CANDIDATE     # truly new: eligible


def test_contaminated_and_consumed_sessions_stay_ineligible_across_generations(db):
    study(db)
    lock(db, target=2)
    observe(db, FUTURE[:3])
    registry.record_exposure(db, sessions=[FUTURE[0]], channel="manual", detail="peek")
    db.commit()
    s = trials.preregister(db, spec("t-1"))
    db.commit()
    registry.final_evaluation(factory(db), candidate(s), lambda _: {"r": 0})
    consumed = [d for d in FUTURE[:3] if category(db, d) == registry.HOLDOUT_CONSUMED]
    assert len(consumed) == 2

    lock(db, target=2, archive=ARCHIVE)             # new generation, archive omits all
    observe(db, FUTURE[:6])
    assert category(db, FUTURE[0]) == registry.SEEN_CONTAMINATED
    assert all(category(db, d) == registry.HOLDOUT_CONSUMED for d in consumed)
    pool = registry.state(db)._pool()                                   # noqa: SLF001
    assert not {FUTURE[0].isoformat(), *(d.isoformat() for d in consumed)} & set(pool)
    assert pool == [d.isoformat() for d in FUTURE[3:6]]


def protected_setup(db, target=2):
    store(db, frame(ARCHIVE))
    lock(db, target=target)
    store(db, frame(FUTURE[:target]))
    observe(db, FUTURE[:target])
    assert category(db, FUTURE[0]) == registry.HOLDOUT_LOCKED


def test_as_known_withholds_a_locked_session(db):
    protected_setup(db)
    frame_, _ = research.as_known(db, "NIFTY", "5m", datetime.now(UTC))
    assert FUTURE[0].isoformat() not in registry.archive_sessions(frame_)
    assert FUTURE[0].isoformat() in frame_.attrs["holdout"]["withheld_sessions"]
    raw, _ = research.as_known(db, "NIFTY", "5m", datetime.now(UTC),
                               access=trusted(registry.DATA_QUALITY))
    assert FUTURE[0].isoformat() in registry.archive_sessions(raw)


def test_the_repository_itself_withholds_unless_trusted_code_holds_a_grant(db):
    from app.data import repository

    protected_setup(db)
    chart = repository.load_index_candles(db)        # e.g. the indicator chart
    assert FUTURE[0].isoformat() not in registry.archive_sessions(chart)
    seen = repository.load_index_candles(db, access=trusted())
    assert FUTURE[0].isoformat() in registry.archive_sessions(seen)
    with pytest.raises(ValueError, match="purpose"):
        repository.load_index_candles(db, access="whatever")


def regime_row(day):
    from app.models import MarketRegime

    return MarketRegime(symbol="NIFTY", timeframe="5m",
                        timestamp=ist(day, 15, 25).to_pydatetime(), session_date=day,
                        day_regime="TREND_UP", day_confidence=0.9, day_reasons=["x"],
                        hour_regime="TREND_UP", hour_confidence=0.8, hour_reasons=["y"],
                        features={"day": {"adx": 30}}, engine_version="t",
                        computed_at=datetime.now(UTC))


def test_regimes_of_a_locked_session_are_refused_on_every_path(db, monkeypatch):
    import asyncio

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import stream
    from app.data import regime_store
    from app.db import get_db

    protected_setup(db)
    db.add(regime_row(ARCHIVE[-1]))
    db.add(regime_row(FUTURE[1]))
    db.commit()

    loaded = regime_store.load(db)
    assert [d.isoformat() for d in loaded["session_date"]] == [ARCHIVE[-1].isoformat()]

    latest = regime_store.latest(db)
    assert latest["withheld"] is True and latest["day"] is None and latest["hour"] is None

    app = FastAPI()
    app.include_router(stream.router)
    app.dependency_overrides[get_db] = lambda: db
    body = TestClient(app).get("/market/regime").json()
    assert body["withheld"] is True and "TREND_UP" not in str(body)

    class Held:
        def __enter__(self):
            return db

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(stream, "SessionLocal", Held)
    streamed = asyncio.run(stream._current_regime())                    # noqa: SLF001
    assert streamed["withheld"] is True and "TREND_UP" not in str(streamed)
    assert category(db, FUTURE[1]) == registry.HOLDOUT_LOCKED          # still unseen


def test_option_bars_of_a_locked_session_are_withheld(db):
    from app.models import OptionCandle, OptionContract
    from app.optionbuy import chain

    protected_setup(db)
    contract = OptionContract(underlying="NIFTY", expiry_date=FUTURE[3], strike=24_000.0,
                              option_type="CE", source="test",
                              first_seen=datetime(2026, 6, 1, tzinfo=UTC),
                              last_seen=datetime(2026, 6, 20, tzinfo=UTC))
    db.add(contract)
    db.flush()
    for day in (ARCHIVE[-1], FUTURE[0]):
        db.add(OptionCandle(contract_id=contract.id, timeframe="5m",
                            timestamp=ist(day, 10, 0).to_pydatetime(), open=100.0,
                            high=100.0, low=100.0, close=100.0, bar_kind="snapshot",
                            source="test", session_date=day))
    db.commit()
    store_ = chain.load(db)
    days = {b.session_date for bars in store_._bars.values() for b in bars}  # noqa: SLF001
    assert days == {ARCHIVE[-1]}


def test_a_caller_cannot_weaken_fold_protection(db):
    protected_setup(db)
    table_ = {d.isoformat(): {"quality": "clean"} for d in ARCHIVE + FUTURE[:2]}
    with pytest.raises(registry.HoldoutAccessDenied):
        folds.build(db, table_, n_folds=1, min_train_sessions=2, validation_sessions=1,
                    categories={FUTURE[0].isoformat(): registry.SEEN_DEVELOPMENT})
    with pytest.raises(ValueError, match="registry"):
        folds.build(None, table_, n_folds=1, min_train_sessions=2, validation_sessions=1)


def with_1530(day):
    extra = session_bars(day).iloc[[-1]].copy()
    extra["timestamp"] = [ist(day, 15, 30).tz_convert("UTC")]
    return pd.concat([session_bars(day), extra], ignore_index=True)


def test_an_extra_1530_bar_keeps_the_session_excluded_after_quarantine(db):
    """Row quarantine removes the 15:30 bar; the session still failed the
    grid as it arrived, and every consumer says so."""
    from app.data import clock_grid

    day = DAYS[5]
    raw = pd.concat([frame(DAYS[:5]), with_1530(day), frame(DAYS[6:12])],
                    ignore_index=True)
    clean = certified(raw)
    assert clean.attrs["raw_certification"]["quarantined_rows"] == 1
    assert len(clean[clean["timestamp"].dt.tz_convert(IST).dt.date == day]) == 75

    quality = clock_grid.session_quality(clean)
    assert quality[day.isoformat()]["quality"] == clock_grid.FAULTY_SESSION
    assert "out_of_session" in quality[day.isoformat()]["faults"]

    # folds
    table_ = folds.session_table(clean)
    assert table_[day.isoformat()]["quality"] == "faulty"
    m = folds.build(db, table_, n_folds=1, min_train_sessions=4, validation_sessions=2)
    assert day.isoformat() in m.excluded
    assert all(day.isoformat() not in f.train + f.embargo + f.validation for f in m.folds)

    # MTM: excluded, never a zero-return day
    status = mtm.session_status(clean)
    assert status[day.isoformat()] == mtm.EXCLUDED
    row = next(r for r in mtm.build([], sessions=status, starting_equity=100_000)
               if r["session"] == day.isoformat())
    assert row["status"] == mtm.EXCLUDED and row["session_return"] is None

    # holdout observation, given the already-quarantined frame
    lock(db, archive=DAYS[:5])
    later = FUTURE[3]
    raw_later = pd.concat([with_1530(later), frame(FUTURE[4:5])], ignore_index=True)
    clean_later = certified(raw_later)
    observe_rows = registry.observe_sessions(db, clean_later, as_of=after_close(FUTURE[4]))
    db.commit()
    verdict = {r["session"]: r["quality"] for r in observe_rows}
    assert verdict[later.isoformat()] == "faulty"
    assert category(db, later) == registry.EXCLUDED_DATA_QUALITY
    assert category(db, FUTURE[4]) == registry.HOLDOUT_CANDIDATE


def test_the_research_loader_carries_the_raw_verdict_to_every_consumer(db):
    """End to end through the real loader: stored 15:30 bar → quarantined row
    → the session is faulty for folds and MTM alike."""
    from app.data import clock_grid

    day = ARCHIVE[2]
    store(db, pd.concat([frame(ARCHIVE[:2]), with_1530(day), frame(ARCHIVE[3:])],
                        ignore_index=True))
    candles = research.load_research_candles(db)
    assert not (candles["timestamp"] == ist(day, 15, 30).tz_convert("UTC")).any()
    assert clock_grid.session_quality(candles)[day.isoformat()]["quality"] == "faulty"
    assert folds.session_table(candles)[day.isoformat()]["quality"] == "faulty"
    assert mtm.session_status(candles)[day.isoformat()] == mtm.EXCLUDED


def test_warmup_context_cannot_carry_a_protected_session(db):
    """A protected candidate that sits before a (contaminated, hence seen)
    validation session is withheld from the warmup context too."""
    lock(db, target=5)
    observe(db, FUTURE[:3])
    registry.record_exposure(db, sessions=[FUTURE[2]], channel="manual", detail="peek")
    db.commit()
    assert category(db, FUTURE[0]) == registry.HOLDOUT_CANDIDATE
    fold = folds.Fold(index=1, train=tuple(d.isoformat() for d in ARCHIVE),
                      embargo=(), validation=(FUTURE[2].isoformat(),))
    out = folds.validation_frame(db, frame(ARCHIVE + FUTURE[:3]), fold)
    days = set(registry.archive_sessions(out))
    assert FUTURE[2].isoformat() in days
    assert not {FUTURE[0].isoformat(), FUTURE[1].isoformat()} & days


# ===========================================================================
# Pass 2D.2 — every source through one boundary
# ===========================================================================

class FakeBroker:
    """A broker whose live pull includes whatever sessions it is given."""
    name = "fake"

    def __init__(self, candles):
        self._candles = candles

    def candles(self, symbol, timeframe, days):
        return self._candles.copy()


def broker_load(db, monkeypatch, candles, **payload):
    from app.api import backtest

    monkeypatch.setattr(backtest, "get_broker", lambda: FakeBroker(candles))
    served, block = backtest.load_candles(
        db, backtest.BacktestIn(source="broker", **payload))
    assert block["mode"] == "broker"
    return served


def test_a_locked_session_is_withheld_whatever_the_source(db, monkeypatch):
    """database, broker and in-memory sources all meet the same boundary."""
    from app.api import backtest

    protected_setup(db)                                  # FUTURE[:2] locked, stored
    locked = {d.isoformat() for d in FUTURE[:2]}
    everything = frame(ARCHIVE + FUTURE[:2])

    from_db = research.load_research_candles(db)
    from_broker = broker_load(db, monkeypatch, everything)
    from_memory = protection.protect_research_frame(db, certified(everything),
                                                    source=protection.MEMORY)
    uncertified = protection.protect_research_frame(db, everything, source=protection.MEMORY)
    for served in (from_db, from_broker, from_memory, uncertified):
        assert not set(registry.archive_sessions(served)) & locked
        assert set(served.attrs["holdout"]["withheld_sessions"]) == locked
        assert set(registry.archive_sessions(served)) == {d.isoformat() for d in ARCHIVE}
    for served in (from_db, from_broker, from_memory):
        assert {v["quality"] for v in clock_grid.session_quality(served).values()} \
            == {clock_grid.CLEAN_SESSION}
    # Protection alone never grades: without raw certification, unknown.
    assert {v["quality"] for v in clock_grid.session_quality(uncertified).values()} \
        == {clock_grid.QUALITY_UNKNOWN}
    assert from_broker.attrs["research_boundary"]["source"] == protection.BROKER

    # Every backtest endpoint reads through `load_candles`; the request has
    # no field that could widen access, and an extra one is ignored.
    assert not {"access", "purpose"} & set(backtest.BacktestIn.model_fields)
    spoofed = broker_load(db, monkeypatch, everything, purpose="data_quality")
    assert not set(registry.archive_sessions(spoofed)) & locked


def test_a_purpose_flag_cannot_be_spoofed_into_seeing_the_holdout(db):
    protected_setup(db)
    everything = frame(ARCHIVE + FUTURE[:2])
    for spoof in (registry.DATA_QUALITY, registry.COLLECTION):
        with pytest.raises(registry.HoldoutAccessDenied, match="purpose string"):
            registry.withhold(db, everything, access=spoof)
        with pytest.raises(registry.HoldoutAccessDenied):
            protection.protect_research_frame(db, everything, source="x", access=spoof)
        with pytest.raises(registry.HoldoutAccessDenied):
            research.load_research_candles(db, access=spoof)
        # Strategy code cannot issue itself a grant, or build one.
        with pytest.raises(registry.HoldoutAccessDenied, match="not a trusted"):
            registry.trusted_access(spoof)
        with pytest.raises(registry.HoldoutAccessDenied, match="issued by"):
            registry.AccessGrant(spoof, __name__, object())
    with pytest.raises(registry.HoldoutAccessDenied, match="not an access grant"):
        registry.withhold(db, everything, access=SimpleNamespace(purpose="collection"))
    # The trusted internal paths are named. Only collection and data-quality
    # code may see protected sessions; the two raw loaders may only certify.
    assert registry.TRUSTED_HOLDERS == {
        "app.data.angel_history": {registry.COLLECTION},
        "app.api.data": {registry.DATA_QUALITY},
        "app.data.research": {registry.RAW_CERTIFICATION},
        "app.api.backtest": {registry.RAW_CERTIFICATION}}
    # A grant the trusted path does hold does see the session.
    seen = protection.protect_research_frame(db, everything, source="x", access=trusted())
    assert FUTURE[0].isoformat() in registry.archive_sessions(seen)


def test_a_broker_source_follows_normal_seen_rules(db, monkeypatch):
    """Contaminated sessions are ordinary seen data; with no holdout program
    a genuinely new session is ordinary data too. Only protection withholds."""
    new = FUTURE[5]
    no_lock = broker_load(db, monkeypatch, frame(ARCHIVE + [new]))
    assert new.isoformat() in registry.archive_sessions(no_lock)
    assert registry.state(db).category(new.isoformat()) == registry.SEEN_PRE_REGISTRY

    protected_setup(db)
    registry.record_exposure(db, sessions=[FUTURE[0]], channel="test", detail="shown")
    db.commit()
    assert category(db, FUTURE[0]) == registry.SEEN_CONTAMINATED
    served = broker_load(db, monkeypatch, frame(ARCHIVE + FUTURE[:2]))
    days = set(registry.archive_sessions(served))
    assert FUTURE[0].isoformat() in days                 # seen: accessible
    assert FUTURE[1].isoformat() not in days             # still protected


# ===========================================================================
# Pass 2D.2 — durable session quality
# ===========================================================================

def test_session_quality_survives_every_transformation(db):
    """Codex's reproduction: sessions loaded separately and concatenated
    lost differing attrs, and the excluded count collapsed. The record now
    rides on the rows."""
    day = ARCHIVE[2]
    store(db, pd.concat([frame(ARCHIVE[:2]), with_1530(day), frame(ARCHIVE[3:])],
                        ignore_index=True))
    pieces = [research.load_research_candles(db, start=d, end=d) for d in ARCHIVE]
    joined = pd.concat(pieces, ignore_index=True)

    def faulty(candles):
        return {d for d, v in clock_grid.session_quality(candles).items()
                if v["quality"] == clock_grid.FAULTY_SESSION}

    days = joined["timestamp"].dt.tz_convert(IST).dt.date
    transformed = {
        "concat": joined,
        "copy": joined.copy(),
        "slice": joined.iloc[75:300],
        "filter": joined[joined["close"] > 0],
        "one session": joined[days == day],
        "reindex": joined.set_index("timestamp").sort_index().reset_index(),
        "attrs dropped": joined.copy().pipe(lambda f: setattr(f, "attrs", {}) or f),
    }
    for name, candles in transformed.items():
        assert faulty(candles) == {day.isoformat()}, name
        assert folds.session_table(candles)[day.isoformat()]["quality"] == "faulty", name
        assert mtm.session_status(candles)[day.isoformat()] == mtm.EXCLUDED, name
    assert "out_of_session" in clock_grid.session_quality(joined)[day.isoformat()]["faults"]


def test_missing_quality_provenance_never_means_clean(db):
    raw = pd.concat([frame(DAYS[:5]), with_1530(DAYS[5]), frame(DAYS[6:12])],
                    ignore_index=True)
    stripped, _, _ = clock_grid.quarantine(raw, "5m")           # no record stamped
    stripped.attrs = {}
    quality = clock_grid.session_quality(stripped)
    assert {v["quality"] for v in quality.values()} == {clock_grid.QUALITY_UNKNOWN}
    assert set(mtm.session_status(stripped).values()) == {mtm.QUALITY_UNKNOWN}
    with pytest.raises(ValueError, match="clean sessions"):     # nothing to score
        folds.build(db, folds.session_table(stripped), n_folds=1,
                    min_train_sessions=2, validation_sessions=1)

    # A column dropped, or rows padded in by a reindex, are unknown too.
    stamped = certified(raw)
    bare = stamped[["timestamp", "open", "high", "low", "close", "volume"]]
    verdicts = {d: v["quality"] for d, v in clock_grid.session_quality(bare).items()}
    # The leftover diagnostic report may add a fault; it certifies nothing.
    assert verdicts.pop(DAYS[5].isoformat()) == clock_grid.FAULTY_SESSION
    assert set(verdicts.values()) == {clock_grid.QUALITY_UNKNOWN}
    padded = pd.concat([stamped, session_bars(DAYS[13])], ignore_index=True)
    assert clock_grid.session_quality(padded)[DAYS[13].isoformat()]["quality"] \
        == clock_grid.QUALITY_UNKNOWN
    assert clock_grid.session_quality(padded)[DAYS[0].isoformat()]["quality"] \
        == clock_grid.CLEAN_SESSION

    # A frame that was quarantined before is refused by raw certification.
    already, _, _ = clock_grid.quarantine(raw, "5m")            # keeps its attrs report
    with pytest.raises(ValueError, match="processed frame"):
        certified(already)

    # Holdout observation leaves an unknown session pending, never counted.
    lock(db)
    rows = registry.observe_sessions(db, frame(FUTURE[:2]), as_of=after_close(FUTURE[1]))
    assert rows == [] and category(db, FUTURE[0]) == registry.HOLDOUT_PENDING_QUALITY
    assert registry.state(db).locked_sessions() == []


def test_the_1530_session_stays_excluded_for_every_consumer_after_concat(db):
    """raw failure → quarantine → concat with other sessions → excluded in
    folds, embargo manifest, holdout counting, MTM and readiness."""
    day = FUTURE[1]
    lock(db, target=2)
    parts = [collected(db, frame([FUTURE[0]])), collected(db, with_1530(day)),
             collected(db, frame(FUTURE[2:4]))]
    joined = pd.concat(parts, ignore_index=True)
    assert not (joined["timestamp"] == ist(day, 15, 30).tz_convert("UTC")).any()

    rows = registry.observe_sessions(db, joined, as_of=after_close(FUTURE[3]))
    db.commit()
    assert {r["session"]: r["quality"] for r in rows}[day.isoformat()] == "faulty"
    assert category(db, day) == registry.EXCLUDED_DATA_QUALITY
    assert registry.state(db).locked_sessions() == [FUTURE[0].isoformat(),
                                                    FUTURE[2].isoformat()]

    seen = pd.concat([collected(db, frame(ARCHIVE[:4])), collected(db, with_1530(ARCHIVE[4])),
                      collected(db, frame(ARCHIVE[5:]))], ignore_index=True)
    manifest = folds.build(db, folds.session_table(seen), n_folds=1, min_train_sessions=2,
                           validation_sessions=1)
    assert ARCHIVE[4].isoformat() in manifest.to_dict()["excluded_sessions"]
    assert mtm.session_status(seen)[ARCHIVE[4].isoformat()] == mtm.EXCLUDED
    summary = readiness.summary(db, seen)["session_quality"]
    assert summary["excluded_data_quality"] == 1 and summary["quality_unknown"] == 0


# ===========================================================================
# Pass 2D.2 — clean research equity vs continuity equity
# ===========================================================================

def excluded_then_clean(excluded_pnl):
    """DAYS[0] clean flat, DAYS[1] excluded and earns `excluded_pnl`,
    DAYS[2] clean and earns +100, DAYS[3] clean and loses 50."""
    sessions = {DAYS[0].isoformat(): mtm.CLEAN, DAYS[1].isoformat(): mtm.EXCLUDED,
                DAYS[2].isoformat(): mtm.CLEAN, DAYS[3].isoformat(): mtm.CLEAN}
    fills = [fill("x", "open", DAYS[1], 10, 0, 1_000.0, quantity=1),
             fill("x", "close", DAYS[1], 11, 0, 1_000.0 + excluded_pnl, quantity=1),
             fill("a", "open", DAYS[2], 10, 0, 100.0, quantity=1),
             fill("a", "close", DAYS[2], 11, 0, 200.0, quantity=1),
             fill("b", "open", DAYS[3], 10, 0, 100.0, quantity=1),
             fill("b", "close", DAYS[3], 11, 0, 50.0, quantity=1)]
    return mtm.build(fills, sessions=sessions, starting_equity=100_000)


def test_an_excluded_session_does_not_move_the_clean_equity_base():
    rows = excluded_then_clean(+1_000)
    excluded, nxt = rows[1], rows[2]
    assert excluded["continuity_ending_equity"] == 101_000
    assert excluded["session_return"] is None and excluded["scored"] is False
    assert excluded["clean_ending_equity"] is None
    assert nxt["continuity_starting_equity"] == 101_000
    assert nxt["clean_starting_equity"] == 100_000
    assert nxt["session_return"] == 100 / 100_000 == 0.001      # not 100 / 101_000
    assert nxt["continuity_ending_equity"] == 101_100
    assert nxt["clean_ending_equity"] == 100_100


SCORED_FIELDS = ("session", "status", "scored", "clean_starting_equity",
                 "clean_ending_equity", "session_pnl", "session_return", "drawdown",
                 "realised_pnl", "fees", "trades_closed")


def test_changing_an_excluded_sessions_pnl_changes_no_clean_statistic():
    up, down = excluded_then_clean(+1_000), excluded_then_clean(-5_000)
    assert [{k: r[k] for k in SCORED_FIELDS} for r in mtm.scored(up)] == \
        [{k: r[k] for k in SCORED_FIELDS} for r in mtm.scored(down)]
    assert mtm.clean_statistics(up) == mtm.clean_statistics(down)
    stats = mtm.clean_statistics(up)
    assert (stats["scored_sessions"], stats["positive_sessions"],
            stats["negative_sessions"], stats["flat_sessions"]) == (3, 1, 1, 1)
    assert stats["max_drawdown"] == pytest.approx(100_050 / 100_100 - 1)

    trades = [SimpleNamespace(exit_time=ist(d, 11, 0), r_multiple=r, net_pnl=p)
              for d, r, p in ((DAYS[1], 9.0, 1_000.0), (DAYS[2], 1.0, 100.0),
                              (DAYS[3], -0.5, -50.0))]
    outcomes_up, outcomes_down = (mtm.session_outcomes(x, trades) for x in (up, down))
    assert outcomes_up == outcomes_down
    assert DAYS[1].isoformat() not in {o.session for o in outcomes_up}
    boot = [sample.session_bootstrap(o, seed=7, resamples=200)["intervals"]
            for o in (outcomes_up, outcomes_down)]
    assert boot[0] == boot[1]
    assert sample.adequacy(outcomes_up) == sample.adequacy(outcomes_down)

    bench = [{"session": d.isoformat(), "session_return": 0.0005, "status": "ok"}
             for d in DAYS[:4]]
    assert mtm.compare_to_benchmark(up, bench) == mtm.compare_to_benchmark(down, bench)
    assert mtm.compare_to_benchmark(up, bench)["paired_sessions"] == 3

    # The continuity curve does retain it — it is the economic account.
    assert up[-1]["continuity_ending_equity"] - down[-1]["continuity_ending_equity"] == 6_000


def test_a_clean_session_opening_on_an_excluded_sessions_marks_is_not_scored():
    sessions = {DAYS[0].isoformat(): mtm.EXCLUDED, DAYS[1].isoformat(): mtm.CLEAN}
    fills = [fill("o", "open", DAYS[0], 14, 0, 100.0, quantity=1),
             fill("o", "close", DAYS[1], 10, 0, 120.0, quantity=1)]
    rows = mtm.build(fills, sessions=sessions, starting_equity=100_000,
                     marks=lambda inst, pid, at: mtm.Mark(
                         price=110.0, observed_at=at - pd.Timedelta(minutes=5),
                         available_at=at, basis="test_mark"))
    assert rows[1]["scored"] is False
    assert rows[1]["unscored_reason"] == "opened on marks from an unscored session"
    assert rows[1]["session_return"] is None


# ===========================================================================
# Pass 2D.2 — fee components: absent is not zero
# ===========================================================================

@pytest.mark.parametrize("brokerage,statutory,outcome", [
    (10.0, 30.0, mtm.FEES_RECONCILED),
    (0.0, 0.0, "≠ fees"),
    (None, None, mtm.FEES_TOTAL_ONLY),
    (10.0, None, "partial"),
    (None, 30.0, "partial"),
])
def test_recorded_fee_components_must_reconcile(brokerage, statutory, outcome):
    trade = engine_trade(brokerage=brokerage, statutory_fees=statutory)
    if outcome in (mtm.FEES_RECONCILED, mtm.FEES_TOTAL_ONLY):
        _, close = mtm.fills_from_engine([trade])
        assert close.fee_basis == outcome and close.fees == 40.0
        row = mtm.build(mtm.fills_from_engine([trade]), sessions=SESSIONS,
                        starting_equity=100_000)[0]
        assert row["fee_bases"] == [outcome]
    else:
        with pytest.raises(mtm.LedgerError, match=outcome):
            mtm.fills_from_engine([trade])


def test_an_engine_trade_without_a_split_is_not_a_recorded_zero():
    from app.backtest.engine import Trade, _component_total

    blank = Trade(entry_time="", exit_time="", side="BUY", entry=1, exit=1, quantity=1,
                  stop_loss=0, target=2, pnl=0, r_multiple=0, exit_reason="t",
                  confidence=0)
    assert (blank.brokerage, blank.statutory_fees) == (None, None)
    assert _component_total([blank], "brokerage") is None
    assert _component_total([engine_trade()], "brokerage") == 30.0


# ===========================================================================
# Pass 2D.3 — raw certification is separate; provenance loss is permanent
# ===========================================================================

def launder(certified_frame):
    """Everything research might do to a frame that loses its provenance:
    concat, copy, filter, drop attrs, drop the quality column."""
    out = pd.concat([certified_frame.iloc[:0], certified_frame], ignore_index=True).copy()
    out = out[out["close"] > 0].reset_index(drop=True)
    out.attrs = {}
    return out.drop(columns=[clock_grid.QUALITY_COLUMN])


def verdicts(candles):
    return {d: v["quality"] for d, v in clock_grid.session_quality(candles).items()}


def test_codex_reproduction_a_faulty_session_cannot_be_laundered_clean(db):
    """A. raw 15:30 → FAULTY(out_of_session) → quarantine → concat/copy/filter
    → attrs removed → session_quality removed → re-entry → QUALITY_UNKNOWN."""
    day = DAYS[5]
    raw = pd.concat([frame(DAYS[:5]), with_1530(day), frame(DAYS[6:12])],
                    ignore_index=True)
    stamped = certified(raw)                                     # steps 1-3
    assert verdicts(stamped)[day.isoformat()] == clock_grid.FAULTY_SESSION
    assert "out_of_session" in clock_grid.session_quality(stamped)[day.isoformat()]["faults"]
    assert not (stamped["timestamp"] == ist(day, 15, 30).tz_convert("UTC")).any()

    processed = launder(stamped)                                 # steps 4-6
    reentered = protection.protect_research_frame(db, processed,  # step 7
                                                  source=protection.MEMORY)
    assert set(verdicts(reentered).values()) == {clock_grid.QUALITY_UNKNOWN}
    assert clock_grid.CLEAN_SESSION not in reentered[clock_grid.QUALITY_COLUMN].values
    assert mtm.session_status(reentered)[day.isoformat()] == mtm.QUALITY_UNKNOWN

    # Nor can ordinary code certify it: no grant, a string, or a strategy
    # grant are all refused before any row is looked at.
    for attempt in (None, registry.RAW_CERTIFICATION, "raw", registry.STRATEGY):
        with pytest.raises((registry.HoldoutAccessDenied, ValueError)):   # "raw": unknown
            protection.certify_raw_frame(processed, source="raw", certification=attempt)
    with pytest.raises(registry.HoldoutAccessDenied, match="not a trusted"):
        registry.trusted_access(registry.RAW_CERTIFICATION)
    # And the re-entered frame carries processing marks, so even a raw
    # loader's grant refuses it.
    with pytest.raises(ValueError, match="processed frame"):
        certified(reentered)


def test_trusted_raw_certification_still_grades_fresh_raw_data(db):
    """B/C. Untouched raw observations through a trusted raw path are
    graded from the grid: clean is CLEAN, faulty is FAULTY."""
    stamped = certified(pd.concat([frame(DAYS[:2]), with_1530(DAYS[2])], ignore_index=True))
    quality = clock_grid.session_quality(stamped)
    assert quality[DAYS[0].isoformat()] | {} == {
        "quality": clock_grid.CLEAN_SESSION, "faults": [], "basis": "raw_certification",
        "certified_from_raw": True, "source": protection.MEMORY}
    assert quality[DAYS[2].isoformat()]["quality"] == clock_grid.FAULTY_SESSION
    assert quality[DAYS[2].isoformat()]["certified_from_raw"] is True

    # The database loader is a raw loader: it certifies bars it read itself.
    store(db, pd.concat([frame(ARCHIVE[:2]), with_1530(ARCHIVE[2])], ignore_index=True))
    loaded = clock_grid.session_quality(research.load_research_candles(db))
    assert loaded[ARCHIVE[0].isoformat()]["quality"] == clock_grid.CLEAN_SESSION
    assert loaded[ARCHIVE[0].isoformat()]["source"] == protection.DATABASE
    assert loaded[ARCHIVE[2].isoformat()]["quality"] == clock_grid.FAULTY_SESSION


def test_a_clean_looking_frame_without_evidence_stays_unknown_through_protection(db):
    """D/E. No quality evidence → unknown, and unknown → unknown → unknown."""
    looks_clean = frame(DAYS[:4])                    # every bar exactly on the grid
    once = protection.protect_research_frame(db, looks_clean, source=protection.MEMORY)
    twice = protection.protect_research_frame(db, once, source=protection.MEMORY)
    thrice = protection.protect_research_frame(db, launder(twice), source=protection.BROKER)
    for passed in (once, twice, thrice):
        assert set(verdicts(passed).values()) == {clock_grid.QUALITY_UNKNOWN}
        assert all(not v["certified_from_raw"]
                   for v in clock_grid.session_quality(passed).values())
    # A recorded verdict is carried exactly, never re-derived.
    stamped = certified(frame(DAYS[:2]))
    assert (protection.protect_research_frame(db, stamped, source="x")
            [clock_grid.QUALITY_COLUMN] == stamped[clock_grid.QUALITY_COLUMN]).all()


def test_unknown_sessions_are_excluded_from_every_scoring_consumer(db):
    """F. folds, holdout count, clean MTM, bootstrap source."""
    unknown = protection.protect_research_frame(db, launder(certified(frame(DAYS[:8]))),
                                                source=protection.MEMORY)
    table_ = folds.session_table(unknown)
    assert {v["quality"] for v in table_.values()} == {clock_grid.QUALITY_UNKNOWN}
    with pytest.raises(ValueError, match="clean sessions"):
        folds.build(db, table_, n_folds=1, min_train_sessions=2, validation_sessions=1)

    status = mtm.session_status(unknown)
    fills = [fill("u", "open", DAYS[1], 10, 0, 100.0, quantity=1),
             fill("u", "close", DAYS[1], 11, 0, 150.0, quantity=1)]
    ledger = mtm.build(fills, sessions=status, starting_equity=100_000)
    assert all(r["status"] == mtm.QUALITY_UNKNOWN and r["scored"] is False for r in ledger)
    assert mtm.clean_statistics(ledger)["scored_sessions"] == 0
    trade = SimpleNamespace(exit_time=ist(DAYS[1], 11, 0), r_multiple=0.5, net_pnl=50.0)
    assert mtm.session_outcomes(ledger, [trade]) == []

    lock(db)
    later = protection.protect_research_frame(
        db, launder(certified(frame(FUTURE[:2]))), source=protection.MEMORY,
        access=trusted())
    assert registry.observe_sessions(db, later, as_of=after_close(FUTURE[1])) == []
    assert category(db, FUTURE[0]) == registry.HOLDOUT_PENDING_QUALITY
    assert registry.state(db).locked_sessions() == []
