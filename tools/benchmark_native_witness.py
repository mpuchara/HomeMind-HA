"""Paired 0.14.162/current full-content witnesses, full SHA and Shadow JSON."""
from pathlib import Path
import argparse
import hashlib
import json
import pickle
import statistics
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'adaptive_ai/src'))
import model_encoding_cache as encoding
import policy_backend
import shadow_model_json


def previous_builtin_tree(value):
    scalars = (str, int, float, bool, type(None))
    containers = (dict, list, tuple)
    pending, visited = [value], set()
    while pending:
        current = pending.pop()
        kind = type(current)
        if kind in scalars:
            continue
        if kind not in containers:
            return False
        if id(current) in visited:
            continue
        visited.add(id(current))
        if kind is dict:
            if any(type(key) not in scalars for key in current):
                return False
            items = current.values()
        else:
            items = current
        for item in items:
            item_type = type(item)
            if item_type in scalars:
                continue
            if item_type not in containers:
                return False
            pending.append(item)
    return True


def previous_witness(value):
    return pickle.dumps(value, protocol=5) if previous_builtin_tree(value) else None


def fixture(width=512, row_count=512):
    matrix = [[(i+1)/(j+3) for i in range(width)] for j in range(80)]
    rows = [dict(id=str(n), x={str(i): (n+i+1)/1234 for i in range(64)},
                 weight=.987654321) for n in range(row_count)]
    return [json.loads(json.dumps(dict(model_revision=str(i), matrix=matrix, rows=rows)))
            for i in range(4)]


def run(passes=8):
    current = encoding.builtin_witness
    series = []
    for width, row_count in [(128, 128), (512, 512)]:
        models = fixture(width, row_count)
        expected = [hashlib.sha256(policy_backend._canonical(raw).encode()).hexdigest()
                    for raw in models]
        caches = {(mode, name): encoding.ModelEncodingCache()
                  for mode in ('checksum', 'shadow_json') for name in ('previous', 'current')}
        functions = {'previous': previous_witness, 'current': current}
        for name in ('previous', 'current'):
            cache = caches['checksum', name]
            with patch.object(encoding, 'builtin_witness', functions[name]):
                for raw, digest in zip(models, expected):
                    assert hashlib.sha256(cache.encode(raw, policy_backend._canonical)).hexdigest() == digest
        snapshots = [pickle.dumps(dict(candidate_policy={**raw, 'model_checksum': digest},
                                        samples=17, counts={'on': 4}), protocol=5)
                     for raw, digest in zip(models, expected)]
        for name in ('previous', 'current'):
            with patch.object(encoding, 'builtin_witness', functions[name]), patch.object(
                    shadow_model_json, 'MODEL_ENCODING_CACHE', caches['shadow_json', name]):
                for raw in snapshots:
                    shadow_model_json.shadow_model_json(pickle.loads(raw))
        for repeat in range(3):
            for mode in ('checksum', 'shadow_json'):
                result = dict(width=width, rows=row_count, mode=mode, repeat=repeat)
                for name in (('previous', 'current') if repeat % 2 == 0 else ('current', 'previous')):
                    times = []
                    with patch.object(encoding, 'builtin_witness', functions[name]), patch.object(
                            shadow_model_json, 'MODEL_ENCODING_CACHE', caches[mode, name]):
                        for _ in range(passes):
                            started = time.perf_counter()
                            if mode == 'checksum':
                                outputs = [hashlib.sha256(caches[mode, name].encode(raw, policy_backend._canonical)).hexdigest()
                                           for raw in models]
                            else:
                                outputs = [shadow_model_json.shadow_model_json(pickle.loads(raw))
                                           for raw in snapshots]
                            times.append((time.perf_counter()-started)*1000)
                            if mode == 'checksum':
                                assert outputs == expected
                            else:
                                for raw, output in zip(snapshots, outputs):
                                    assert json.loads(output) == json.loads(json.dumps(pickle.loads(raw)))
                    result[name+'_median_ms'] = statistics.median(times)
                result['speedup'] = result['previous_median_ms']/result['current_median_ms']
                result['cache'] = {name: caches[mode, name].snapshot() for name in ('previous', 'current')}
                assert all(row['misses'] == 4 and row['entries'] == 4
                           for row in result['cache'].values()), 'benchmark must remain warm'
                series.append(result)
    return dict(synthetic=True, full_sha_and_json_parity=True, series=series,
                limit='Four-model warm component including full SHA or snapshot decode/JSON; not an HA latency forecast.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--passes', type=int, default=8)
    parser.add_argument('--compact', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(max(1, args.passes)), indent=None if args.compact else 2))
