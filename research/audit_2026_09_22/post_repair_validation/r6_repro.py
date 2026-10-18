import sqlite3, json, hashlib, pandas as pd, warnings, subprocess; warnings.filterwarnings("ignore")
from app.backtest import engine
c=sqlite3.connect('file:research/audit_2026_09_22/snapshot.sqlite?mode=ro',uri=True)
df=pd.read_sql("select timestamp,open,high,low,close,volume from candles where symbol='NIFTY' and timeframe='5m' order by timestamp",c)
df['timestamp']=pd.to_datetime(df.timestamp,utc=True); df=df.iloc[-1500:].reset_index(drop=True)
runs=[]
for _ in range(2):
    d=engine.run(df).to_dict()
    blob=json.dumps(d,sort_keys=True,default=str).encode()
    runs.append((hashlib.sha256(blob).hexdigest(),d))
r=runs[0][1]['dataset']['reproducibility']
print(json.dumps({'run1_sha':runs[0][0],'run2_sha':runs[1][0],'identical':runs[0][0]==runs[1][0],
 'trades':len(runs[0][1]['trades']),
 'provenance_keys':sorted(r.keys()),'code_sha256':r['code_sha256'][:16],'config_sha256':r['config_sha256'][:16],'data_sha256':r['data_sha256'][:16],
 'records_git_commit':'git' in json.dumps(r).lower(),'records_dirty_flag':'dirty' in json.dumps(r).lower(),'strategy_version_field':[k for k in r if 'version' in k]},indent=1))
