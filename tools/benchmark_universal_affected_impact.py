"""Exact-cardinality historical baseline A/A comparison with model/SQLite probes.

Both sources use the SAME current harness. Probe overhead is present in every
case; measurements are host observations, never Pi4 acceptance or bare runtime.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def quantiles(rows):
    rows=sorted(rows)
    return {f'p{p}_ms':rows[int((len(rows)-1)*p/100)] if rows else None for p in (50,95,99)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-source',type=Path,required=True)
    parser.add_argument('--baseline-label',default='PR #361 / 1b298554490a629cbc00d024c3879659bbfeb253')
    parser.add_argument('--current-source',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--cases',default='5:1,50:10,200:30,1000:200')
    parser.add_argument('--events',type=int,default=40)
    parser.add_argument('--warmup',type=int,default=8)
    parser.add_argument('--event-timeout',type=float,default=30.)
    parser.add_argument('--candidates',action='store_true')
    parser.add_argument('--training',action='store_true')
    parser.add_argument('--correct',action='store_true')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--log-dir',type=Path,required=True)
    args=parser.parse_args();results=[]
    if not 30.<=args.event_timeout<=120.:parser.error('event-timeout must be 30..120 seconds')
    args.output.parent.mkdir(parents=True,exist_ok=True);args.log_dir.mkdir(parents=True,exist_ok=True)
    for item in args.cases.split(','):
        sensors,devices=map(int,item.split(':'))
        if not 1<=devices<sensors<=1000 or devices>200:parser.error('bounded cardinality required')
        cases=[]
        for name,root,enabled in [('baseline-on-A',args.baseline_source,True),('current-off',args.current_source,False),
                                 ('current-on-A',args.current_source,True),('baseline-on-B',args.baseline_source,True),
                                 ('current-on-B',args.current_source,True)]:
            print(f'RUN {sensors}/{devices} {name}',flush=True)
            command=[sys.executable,str(args.current_source/'tools/benchmark_runtime_scaling.py'),
                '--source-root',str(root.resolve()),'--agents',str(devices),'--sensor-count',str(sensors),
                '--entities',str(sensors+devices),'--events',str(args.events),'--warmup',str(args.warmup),'--probe',
                '--event-timeout',str(args.event_timeout)]
            if enabled:command.append('--collector')
            for flag in ('candidates','training','correct'):
                if getattr(args,flag):command.append('--'+flag)
            result=subprocess.run(command,text=True,capture_output=True,timeout=600)
            (args.log_dir/f'{sensors}-{devices}-{name}.log').write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
            if result.returncode:raise RuntimeError(name+': '+result.stderr[-2000:])
            row=json.loads(next(l[len('SCALING_JSON='):] for l in result.stdout.splitlines() if l.startswith('SCALING_JSON=')))
            raw=row.get('telemetry_raw_ms',{});operations=row.get('probe',{}).get('operations',{})
            probes={key:dict(count=v['count'],dropped=v['dropped'],wall=quantiles([r[0] for r in v['samples']]),
                        thread_cpu=quantiles([r[1] for r in v['samples'] if r[1] is not None]))
                    for key,v in operations.items()}
            assert row['sensor_count']==sensors and row['agents']==devices
            cases.append(dict(name=name,sensors=sensors,devices=devices,input_sha256=row['input_sha256'],
                decisions_sha256=row['decisions_sha256'],event_to_intent=quantiles(raw.get('event_to_intent',[])),
                inference=quantiles(raw.get('inference',[])),event_to_intent_count=len(raw.get('event_to_intent',[])),
                event_action_intents=len(raw.get('event_to_intent',[])),
                model_prediction=probes.get('model_inference'),pipeline=row['event_to_pipeline_return'],
                warmup_pipeline=quantiles(row['warmup_pipeline_samples_ms']),
                observation_timeout_seconds=row['observation_timeout_seconds'],
                cpu_ms=row['process_cpu_ms'],wall_ms=row['wall_ms'],rss_mb=row['rss_mb'],peak_rss_mb=row['peak_rss_mb'],
                sqlite_statements=sum(v['count'] for k,v in operations.items() if k.startswith('sqlite_execute.')),
                probes=probes,sqlite=row['storage_runtime'],collector=row.get('collector'),ha_calls=row['ha_calls'],
                candidate_count=row.get('candidate_count'),candidate_diagnostics=row.get('candidate_diagnostics'),
                background_work=row.get('background_work'),
                training_eligible=0,qualification='no operator declarations in this busy runtime fixture'))
        results.append(dict(sensors=sensors,devices=devices,cases=cases,
            equal_inputs=len({r['input_sha256'] for r in cases})==1,
            equal_decisions=len({r['decisions_sha256'] for r in cases})==1,
            equal_event_action_intents=len({r['event_action_intents'] for r in cases})==1,
            zero_ha_calls=all(r['ha_calls']==0 for r in cases)))
        report=dict(scope='WSL/Linux host composed runtime with equal instrumentation; not Pi4/bare runtime acceptance',
            baseline=args.baseline_label,events=args.events,warmup=args.warmup,
            candidates=args.candidates,concurrent_training=args.training,correct=args.correct,
            observation_timeout_seconds=args.event_timeout,
            results=results,completed=False,
            limits='40 events are not a production p99 certificate. Busy runs measure missing declarations; complete native evidence is measured separately. A/A and adverse cases retained.')
        args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    report['completed']=True
    report['passed']=all(r['equal_inputs'] and r['equal_decisions'] and r['equal_event_action_intents'] and r['zero_ha_calls'] for r in results)
    args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    if not report['passed']:raise RuntimeError('runtime parity failed; report retained')
    print('COMPLETE',flush=True)


if __name__=='__main__':main()
