import json, numpy as np, pandas as pd, warnings; warnings.filterwarnings("ignore")
from app.backtest.costs import CostModel
from app.analytics import indicators, signal_engine
from app.backtest import engine
from app.risk.manager import RiskConfig
out={}
# ---- CA-1 manual: buy 75 @100, sell 75 @120 (win) and buy 75 @120, sell 75 @100 (loss)
def hand(b,s,q=75):
    bt,st=b*q,s*q; t=bt+st
    brk=40; stt=st*0.001; ex=t*0.0003503; sebi=t*0.000001; ipft=t*0.000005; stamp=bt*0.00003
    gst=(brk+ex+sebi+ipft)*0.18
    return round(brk+stt+ex+sebi+ipft+stamp+gst,6)
cm=CostModel()
out['CA1_win']={'hand':hand(100,120),'code':round(cm.round_trip(100,120,75).total,6)}
out['CA2_loss_correct_legs']={'hand_buy120_sell100':hand(120,100),'code_correct_legs':round(cm.round_trip(120,100,75).total,6),
    'code_minmax_as_in_outcomes_and_engine':round(cm.round_trip(min(120,100),max(120,100),75).total,6)}
# ---- FC-1: prices 1,2,3 equal volume -> population sigma 0.816497
t=pd.date_range('2026-06-01 09:15',periods=3,freq='5min',tz='Asia/Kolkata').tz_convert('UTC')
f=pd.DataFrame(dict(timestamp=t,open=[1.,2,3],high=[1.,2,3],low=[1.,2,3],close=[1.,2,3],volume=[100.,100,100]))
vw,up,lo=indicators.vwap_bands(f)
out['FC1_vwap']={'vwap':float(vw.iloc[2]),'sigma':round(float(up.iloc[2]-vw.iloc[2]),6),'expected':0.816497}
f2=f.copy(); f2['volume']=[1.,1,1]
v2,_,_=indicators.vwap_bands(f2); out['FC2_vwap_placeholder_volume']=None if pd.isna(v2.iloc[2]) else float(v2.iloc[2])
f3=f.copy(); f3['volume']=[1000.,1000,1000]
out['FC2_constant_volume_1000_treated_real']=indicators.has_real_volume(f3)
# ---- lifecycle scenarios on engine.run
def bars(n,price=100.):
    s=pd.date_range('2026-06-01 09:15',periods=n,freq='5min',tz='Asia/Kolkata').tz_convert('UTC')
    return pd.DataFrame(dict(timestamp=s,open=price,high=price+.05,low=price-.05,close=price,volume=100.))
def once(at):
    def fn(frame):
        a='BUY' if len(frame)-1 in at or frame.timestamp.iloc[-1] in at else 'HOLD'
        return signal_engine.Signal('NIFTY','5m',frame.timestamp.iloc[-1].isoformat(),a,.8,100.,entry=100.,stop_loss=99. if a=='BUY' else None,target=102. if a=='BUY' else None)
    return fn
def runit(f,fn,**kw):
    r=engine.run(f,signal_fn=fn,warmup=60,risk_config=RiskConfig(capital=100000,lot_size=1),slippage_pct=0,cost_per_round_trip=0,**kw)
    d=r.to_dict(); led=d['dataset']['positions']
    return {'trades':[(t['exit_reason'],t['entry_time'][11:16],t['exit_time'][11:16],t['exit']) for t in d['trades']],
            'entries':led['entries'],'closed':led['closed_positions'],'final_state':led['state']}
sc={}
f=bars(75); f.loc[63,['high']]=103; sc['target']=runit(f,once({61}))
f=bars(75); f.loc[63,['low']]=98.5; sc['stop']=runit(f,once({61}))
f=bars(75); f.loc[63,['open','high','low','close']]=[97,97.5,96.5,97]; sc['stop_gap']=runit(f,once({61}))
sc['time_exit_24bars']=runit(bars(75),once({61}))
f=bars(150); sc['session_end']=runit(f,once({70}))  # entry ~15:05 day1
sc['final_bar']=runit(bars(63),once({61}))
sc['signal_while_open_every_bar']=runit(bars(75),lambda fr: signal_engine.Signal('NIFTY','5m',fr.timestamp.iloc[-1].isoformat(),'BUY',.8,100.,entry=100.,stop_loss=99.,target=102.))
# last bar of session 1 signal -> must not fill at next session open
sc['signal_on_last_bar_of_session']=runit(bars(150),once({74}))
# daily limit: stop-outs every trade
f=bars(75)
for i in range(62,75): f.loc[i,'low']=98.5
sc['daily_limits_repeated_losses']=runit(f,lambda fr: signal_engine.Signal('NIFTY','5m',fr.timestamp.iloc[-1].isoformat(),'BUY',.8,100.,entry=100.,stop_loss=99.,target=102.))
out['lifecycle']=sc
print(json.dumps(out,indent=1,default=str))
