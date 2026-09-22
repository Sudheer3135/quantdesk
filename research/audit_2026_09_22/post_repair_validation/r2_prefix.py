"""Independent prefix-invariance probe on the frozen audit snapshot (read-only)."""
import sqlite3, json, hashlib, numpy as np, pandas as pd, warnings
warnings.filterwarnings("ignore")
from app.analytics import indicators, timeframes, regime, structure, smc
SNAP="research/audit_2026_09_22/snapshot.sqlite"
raw=open(SNAP,'rb').read(); snap_sha=hashlib.sha256(raw).hexdigest()
c=sqlite3.connect(f'file:{SNAP}?mode=ro',uri=True)
df=pd.read_sql("select timestamp,open,high,low,close,volume from candles where symbol='NIFTY' and timeframe='5m' order by timestamp",c)
df['timestamp']=pd.to_datetime(df.timestamp,utc=True)
N=len(df); rng=np.random.default_rng(7)
ks=sorted(set(rng.integers(400,N,40).tolist())|{N-1,N})
out={'snapshot_sha256':snap_sha,'rows':N,'ks_tested':len(ks)}
cols=['ema20','ema50','ema100','ema200','atr14','vwap','vwap_upper','vwap_lower','rvol']
def cmp_cols(frame,label):
    full=indicators.enrich(frame); bad={}
    for k in ks:
        pre=indicators.enrich(frame.iloc[:k])
        for col in cols:
            if col not in full: continue
            a,b=full[col].iloc[k-1],pre[col].iloc[-1]
            same=(pd.isna(a) and pd.isna(b)) or (not pd.isna(a) and not pd.isna(b) and abs(a-b)<=1e-9*max(1,abs(a)))
            if not same: bad.setdefault(col,[]).append((int(k),None if pd.isna(a) else float(a),None if pd.isna(b) else float(b)))
    nonnull={col:int(full[col].notna().sum()) for col in cols if col in full}
    out[label]={'mismatch_columns':{k:len(v) for k,v in bad.items()},'examples':{k:v[:2] for k,v in bad.items()},'non_null_counts_full':nonnull}
cmp_cols(df,'indicators_real_snapshot_volume')
syn=df.copy(); syn['volume']=rng.integers(1000,50000,N).astype(float)
cmp_cols(syn,'indicators_synthetic_real_volume')
# HTF: every closed bin in prefix must equal the same bin in full
bad_htf=[]
for n,per in (('15m',3),('1h',12)):
    full=timeframes.fold(df,per)
    for k in ks:
        pre=timeframes.fold(df.iloc[:k],per)
        m=full.merge(pre,on='timestamp',suffixes=('_f','_p'))
        if len(m)!=len(pre): bad_htf.append((n,int(k),'prefix bin missing in full'))
        for col in ('open','high','low','close'):
            if not np.allclose(m[col+'_f'],m[col+'_p']): bad_htf.append((n,int(k),col))
out['htf_fold_closed_bins']={'mismatches':len(bad_htf),'examples':bad_htf[:3]}
# Regime per-row: classify_frame(full) row k-1 vs classify_frame(prefix) last row
full_r=regime.classify_frame(df); bad_r=[]
for k in ks[:15]:
    pre_r=regime.classify_frame(df.iloc[:k])
    for lvl in ('day','hour'):
        a=full_r[lvl].iloc[k-1]; b=pre_r[lvl].iloc[-1]
        ad=a.to_dict() if hasattr(a,'to_dict') else a; bd=b.to_dict() if hasattr(b,'to_dict') else b
        if json.dumps(ad,sort_keys=True,default=str)!=json.dumps(bd,sort_keys=True,default=str):
            bad_r.append((int(k),lvl,(ad or {}).get('label') if isinstance(ad,dict) else str(ad)[:40],(bd or {}).get('label') if isinstance(bd,dict) else str(bd)[:40]))
out['regime_rowwise']={'tested':15*2,'mismatches':len(bad_r),'examples':bad_r[:4]}
# Swings: swings confirmed by k-1 (index <= k-1-lookback) must match
fs=structure.find_swings(df); bad_s=0
for k in ks:
    ps={(s.index,s.kind) for s in structure.find_swings(df.iloc[:k])}
    fset={(s.index,s.kind) for s in fs if s.index<=k-1-3}
    if ps!=fset: bad_s+=1
out['swings_confirmed']={'mismatching_ks':bad_s}
# FVG formation (not fill status)
fe=indicators.enrich(df); ff={(g.index,g.direction,round(g.top,4)) for g in smc.find_fair_value_gaps(fe)}; bad_g=0
for k in ks:
    pe=indicators.enrich(df.iloc[:k])
    pg={(g.index,g.direction,round(g.top,4)) for g in smc.find_fair_value_gaps(pe)}
    if pg!={g for g in ff if g[0]<=k-2}: bad_g+=1
out['fvg_formation']={'mismatching_ks':bad_g}
print(json.dumps(out,indent=1,default=str))
