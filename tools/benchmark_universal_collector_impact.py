"""Interleaved host A/A, predecessor and collector-on composed runtime receipts."""
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
    for name,root,enabled in [('base-1',args.baseline_source,False),('current-off',args.current_source,False),
                              ('current-on-1',args.current_source,True),('base-2',args.baseline_source,False),
                              ('current-on-2',args.current_source,True)]:
        command=[sys.executable,str(root/'tools/benchmark_runtime_scaling.py'),'--agents','3','--events','120',
                 '--warmup','20','--entities','1000']+(['--collector'] if enabled else [])
        result=subprocess.run(command,text=True,capture_output=True,timeout=180)
        (args.output.parent/(name+'.log')).write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
        if result.returncode:raise RuntimeError(name+': '+result.stderr[-3000:])
        row=json.loads(next(line[len('SCALING_JSON='):] for line in result.stdout.splitlines() if line.startswith('SCALING_JSON=')))
        assert row['ha_calls']==0,row
        if enabled:assert row['collector']['errors']==0 and not row['collector']['worker_alive'],row['collector']
        metrics=row.get('telemetry_raw_ms',{}).get('event_to_intent',[])
        metrics=sorted(metrics)
        results.append(dict(name=name,enabled=enabled,input_sha256=row['input_sha256'],
            decisions_sha256=row['decisions_sha256'],ha_calls=row['ha_calls'],
            pipeline=row['event_to_pipeline_return'],
            event_to_intent_p95_ms=metrics[int(.95*(len(metrics)-1))] if metrics else None,
            process_cpu_ms=row['process_cpu_ms'],wall_ms=row['wall_ms'],rss_mb=row['rss_mb'],peak_rss_mb=row['peak_rss_mb'],
            collector=row.get('collector')))
    report=dict(scope='synthetic host Engine ingress; no physical HA/Pi/network acceptance',
                baseline='PR #359 / 3c3ccee8354cc74aab8ac871a010883b7054cbf1',
                warmup_events=20,events=120,agents=3,entities=1000,
                equal_inputs=len({r['input_sha256'] for r in results})==1,
                equal_decisions=len({r['decisions_sha256'] for r in results})==1,cases=results,
                interpretation='A/A spread retained; single-host timing is informational, not proof of production equivalence')
    args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
