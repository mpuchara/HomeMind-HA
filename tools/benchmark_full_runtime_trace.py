"""Alternate trace OFF/ON against the same shipped source and input values."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from benchmark_full_runtime_pair import percentile


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root',type=Path,default=Path(__file__).resolve().parents[1])
    p.add_argument('--seconds',type=float,default=36)
    p.add_argument('--pairs',type=int,default=5)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();runs=[]
    for pair in range(a.pairs):
        for enabled in ((False,True) if pair%2==0 else (True,False)):
            command=[sys.executable,str(Path(__file__).with_name('benchmark_full_runtime.py')),
                '--source-root',str(a.source_root),'--seconds',str(a.seconds),
                '--active','3','--configured','20','--candidates','--ui']
            if enabled:command.append('--trace')
            result=subprocess.run(command,cwd=a.source_root,capture_output=True,text=True,timeout=a.seconds+150)
            if result.returncode:raise RuntimeError(result.stdout[-4000:]+result.stderr[-4000:])
            row=json.loads(result.stdout.strip().splitlines()[-1]);row['pair']=pair;runs.append(row)
            print(json.dumps(dict(trace=enabled,pair=pair,p95=row['measured_window']['event_to_decision']['p95'])),flush=True)
    assert len({r['input_value_sha256'] for r in runs})==1
    assert len({r['source_sha256'] for r in runs})==1
    assert len({r['benchmark_tool_sha256'] for r in runs})==1
    summary={}
    for enabled in (False,True):
        rows=[r for r in runs if r['trace']==enabled]
        summary['on' if enabled else 'off']=dict(
            p95_ms=percentile([value for row in rows for value in row['latency_samples']['event_to_decision']]),
            cpu_seconds=sum(row['cpu_seconds'] for row in rows),samples=sum(len(row['latency_samples']['event_to_decision']) for row in rows))
    summary['p95_relative_change_percent']=(summary['on']['p95_ms']/summary['off']['p95_ms']-1)*100
    summary['cpu_relative_change_percent']=(summary['on']['cpu_seconds']/summary['off']['cpu_seconds']-1)*100
    a.output.write_text(json.dumps(dict(runs=runs,summary=summary,
        limitation='Same-source host trace overhead experiment; no physical HA latency claim.'),indent=2)+'\n',encoding='utf-8')
    print(json.dumps(summary),flush=True)


if __name__=='__main__':main()
