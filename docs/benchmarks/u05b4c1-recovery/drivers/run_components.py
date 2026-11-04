from pathlib import Path
import sys,json
base=Path('/home/mpuchara/homemind-u05b4c1-recovery')
sys.path.insert(0,str(base/'validated/tools'))
from benchmark_runtime_recovery import run_case
out=base/'results/components';out.mkdir(parents=True,exist_ok=True)
rows=[]
for label in ('runtime-only','collector-only'):
 row=run_case(base/label,out,label+'-1000-200',1000,200,40,8,True)
 rows.append(row)
 (out/'summary.json').write_text(json.dumps(dict(scope='isolated HOST components; original 30s deadline; continuous trainer work is not fixed',cases=rows),indent=2)+'\n')
 print(json.dumps({k:row.get(k) for k in ('name','status','event_to_intent','warmup','collector')}),flush=True)
