"""Validate and summarize preserved U05b.4b.3 HOST A/A observations."""
import hashlib
import json
from pathlib import Path
import platform
import sys


def main():
    root=Path(__file__).resolve().parents[1];folder=root/'docs/benchmarks'
    reports={name:json.loads((folder/f'u05b4b3-components-{name}.json').read_text())
             for name in ('pr362-A','current-A','pr362-B','current-B')}
    runtime=json.loads((folder/'u05b4b3-runtime-aa.json').read_text())
    assert runtime['completed'] and runtime['passed']
    results=[]
    for index,(sensors,devices) in enumerate(((5,1),(50,10),(200,30),(1000,200))):
        row=dict(sensors=sensors,devices=devices,components={})
        for name,report in reports.items():
            case=report['cases'][index];retention=case['retention'];current=name.startswith('current')
            assert (case['sensors'],case['devices'])==(sensors,devices)
            assert retention['samples_initial']==devices and retention['samples_after_required']==0
            assert retention['samples_after_unrelated']==(devices if current else 0)
            assert retention['coverage_version']==(3 if current else 2)
            assert retention['damage_metadata_rows']<=10000
            operations={op['name']:op for op in retention['operations']}
            assert operations['protected_global_hard_cap_stress']['result']['deleted']>=2032
            assert operations['protected_global_hard_cap_stress_no_trace']['result']['deleted']==2048
            sizes=retention['sqlite_object_bytes'] or {}
            metadata=sum(size for key,size in sizes.items() if 'universal_retention_' in key)
            row['components'][name]=dict(
                complete_evidence=case['complete_evidence'],eligible_before=retention['samples_initial'],
                eligible_after_unrelated=retention['samples_after_unrelated'],eligible_after_required=0,
                false_invalidations=retention['false_invalidations_after_unrelated'],
                evidence_prepare=case['prepare'],ram_boundary=case['ram_boundary'],
                prepare_sqlite_trace_statements=case['sqlite_statements'],cpu_seconds=case['cpu_seconds'],
                rss_before_mb=case['rss_before_mb'],rss_after_mb=case['rss_after_mb'],
                database_bytes=case['database_bytes'],database_growth_bytes=case['database_growth_bytes'],
                metadata_page_bytes=metadata,metadata_rows=retention['damage_metadata_rows'],
                operations=operations)
        group=runtime['results'][index]
        assert group['equal_inputs'] and group['equal_decisions'] and group['zero_ha_calls']
        row['runtime']=[dict(name=case['name'],event_to_intent=case['event_to_intent'],
            inference=case['inference'],model_prediction=case['model_prediction'],
            cpu_ms=case['cpu_ms'],rss_mb=case['rss_mb'],peak_rss_mb=case['peak_rss_mb'],
            sqlite_statements=case['sqlite_statements'],collector=case['collector'],
            training_eligible=case['training_eligible']) for case in group['cases']]
        results.append(row)
    import numpy
    report=dict(scope='WSL/Linux HOST; not Pi, HA Recorder or physical acceptance',
        baseline_commit='4755830a0244c6ae33e115e27ce321600806c341',
        runtime_implementation_commit='728ba13091f7a5d3ff0a90230cda9f6bd77c9553',
        offline_membership_and_component_commit='e0ac6f3525005b007f4f7af862b2041c37126453',
        versions=dict(python=sys.version,numpy=numpy.__version__,platform=platform.platform(),machine=platform.machine()),
        harness_sha256={name:hashlib.sha256((root/'tools'/name).read_bytes()).hexdigest() for name in (
            'benchmark_universal_retention.py','benchmark_universal_affected_sensors.py',
            'benchmark_universal_affected_impact.py','benchmark_runtime_scaling.py')},
        runtime_events=runtime['events'],runtime_warmup=runtime['warmup'],results=results,
        limits=['Later hardening changes offline U05a validation, not Engine/WS/Executor/collector/journal execution logic. Its new qualification branch is not called by the missing-declaration runtime fixture.',
                'The last 1000/200 current-on-B runtime case overlapped focused offline regression checks. The first baseline component A run overlapped their tail. Preserve this confound and A/A spread; no isolated latency certificate.',
                'Host noise and tracing overhead are material; no regression-free or Pi throughput claim. Small-event tails are descriptive only.',
                'Component dataset timings include witness verification; they are not an isolated witness microbenchmark.',
                'Trace counters include trigger-expanded callbacks and benchmark COUNT instrumentation. No-trace stress verifies actual deletions.',
                'Original runtime high-load optional drops and all development/adverse/calibration results are preserved alongside final reports.'])
    output=folder/'UNIVERSAL_U05B4B3_SUMMARY.json'
    output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print('Validated four scales, all A/A variants, eligibility and real protected deletions:',output)


if __name__=='__main__':main()
