"""Independent-source populated Sensor Tournament observer and screening parity."""
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
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def one(source, passes):
    with tempfile.TemporaryDirectory(prefix='screening-reaction-') as directory, ExitStack() as stack:
        os.environ['ADAPTIVE_AI_DATA'] = str(Path(directory) / 'global')
        sys.path[:0] = [str(source / 'adaptive_ai/src'), str(source / 'tools'), str(source / 'tests')]
        import benchmark_shadow_context as fixture
        import context_tournament_promotion as promotion
        from storage import Store
        stack.enter_context(fixture.training_contract())
        for name in ('_choose_schema_after_promotion', '_migrate_schema'):
            stack.enter_context(patch.object(promotion, name, getattr(promotion, name)))
        stamp = [fixture.STAMP]
        stack.enter_context(patch('time.time', side_effect=lambda: stamp[0]))
        samples = [dict(episode=str(i), ts=float(i), label=float(i % 2), value=i / 7.,
                        quality=.98, lag_1=i / 8., lag_3=i / 9., lag_10=i / 10.,
                        trend=i / 17., time_since_edge=.95,
                        interactions={f'primary_{j}': i / (13. + j) for j in range(3)})
                   for i in range(96)]
        result = {}
        started, cpu = time.perf_counter(), time.process_time()
        scores = [fixture.candidates.semantic_predictive_score(samples) for _ in range(96 * passes)]
        result['screening'] = dict(elapsed_ms=(time.perf_counter() - started) * 1000,
                                   cpu_ms=(time.process_time() - cpu) * 1000,
                                   evaluations=len(scores), sha256=digest(scores))
        store = Store(Path(directory) / 'observer.db')
        stack.callback(store.stop_runtime_cache)
        f = fixture.Fixture(store)
        f.service.background_persistence_available = lambda: True
        for i in range(84):
            eid = f'sensor.extra_{i}'
            f.states[eid] = fixture.state(eid, 20 + i, device_class='temperature')
        f.service._eligible_entities = lambda *args: set(f.states) - {f.agent['target_entity']}
        with store.conn() as connection:
            for eid in f.states:
                if eid == f.agent['target_entity']:
                    continue
                connection.execute('INSERT INTO context_tournament_observed_pool'
                    '(agent_id,entity_id,opportunities,available_count,history_json,samples_json,screening_json,updated_ts)'
                    ' VALUES(?,?,?,?,?,?,?,?)', ('paired', eid, 100, 100,
                    json.dumps([[fixture.STAMP-i, 20.] for i in reversed(range(32))]), json.dumps(samples),
                    json.dumps(scores[0]), fixture.STAMP))
        # Warm policy construction, immutable checksum admission and pool reads
        # before measuring the recurring independent target transitions.
        with store.connection_session():
            f.observe()
            # The isolated Fixture omits the outer evaluation-epoch installer.
            # Bind its epoch exactly as benchmark_observation_census does, so
            # target outcomes exercise actual post-prediction policy learning.
            for model in f.service._shadow_models.values():
                model['evaluation_champion_revision'] = model['candidate_source_model_revision']
                model['evaluation_schema_revision'] = f.tournament['schema_revision']
            f.observe()
            times, cpu_times, predictions = [], [], []
            for i in range(passes):
                stamp[0] += 2.
                target = f.agent['target_entity']
                f.states[target] = fixture.state(target, 'on' if i % 2 == 0 else 'off', ts=stamp[0])
                f.engine.state_revision += 1
                started, cpu = time.perf_counter(), time.process_time()
                observation = f.service.observe_shadow(f.agent, f.states, {target})
                times.append((time.perf_counter() - started) * 1000)
                cpu_times.append((time.process_time() - cpu) * 1000)
                assert len(observation['predictions']) == 4
                predictions.append(observation)
            f.service._flush_shadow_models()
            f.service._flush_background_context(force=True)
        with store.conn() as connection:
            rows = [tuple(row) for row in connection.execute(
                'SELECT * FROM context_tournament_observed_pool ORDER BY agent_id,entity_id')]
        learned = {str(key): copy.deepcopy(value['candidate_policy']['heads'])
                   for key, value in f.service._shadow_models.items()}
        trained = [int(value.get('candidate_training_samples', 0)) for value in f.service._shadow_models.values()]
        assert len(learned) == 4 and all(count == passes for count in trained), trained
        result['observer'] = dict(median_ms=statistics.median(times), total_ms=sum(times),
            cpu_ms=sum(cpu_times), observations=passes, pool_sensors=96, examples_per_sensor=96,
            challenger_policies=4, fresh_labels_per_candidate=trained,
            pool_sha256=digest(rows), predictions_sha256=digest(predictions),
            learned_heads_sha256=digest(learned))
        return result


def paired(baseline, passes):
    series = []
    for repeat in range(3):
        pair = {}
        for name in (('before', 'after') if repeat % 2 == 0 else ('after', 'before')):
            output = subprocess.check_output([sys.executable, str(Path(__file__).resolve()), '--one',
                '--source-root', str(baseline if name == 'before' else ROOT), '--passes', str(passes)],
                text=True)
            pair[name] = json.loads(output.strip().splitlines()[-1])
        for section, keys in [('screening', ['sha256']), ('observer',
                ['pool_sha256', 'predictions_sha256', 'learned_heads_sha256'])]:
            for key in keys:
                assert pair['before'][section][key] == pair['after'][section][key], (section, key)
        pair['screening_speedup'] = pair['before']['screening']['elapsed_ms'] / pair['after']['screening']['elapsed_ms']
        pair['observer_speedup'] = pair['before']['observer']['median_ms'] / pair['after']['observer']['median_ms']
        series.append(pair)
    return dict(synthetic=True, source_paired=True,
        baseline=json.loads((baseline / 'adaptive_ai/BUILD_INFO.json').read_text(encoding='utf-8'))['version'], series=series,
        median_screening_speedup=statistics.median(x['screening_speedup'] for x in series),
        median_observer_speedup=statistics.median(x['observer_speedup'] for x in series),
        exact_screening_pool_predictions_and_learned_heads=True,
        limit='Real synchronous Tournament observer with 96 sensors, 96 retained examples and four candidate policies. '
              'Models, fresh target labels, full checksum checks and SQLite persistence use production code. '
              'Excludes HA ingress, the outer shipped observer composition, network, physical ACK and Pi SD I/O; '
              'measures worker occupancy after the authoritative decision, not physical-device latency.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--one', action='store_true')
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--baseline-root', type=Path)
    parser.add_argument('--passes', type=int, default=8)
    args = parser.parse_args()
    if args.passes < 2 or (not args.one and args.baseline_root is None):
        parser.error('passes >= 2 and baseline-root required (except --one)')
    print(json.dumps(one(args.source_root.resolve(), args.passes) if args.one else
                     paired(args.baseline_root.resolve(), args.passes), indent=None if args.one else 2))
