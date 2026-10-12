"""Compare .187 and current source: repeated causal statistics + real worker sequence.

Synthetic inputs, no HA dispatch; --baseline-root must point to the .187 checkout.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def measure(source):
    sys.path[:0] = [str(source), str(source/'tests'), str(source/'tools'), str(source/'adaptive_ai/src')]
    import support
    from tools.benchmark_causal_statistics import fixture
    from causal_home_statistics import CausalHomeStatistics
    from history import HistoryManager
    from replay import SQLiteTemporalTracker
    from storage import Store
    conn, context, start, end = fixture(1600)
    manager = object.__new__(HistoryManager)
    manager.engine = SimpleNamespace(context=context)
    manager.training_phase_timings = {}
    timings, outputs, rebuilt = [], [], 0
    try:
        for chunk in range(6):
            started = time.perf_counter()
            if hasattr(manager, '_training_home_statistics'):
                index = manager._training_home_statistics(conn, start, end, True)
                reused = manager.training_phase_timings['causal_home_statistics_reused']
            else:
                index = CausalHomeStatistics(conn, context, start, end)
                reused = False
            timings.append(time.perf_counter()-started)
            rebuilt += not reused
            outputs.append(index.statistics_at(start + (chunk+1)*(end-start)/7))
        component = dict(seconds=sum(timings), chunk_seconds=timings, full_builds=rebuilt,
                         source_rows=index.rows, max_bytes=index.max_bytes,
                         output_sha256=hashlib.sha256(repr(outputs).encode()).hexdigest())
    finally:
        conn.close()
    import benchmark_feature_snapshot_reuse as fixture_worker
    from training_process import descriptor_checksum
    with tempfile.TemporaryDirectory(prefix='hm-chunk-parity-') as temp:
        root = Path(temp)
        _, aid, base, stop, states, registry = fixture_worker.seed_database(root)
        original = fixture_worker.worker_job
        def job(*args, **kwargs):
            path = original(*args, **kwargs)
            data = json.loads(path.read_text())
            data['options']['tiny_mlp_supervised_training_enabled'] = False
            data['sequence_start_ts'] = data['start_ts']
            data['sequence_target_end_ts'] = data['end_ts']
            chunks = []
            for i in range(3):
                lo = data['start_ts'] + i*(data['end_ts']-data['start_ts'])/3
                hi = data['start_ts'] + (i+1)*(data['end_ts']-data['start_ts'])/3
                chunk_kwargs = dict(data['train_kwargs'], accumulate_benchmark=i>0,
                                    long_memory_recent_start_ts=data['start_ts'],
                                    long_memory_reference_end_ts=data['end_ts'],
                                    finalize_neural=i==2)
                chunks.append(dict(index=i, start_ts=lo, end_ts=hi,
                                   train_kwargs=chunk_kwargs, checkpoint_cursor_ts=hi))
            data['sequence_chunks'] = chunks
            data['checksum'] = descriptor_checksum(data)
            path.write_text(json.dumps(data))
            return path
        fixture_worker.worker_job = job
        result = fixture_worker.run_worker(root, aid, base, stop, states, registry, True)
        model = fixture_worker.semantic_ridge_model(result['store'], aid)
        experiences = fixture_worker.semantic_experiences(result['store'], aid)
        worker = dict(wall_seconds=result['wall_seconds'],
                      model_sha256=hashlib.sha256(json.dumps(model,sort_keys=True).encode()).hexdigest(),
                      experience_sha256=hashlib.sha256(json.dumps(experiences,sort_keys=True).encode()).hexdigest(),
                      experiences=len(experiences), chunks=result['result']['sequence_chunks_completed'],
                      phase_timings=[row['training_phase_timings'] for row in result['result']['sequence_chunk_reports']])
    with tempfile.TemporaryDirectory(prefix='hm-fallback-range-') as temp:
        store = Store(Path(temp)/'archive.db')
        base = 1700000000.0
        rows = [('sensor.fallback', base+i, str(i%100), {}, None, 'live', None) for i in range(60000)]
        for offset in range(0,len(rows),128):
            store.archive_batch(rows[offset:offset+128])
        store.archive_batch([('sensor.fallback', base+5, 'late', {}, None, 'live', base+59972)])
        tracker = SQLiteTemporalTracker(store,['sensor.fallback'],
            SimpleNamespace(options={},relevant_entities=lambda:[]),base,base+60000)
        digest = hashlib.sha256()
        steps = [0]
        def tick():
            steps[0] += 100
            return 0
        tracker.conn.set_progress_handler(tick,100)
        started = time.perf_counter()
        try:
            for q in range(128):
                lo = base+59970+q%10
                found = tracker._sqlite_base_interval_rows(['sensor.fallback'],lo,lo+3)
                digest.update(json.dumps(found,sort_keys=True).encode())
            fallback = dict(seconds=time.perf_counter()-started,vm_steps=steps[0],
                            queries=128,old_prefix_rows=60000,result_sha256=digest.hexdigest())
        finally:
            tracker.conn.set_progress_handler(None,0)
            tracker.close()
    return dict(component=component, worker=worker, fallback=fallback)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-root',type=Path)
    parser.add_argument('--source-root',type=Path)
    parser.add_argument('--output',type=Path)
    args = parser.parse_args()
    if args.source_root:
        print(json.dumps(measure(args.source_root.resolve())))
        return
    if not args.baseline_root:
        parser.error('--baseline-root is required')
    reports = {}
    for label, source in [('baseline',args.baseline_root),('optimized',ROOT)]:
        reports[label] = json.loads(subprocess.check_output([
            sys.executable,str(Path(__file__).resolve()),'--source-root',str(source)],text=True,timeout=240))
    a,b = reports['baseline'],reports['optimized']
    parity = (a['component']['output_sha256']==b['component']['output_sha256']
              and a['fallback']['result_sha256']==b['fallback']['result_sha256']
              and all(a['worker'][key]==b['worker'][key] for key in
                      ['model_sha256','experience_sha256','experiences','chunks']))
    report = dict(scope='Synthetic component and real isolated three-chunk worker; no Pi end-to-end timing',
                  baseline='0.14.187',optimized='0.14.188',pass_parity=parity,reports=reports,
                  component_speedup=a['component']['seconds']/b['component']['seconds'],
                  fallback_speedup=a['fallback']['seconds']/b['fallback']['seconds'])
    if args.output:
        args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(dict(parity=parity,component_speedup=report['component_speedup'],
                         fallback_speedup=report['fallback_speedup'],
                         baseline_builds=a['component']['full_builds'],optimized_builds=b['component']['full_builds'])))
    raise SystemExit(0 if parity else 1)


if __name__=='__main__':
    main()
