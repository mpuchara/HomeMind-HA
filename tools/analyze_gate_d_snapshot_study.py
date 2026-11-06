"""Summarize retained Gate D observations without dropping rejected runs."""
import argparse
import json
from pathlib import Path


def profile(path):
    row=json.loads(path.read_text().splitlines()[-1])
    if 'error' in row:
        return row
    phases={name:dict(calls=entry['count'],wall_ms=sum(v[0] for v in entry['samples']),
                     thread_cpu_ms=sum(v[1] for v in entry['samples']))
            for name,entry in row['preparation_profile']['operations'].items()}
    revisions=row['preparation_source_revisions']
    turns=phases.get('prepare_pending_turn',{}).get('calls',0)
    cpu=phases.get('prepare_pending_turn',{}).get('thread_cpu_ms',0)
    return dict(phases=phases, turns=turns, cpu_per_turn_ms=cpu/turns if turns else None,
        materialization=row['preparation_materialization'],source_revisions=revisions,
        consecutive_unchanged=sum(a==b for a,b in zip(revisions,revisions[1:])),
        cache=row['collector'].get('predecision'),
        cache_at_load_end=row['collector_at_load_end'].get('predecision'),
        process_cpu_seconds=row['process_cpu_seconds'],thread_cpu=row['thread_cpu'],
        functions=row.get('preparation_cpu_functions'),rss_after_mb=row['rss_after_mb'])


def main():
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);p.add_argument('output',type=Path)
    args=p.parse_args();root=args.root
    result=dict(profiles={path.stem:profile(path) for path in sorted((root/'profiles').glob('*.jsonl'))})
    for phase in ('matrix','variance'):
        directory=root/phase
        result[phase]=[json.loads(path.read_text()) for path in sorted(directory.glob('*-comparison.json'))]
    validations=[]
    for comparison in result['matrix']:
        for arm in ('base','source'):
            path=root/'matrix'/(comparison['name']+'-'+arm+'.jsonl')
            row=json.loads(path.read_text().splitlines()[-1])
            if 'error' in row:
                validations.append(dict(name=path.stem,pass_screen=False,error=row['error']))
                continue
            written=row['history']['counts'].get('written_decisions',0)
            native=(written==(0 if row['mode']=='off' else row['original_intents']))
            negative=(row['mode'] in ('off','incomplete') or row['durable_configs']>256)
            fail_closed=(not negative or (row['dataset']['eligible']==0 and
                         row['history']['counts'].get('v2_receipts',0)==0))
            unsupported=(row['durable_configs']<=256 or
                         set(row['prepare_error_reasons'])=={'agent bound'})
            validations.append(dict(name=path.stem,native_decisions=written,native_persistence=native,
                fail_closed=fail_closed,unsupported_agent_bound=unsupported,
                pass_screen=native and fail_closed and unsupported))
    result['mandatory_validation']=validations
    supported=[r for r in result['matrix'] if r['name'].startswith(('normal','busy','small')) and 'qualified' in r]
    primary=[r for r in supported if not r['name'].startswith('small')]
    # Original registration: scalability report lines 123-125 specifies the
    # three active preparation workloads; smaller cases remain controls.
    qualified=[sum(r['qualified'][i] for r in primary) for i in (0,1)]
    consumed=[sum(r['consumed'][i] for r in primary) for i in (0,1)]
    improvements=all(r['qualified'][1]>r['qualified'][0] and r['consumed'][1]>r['consumed'][0] for r in supported)
    throughput=(bool(primary) and qualified[1]>=2*qualified[0] and consumed[1]>=2*consumed[0] and improvements)
    result['throughput']=dict(scope='Original three active preparation cases, all repetitions',
        qualified=qualified,consumed=consumed,every_case_including_smaller_controls_improves=improvements,
        all_supported_qualified=[sum(r['qualified'][i] for r in supported) for i in (0,1)],
        all_supported_consumed=[sum(r['consumed'][i] for r in supported) for i in (0,1)],pass_screen=throughput)
    result['gate_d']='PASS' if (len(result['matrix'])==13 and throughput and
        all(r['status']=='PASS' for r in result['matrix']) and
        all(r['pass_screen'] for r in validations)) else 'BLOCKER'
    with args.output.open('x') as stream:json.dump(result,stream,indent=2)
    print(result['gate_d'], result['throughput'])
    for name,row in result['profiles'].items():
        print(name,'turns',row.get('turns'),'CPU/turn',row.get('cpu_per_turn_ms'),'rows',row.get('materialization'))
    for row in result['matrix']:print(row)


if __name__=='__main__':main()
