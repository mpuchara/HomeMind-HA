"""Paired production Shadow snapshot/encoding/write paths with dense classifier rows."""
import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pickle
import statistics
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch


def worker(root, passes):
    sys.path.insert(0, str(root / 'adaptive_ai/src'))
    with tempfile.TemporaryDirectory() as folder, patch('time.time', return_value=1791974000.):
        os.environ['ADAPTIVE_AI_DATA'] = folder
        from storage import Store
        from context_tournament import ContextTournament
        from policy import MultiHorizonPolicy
        from binary_state_classifier import BinaryStateClassifier
        from sealed_policy_snapshot import seal_policy, snapshot
        from shadow_model_json import shadow_model_json
        from policy_backend import model_checksum, verify_model_checksum
        if importlib.util.find_spec('shadow_model_snapshot'):
            from shadow_model_snapshot import snapshot_json
        else:
            snapshot_json = lambda raw: shadow_model_json(pickle.loads(raw))
        agent = dict(id='bench', target_entity='light.room', target_property='power',
                     min_value=0, max_value=1, deadband=.5, input_entities=['*'])
        policy = MultiHorizonPolicy(agent, {}, {}, set())
        for head in policy.heads.values():
            clf = BinaryStateClassifier(policy.dims)
            for action in (0, 1):
                for i in range(128):
                    clf.rows[action].append({'x': {j: (i + j + action) / 255. for j in range(1,120)},
                                             'weight': 1., 'id': i + 1})
                clf.seen[action] = 128
            clf.ready = True
            head.state_classifier = clf
        policy.model_revision = policy.tournament_revision = "initial-model"
        source_raw = policy.serialize()
        source_raw['_history_watermark'] = {'ts': 1.}
        source = seal_policy(source_raw)
        assert source is not None
        source_bytes = len(source._canonical_bytes)
        store = Store(Path(folder) / 'persist.db')
        service = ContextTournament(store, SimpleNamespace())
        model = dict(version=1, action_count=2, samples=0, counts={'0:4': [0, 0]}, candidate_policy=source)
        snapshots, encodings, flushes, admissions, outputs = [], [], [], [], []
        for i in range(passes):
            if i and i % 8 == 0:
                # New source, including ignored metadata, never reuses the old fragment.
                source_raw = copy.deepcopy(source)
                source_raw['_history_watermark']['ts'] = float(i + 1)
                source_raw['model_revision'] = 'generation-' + str(i)
                source_raw['model_checksum'] = model_checksum(source_raw)
                start = time.perf_counter()
                source = seal_policy(source_raw)
                admissions.append((time.perf_counter() - start) * 1000)
                assert source is not None
                model['candidate_policy'] = source
            assert source.verify()  # Mandatory fresh SHA stays active.
            model['samples'] = i + 1
            model['counts']['0:4'] = [i // 2, (i + 1) // 2]
            start = time.perf_counter()
            service._save_shadow_model('bench', 'sensor.radar', model)
            snapshots.append((time.perf_counter() - start) * 1000)
            frozen = service._shadow_dirty[('bench', 'sensor.radar')]
            start = time.perf_counter()
            encoded = snapshot_json(frozen)
            encodings.append((time.perf_counter() - start) * 1000)
            decoded = json.loads(encoded)
            assert decoded['samples'] == i + 1
            assert decoded['counts'] == model['counts']
            assert decoded['candidate_policy']['_history_watermark'] == source_raw['_history_watermark']
            assert verify_model_checksum(decoded['candidate_policy'])
            outputs.append(encoded)
            start = time.perf_counter()
            assert service._flush_shadow_models() == 1
            flushes.append((time.perf_counter() - start) * 1000)
        restarted = ContextTournament(store, SimpleNamespace())
        restored = restarted._load_shadow_model('bench', 'sensor.radar', 2)
        assert restored == json.loads(outputs[-1])
        return dict(snapshot_median_ms=statistics.median(snapshots),
                    json_median_ms=statistics.median(encodings),
                    flush_median_ms=statistics.median(flushes),
                    new_source_admission_ms=admissions,
                    source_canonical_bytes=source_bytes,
                    all_snapshots_sha256=hashlib.sha256(json.dumps(outputs).encode()).hexdigest(),
                    snapshot_count=passes, flushed=service.shadow_persistence_snapshot()['flushed'],
                    persisted_restart_exact=True, ram=snapshot())


def run(before, after, passes):
    series = []
    for repeat in range(3):
        values = {}
        order = [('before', before), ('after', after)]
        if repeat % 2: order.reverse()
        for name, root in order:
            result = subprocess.run([sys.executable, __file__, '--worker-root', str(root), '--passes', str(passes)],
                                    check=True, capture_output=True, text=True)
            values[name] = json.loads(result.stdout.strip().splitlines()[-1])
        assert values['before']['all_snapshots_sha256'] == values['after']['all_snapshots_sha256']
        assert values['before']['flushed'] == values['after']['flushed'] == passes
        for phase in ('snapshot', 'json', 'flush'):
            values[phase + '_speedup'] = values['before'][phase + '_median_ms'] / values['after'][phase + '_median_ms']
        series.append(values)
    return dict(synthetic=True, passes=passes, dense_rows_per_class=128, fresh_source_every=8,
                series=series, every_json_byte_identical=True,
                limit='Production snapshot/JSON/SQLite paths on test host. No microSD contention, not whole Pi inference or training speed.')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline-root', type=Path)
    p.add_argument('--worker-root', type=Path)
    p.add_argument('--passes', type=int, default=24)
    a = p.parse_args()
    result = worker(a.worker_root, a.passes) if a.worker_root else run(a.baseline_root, Path(__file__).resolve().parents[1], a.passes)
    print(json.dumps(result))
