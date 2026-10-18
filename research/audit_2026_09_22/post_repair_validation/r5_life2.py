import json, pandas as pd, warnings; warnings.filterwarnings("ignore")
from app.analytics import signal_engine
from app.backtest import engine
from app.risk.manager import RiskConfig
def two_days():
    d1=pd.date_range('2026-06-01 09:15',periods=75,freq='5min',tz='Asia/Kolkata')
    d2=pd.date_range('2026-06-02 09:15',periods=75,freq='5min',tz='Asia/Kolkata')
    s=d1.append(d2).tz_convert('UTC')
    return pd.DataFrame(dict(timestamp=s,open=100.,high=100.05,low=99.95,close=100.,volume=100.))
def at(idxs):
    def fn(fr):
        a='BUY' if len(fr)-1 in idxs else 'HOLD'
        return signal_engine.Signal('NIFTY','5m',fr.timestamp.iloc[-1].isoformat(),a,.8,100.,entry=100.,stop_loss=99. if a=='BUY' else None,target=102. if a=='BUY' else None)
    return fn
def run(f,fn):
    d=engine.run(f,signal_fn=fn,warmup=60,risk_config=RiskConfig(capital=100000,lot_size=1),slippage_pct=0,cost_per_round_trip=0).to_dict()
    ist=lambda x: pd.Timestamp(x).tz_convert('Asia/Kolkata').strftime('%m-%d %H:%M')
    return [(t['exit_reason'],ist(t['entry_time']),ist(t['exit_time'])) for t in d['trades']]
out={}
out['time_exit_day2_morning']=run(two_days(),at({80}))           # signal bar 80 = 06-02 09:40
out['signal_last_bar_day1']=run(two_days(),at({74}))            # 06-01 15:25 bar
out['signal_1520_day1']=run(two_days(),at({73}))                # 06-01 15:20 bar -> next open 15:25?
out['signal_1455_day1']=run(two_days(),at({68}))                # late-day entry allowed?
# gap entry: next open gaps 1.5 above proposed entry -> levels shift?
f=two_days(); f.loc[81,['open','high','low','close']]=[101.5,101.6,101.4,101.5]
d=engine.run(f,signal_fn=at({80}),warmup=60,risk_config=RiskConfig(capital=100000,lot_size=1),slippage_pct=0,cost_per_round_trip=0).to_dict()
t=d['trades'][0]; out['gapped_entry']={'proposed':(100,99,102),'filled_entry':t['entry'],'stop_used':t['stop_loss'],'target_used':t['target']}
print(json.dumps(out,indent=1))
