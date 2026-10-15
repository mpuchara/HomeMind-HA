"""Compare real candidate observers from two independent source checkouts."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def run_one(source, passes=24, contended=False):
    sys.path[:0] = [str(source / 'adaptive_ai/src'), str(source)]
    with tempfile.TemporaryDirectory(prefix='runtime-hot-path-') as directory, ExitStack() as stack:
        os.environ['ADAPTIVE_AI_DATA'] = str(Path(directory) / 'global')
        from tools.benchmark_shadow_context import Fixture, STAMP, training_contract
        from storage import Store
        from policy import MultiHorizonPolicy
        import context_tournament_policy_candidate as candidates
        import context_tournament_promotion as promotion
        import observation_contract
        if (source / 'adaptive_ai/src/runtime_threading.py').exists():
            from runtime_threading import configure_runtime_threads
            configure_runtime_threads({})
        stack.enter_context(patch.object(promotion, '_choose_schema_after_promotion', promotion._choose_schema_after_promotion))
        stack.enter_context(patch.object(promotion, '_migrate_schema', promotion._migrate_schema))
        stack.enter_context(training_contract())
        stack.enter_context(patch('time.time', return_value=STAMP))
        store = Store(Path(directory) / 'fixture.db')
        store.start_wal_keeper()
        stack.callback(store.stop_wal_keeper)
        fixture = Fixture(store, prefix='paired-hot-path')
        # The shipped Engine retains 24 rows. Keep the same source fixture but
        # use its runtime history depth and RAM-first background-write contract.
        from collections import deque
        for eid, samples in fixture.temporal.samples.items():
            fixture.temporal.samples[eid] = deque(list(samples)[-24:], maxlen=24)
        fixture.service.background_persistence_available = lambda: True
        fixture.observe()
        fixture.observe()
        rows, predictions, feature_times = [], [], []
        features, predict = MultiHorizonPolicy.features, MultiHorizonPolicy.predict
        def record_features(policy, *args, **kwargs):
            started = time.perf_counter()
            result = features(policy, *args, **kwargs)
            feature_times.append((time.perf_counter() - started) * 1000)
            rows.append(result)
            return result
        def record_prediction(policy, *args, **kwargs):
            result = predict(policy, *args, **kwargs)
            chosen, confidence, _, horizon, support, novelty = result
            predictions.append((chosen['value'], confidence, horizon, support, novelty))
            return result
        samples, background_units = [], [0]
        stop, ready = threading.Event(), threading.Event()
        def background_cpu():
            ready.set()
            while not stop.is_set():
                sum(i * i for i in range(3000))
                background_units[0] += 1
        worker = threading.Thread(target=background_cpu, daemon=True) if contended else None
        if worker is not None:
            worker.start()
            assert ready.wait(3)
        wall_started, cpu_started = time.perf_counter(), time.process_time()
        try:
            with patch.object(MultiHorizonPolicy, 'features', record_features), \
                 patch.object(MultiHorizonPolicy, 'predict', record_prediction), \
                 patch.object(candidates, '_verify_candidate_checksum', wraps=candidates._verify_candidate_checksum) as checks:
                for _ in range(passes):
                    started = time.perf_counter()
                    result = fixture.observe()
                    samples.append((time.perf_counter() - started) * 1000)
                    assert len(result['predictions']) == 4
                assert checks.call_count == 4 * passes
        finally:
            elapsed, process_cpu = time.perf_counter() - wall_started, time.process_time() - cpu_started
            stop.set()
            if worker is not None:
                worker.join(5)
                assert not worker.is_alive()
        assert len(rows) == len(predictions) == 4 * passes
        # Count actual descriptor reconstructions separately from the timer.
        with patch.object(observation_contract, '_feature_observation', wraps=observation_contract._feature_observation) as computations:
            fixture.observe()
        digest = hashlib.sha256(json.dumps((rows, predictions), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return dict(passes=passes, observation_total_ms=sum(samples), observation_median_ms=statistics.median(samples),
                    feature_total_ms=sum(feature_times), feature_median_ms=statistics.median(feature_times),
                    full_checksum_calls=4 * passes, feature_observation_computations_per_pass=computations.call_count,
                    every_feature_label_meta_and_prediction_sha256=digest,
                    thread_switch_ms=sys.getswitchinterval() * 1000,
                    background_work_units_per_second=background_units[0] / elapsed if worker is not None else None,
                    process_cpu_seconds_per_second=process_cpu / elapsed)


def run(baseline, passes=24, contended=False):
    series = []
    for repeat in range(3):
        pair = {}
        for label in (('previous', 'current') if repeat % 2 == 0 else ('current', 'previous')):
            source = baseline if label == 'previous' else ROOT
            command = [sys.executable, str(Path(__file__).resolve()), '--one',
                       '--source-root', str(source), '--passes', str(passes)]
            if contended:
                command.append('--contended')
            raw = subprocess.check_output(command, text=True)
            pair[label] = json.loads(raw)
        assert pair['previous']['every_feature_label_meta_and_prediction_sha256'] == pair['current']['every_feature_label_meta_and_prediction_sha256']
        series.append(dict(**pair, observation_speedup=pair['previous']['observation_total_ms'] / pair['current']['observation_total_ms'],
                           feature_speedup=pair['previous']['feature_total_ms'] / pair['current']['feature_total_ms']))
    return dict(synthetic=True, actual_independent_source_checkouts=True, baseline=json.loads((baseline / 'adaptive_ai/BUILD_INFO.json').read_text(encoding='utf-8'))['version'],
                challengers=4, sensor_history_rows_per_entity=24, background_cpu_thread=contended,
                ram_first_pool_buffer=True, series=series,
                observation_speedup_median=statistics.median(row['observation_speedup'] for row in series),
                feature_speedup_median=statistics.median(row['feature_speedup'] for row in series),
                limit='Host component fixture with real candidates, full checksum, vectors, metadata, prediction and SQLite. Excludes HA ingress and concurrent checkpoint I/O; not a device latency forecast.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-root', type=Path)
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--passes', type=int, default=24)
    parser.add_argument('--one', action='store_true')
    parser.add_argument('--contended', action='store_true')
    args = parser.parse_args()
    if args.passes < 1 or (not args.one and args.baseline_root is None):
        parser.error('positive passes and baseline-root required (except --one)')
    result = (run_one(args.source_root.resolve(), args.passes, args.contended) if args.one
              else run(args.baseline_root.resolve(), args.passes, args.contended))
    print(json.dumps(result, indent=2))
