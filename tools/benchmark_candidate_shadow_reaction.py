"""Source-paired Candidate bundle computation and actual worker wake latency.

The policies in the computation fixture are real. The worker fixture uses the
deterministic test policy to isolate scheduling; neither fixture dispatches HA.
"""
import argparse
from contextlib import ExitStack
import copy
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def one(source, passes=32, worker=False):
    with tempfile.TemporaryDirectory() as scratch, ExitStack() as stack:
        previous_data = os.environ.get('ADAPTIVE_AI_DATA')
        stack.callback(lambda: os.environ.pop('ADAPTIVE_AI_DATA', None) if previous_data is None
                       else os.environ.__setitem__('ADAPTIVE_AI_DATA', previous_data))
        os.environ['ADAPTIVE_AI_DATA'] = scratch
        sys.path[:0] = [str(source / 'adaptive_ai/src'), str(source / 'tests'), str(source / 'tools')]
        import test_candidate_shadow_runtime as tests
        import agent_candidate_shadow_runtime as runtime
        f = tests.CandidateShadowRuntimeTests(methodName='runTest')
        f.setUp()
        stack.callback(f.tearDown)
        _, generation = f._g1()
        f.store.update_agent(f.root['id'], {'mode': 'paused'})
        f.manager.drain_candidate_shadow_events(force=True)
        if worker:
            f.manager.poll_seconds = 3.0  # Shipped Pi idle cadence.
            f.manager._all_rows = lambda: []  # Isolate runtime from build lifecycle.
            done = threading.Event()
            original = runtime.TELEMETRY.observe
            reaction = []

            def observed(name, value, *args, **kwargs):
                original(name, value, *args, **kwargs)
                if name == 'candidate_event_to_decision':
                    reaction.append(value)
                    done.set()

            stack.enter_context(patch.object(runtime.TELEMETRY, 'observe', observed))
            f.engine.last_event_received = time.perf_counter()
            f.engine.last_event_monotonic = time.monotonic()
            f.engine.on_state_changed({'entity_id': 'binary_sensor.presence',
                                      'new_state': f._states(presence='off')['binary_sensor.presence']})
            thread = threading.Thread(target=f.manager._worker_loop, daemon=True)
            started = time.perf_counter()
            thread.start()
            try:
                assert done.wait(8), 'Candidate worker did not observe event'
                elapsed = (time.perf_counter() - started) * 1000
            finally:
                f.manager.stop()
                thread.join(5)
                assert not thread.is_alive()
            latest = f.manager.candidate_latest_runtime(generation['generation_id'])
            assert latest['desired'] == 1.0
            f.executor.service.assert_not_called()
            return dict(event_to_candidate_ms=reaction[-1], worker_elapsed_ms=elapsed,
                        desired=latest['desired'], ha_calls=0, idle_poll_seconds=3,
                        coalescing_and_realtime_quiet_preserved=True)

        import benchmark_shadow_context as context
        import context_tournament_promotion as promotion
        for name in ('_choose_schema_after_promotion', '_migrate_schema'):
            stack.enter_context(patch.object(promotion, name, getattr(promotion, name)))
        from storage import Store
        from policy import MultiHorizonPolicy
        from observation_contract import _feature_observation
        import observation_contract
        stack.enter_context(context.training_contract())
        stack.enter_context(patch('time.time', return_value=context.STAMP))
        model_fixture = context.Fixture(Store(Path(scratch) / 'models.db'))
        stack.callback(model_fixture.service.store.stop_runtime_cache)
        states = model_fixture.states
        states['light.shadow'] = context.state('light.shadow', 'off')
        registry = dict(model_fixture.home.entities)
        registry['light.shadow'] = dict(area_id='room_0')
        model_fixture.home.configure(states, entities=registry)
        f.engine.state_map = states
        f.engine.temporal_history = model_fixture.temporal
        # Same target semantics and overlapping schemas; each owns separate heads.
        generations = []
        source_model = model_fixture.live.serialize()
        source_model['model_revision'] = 'candidate-source'
        from policy_backend import serialize_backend_model
        source_model = serialize_backend_model(source_model, policy_backend=MultiHorizonPolicy.BACKEND,
                                               backend_version=MultiHorizonPolicy.VERSION)
        for i in range(4):
            aid = f'candidate-{i}'
            agent = dict(f.root, id=aid, mode='shadow')
            policy = MultiHorizonPolicy(agent, states, registry, set(), model=source_model,
                                        context_engine=model_fixture.home)
            f.engine.models[aid] = policy
            generations.append(dict(generation, generation_id=f'g{i}', agent_id=aid))
        stack.enter_context(patch.object(runtime, '_shadow_generations', return_value=generations))
        f.manager.invalidate_candidate_shadow_cache()
        # Return the same causal temporal object on both source versions.
        from shared_inference_context import shared_inference_temporal
        temporal = shared_inference_temporal(model_fixture.temporal, context.STAMP,
                                             home_provider=model_fixture.home)
        f.manager.candidate_shadow_temporal = lambda: temporal
        f.manager.after_live_process(f.root, states)
        times, rows = [], []
        original_features = MultiHorizonPolicy.features

        def features(policy, *args, **kwargs):
            result = original_features(policy, *args, **kwargs)
            rows.append(copy.deepcopy(result))
            return result

        with patch.object(observation_contract, '_feature_observation', wraps=_feature_observation) as descriptors, \
             patch.object(MultiHorizonPolicy, 'features', features):
            for _ in range(passes):
                # A new frame per observation; nothing survives to the next event.
                temporal = shared_inference_temporal(model_fixture.temporal, context.STAMP,
                                                     home_provider=model_fixture.home)
                started = time.perf_counter()
                result = f.manager.after_live_process(f.root, states)
                times.append((time.perf_counter() - started) * 1000)
                assert len(result['results']) == 4
                rows.append([(gid, x['desired'], x['confidence']) for gid, x in sorted(result['results'].items())])
            computations = descriptors.call_count
        f.executor.service.assert_not_called()
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return dict(passes=passes, candidates=4, median_ms=statistics.median(times), total_ms=sum(times),
                    feature_descriptor_computations=computations, features_labels_metadata_predictions_sha256=digest,
                    ha_calls=0)


def paired(baseline, passes, worker):
    series = []
    for i in range(3):
        pair = {}
        for name in (('before', 'after') if i % 2 == 0 else ('after', 'before')):
            output = subprocess.check_output([sys.executable, str(Path(__file__).resolve()), '--one',
                '--source-root', str(baseline if name == 'before' else ROOT), '--passes', str(passes)]
                + (['--worker'] if worker else []), text=True)
            pair[name] = json.loads(output.strip().splitlines()[-1])
        key = 'event_to_candidate_ms' if worker else 'median_ms'
        if not worker:
            assert pair['before']['features_labels_metadata_predictions_sha256'] == pair['after']['features_labels_metadata_predictions_sha256']
        else:
            assert pair['before']['desired'] == pair['after']['desired']
        pair['speedup'] = pair['before'][key] / pair['after'][key]
        series.append(pair)
    return dict(synthetic=True, independent_sources=True, worker_scheduling_fixture=worker,
                baseline=json.loads((baseline / 'adaptive_ai/BUILD_INFO.json').read_text())['version'],
                series=series, median_speedup=statistics.median(x['speedup'] for x in series),
                limit='Host Candidate Shadow only. Worker uses deterministic policy; bundle uses four real policies. '
                      'No HA ingress/network, physical actuation/ACK or Raspberry Pi result.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--one', action='store_true')
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--baseline-root', type=Path)
    parser.add_argument('--passes', type=int, default=32)
    args = parser.parse_args()
    if args.passes < 1 or (not args.one and args.baseline_root is None):
        parser.error('positive passes and baseline-root required except --one')
    print(json.dumps(one(args.source_root.resolve(), args.passes, args.worker) if args.one else
                     paired(args.baseline_root.resolve(), args.passes, args.worker), indent=None if args.one else 2))
