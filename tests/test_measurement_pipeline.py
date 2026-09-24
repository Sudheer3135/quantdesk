"""Measurement regressions: clocks, numerical definitions, fills and reconciliation."""
from datetime import date, datetime, timezone, timedelta
from dataclasses import replace
import numpy as np
import pandas as pd
import pytest
from app.analytics import indicators, timeframes, signal_engine
from app.backtest import engine, option_engine
from app.backtest.feed import (FAIL, PASS, UNVERIFIED, CausalityReport,
                               HistoricalFeed, LookaheadError)
from app.backtest.costs import SlippageModel, CostModel, buy_fill, sell_fill
from app.backtest.measurement import PositionLedger, State
from app.risk.manager import RiskConfig
from app.optionbuy.chain import ChainStore, ContractKey, ContractMeta, OptionBar


def bars(n=70, volume=100., provenance=indicators.UNKNOWN):
    """Volume provenance is UNKNOWN unless a test declares otherwise: a
    frame built here has no source vouching for its volume."""
    stamps=pd.date_range('2026-06-01 09:15',periods=n,freq='5min',tz='Asia/Kolkata').tz_convert('UTC')
    f=pd.DataFrame(dict(timestamp=stamps,open=100.,high=100.1,low=99.9,close=100.,volume=volume))
    return indicators.declare_volume(f,provenance)


@pytest.mark.parametrize('minute,count',[(19,0),(20,1),(24,1),(25,2),(29,2),(30,3)])
def test_closed_bar_boundary(minute,count):
    frame=bars(3)
    now=pd.Timestamp(f'2026-06-01 09:{minute}',tz='Asia/Kolkata')
    assert len(indicators.drop_unclosed(frame,'5m',as_of=now))==count


@pytest.mark.parametrize('minute,count',[(20,0),(25,0),(29,0),(30,1)])
def test_htf_not_visible_before_close(minute,count):
    assert len(timeframes.fold(bars(3),3,as_of=pd.Timestamp(f'2026-06-01 09:{minute}',tz='Asia/Kolkata')))==count


def test_missing_piece_never_shifts_clock_buckets():
    frame=bars(6).drop(index=1)
    folded=timeframes.fold(frame,3)
    assert len(folded)==1
    assert folded.timestamp.iloc[0]==bars(6).timestamp.iloc[3]


def test_weighted_vwap_variance_and_session_reset():
    frame=bars(3,provenance=indicators.GENUINE)
    frame[['open','high','low','close']]=np.array([10,20,30])[:,None]*np.ones((1,4))
    frame['volume']=[10,20,30]
    nextday=frame.iloc[:1].copy();nextday.timestamp+=pd.Timedelta(days=1)
    frame=pd.concat([frame,nextday],ignore_index=True)
    vw,up,lo=indicators.vwap_bands(frame)
    assert vw.iloc[2]==pytest.approx(1400/60)
    sigma=np.sqrt((10*100+20*400+30*900)/60-(1400/60)**2)
    assert up.iloc[2]==pytest.approx(vw.iloc[2]+sigma)
    assert lo.iloc[2]==pytest.approx(vw.iloc[2]-sigma)
    assert vw.iloc[3]==10 and up.iloc[3]==10


@pytest.mark.parametrize('volume',[0.,1.,2.])
def test_placeholder_features_are_unavailable(volume):
    f=bars(volume=volume)
    enriched=indicators.enrich(f)
    assert enriched.vwap.isna().all() and enriched.rvol.isna().all()
    assert not indicators.has_real_volume(timeframes.fold(f,3))
    sig=signal_engine.generate(f)
    assert {'vwap','volume'} <= {c.name for c in sig.checks if c.disabled}


def test_volume_prefix_does_not_read_future_availability():
    f=bars();f.loc[:30,'volume']=1.
    whole=indicators.enrich(f)
    prefix=indicators.enrich(f.iloc[:25])
    pd.testing.assert_series_equal(whole.rvol.iloc[:25],prefix.rvol)


def test_signal_uses_closed_prefix_and_records_all_clocks():
    f=bars();f.attrs['decision_time']=(f.timestamp.iloc[60]+pd.Timedelta(minutes=2)).isoformat()
    sig=signal_engine.generate(f)
    timing=sig.context['timing']
    assert pd.Timestamp(timing['bar_open_time'])==f.timestamp.iloc[59]
    assert pd.Timestamp(timing['bar_close_time'])<=pd.Timestamp(timing['signal_time'])
    assert timing['earliest_execution_time']==timing['signal_time']


def test_feed_never_exposes_future_bar():
    f=HistoricalFeed(bars()); f.seek(10)
    with pytest.raises(LookaheadError):f.bar(11)
    assert f.view(10).attrs['decision_time']==f.close_time(10).isoformat()


@pytest.mark.parametrize('ltp',[1.,95.,110.,1000.])
def test_observed_book_fills_at_touch_not_ltp(ltp):
    cfg=SlippageModel(impact_ticks=1)
    assert buy_fill(ltp,cfg,bid=100,ask=102).filled==pytest.approx(102.05)
    assert sell_fill(ltp,cfg,bid=100,ask=102).filled==pytest.approx(99.95)


def test_estimated_spread_is_configurable_and_labelled():
    f=buy_fill(100,SlippageModel(execution_model='conservative_spread',estimated_spread_pct=2,ticks=2))
    assert f.filled==pytest.approx(101.1)
    assert f.basis.startswith('estimated_')


def proposal(frame):
    return signal_engine.Signal('NIFTY','5m',frame.timestamp.iloc[-1].isoformat(),'BUY',.8,100.,entry=100.,stop_loss=99.,target=102.)


def test_terminal_bar_is_closed_once_and_reproducible():
    cfg=RiskConfig(capital=100000,lot_size=1)
    kw=dict(signal_fn=proposal,risk_config=cfg,warmup=60,slippage_pct=0)
    one=engine.run(bars(62),**kw).to_dict(); two=engine.run(bars(62),**kw).to_dict()
    assert one==two
    assert len(one['trades'])==1
    trade=one['trades'][0]
    assert trade['exit_reason']=='end_of_data'
    ledger=one['dataset']['positions']
    assert ledger['entries']==ledger['closed_positions']==1
    assert [e['state'] for e in ledger['events']]==['ENTRY_PENDING','OPEN','EXIT_PENDING','CLOSED']
    assert one['equity_curve'][-1]==pytest.approx(100000+trade['pnl'])
    assert trade['gross_pnl']-trade['execution_friction']-trade['fees']==pytest.approx(trade['pnl'],abs=.02)
    assert cfg.capital==100000
    assert pd.Timestamp(trade['entry_time'])>=pd.Timestamp(trade['timing']['signal_time'])
    assert pd.Timestamp(trade['exit_time'])>pd.Timestamp(trade['entry_time'])


def test_invalid_quantity_and_overlap_are_rejected():
    ledger=PositionLedger();t=pd.Timestamp('2026-06-01',tz='UTC')
    with pytest.raises(ValueError):ledger.enter(t,t,0)
    ledger=PositionLedger();ledger.enter(t,t,1)
    with pytest.raises(ValueError):ledger.enter(t,t,1)


def test_gap_stop_fills_at_adverse_open():
    f=bars(64);f.loc[62,['open','high','low','close']]=[97,98,96,97]
    result=engine.run(f,signal_fn=proposal,warmup=60,risk_config=RiskConfig(capital=100000,lot_size=1),slippage_pct=0)
    assert result.trades[0].exit_reason=='stop_gap'
    assert result.trades[0].exit==97


def test_option_bucket_and_contract_not_visible_early():
    stamp=datetime(2026,6,1,4,tzinfo=timezone.utc)
    key=ContractKey(date(2026,6,9),100.,'CE')
    bar=OptionBar(1,1,key,stamp,10,11,9,10,100,100,0.2,None,None,100,'snapshot','test',1,stamp.date())
    meta=ContractMeta(1,key,75,'test','test',first_seen=stamp+timedelta(minutes=7))
    store=ChainStore({key:[bar]},{key:meta})
    store.seek(stamp+timedelta(minutes=5))
    assert store.bar_at(key,store.now) is None
    assert store.strikes(key.expiry,'CE',store.now)==[]
    store.advance(stamp+timedelta(minutes=7))
    assert store.bar_at(key,store.now) is not None
    with pytest.raises(Exception):store.bar_at(key,store.now+timedelta(seconds=1))


# ---- volume provenance matrix (2A.1) ----------------------------------
#
# Usability is decided by where the numbers came from. Every row states a
# provenance and a shape, and asserts the two are judged independently:
# no shape rescues an unvouched frame, and no shape disqualifies a
# vouched one.

GENUINE, SYNTHETIC = indicators.GENUINE, indicators.SYNTHETIC
UNAVAILABLE, UNKNOWN = indicators.UNAVAILABLE, indicators.UNKNOWN


def volume_case(provenance, values, n=70):
    f = bars(n, provenance=provenance)
    f['volume'] = values(n) if callable(values) else values
    return f


varying = lambda n: np.random.default_rng(9).uniform(500, 5000, n)


@pytest.mark.parametrize('name,provenance,values,usable', [
    ('genuine varying',    GENUINE,     varying,              True),
    ('genuine constant',   GENUINE,     1000.,                True),
    ('genuine zero',       GENUINE,     0.,                   True),
    ('genuine low',        GENUINE,     1.,                   True),
    ('synthetic constant', SYNTHETIC,   1000.,                False),
    ('synthetic varying',  SYNTHETIC,   varying,              False),
    ('unavailable',        UNAVAILABLE, 0.,                   False),
    ('unknown constant',   UNKNOWN,     1000.,                False),
    ('unknown varying',    UNKNOWN,     varying,              False),
])
def test_volume_usability_follows_provenance_not_shape(name, provenance, values, usable):
    f = volume_case(provenance, values)
    assert indicators.has_real_volume(f) is usable, name
    weights = indicators.volume_weights(f)
    assert bool(weights.notna().all()) is usable
    # And the same verdict survives aggregation to a higher timeframe.
    folded = timeframes.fold(f, 3)
    assert indicators.volume_provenance(folded) == provenance
    assert indicators.has_real_volume(folded) is usable, f'{name} after folding'


def test_missing_volume_is_not_a_zero():
    """NaN is absence of an observation; 0.0 is an observation of no trade.

    Only the first invalidates anything: a genuine zero is a zero weight,
    and the session's VWAP carries on across it.
    """
    missing = volume_case(GENUINE, 100.);  missing.loc[40, 'volume'] = np.nan
    assert not indicators.has_real_volume(missing)
    assert indicators.volume_weights(missing).isna().sum() == 1

    isolated = volume_case(GENUINE, 100.); isolated.loc[40, 'volume'] = 0.
    assert indicators.has_real_volume(isolated)
    assert indicators.volume_weights(isolated).notna().all()
    vwap = indicators.vwap_bands(isolated)[0]
    assert vwap.iloc[41:].notna().all()      # the zero poisoned nothing


def test_a_genuine_zero_bar_carries_no_weight():
    f = volume_case(GENUINE, 100.)
    f.loc[:, 'close'] = 100.
    f.loc[40, ['open', 'high', 'low', 'close']] = 500.
    f.loc[40, 'volume'] = 0.
    vwap = indicators.vwap_bands(f)[0]
    # A 500 print with no volume behind it cannot drag a volume-weighted mean.
    assert vwap.iloc[41] == pytest.approx(100.0)


def test_declaring_genuine_needs_a_real_source_not_a_shape():
    """The rule that Pass 2A got wrong, stated directly."""
    varied_but_unvouched = volume_case(UNKNOWN, varying)
    flat_but_vouched = volume_case(GENUINE, 1000.)
    assert not indicators.has_real_volume(varied_but_unvouched)
    assert indicators.has_real_volume(flat_but_vouched)


def test_an_unknown_frame_is_never_promoted_by_its_own_numbers():
    for values in (varying, 1000., 0., 1., 3.):
        assert indicators.volume_provenance(volume_case(UNKNOWN, values)) == UNKNOWN
        assert not indicators.has_real_volume(volume_case(UNKNOWN, values))


def test_a_legacy_synthetic_flag_still_reads_as_synthetic():
    """Frames predating the four-way declaration still carry a bare flag."""
    rng = np.random.default_rng(8)
    f = bars(); f['volume'] = rng.uniform(500, 5000, len(f))
    f.attrs.clear()                      # as a pre-provenance frame arrives
    f.attrs['volume_is_synthetic'] = True
    assert indicators.volume_provenance(f) == SYNTHETIC
    assert not indicators.has_real_volume(f)


@pytest.mark.parametrize('provenance', [GENUINE, SYNTHETIC, UNAVAILABLE, UNKNOWN])
def test_volume_availability_is_decided_without_later_bars(provenance):
    """Whether bar i has usable volume cannot depend on bar i+1.

    Provenance is a property of the frame, so a prefix must produce the
    same weights as the same rows of the whole frame.
    """
    f = volume_case(provenance, 100.)
    f.loc[:30, 'volume'] = 1000.
    whole = indicators.volume_weights(f)
    for cut in (10, 25, 31, 50):
        prefix = indicators.volume_weights(f.iloc[:cut])
        pd.testing.assert_series_equal(whole.iloc[:cut], prefix)


def test_the_causality_check_looks_at_columns_nobody_listed():
    """The detector has to cover the indicator added after it was written."""
    f=bars(200)
    f['close']=np.linspace(100,120,200)
    f['high']=f['close']+.1;f['low']=f['close']-.1;f['open']=f['close']
    import app.backtest.feed as feed_module
    real=feed_module.indicators.enrich
    def enrich_with_a_new_indicator(df):
        out=real(df);out['added_later']=out['close']/out['close'].max();return out
    feed_module.indicators.enrich=enrich_with_a_new_indicator
    try:
        report=HistoricalFeed(f).verify_causality(samples=4)
    finally:
        feed_module.indicators.enrich=real
    # Discovered, compared, and caught — none of which a fixed column list
    # could do for an indicator added after it was written.
    assert 'added_later' in report.columns
    assert 'added_later' in report.leaks
    assert report.status == FAIL and not report.causal


def test_a_column_the_enrichment_cannot_rebuild_is_not_called_clean():
    f=bars(200)
    f['close']=np.linspace(100,120,200)
    f['high']=f['close']+.1;f['low']=f['close']-.1;f['open']=f['close']
    feed=HistoricalFeed(f)
    feed._frame['bolted_on']=feed._frame['close'].cummax()      # noqa: SLF001
    report=feed.verify_causality(samples=4)
    assert 'bolted_on' in report.unverifiable
    assert 'bolted_on' not in report.leaks
    # The public verdict, which is the part that can mislead a caller: a
    # column nobody could check must never read as a clean bill of health.
    assert report.status == UNVERIFIED
    assert report.causal is False
    assert report.to_dict()['status'] == UNVERIFIED


def test_a_forming_bar_changes_no_plan():
    """`plan.build` reads the last bar directly, so it needs the same
    completed-bar filter the signal has."""
    from app.analytics import plan
    f=bars(120)
    f['close']=np.linspace(100,130,120);f['high']=f['close']+.2
    f['low']=f['close']-.2;f['open']=f['close']
    decision=f.timestamp.iloc[-1]+pd.Timedelta(minutes=5)+pd.Timedelta(seconds=160)
    base=f.copy();base.attrs['decision_time']=decision.isoformat()
    forming=f.iloc[[-1]].copy()
    forming['timestamp']=f.timestamp.iloc[-1]+pd.Timedelta(minutes=5)
    forming[['open','high','low','close']]=[130.,160.,129.,158.]
    injected=pd.concat([f,forming],ignore_index=True)
    injected.attrs['decision_time']=decision.isoformat()

    def shape(p):
        d=p.to_dict();return (d['bias']['label'],d['entry']['state'],d['timestamp'])
    assert shape(plan.build(base))==shape(plan.build(injected))


def test_a_clean_run_is_the_only_thing_that_passes():
    """PASS requires work done and all of it clean; the other two do not."""
    from app.brokers.mock import MockBroker
    report = HistoricalFeed(MockBroker(seed=3).candles(days=10)).verify_causality(samples=5)
    assert report.checked > 0 and report.status == PASS and report.causal


@pytest.mark.parametrize('report,expected', [
    (CausalityReport(checked=5, columns=('ema20',)), PASS),
    (CausalityReport(checked=5, leaks=('peek',)), FAIL),
    (CausalityReport(checked=5, unverifiable=('bolted_on',)), UNVERIFIED),
    (CausalityReport(checked=0), UNVERIFIED),
    (CausalityReport(checked=0, leaks=('peek',)), FAIL),
    (CausalityReport(checked=5, leaks=('peek',), unverifiable=('x',)), FAIL),
])
def test_causality_status_never_collapses_unverified_into_pass(report, expected):
    assert report.status == expected
    assert report.causal is (expected == PASS)


def test_nothing_checked_is_not_a_pass():
    """A frame too short to sample reports UNVERIFIED, not a clean result."""
    report = HistoricalFeed(bars(20)).verify_causality()
    assert report.checked == 0
    assert report.status == UNVERIFIED and not report.causal
