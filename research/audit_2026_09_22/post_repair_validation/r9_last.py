import json, sqlite3, pandas as pd, warnings; warnings.filterwarnings("ignore")
from app.analytics import indicators, signal_engine
from app.backtest import engine
from app.risk.manager import RiskConfig
out={}
# TC-1: snapshot off-grid 15:30 bars survive the 5m pipeline?
c=sqlite3.connect('file:research/audit_2026_09_22/snapshot.sqlite?mode=ro',uri=True)
df=pd.read_sql("select timestamp,open,high,low,close,volume from candles where symbol='NIFTY' and timeframe='5m' order by timestamp",c)
df['timestamp']=pd.to_datetime(df.timestamp,utc=True)
loc=df.timestamp.dt.tz_convert('Asia/Kolkata')
out['snapshot_1530_rows']=int(((loc.dt.hour==15)&(loc.dt.minute==30)).sum())
kept=indicators.drop_outside_session(indicators.validate(df))
kl=kept.timestamp.dt.tz_convert('Asia/Kolkata')
out['1530_rows_after_session_filter']=int(((kl.dt.hour==15)&(kl.dt.minute==30)).sum())
days=kl.dt.date; counts=kept.groupby(days).size()
out['sessions_not_75']=int((counts!=75).sum())
# PL-6: two losses day 1 -> blocked; day 2 -> allowed again
d1=pd.date_range('2026-06-01 09:15',periods=75,freq='5min',tz='Asia/Kolkata'); d2=pd.date_range('2026-06-02 09:15',periods=75,freq='5min',tz='Asia/Kolkata')
f=pd.DataFrame(dict(timestamp=d1.append(d2).tz_convert('UTC'),open=100.,high=100.05,low=99.95,close=100.,volume=100.))
for i in list(range(62,72))+list(range(80,84)): f.loc[i,'low']=98.5
sig=lambda fr: signal_engine.Signal('NIFTY','5m',fr.timestamp.iloc[-1].isoformat(),'BUY',.8,100.,entry=100.,stop_loss=99.,target=102.)
d=engine.run(f,signal_fn=sig,warmup=60,risk_config=RiskConfig(capital=100000,lot_size=1),slippage_pct=0,cost_per_round_trip=0).to_dict()
ist=lambda x: pd.Timestamp(x).tz_convert('Asia/Kolkata').strftime('%m-%d %H:%M')
out['daily_limit_and_reset']=[(t['exit_reason'],ist(t['entry_time'])) for t in d['trades']]
print(json.dumps(out,indent=1))
