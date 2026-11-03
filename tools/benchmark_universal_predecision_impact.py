"""Interleaved PR #360 collector-on A/A versus U05b.4b.1, HOST ONLY."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-source',type=Path,required=True)
    parser.add_argument('--current-source',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();results=[]
    args.output.parent.mkdir(parents=True,exist_ok=True)
    for name,root,enabled in [('pr360-on-A',args.baseline_source,True),('current-off',args.current_source,False),
                              ('current-on-A',args.current_source,True),('pr360-on-B',args.baseline_source,True),
                              ('current-on-B',args.current_source,True)]:
        command=[sys.executable,str(root/'tools/benchmark_runtime_scaling.py'),'--agents','3','--events','120',
                 '--warmup','20','--entities','1000']+(['--collector'] if enabled else [])
        result=subprocess.run(command,text=True,capture_output=True,timeout=240)
        log=args.output.parent/(args.output.stem+'-'+name+'.log')
        log.write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
        if result.returncode:raise RuntimeError(name+': '+result.stderr[-3000:])
        row=json.loads(next(line[len('SCALING_JSON='):] for line in result.stdout.splitlines() if line.startswith('SCALING_JSON=')))
        values=sorted(row.get('telemetry_raw_ms',{}).get('event_to_intent',[]))
        results.append(dict(name=name,input_sha256=row['input_sha256'],decisions_sha256=row['decisions_sha256'],
            ha_calls=row['ha_calls'],pipeline=row['event_to_pipeline_return'],
            event_to_intent={f'p{p}_ms':values[int(p/100*(len(values)-1))] if values else None for p in (50,95,99)},
            event_to_intent_count=len(values),process_cpu_ms=row['process_cpu_ms'],wall_ms=row['wall_ms'],
            rss_mb=row['rss_mb'],peak_rss_mb=row['peak_rss_mb'],sqlite=row['storage_runtime'],collector=row.get('collector'),
            training_eligible=0,qualification='production affected_sensor_ids remains unknown'))
    report=dict(scope='WSL/Linux host composed Engine ingress; no Pi4/socket/physical acceptance',
        baseline='PR #360 / 72487b945d521c8564928cea770de73023b82136',
        sensors=1000,agents=3,events=120,warmup=20,cases=results,
        equal_inputs=len({r['input_sha256'] for r in results})==1,
        equal_decisions=len({r['decisions_sha256'] for r in results})==1,
        zero_ha_calls=all(r['ha_calls']==0 for r in results),
        limits='A/A and slower cases retained; 120 events do not establish production p99. '
               'Idle-only preparation may not run during busy measured windows; see separate cardinality worker measurements.')
    args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))
    if not (report['equal_inputs'] and report['equal_decisions'] and report['zero_ha_calls']):
        raise RuntimeError('runtime parity failed; complete report retained')


if __name__=='__main__':main()
