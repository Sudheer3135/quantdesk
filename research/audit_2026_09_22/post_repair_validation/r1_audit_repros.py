"""Re-run each original audit reproduction; report whether the ORIGINAL BUG still reproduces."""
import pandas as pd, traceback
from app.analytics import indicators, signal_engine
from app.backtest.engine import run
from app.backtest.costs import SlippageModel, buy_fill, sell_fill
from app.risk.manager import RiskConfig
res={}
def probe(name,fn):
    try: res[name]=fn()
    except Exception as e: res[name]=f"ERROR {type(e).__name__}: {e}"
# 1 forming bar retained by default (no as_of)
f=pd.DataFrame({'timestamp':[pd.Timestamp.now(tz='UTC').floor('5min')],'open':[100.],'high':[101.],'low':[99.],'close':[100.],'volume':[1.]})
probe('forming_bar_retained_default_call', lambda: len(indicators.drop_unclosed(f,'5m'))==1)
# 2 rvol future availability
x=pd.concat([f.assign(timestamp=pd.Timestamp('2026-09-01 03:45',tz='UTC')+pd.Timedelta(minutes=5*i),volume=1.) for i in range(30)],ignore_index=True)
x.loc[29,'volume']=2.
def rv():
    a=indicators.relative_volume(x.iloc[:20]).iloc[-1]; b=indicators.relative_volume(x).iloc[19]
    return {'prefix':None if pd.isna(a) else float(a),'full':None if pd.isna(b) else float(b),'bug_reproduces': (pd.isna(a)) != (pd.isna(b)) or (not pd.isna(a) and a!=b)}
probe('rvol_prefix_vs_full', rv)
# 3 LTP+half spread
probe('buy_bid100_ask102_ltp95', lambda: buy_fill(95.,SlippageModel(),bid=100.,ask=102.).to_dict())
probe('sell_bid100_ask102_ltp110', lambda: sell_fill(110.,SlippageModel(),bid=100.,ask=102.).to_dict())
# 4 final-bar entry
y=pd.concat([f.assign(timestamp=pd.Timestamp('2026-09-01 03:45',tz='UTC')+pd.Timedelta(minutes=5*i),volume=1.) for i in range(62)],ignore_index=True)
def sig(frame):
    return signal_engine.Signal(symbol='NIFTY',timeframe='5m',timestamp=frame.iloc[-1].timestamp.isoformat(),action='BUY',confidence=.8,price=100.,entry=100.,stop_loss=99.,target=102.)
def fb():
    r=run(y,signal_fn=sig,risk_config=RiskConfig(capital=100000,lot_size=1),slippage_pct=0,cost_per_round_trip=0)
    return {'trades':len(r.trades),'exit_reasons':[t.exit_reason for t in r.trades]}
probe('final_bar_entry', fb)
import json; print(json.dumps(res,indent=1,default=str))
