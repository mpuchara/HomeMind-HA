"""Serial, immutable host comparisons for U05b.4c.1; never HA/Pi acceptance.

Uses the unmodified composed workload (8 warmup / 40 steps by default), real
Candidate/Shadow, concurrent training and Correct/Undo. Each run is a fresh
process. Failed 30-second observations remain failures; no deadline rescue.
Background work counts and A/A spread remain visible rather than normalized
away. No transport experiment is silently substituted for the runtime.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys


def quantiles(values):
    values=sorted(values)
    return dict(count=len(values),**{f'p{p}_ms':values[int((len(values)-1)*p/100)] if values else None
                                   for p in (50,95,99)},max_ms=max(values) if values else None)


def source_identity(root):
    paths=sorted((root/'adaptive_ai/src').rglob('*.py'))
    paths += [root/'tools/benchmark_runtime_scaling.py',root/'tools/runtime_scaling_probe.py']
    rows=[(str(p.relative_to(root)).replace('\\','/'),hashlib.sha256(p.read_bytes().replace(b'\r\n',b'\n')).hexdigest()) for p in paths]
    return dict(sha256=hashlib.sha256(json.dumps(rows,separators=(',',':')).encode()).hexdigest(),files=rows)


def summarize(raw):
    probe=(raw.get('probe') or {}).get('operations',{})
    return dict(
        event_to_intent=quantiles(raw.get('telemetry_raw_ms',{}).get('event_to_intent',[])),
        prediction=quantiles([s[0] for s in probe.get('model_inference',{}).get('samples',[])]),
        warmup=quantiles(raw.get('warmup_pipeline_samples_ms',[])),
        pipeline=raw.get('event_to_pipeline_return'),
        evaluated_roots=probe.get('composed_agent',{}).get('count'),
        sql_api_calls=sum(v['count'] for k,v in probe.items() if k.startswith('sqlite_execute.')),
        store_lock_wait=quantiles([s[0] for s in probe.get('store_lock_wait',{}).get('samples',[])]),
        executor_queue_wait=quantiles([s[0] for s in probe.get('target_executor_queue',{}).get('samples',[])]),
        process_cpu_ms=raw.get('process_cpu_ms'),wall_ms=raw.get('wall_ms'),
        rss_mb=raw.get('rss_mb'),peak_rss_mb=raw.get('peak_rss_mb'),
        collector=raw.get('collector'),native_rows=raw.get('collector_native_rows'),
        background_work=raw.get('background_work'),candidate_count=raw.get('candidate_count'),
        ha_calls=raw.get('ha_calls'),input_sha256=raw.get('input_sha256'),decisions_sha256=raw.get('decisions_sha256'))


def child(root, sensors, devices, events, warmup, enabled):
    """Extra native counts run AFTER the measured workload and collector stop."""
    sys.path[:0]=[str(root/'tools'),str(root/'tests'),str(root/'adaptive_ai/src')]
    # Import through harness first: it initializes the disposable environment.
    # The stop patch is installed lazily by the benchmark Probe, after Store and
    # collector modules have been imported by normal composition.
    import benchmark_runtime_scaling as harness
    from unittest.mock import patch
    counts={}
    base=harness.Probe
    class Inspect(base):
        def install(self, stack, engine, store):
            super().install(stack,engine,store)
            service=getattr(engine,'universal_training_collector',None)
            if service is None:return
            stop=service.stop
            def stopped(*args,**kw):
                result=stop(*args,**kw)
                if result:
                    with store.conn() as c:
                        for name,table in [('observations','feature_observation_events'),
                            ('decisions','universal_collection_decisions'),('masks','universal_collection_masks'),
                            ('gaps','universal_collection_gaps'),('segments','universal_collection_segments')]:
                            counts[name]=c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                        try:
                            groups={'collector':'universal_collection_%','retention':'universal_retention_%',
                                    'native_observations':'feature_observation_events','protection_windows':'feature_window%'}
                            counts['object_page_bytes']={name:c.execute(
                                'SELECT COALESCE(SUM(pgsize),0) FROM dbstat WHERE name IN '
                                '(SELECT name FROM sqlite_master WHERE tbl_name LIKE ?)',(pattern,)).fetchone()[0]
                                for name,pattern in groups.items()}
                        except sqlite3.OperationalError as exc:
                            if 'no such table: dbstat' not in str(exc):raise
                            counts['object_page_bytes']=None
                            counts['object_page_limitation']='SQLite dbstat unavailable'
                    from telemetry import rss_mb
                    counts['rss_after_stop_mb']=rss_mb()
                    counts['sqlite_files']={p.name:p.stat().st_size for p in Path(store.path).parent.glob(Path(store.path).name+'*') if p.is_file()}
                    # This composed fixture has no approved affected declarations
                    # or reconciled historical topology; no training yield claim.
                    counts['qualified_training_samples']=None
                    counts['qualification_status']='not measured: composed fixture has no qualified topology/declarations'
                return result
            stack.enter_context(patch.object(service,'stop',stopped))
    with patch.object(harness,'Probe',Inspect):
        raw=harness.run(root,devices,events,entities=sensors+devices,warmup=warmup,probe=True,
                        candidates=True,training=True,correct=True,collector=enabled,sensor_count=sensors)
    raw['collector_native_rows']=counts or None
    print('RECOVERY_JSON='+json.dumps(raw,separators=(',',':')))
    return raw


def run_case(root, directory, name, sensors, devices, events=40, warmup=8, enabled=True):
    log=directory/(name+'.log');archive=directory/(name+'.raw.json.gz')
    if log.exists() or archive.exists():raise ValueError('results are immutable: '+name)
    command=[sys.executable,str(Path(__file__).resolve()),'--child','--current-source',str(root),
             '--cases',f'{sensors}:{devices}','--events',str(events),'--warmup',str(warmup)]
    if not enabled:command+=['--collector-off']
    with log.open('w',encoding='utf-8') as stream:
        process=subprocess.Popen(command,stdout=stream,stderr=subprocess.STDOUT,start_new_session=os.name!='nt')
        try:code=process.wait(timeout=900)
        except subprocess.TimeoutExpired:
            if os.name!='nt':os.killpg(process.pid,signal.SIGKILL)
            else:process.kill()
            process.wait();code=124
    raw_log=log.read_bytes();(directory/(name+'.exit')).write_text(str(code)+'\n')
    result=dict(name=name,sensors=sensors,devices=devices,collector_enabled=enabled,exit_code=code,
                observation_timeout_seconds=30,events=events,warmup_steps=warmup,
                log_sha256=hashlib.sha256(raw_log).hexdigest(),source=source_identity(root))
    line=next((s[len('RECOVERY_JSON='):] for s in raw_log.decode('utf-8').splitlines() if s.startswith('RECOVERY_JSON=')),None)
    if code==0 and line:
        encoded=line.encode();raw=json.loads(line)
        with gzip.open(archive,'wb') as stream:stream.write(encoded)
        result.update(status='completed',raw_archive=archive.name,raw_sha256=hashlib.sha256(encoded).hexdigest(),**summarize(raw))
    else:result.update(status='failed',error_tail=raw_log.decode('utf-8')[-6000:])
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline-source',type=Path)
    p.add_argument('--current-source',type=Path,default=Path(__file__).resolve().parents[1])
    p.add_argument('--cases',default='5:1,50:10,200:30,1000:200')
    p.add_argument('--events',type=int,default=40);p.add_argument('--warmup',type=int,default=8)
    p.add_argument('--output',type=Path);p.add_argument('--repeats',type=int,default=2)
    p.add_argument('--child',action='store_true');p.add_argument('--collector-off',action='store_true')
    p.add_argument('--require-no-drops',action='store_true',help='CI correctness gate for a bounded collector-on child')
    a=p.parse_args();cases=[tuple(map(int,item.split(':'))) for item in a.cases.split(',')]
    if not cases or len(cases)>4 or any(not 5<=s<=1000 or not 1<=d<=200 for s,d in cases):p.error('bounded four-scale cases')
    if not 2<=a.events<=100 or not 0<=a.warmup<=20 or not 1<=a.repeats<=3:p.error('bounded workload required')
    if a.child:
        if len(cases)!=1:p.error('one child case required')
        if a.require_no_drops and a.collector_off:p.error('loss gate needs an enabled collector')
        raw=child(a.current_source.resolve(),*cases[0],a.events,a.warmup,not a.collector_off)
        if a.require_no_drops:
            stats=raw['collector'];native=raw['collector_native_rows']
            assert stats['errors']==stats['dropped']==stats['queue']==0,stats
            assert native['decisions']==(a.events+a.warmup)*cases[0][1],native
            assert native['observations']==a.events+a.warmup,native
            assert raw['ha_calls']==0,raw['ha_calls']
        return
    if a.require_no_drops:p.error('loss gate is for --child; matrix retains adverse baseline results')
    if not a.output or not a.baseline_source:p.error('baseline-source and output required')
    if a.output.exists():p.error('immutable output exists')
    a.output.parent.mkdir(parents=True,exist_ok=True);rows=[]
    report=dict(scope='synthetic serial HOST; no HA socket, Recorder, Pi or physical acceptance',
                matched_background_work=False,baseline_reference='b3a67c0a2d264697ac69f5ee16bdd673b843f6d3',cases=rows)
    for sensors,devices in cases:
        for repeat in range(a.repeats):
            for label,root,enabled in [('baseline',a.baseline_source,True),('current',a.current_source,True),('current-off',a.current_source,False)]:
                name=f'{sensors}-{devices}-{label}-{repeat+1}'
                row=run_case(root.resolve(),a.output.parent,name,sensors,devices,a.events,a.warmup,enabled)
                rows.append(row)
                # Checkpoint after every attempt, retaining failed observations.
                a.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
                print(json.dumps({k:row.get(k) for k in ('name','status','event_to_intent','warmup','collector')}),flush=True)


if __name__=='__main__':main()
