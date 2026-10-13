"""Compare 0.14.190 and current bounded RAM paths using identical input traces."""
import argparse
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


def worker(root):
    sys.path.insert(0, str(root / 'adaptive_ai/src'))
    scratch = tempfile.TemporaryDirectory()
    os.environ['ADAPTIVE_AI_DATA'] = scratch.name
    from storage import Store
    from context_row_buffer import ContextRowBuffer
    from policy_backend import model_checksum, verify_model_checksum, _canonical
    store = Store(Path(scratch.name) / 'ram.db')
    with store.conn() as db:
        db.execute('CREATE TABLE counters(aid TEXT,eid TEXT,v INTEGER,raw TEXT,PRIMARY KEY(aid,eid))')
    buf = ContextRowBuffer(store, 'counters', 'INSERT INTO counters VALUES(?,?,?,?) '
                           'ON CONFLICT(aid,eid) DO UPDATE SET v=excluded.v,raw=excluded.raw')
    started = time.perf_counter()
    flushes = 0
    for tick in range(1, 601):
        # The same 96 cumulative rows, latest independent labels retained, 60 s.
        rows = [('a', str(i), tick, json.dumps({'labels': list(range(tick // 20))})) for i in range(96)]
        with patch('context_row_buffer.time.monotonic', return_value=100. + tick / 10.):
            buf.submit(rows)
            if tick % 50 == 0:
                poll = getattr(buf, 'flush_if_due', buf.flush)
                flushes += bool(poll())
    flushes += bool(buf.flush())
    counter_seconds = time.perf_counter() - started
    with store.conn() as db:
        final = [tuple(row) for row in db.execute('SELECT * FROM counters ORDER BY eid')]
    assert len(final) == 96 and all(row[2] == 600 for row in final)
    digest = hashlib.sha256(json.dumps(final, separators=(',', ':')).encode()).hexdigest()
    # String keys and dense accepted classifier samples resemble loaded JSON policy data.
    raw = {'schema': {'entities': ['sensor.radar']}, 'heads': {'1': {'rows': [
        {'x': {str(j): (i + j) / 255. for j in range(128)}, 'weight': 1.}
        for i in range(256)]}}}
    raw['model_checksum'] = model_checksum(raw)
    expected = hashlib.sha256(_canonical({k: v for k, v in raw.items() if k != 'model_checksum'}).encode()).hexdigest()
    assert raw['model_checksum'] == expected
    assert verify_model_checksum(raw)
    durations = []
    for _ in range(7):
        start = time.perf_counter()
        for i in range(30):
            assert verify_model_checksum(raw)
        durations.append((time.perf_counter() - start) * 1000 / 30)
    raw['heads']['1']['rows'][1]['x']['1'] += .0001
    assert not verify_model_checksum(raw)
    return {'counter_flushes': flushes, 'counter_rows_written': buf.snapshot()['flushed'],
            'counter_seconds': counter_seconds, 'final_counter_sha256': digest,
            'warm_checksum_median_ms': statistics.median(durations),
            'full_canonical_sha_preserved': True, 'deep_mutation_detected': True,
            'ram_buffer': buf.snapshot()}


def run(baseline, current):
    reports = []
    for iteration in range(3):
        values = {}
        pairs = [('before', baseline), ('after', current)]
        if iteration % 2:
            pairs.reverse()
        for name, root in pairs:
            result = subprocess.run([sys.executable, __file__, '--worker', '--root', str(root)],
                                    capture_output=True, text=True, check=True)
            values[name] = json.loads(result.stdout.strip().splitlines()[-1])
        assert values['before']['final_counter_sha256'] == values['after']['final_counter_sha256']
        values['checksum_speedup'] = values['before']['warm_checksum_median_ms'] / values['after']['warm_checksum_median_ms']
        values['counter_write_reduction'] = values['before']['counter_rows_written'] / values['after']['counter_rows_written']
        reports.append(values)
    return {'synthetic': True, 'series': reports, 'identical_final_counters_and_labels': True,
            'limits': 'Component workload on this host, not measured Raspberry Pi latency. RAM counters flush after the configured interval, on capacity pressure, explicit barriers and shutdown.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--baseline-root', type=Path)
    args = parser.parse_args()
    if args.worker:
        result = worker(args.root)
    else:
        if args.baseline_root is None:
            parser.error('--baseline-root is required')
        result = run(args.baseline_root, Path(__file__).resolve().parents[1])
    print(json.dumps(result))
