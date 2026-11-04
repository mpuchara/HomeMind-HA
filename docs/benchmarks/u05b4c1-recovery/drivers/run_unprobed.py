from pathlib import Path
import sys,json,subprocess,gzip,hashlib,os,signal
base=Path('/home/mpuchara/homemind-u05b4c1-recovery')
sys.path.insert(0,str(base/'validated2/tools'))
from benchmark_runtime_recovery import source_identity,summarize
out=base/'results/unprobed';out.mkdir(parents=True,exist_ok=True)
rows=[]
for sensors,devices in [(5,1),(50,10),(200,30),(1000,200)]:
 for repeat in (1,2):
  for label,root in [('baseline',base/'base364'),('current',base/'validated2')]:
   deadline=120 if label=='baseline' and sensors==1000 else 30
   name=f'{sensors}-{devices}-{label}-{repeat}';log=out/(name+'.log')
   assert not log.exists(),name
   command=[sys.executable,str(root/'tools/benchmark_runtime_scaling.py'),'--agents',str(devices),'--sensor-count',str(sensors),'--events','40','--warmup','8','--collector','--candidates','--training','--correct','--event-timeout',str(deadline)]
   with log.open('w') as stream:
    proc=subprocess.Popen(command,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
    try:code=proc.wait(900)
    except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait();code=124
   (out/(name+'.exit')).write_text(str(code)+'\n')
   blob=log.read_bytes();line=next((x[13:] for x in blob.decode().splitlines() if x.startswith('SCALING_JSON=')),None)
   row=dict(name=name,sensors=sensors,devices=devices,exit_code=code,source=source_identity(root),probe_enabled=False,observation_timeout_seconds=deadline,diagnostic_only=deadline!=30,log_sha256=hashlib.sha256(blob).hexdigest())
   if code==0 and line:
    raw=json.loads(line);encoded=line.encode()
    with gzip.open(out/(name+'.raw.json.gz'),'wb') as stream:stream.write(encoded)
    row.update(status='completed',raw_sha256=hashlib.sha256(encoded).hexdigest(),**summarize(raw))
    row.update(sql_api_calls=None,prediction=None,store_lock_wait=None,executor_queue_wait=None,evaluated_roots=len(raw['samples']))
   else:row.update(status='failed',error_tail=blob.decode()[-6000:])
   rows.append(row)
   (out/'summary.json').write_text(json.dumps(dict(scope='HOST unprobed sensitivity check. Largest baseline120 is diagnostic only, never original-deadline acceptance; all current30 unchanged.',cases=rows),indent=2)+'\n')
   print(json.dumps({k:row.get(k) for k in ('name','status','event_to_intent','warmup','collector')}),flush=True)
