"""Measurement regressions: clocks, numerical definitions, fills and reconciliation."""
from datetime import date, datetime, timezone, timedelta
from dataclasses import replace
import numpy as np
import pandas as pd
import pytest
from app.analytics import indicators, timeframes, signal_engine
from app.backtest import engine, option_engine
from app.backtest.feed import HistoricalFeed, LookaheadError
from app.backtest.costs import SlippageModel, CostModel, buy_fill, sell_fill
from app.backtest.measurement import PositionLedger, State
from app.risk.manager import RiskConfig
from app.optionbuy.chain import ChainStore, ContractKey, ContractMeta, OptionBar


def bars(n=70, volume=100.):
    stamps=pd.date_range('2026-06-01 09:15',periods=n,freq='5min',tz='Asia/Kolkata').tz_convert('UTC')
    return pd.DataFrame(dict(timestamp=stamps,open=100.,high=100.1,low=99.9,close=100.,volume=volume))


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
    frame=bars(3)
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
