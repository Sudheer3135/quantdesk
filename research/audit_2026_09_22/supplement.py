import json,sqlite3
from pathlib import Path
import pandas as pd
import numpy as np
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.models import SignalRecord
from app.evaluation.outcomes import evaluate_signal,is_in_session,is_actionable
from app.backtest.feed import HistoricalFeed
from app.backtest.costs import CostModel,SlippageModel
from app.analytics import indicators,timeframes
ROOT=Path(__file__).resolve().parent
engine=create_engine('sqlite:///'+str(ROOT/'snapshot.sqlite'))
c=pd.read_sql_query("select * from candles where symbol='NIFTY' and timeframe='5m' order by timestamp",engine)
c.timestamp=pd.to_datetime(c.timestamp,utc=True)
r=json.loads((ROOT/'signal_outcomes.json').read_text())['outcomes']
def stats(a):
 a=np.asarray(a,dtype=float);w=a[a>0];l=a[a<0]
 return {'n':len(a),'win_rate_pct':float((a>0).mean()*100),'avg_r':float(a.mean()),'profit_factor_r':float(w.sum()/-l.sum()) if len(l) else None}
feed=HistoricalFeed(c[indicators.REQUIRED_COLS]); corrected=[]; noslip=[]
with Session(engine) as db:
 signals=list(db.scalars(select(SignalRecord).order_by(SignalRecord.created_at)))
 for sig in signals:
  t=pd.Timestamp(sig.created_at).tz_localize('UTC')
  if not is_actionable(sig) or not is_in_session(t):continue
  i=int(c.timestamp.searchsorted(t-pd.Timedelta(minutes=5),side='right'))-1
  if i<0 or i>=len(c)-1:continue
  corrected.append(evaluate_signal(feed,i,sig,CostModel(),SlippageModel(),75).to_dict())
  j=int(c.timestamp.searchsorted(t,side='right'))-1
  noslip.append(evaluate_signal(feed,j,sig,CostModel(),SlippageModel(index_pct=0),75).to_dict())
 meta={s.id:s for s in signals}
 groups={}
 for row in r:
  sig=meta[row['signal_id']]; context=sig.context or {};plan=sig.plan or {}
  for kind,val in [('regime',(plan.get('entry') or {}).get('regime_day') or 'unknown'),('vix', 'missing' if context.get('india_vix') is None else 'below15' if context['india_vix']<15 else '15to20' if context['india_vix']<=20 else 'above20'),('risk',(sig.risk or {}).get('state','unknown'))]:
   groups.setdefault(kind,{}).setdefault(val,[]).append(row['r_multiple'])
# Daily-block bootstrap preserves within-session dependence; not portfolio drawdown simulation.
days={}
for row in r:days.setdefault(str(pd.Timestamp(row['entry_time']).tz_convert('Asia/Kolkata').date()),[]).append(row['r_multiple'])
blocks=list(days.values());rng=np.random.default_rng(20260922)
means=[np.concatenate([blocks[i] for i in rng.integers(0,len(blocks),len(blocks))]).mean() for _ in range(2000)]
events=[]
for row in r:
 events.extend([(row['entry_time'],1),(row['exit_time'],-1)])
active=peak=0
for _,delta in sorted(events): active+=delta;peak=max(peak,active)
# Missing bar changes timeframes from clock bins to count bins.
x=c[indicators.REQUIRED_COLS].iloc[:6].copy(); missing=x.drop(x.index[1]);fold=timeframes.fifteen_minute(missing)
out={'alignment_sensitivity_NOT_reconstructed_truth':stats([o['r_multiple'] for o in corrected if o['resolved']]),'zero_slippage_existing_alignment':stats([o['r_multiple'] for o in noslip if o['resolved']]),'groupings':{k:{g:stats(v) for g,v in gs.items()} for k,gs in groups.items()},'signal_days':len(days),'signal_days_first':min(days),'signal_days_last':max(days),'mean_signals_per_active_day':len(r)/len(days),'max_signals_in_day':max(map(len,blocks)),'max_overlapping_outcomes':peak,'daily_block_bootstrap_mean_r_95pct':np.quantile(means,[.025,.975]).tolist(),'bootstrap_warning':'conditional on flawed outcome alignment; not strategy validation','session_close_timestamp_rows':int((c.timestamp.dt.tz_convert('Asia/Kolkata').dt.strftime('%H:%M')=='15:30').sum()),'volume_values':c.volume.value_counts().head(10).to_dict(),'paper_rejections':pd.read_sql_query('select code,count(*) as n from paper_decisions group by code',engine).to_dict('records'),'journal':pd.read_sql_query('select status,side,quantity,pnl from trades',engine).to_dict('records'),'missing_bar_fold_example':{'input_start_times':missing.timestamp.astype(str).tolist(),'emitted_bars':fold.to_dict('records'),'issue':'first 3 records span 20 minutes after one missing 5-minute bar'}}
(ROOT/'supplement.json').write_text(json.dumps(out,indent=2,default=str));print(json.dumps(out,indent=2,default=str))
