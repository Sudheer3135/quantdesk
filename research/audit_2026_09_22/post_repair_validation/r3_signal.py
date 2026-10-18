"""Signal/plan level: forming-bar injection, boundary instants, warmup parity."""
import sqlite3, json, numpy as np, pandas as pd, warnings
warnings.filterwarnings("ignore")
from app.analytics import indicators, signal_engine, plan
c=sqlite3.connect('file:research/audit_2026_09_22/snapshot.sqlite?mode=ro',uri=True)
df=pd.read_sql("select timestamp,open,high,low,close,volume from candles where symbol='NIFTY' and timeframe='5m' order by timestamp",c)
df['timestamp']=pd.to_datetime(df.timestamp,utc=True)
rng=np.random.default_rng(11); N=len(df)
def sigkey(s): return (s.action,round(s.confidence,9),s.entry,s.stop_loss,s.target,[ (ch.name,round(ch.score,9),ch.disabled) for ch in s.checks])
def plankey(p): d=p.to_dict(); return json.dumps({'bias':d.get('bias',{}).get('label'),'entry':d.get('entry',{}).get('state')},sort_keys=True)
out={}
# 1) forming-bar injection: append a wild bar that starts at the next slot, decision is 2m40s into it
bad_sig=bad_plan=0; ks=sorted(rng.integers(500,N-1,25).tolist())
for k in ks:
    pre=df.iloc[:k].copy(); last=pre.timestamp.iloc[-1]
    decision=last+pd.Timedelta(minutes=5)+pd.Timedelta(seconds=160)
    base=pre.copy(); base.attrs['decision_time']=decision.isoformat()
    forming=pd.DataFrame({'timestamp':[last+pd.Timedelta(minutes=5)],'open':[pre.close.iloc[-1]],'high':[pre.close.iloc[-1]*1.02],'low':[pre.close.iloc[-1]*0.98],'close':[pre.close.iloc[-1]*1.015],'volume':[1.]})
    inj=pd.concat([pre,forming],ignore_index=True); inj.attrs['decision_time']=decision.isoformat()
    if sigkey(signal_engine.generate(base))!=sigkey(signal_engine.generate(inj)): bad_sig+=1
    # plan.build has no decision_time param of its own; it relies on attrs through folds
    try:
        if plankey(plan.build(base))!=plankey(plan.build(inj)): bad_plan+=1
    except Exception as e: out.setdefault('plan_errors',[]).append(str(e)[:80])
out['forming_bar_injection']={'tested':len(ks),'signal_changed':bad_sig,'plan_changed':bad_plan}
# 2) boundary instants on drop_unclosed
b=df.iloc[:3].copy(); t0=b.timestamp.iloc[-1]
out['boundary']={'at_close_minus_1ms':len(indicators.drop_unclosed(b,'5m',as_of=t0+pd.Timedelta(minutes=5)-pd.Timedelta(milliseconds=1))),
                 'at_close_exact':len(indicators.drop_unclosed(b,'5m',as_of=t0+pd.Timedelta(minutes=5))),'rows':3,
                 'finality_latency_modelled':False}
# 3) warmup parity: 300-bar backtest window vs ~5 trading days (live) vs full history
diff300=diff5d=0; conf=[]
for k in sorted(rng.integers(1500,N,30).tolist()):
    full=df.iloc[:k]; w300=full.iloc[-300:]; w5d=full.iloc[-375:]
    s_full,s300,s5d=(signal_engine.generate(x) for x in (full,w300,w5d))
    if sigkey(s300)!=sigkey(s5d): diff300+=1
    if sigkey(s5d)!=sigkey(s_full): diff5d+=1
    conf.append((round(s300.confidence,3),round(s5d.confidence,3),s300.action,s5d.action))
act=sum(1 for a in conf if a[2]!=a[3])
out['warmup_parity']={'tested':30,'backtest300_vs_live5d_signal_differs':diff300,'action_differs':act,'live5d_vs_fullhistory_differs':diff5d,'examples':[x for x in conf if x[0]!=x[1]][:5]}
print(json.dumps(out,indent=1))
