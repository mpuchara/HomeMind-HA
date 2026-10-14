"""Paired real context observer, dense binary reservoirs, preserved feature/prediction/update results."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
from unittest.mock import patch


def worker(root, passes):
    sys.path.insert(0, str(root / 'tools'))
    from benchmark_shadow_context import Fixture, training_contract, STAMP
    import context_tournament_policy_candidate as candidates
    import context_tournament_promotion as promotion
    from binary_state_classifier import BinaryStateClassifier
    from policy import MultiHorizonPolicy
    from storage import Store
    import tempfile
    with tempfile.TemporaryDirectory() as folder, training_contract(), patch('time.time', return_value=STAMP), \
         patch.object(promotion, '_choose_schema_after_promotion', promotion._choose_schema_after_promotion), \
         patch.object(promotion, '_migrate_schema', promotion._migrate_schema):
        fixture = Fixture(Store(Path(folder) / 'bench.db'))
        for head in fixture.live.heads.values():
            clf = BinaryStateClassifier(fixture.live.dims)
            for action in (0, 1):
                for i in range(128):
                    clf.rows[action].append({'x': {j: (i + j + action) / 255. for j in range(1,120)}, 'weight': 1., 'id': i + 1})
                clf.seen[action] = 128
            clf.ready = True
            head.state_classifier = clf
        fixture.observe()
        for model in fixture.service._shadow_models.values():
            model['evaluation_champion_revision'] = model['candidate_source_model_revision']
            model['evaluation_schema_revision'] = fixture.tournament['schema_revision']
        fixture.observe()
        features, rows, predictions, durations = MultiHorizonPolicy.features, [], [], []
        def record(policy, *args, **kwargs):
            result = features(policy, *args, **kwargs)
            rows.append(result)
            return result
        with patch.object(MultiHorizonPolicy, 'features', record), \
             patch.object(candidates, '_verify_candidate_checksum', wraps=candidates._verify_candidate_checksum) as checks:
            for _ in range(passes):
                started = time.perf_counter()
                result = fixture.observe()
                durations.append((time.perf_counter() - started) * 1000)
                predictions.append(result['predictions'])
            # A real future target transition scores the preceding predictions and
            # trains the same candidate objects, then subsequent frames reuse them.
            fixture.states[fixture.agent['target_entity']]['state'] = 'on'
            result = fixture.observe()
            predictions.append(result['predictions'])
            for _ in range(3):
                predictions.append(fixture.observe()['predictions'])
            assert checks.call_count >= 4 * passes
        models = fixture.service._shadow_models
        heads = {key[1]: copy.deepcopy(model['candidate_policy']['heads']) for key, model in models.items()}
        labels = {key[1]: model.get('candidate_training_samples') for key, model in models.items()}
        assert len(heads) == 4 and all(n == 1 for n in labels.values()), labels
        digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        # Prediction DTOs include metadata timestamps/index/value but no random UUIDs.
        return {'observation_median_ms': statistics.median(durations),
                'observation_total_ms': sum(durations), 'checks': checks.call_count,
                'features_sha256': digest(rows), 'predictions_sha256': digest(predictions),
                'trained_heads_sha256': digest(heads), 'future_training_samples': labels}


def run(before, after, passes):
    series = []
    for repeat in range(3):
        values = {}
        order = [('before', before), ('after', after)]
        if repeat % 2:
            order.reverse()
        for name, root in order:
            result = subprocess.run([sys.executable, __file__, '--worker-root', str(root), '--passes', str(passes)],
                                    capture_output=True, text=True, check=True)
            values[name] = json.loads(result.stdout.strip().splitlines()[-1])
        for key in ('features_sha256', 'predictions_sha256', 'trained_heads_sha256', 'future_training_samples'):
            assert values['before'][key] == values['after'][key], (key, values)
        values['observation_speedup'] = values['before']['observation_median_ms'] / values['after']['observation_median_ms']
        series.append(values)
    return {'synthetic': True, 'series': series, 'passes_per_pair': passes, 'challengers': 4,
            'accepted_dense_rows_per_class': 128, 'features_predictions_and_future_trained_heads_identical': True,
            'limit': 'Production observer on this test host, no HA ingress or concurrent microSD contention; not measured Pi latency.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-root', type=Path)
    parser.add_argument('--worker-root', type=Path)
    parser.add_argument('--passes', type=int, default=20)
    args = parser.parse_args()
    result = worker(args.worker_root, args.passes) if args.worker_root else run(args.baseline_root, Path(__file__).resolve().parents[1], args.passes)
    print(json.dumps(result))
