"""Audit evidence only. Does not change or optimize the strategy."""
import json, hashlib
from pathlib import Path
import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from app.evaluation import outcomes
from app.backtest.feed import HistoricalFeed
from app.analytics import indicators
ROOT=Path(__file__).resolve().parent
engine=create_engine('sqlite:///'+str(ROOT/'snapshot.sqlite'))
def query(sql):
 return pd.read_sql_query(sql,engine)
def summary(rows):
 a=np.array([r['r_multiple'] for r in rows if r.get('resolved') and r.get('r_multiple') is not None])
 if not len(a): return {'n':0}
 w=a[a>0]; l=a[a<0]
 streaks=[]
 for positive in (True,False):
  best=cur=0
  for x in a:
   cur=cur+1 if (x>0 if positive else x<0) else 0
   best=max(best,cur)
  streaks.append(best)
 return dict(n=len(a),wins=len(w),losses=len(l),flat=int((a==0).sum()),win_rate_pct=len(w)/len(a)*100,
 avg_win_r=float(w.mean()) if len(w) else None,avg_loss_r=float(l.mean()) if len(l) else None,
 expectancy_r=float(a.mean()),total_r=float(a.sum()),profit_factor_r=float(w.sum()/-l.sum()) if len(l) else None,
 max_win_streak=streaks[0],max_loss_streak=streaks[1])
with Session(engine) as db: report=outcomes.evaluate(db).to_dict()
(ROOT/'signal_outcomes.json').write_text(json.dumps(report,indent=2,default=str))
c=query("select * from candles where symbol='NIFTY' and timeframe='5m' order by timestamp")
c['timestamp']=pd.to_datetime(c.timestamp,utc=True)
c['day']=c.timestamp.dt.tz_convert('Asia/Kolkata').dt.date.astype(str)
o=query('select * from option_candles')
s=query('select * from signals order by created_at')
p=query('select * from paper_decisions')
r=report['outcomes']
breakdowns={}
for key,fn in [('direction',lambda r:r['action']),('weekday',lambda r:pd.Timestamp(r['entry_time']).tz_convert('Asia/Kolkata').day_name()),('hour',lambda r:str(pd.Timestamp(r['entry_time']).tz_convert('Asia/Kolkata').hour)),('month',lambda r:r['entry_time'][:7]),('trend',lambda r:r['trend'] or 'unknown')]:
 groups={}
 for row in r: groups.setdefault(fn(row),[]).append(row)
 breakdowns[key]={k:summary(v) for k,v in groups.items()}
# Compare signal price to inferred bar and previous bar, without claiming this reconstructs missing timestamps.
stamps=c.timestamp
alignment=[]
for row in s.to_dict('records'):
 if row['action']=='HOLD':continue
 t=pd.Timestamp(row['created_at']);t=t.tz_localize('UTC') if t.tzinfo is None else t
 i=int(stamps.searchsorted(t,side='right'))-1
 if i<1:continue
 alignment.append({'signal_id':row['id'],'inferred_bar':str(stamps.iloc[i]),'price_gap_inferred':abs(row['price']-c.close.iloc[i]),'price_gap_prior':abs(row['price']-c.close.iloc[i-1]),'inferred_bar_unfinished_at_signal':bool(stamps.iloc[i]+pd.Timedelta(minutes=5)>t)})
# Actual bands versus variance about the current weighted mean.
x=pd.DataFrame({'timestamp':pd.date_range('2026-09-01 03:45',periods=3,freq='5min',tz='UTC'), 'open':[1.,2.,3.],'high':[1.,2.,3.],'low':[1.,2.,3.],'close':[1.,2.,3.],'volume':[1.,1.,1.]})
v,upper,_=indicators.vwap_bands(x)
results={'scope':'stored-signal diagnostics, not executable portfolio returns or option profits',
 'candles':{'n':len(c),'first':str(c.timestamp.min()),'last':str(c.timestamp.max()),'sessions':int(c.day.nunique()),'source':c.source.value_counts().to_dict(),'synthetic_volume_rows':int(c.volume_is_synthetic.sum()),'duplicate_keys':int(c.duplicated(['symbol','timeframe','timestamp']).sum()),'session_counts':c.groupby('day').size().to_dict(),'invalid_ohlc':int(((c.high<c[['open','close','low']].max(axis=1))|(c.low>c[['open','close','high']].min(axis=1))).sum())},
 'options':{'n':len(o),'first':o.timestamp.min(),'last':o.timestamp.max(),'sessions':int(o.session_date.nunique()),'bar_kind':o.bar_kind.value_counts().to_dict(),'source':o.source.value_counts().to_dict(),'bid_ask_rows':int((o.bid.notna()&o.ask.notna()).sum()),'single_sample_rows':int((o.samples==1).sum()),'duplicate_keys':int(o.duplicated(['contract_id','timeframe','timestamp']).sum()),'sessions_by_rows':o.groupby('session_date').size().to_dict()},
 'signal_selection':report['selection'],'metrics':summary(r),'breakdowns':breakdowns,'confidence':report['confidence'],
 'alignment':{'n':len(alignment),'unclosed_inferred_bars':sum(x['inferred_bar_unfinished_at_signal'] for x in alignment),'prior_price_closer':sum(x['price_gap_prior']<x['price_gap_inferred'] for x in alignment)},
 'causality':HistoricalFeed(c[indicators.REQUIRED_COLS]).verify_causality().to_dict(),
 'band_reproduction':{'typical_prices':[1,2,3],'code_sigma':float(upper.iloc[-1]-v.iloc[-1]),'weighted_sigma':float(np.std([1,2,3]))},
 'paper_decision_columns':p.columns.tolist(),
 'evidence_sha256':hashlib.sha256((ROOT/'snapshot.sqlite').read_bytes()).hexdigest()}
(ROOT/'audit_metrics.json').write_text(json.dumps(results,indent=2,default=str))
print(json.dumps({k:v for k,v in results.items() if k not in ['candles','options','breakdowns']},indent=2,default=str))
print('CANDLES',results['candles'])
print('OPTIONS',results['options'])
