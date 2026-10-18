"""Executable reproductions of audit findings; all synthetic inputs are labeled."""
import json
from pathlib import Path
import pandas as pd
from app.analytics import indicators, signal_engine
from app.backtest.engine import run
from app.backtest.costs import SlippageModel, buy_fill, sell_fill
from app.risk.manager import RiskConfig
ROOT=Path(__file__).resolve().parent
# An aligned, still-forming candle passes the named closed-bar guard.
f=pd.DataFrame({'timestamp':[pd.Timestamp.now(tz='UTC').floor('5min')], 'open':[100.], 'high':[101.], 'low':[99.], 'close':[100.], 'volume':[1.]})
assert len(indicators.drop_unclosed(f,'5m'))==1
# Future variation changes historical rvol availability.
x=pd.concat([f.assign(timestamp=pd.Timestamp('2026-09-01 03:45',tz='UTC')+pd.Timedelta(minutes=5*i),volume=1.) for i in range(30)],ignore_index=True)
x.loc[29,'volume']=2.
assert pd.isna(indicators.relative_volume(x.iloc[:20]).iloc[-1])
assert indicators.relative_volume(x).iloc[19]==1.
# LTP plus half-spread is not an executable ask, unless LTP is the midpoint.
buy=buy_fill(95.,SlippageModel(),bid=100.,ask=102.)
sell=sell_fill(110.,SlippageModel(),bid=100.,ask=102.)
assert buy.filled==96. and sell.filled==109.
# Reserve-one feed does not settle a trade opened in the final candle.
y=pd.concat([f.assign(timestamp=pd.Timestamp('2026-09-01 03:45',tz='UTC')+pd.Timedelta(minutes=5*i),volume=1.) for i in range(62)],ignore_index=True)
calls=[]
def signal(frame):
 calls.append(len(frame))
 return signal_engine.Signal(symbol='NIFTY',timeframe='5m',timestamp=frame.iloc[-1].timestamp.isoformat(),action='BUY',confidence=.8,price=100.,entry=100.,stop_loss=99.,target=102.)
result=run(y,signal_fn=signal,risk_config=RiskConfig(capital=100000,lot_size=1),slippage_pct=0,cost_per_round_trip=0)
assert calls and not result.trades
out={'synthetic_reproductions':{'aligned_forming_bar_retained':True,'future_volume_changes_past_rvol_availability':True,'buy_with_bid100_ask102_ltp95':buy.to_dict(),'sell_with_bid100_ask102_ltp110':sell.to_dict(),'final_bar_entry_missing_from_trade_results':True}}
(ROOT/'reproductions.json').write_text(json.dumps(out,indent=2))
print(json.dumps(out,indent=2))
