"""Alternate independent shipped-runtime processes; retain every measured series.

Example: --baseline-root /path/to/216 --seconds 180 --pairs 5 --candidates --ui
Transport, models and workload are synthetic. This cannot certify physical HA latency.
"""
import argparse
import json
from pathlib import Path
import random
import subprocess
import sys


def percentile(values,p=.95):
    ordered=sorted(values)
    if not ordered:return None
    index=(len(ordered)-1)*p;low=int(index);high=min(low+1,len(ordered)-1)
    return ordered[low]+(ordered[high]-ordered[low])*(index-low)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline-root',required=True,type=Path)
    p.add_argument('--current-root',type=Path,default=Path(__file__).resolve().parents[1])
    p.add_argument('--seconds',type=float,default=180)
    p.add_argument('--pairs',type=int,default=5)
    p.add_argument('--active',type=int,default=3);p.add_argument('--configured',type=int,default=20)
    p.add_argument('--mode',choices=('shadow','control'),default='shadow')
    p.add_argument('--ui',action='store_true');p.add_argument('--trace',action='store_true')
    p.add_argument('--candidates',action='store_true');p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();assert a.pairs>=1 and a.seconds>0
    roots=dict(baseline=a.baseline_root,current=a.current_root)
    report=dict(contract='full_shipped_runtime_alternating_v1',configuration=vars(a).copy(),runs=[])
    report['configuration']={key:str(value) if isinstance(value,Path) else value for key,value in vars(a).items()}
    for pair in range(a.pairs):
        # Reverse the order on alternate pairs to reduce order/temperature bias.
        for kind in (('baseline','current') if pair%2==0 else ('current','baseline')):
            args=[sys.executable,str(Path(__file__).with_name('benchmark_full_runtime.py')),
                '--source-root',str(roots[kind]),'--seconds',str(a.seconds),'--active',str(a.active),
                '--configured',str(a.configured),'--mode',a.mode]
            for name in ('ui','trace','candidates'):
                if getattr(a,name):args.append('--'+name)
            completed=subprocess.run(args,cwd=roots[kind],capture_output=True,text=True,timeout=a.seconds+150)
            if completed.returncode:
                print(completed.stdout[-6000:]+completed.stderr[-6000:],file=sys.stderr)
                raise SystemExit(completed.returncode)
            row=json.loads(completed.stdout.strip().splitlines()[-1])
            row.update(kind=kind,pair=pair)
            report['runs'].append(row)
            a.output.parent.mkdir(parents=True,exist_ok=True)
            a.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
            measured=row['measured_window'].get('event_to_decision',{})
            print(json.dumps(dict(pair=pair,kind=kind,p95=measured.get('p95'),
                count=measured.get('count'),candidate_processed=row['candidate_diagnostics']['processed'])),flush=True)
    summary={}
    for pair in range(a.pairs):
        rows=[r for r in report['runs'] if r['pair']==pair]
        assert rows[0]['input_value_sha256']==rows[1]['input_value_sha256'],'different input streams'
        assert rows[0]['sent_state_events']==rows[1]['sent_state_events']
        assert rows[0]['benchmark_tool_sha256']==rows[1]['benchmark_tool_sha256']
    for metric in ('event_to_decision','event_to_target_start','inference','wrapped_inference','event_to_worker_release'):
        series={kind:[r.get('latency_samples',{}).get(metric,[]) for r in report['runs'] if r['kind']==kind]
            for kind in roots}
        if not all(all(rows) for rows in series.values()):continue
        values={kind:[value for rows in groups for value in rows] for kind,groups in series.items()}
        base=percentile(values['baseline']);current=percentile(values['current'])
        rng=random.Random(217);draws=[]
        # Resample whole paired runs, not correlated observations within a burst.
        for _ in range(1000):
            indices=rng.choices(range(a.pairs),k=a.pairs)
            b=percentile([v for i in indices for v in series['baseline'][i]])
            c=percentile([v for i in indices for v in series['current'][i]])
            if b:draws.append((c/b-1)*100)
        summary[metric]=dict(baseline_p95_ms=base,current_p95_ms=current,
            relative_change_percent=(current/base-1)*100 if base else None,
            paired_run_bootstrap_95_percent=[percentile(draws,.025),percentile(draws,.975)],
            baseline_count=len(values['baseline']),current_count=len(values['current']))
    report.update(summary=summary,
        performance_gate='REQUIRES_REVIEW: this report covers one scenario, not the full acceptance matrix',
        limitations='Host synthetic workloads, local HA transport; no deployed Pi/Ingress/physical ACK claim. '
                    'Completion counts and gaps may differ under asynchronous coalescing; inspect every run.')
    a.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(summary),flush=True)


if __name__=='__main__':main()
