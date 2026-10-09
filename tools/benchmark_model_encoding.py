"""Synthetic cold/warm encoding cost; verifies unchanged canonical SHA256."""
from pathlib import Path
import sys
import hashlib
import json
import statistics
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'adaptive_ai/src'))
from model_encoding_cache import ModelEncodingCache
from policy_backend import _canonical


def main():
    matrix = [[(i + 1) / (j + 3) for i in range(512)] for j in range(80)]
    rows = [{'id': str(n), 'x': {str(i): (n + i + 1) / 1234 for i in range(64)},
             'weight': .987654321} for n in range(512)]
    models = [dict(revision=str(i), matrices=matrix, rows=rows, dims=512,
                   schema={'entities': ['sensor.example']}) for i in range(4)]
    expected = [hashlib.sha256(_canonical(raw).encode()).hexdigest() for raw in models]
    cache = ModelEncodingCache()

    def run(cached, passes):
        times = []
        for _ in range(passes):
            for i, raw in enumerate(models):
                started = time.perf_counter()
                encoded = cache.encode(raw, _canonical) if cached else _canonical(raw).encode()
                digest = hashlib.sha256(encoded).hexdigest()
                times.append((time.perf_counter() - started) * 1000)
                assert digest == expected[i]
        return dict(median_ms=statistics.median(times),
                    p95_ms=sorted(times)[int(.95 * len(times))], calls=len(times))

    cold = run(True, 1)
    series = [dict(legacy=run(False, 8), cached=run(True, 8)) for _ in range(3)]
    result = dict(synthetic=True, numeric_values=80*512+512*64, models=4,
                  cold=cold, series=series, cache=cache.snapshot(), checksum_parity=True)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
