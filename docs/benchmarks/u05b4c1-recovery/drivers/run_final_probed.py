from pathlib import Path
import sys,json
base=Path('/home/mpuchara/homemind-u05b4c1-recovery')
sys.path.insert(0,str(base/'validated2/tools'))
from benchmark_runtime_recovery import run_case
out=base/'results/post-hardening-probed';out.mkdir(parents=True,exist_ok=True)
rows=[]
for repeat in (1,2):
 row=run_case(base/'validated2',out,'current-final-1000-200-'+str(repeat),1000,200,40,8,True)
 rows.append(row)
 (out/'summary.json').write_text(json.dumps(dict(scope='Final8036 HOST largest-case original30 acceptance after session-open fault hardening',cases=rows),indent=2)+'\n')
 print(json.dumps({k:row.get(k) for k in ('name','status','event_to_intent','warmup','collector')}),flush=True)
