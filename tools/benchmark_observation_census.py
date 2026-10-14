"""Production process wrapper with four outer state reads and a 96-sensor pool."""
import argparse
import copy
from contextlib import contextmanager
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


def worker(root, passes):
    sys.path.insert(0, str(root / 'tools'))
    import benchmark_shadow_context as fixture_module
    from benchmark_shadow_context import training_contract, STAMP
    import context_tournament_policy_candidate as candidates
    import context_tournament_promotion as promotion
    from context_tournament import install
    from binary_state_classifier import BinaryStateClassifier
    from policy import MultiHorizonPolicy
    from storage import Store

    def compose(store, engine):
        engine.policy = lambda agent: engine.models[agent['id']]
        engine.runtime_for = lambda *_: {}
        engine.process_agent = lambda *_: {'desired': 0, 'authority': 'champion'}
        return install(store, engine)

    with tempfile.TemporaryDirectory() as folder, training_contract(), patch('time.time', return_value=STAMP), \
         patch.object(promotion, '_choose_schema_after_promotion', promotion._choose_schema_after_promotion), \
         patch.object(promotion, '_migrate_schema', promotion._migrate_schema), \
         patch.object(fixture_module, 'ContextTournament', compose):
        store = Store(Path(folder) / 'bench.db')
        fixture = fixture_module.Fixture(store)
        fixture.service.background_persistence_available = lambda: True
        for head in fixture.live.heads.values():
            clf = BinaryStateClassifier(fixture.live.dims)
            for action in (0, 1):
                for i in range(128):
                    clf.rows[action].append({'x': {j: (i + j + action) / 255. for j in range(1, 120)}, 'weight': 1., 'id': i + 1})
                clf.seen[action] = 128
            clf.ready = True
            head.state_classifier = clf
        fixture.observe()
        with store.conn() as c:
            c.executemany('INSERT OR IGNORE INTO context_tournament_observed_pool(agent_id,entity_id,updated_ts) VALUES(?,?,?)',
                          [(fixture.agent['id'], f'sensor.extra_{i}', STAMP) for i in range(84)])
        for model in fixture.service._shadow_models.values():
            model['evaluation_champion_revision'] = model['candidate_source_model_revision']
            model['evaluation_schema_revision'] = fixture.tournament['schema_revision']
        fixture.observe()
        inner = fixture.service.observe_shadow
        features, feature_rows, observations, durations, query_counts = MultiHorizonPolicy.features, [], [], [], []
        latest = {}
        def outer(*args):
            pre = [fixture.service.state(fixture.agent['id']) for _ in range(2)]
            result = inner(*args)
            post = [fixture.service.state(fixture.agent['id']) for _ in range(2)]
            latest.update(result=result, counts=[row['observed_pool_count'] for row in pre + post])
            return result
        fixture.service.observe_shadow = outer
        def record(policy, *args, **kwargs):
            result = features(policy, *args, **kwargs)
            feature_rows.append(result)
            return result
        def frame():
            trace = []
            with store.connection_session() as c:
                c.set_trace_callback(trace.append)
                started = time.perf_counter()
                champion = fixture.engine.process_agent(fixture.agent, fixture.states, set())
                elapsed = (time.perf_counter() - started) * 1000
                c.set_trace_callback(None)
            assert champion == {'desired': 0, 'authority': 'champion'}
            observations.append({'champion': champion, 'shadow': copy.deepcopy(latest)})
            return elapsed, sum('SELECT entity_id FROM context_tournament_observed_pool' in sql for sql in trace)
        with patch.object(MultiHorizonPolicy, 'features', record), \
             patch.object(candidates, '_verify_candidate_checksum', wraps=candidates._verify_candidate_checksum) as checks:
            for _ in range(passes):
                elapsed, count = frame(); durations.append(elapsed); query_counts.append(count)
            fixture.states[fixture.agent['target_entity']]['state'] = 'on'
            frame()
            for _ in range(3): frame()
        models = fixture.service._shadow_models
        heads = {key[1]: copy.deepcopy(model['candidate_policy']['heads']) for key, model in models.items()}
        labels = {key[1]: model.get('candidate_training_samples') for key, model in models.items()}
        assert len(heads) == 4 and all(n == 1 for n in labels.values())
        digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return {'observation_median_ms': statistics.median(durations), 'census_sql_per_frame': query_counts,
                'features_sha256': digest(feature_rows), 'decisions_and_counts_sha256': digest(observations),
                'trained_heads_sha256': digest(heads), 'labels': labels, 'checksum_checks': checks.call_count}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker-root', type=Path)
    parser.add_argument('--baseline-root', type=Path)
    parser.add_argument('--passes', type=int, default=20)
    args = parser.parse_args()
    if args.worker_root:
        result = worker(args.worker_root, args.passes)
    else:
        series = []
        for repeat in range(3):
            values = {}
            order = [('before', args.baseline_root), ('after', Path(__file__).resolve().parents[1])]
            if repeat % 2: order.reverse()
            for name, root in order:
                completed = subprocess.run([sys.executable, __file__, '--worker-root', str(root), '--passes', str(args.passes)],
                                           check=True, capture_output=True, text=True)
                values[name] = json.loads(completed.stdout)
            for key in ('features_sha256', 'decisions_and_counts_sha256', 'trained_heads_sha256', 'labels', 'checksum_checks'):
                assert values['before'][key] == values['after'][key], (key, values)
            assert set(values['before']['census_sql_per_frame']) == {5}
            assert set(values['after']['census_sql_per_frame']) == {1}
            values['speedup'] = values['before']['observation_median_ms'] / values['after']['observation_median_ms']
            series.append(values)
        result = {'series': series, 'real_process_wrapper': True, 'outer_state_reads': 4,
                  'features_decisions_counts_weights_and_labels_identical': True,
                  'limit': 'Synthetic 96-sensor durable/RAM pool and real production wrapper; no simulated delay, HA ingress or Pi microSD contention.'}
    print(json.dumps(result))
