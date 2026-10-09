"""Synthetic immutable Shadow snapshots with changing counters; checks all JSON data."""
from pathlib import Path
import hashlib
import json
import pickle
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'adaptive_ai/src'))
from model_encoding_cache import ModelEncodingCache
import policy_backend
import shadow_model_json


def main():
    cache = ModelEncodingCache()
    policy_backend.MODEL_ENCODING_CACHE = shadow_model_json.MODEL_ENCODING_CACHE = cache
    matrix = [[(i + 1) / (j + 3) for i in range(512)] for j in range(80)]
    rows = [{'id': str(n), 'x': {str(i): (n + i + 1) / 1234 for i in range(64)},
             'weight': .987654321} for n in range(512)]
    models = []
    for i in range(4):
        policy = dict(model_revision=str(i), matrices=matrix, rows=rows, dims=512,
                      schema={'entities': ['sensor.example']})
        # Match MultiHorizonPolicy.serialize: checksum sees a detached JSON tree,
        # not hand-built string aliases that pickle decoding may normalize.
        policy = json.loads(json.dumps(policy))
        policy['model_checksum'] = policy_backend.model_checksum(policy)
        models.append(dict(candidate_policy=policy, counts={'on': i}, samples=i))

    def run(cached, passes):
        times = []
        for step in range(passes):
            snapshots = []
            for raw in models:
                raw['samples'] = step
                raw['candidate_policy']['_history_watermark'] = step
                snapshots.append(pickle.dumps(raw, protocol=5))
            started = time.perf_counter()
            packed = [shadow_model_json.shadow_model_json(pickle.loads(raw)) if cached else
                      json.dumps(pickle.loads(raw), separators=(',', ':'), sort_keys=True)
                      for raw in snapshots]
            times.append((time.perf_counter() - started) * 1000)
            for raw, text in zip(models, packed):
                decoded = json.loads(text)
                assert decoded == json.loads(json.dumps(raw))
                clean = {k: v for k, v in decoded['candidate_policy'].items()
                         if k != 'model_checksum' and not k.startswith('_')}
                assert hashlib.sha256(policy_backend._canonical(clean).encode()).hexdigest() == raw['candidate_policy']['model_checksum']
        return dict(median_ms=statistics.median(times), p95_ms=sorted(times)[int(.95 * len(times))], batches=len(times))

    initial_bytes = cache.snapshot()['bytes']
    series = [dict(legacy=run(False, 8), reused=run(True, 8)) for _ in range(3)]
    print(json.dumps(dict(synthetic=True, numeric_values_per_model=73728, models=4,
        series=series, shared_cache=cache.snapshot(), additional_cached_payload_bytes=cache.snapshot()['bytes']-initial_bytes,
        shadow_encoding=shadow_model_json.snapshot(), data_and_checksum_parity=True), indent=2))


if __name__ == '__main__':
    main()
