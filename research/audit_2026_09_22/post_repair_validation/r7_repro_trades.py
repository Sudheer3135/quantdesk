import sqlite3, json, hashlib, pandas as pd, warnings; warnings.filterwarnings("ignore")
from app.backtest import engine
from app.analytics import signal_engine
from app.risk.manager import RiskConfig
c=sqlite3.connect('file:research/audit_2026_09_22/snapshot.sqlite?mode=ro',uri=True)
df=pd.read_sql("select timestamp,open,high,low,close,volume from candles where symbol='NIFTY' and timeframe='5m' order by timestamp",c)
df['timestamp']=pd.to_datetime(df.timestamp,utc=True); df=df.iloc[-1500:].reset_index(drop=True)
# how many non-HOLD signals exist at all?
acts={}
for i in range(300,1500,10):
    s=signal_engine.generate(df.iloc[:i]); acts[s.action]=acts.get(s.action,0)+1
hs=[]
for _ in range(2):
    d=engine.run(df,risk_config=RiskConfig(capital=100000,lot_size=1)).to_dict()
    hs.append((hashlib.sha256(json.dumps(d,sort_keys=True,default=str).encode()).hexdigest(),len(d['trades']),d['dataset']['positions']['entries'],d['dataset']['positions']['closed_positions']))
print(json.dumps({'sampled_signal_actions':acts,'runs':hs,'identical':hs[0][0]==hs[1][0]},indent=1))
