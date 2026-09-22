import sys, json, numpy as np, warnings; warnings.filterwarnings("ignore")
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.evaluation import outcomes
SNAP=Path(__file__).resolve().parents[1]/'snapshot.sqlite'
eng=create_engine(f'sqlite:///file:{SNAP}?mode=ro&uri=true')
with Session(eng) as db: rep=outcomes.evaluate(db).to_dict()
r=[o for o in rep['outcomes'] if o.get('resolved') and o.get('r_multiple') is not None]
a=np.array([o['r_multiple'] for o in r]); w=a[a>0]; l=a[a<0]
netr=[o.get('r_multiple_net') for o in r if o.get('r_multiple_net') is not None]
print(json.dumps({'evaluator':sys.argv[1],'selected':rep['selection'].get('selected'),'resolved_n':len(a),'win_rate':round(len(w)/len(a)*100,2),'mean_R':round(float(a.mean()),3),
 'sum_R':round(float(a.sum()),3),'PF':round(float(w.sum()/-l.sum()),3),'mean_net_R':round(float(np.mean(netr)),3) if netr else None,
 'selection':rep['selection'],'timing_bases':{b:sum(1 for o in r if o.get('timing_basis')==b) for b in {o.get('timing_basis') for o in r}}},default=str))
