"""Controlled writer pause: submit latest metadata with 10 MiB of owned JSON."""
import argparse
from contextlib import contextmanager
import hashlib
import inspect
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import threading
import time


def worker(root, rounds, pause_ms):
    sys.path.insert(0, str(root / 'adaptive_ai/src'))
    scratch = tempfile.TemporaryDirectory()
    os.environ['ADAPTIVE_AI_DATA'] = scratch.name
    from context_row_buffer import ContextRowBuffer
    results = []
    for repeat in range(rounds):
        entered, release = threading.Event(), threading.Event()
        errors = []
        class FakeStore:
            def __init__(self):
                self.lock = threading.RLock()
                self.rows = {}
                self.calls = 0
            @contextmanager
            def conn(self):
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    if not release.wait(5): raise TimeoutError('controlled writer')
                yield self
            def executemany(self, statement, rows):
                for row in rows: self.rows[(row[0], row[1])] = row
        store = FakeStore()
        kwargs = {'max_bytes': 16 * 1024 * 1024}
        if 'shared_columns' in inspect.signature(ContextRowBuffer).parameters:
            kwargs['shared_columns'] = (3,)
        buffer = ContextRowBuffer(store, 'fixture', 'fixture', **kwargs)
        count = 288
        payloads = [json.dumps({'sensor': i, 'samples': 'x' * (10 * 1024 * 1024 // count - 80)},
                              separators=(',', ':')) for i in range(count)]
        first = [('agent', str(i), 1, payloads[i]) for i in range(count)]
        latest = [('agent', str(i), 2, payloads[i]) for i in range(count)]
        buffer.submit(first)
        def write():
            try: buffer.flush()
            except Exception as exc: errors.append(str(exc))
        thread = threading.Thread(target=write)
        thread.start()
        assert entered.wait(5)
        timer = threading.Timer(pause_ms / 1000., release.set)
        timer.start()
        try:
            start = time.perf_counter()
            buffer.submit(latest)
            elapsed = (time.perf_counter() - start) * 1000
            during = buffer.snapshot()
        finally:
            timer.join()
            release.set()
            thread.join(5)
        assert not thread.is_alive() and not errors, errors
        buffer.flush()
        assert len(store.rows) == count and all(row[2] == 2 for row in store.rows.values())
        assert buffer.snapshot()['bytes'] == 0
        assert during['bytes'] <= during['max_bytes']
        results.append({'submit_ms': elapsed, 'overflow_flushes': during['overflow_flushes'],
                        'while_writer_paused': during['inflight'] > 0,
                        'while_paused_buffer': during, 'commit_count': store.calls,
                        'final_rows_sha256': hashlib.sha256(json.dumps(sorted(store.rows.values())).encode()).hexdigest()})
    return {'results': results, 'submit_median_ms': statistics.median(r['submit_ms'] for r in results),
            'payload_bytes': sum(len(p.encode()) for p in payloads)}


def run(before, after, rounds, pause_ms):
    values = {}
    for name, root in [('before', before), ('after', after)]:
        result = subprocess.run([sys.executable, __file__, '--worker-root', str(root), '--rounds', str(rounds),
                                 '--pause-ms', str(pause_ms)], check=True, capture_output=True, text=True)
        values[name] = json.loads(result.stdout.strip().splitlines()[-1])
    for previous, current in zip(values['before']['results'], values['after']['results']):
        assert previous['final_rows_sha256'] == current['final_rows_sha256']
        assert previous['overflow_flushes'] > 0 and not previous['while_writer_paused']
        assert current['overflow_flushes'] == 0 and current['while_writer_paused']
    return {'controlled_writer_pause_ms': pause_ms, 'rows': 288, 'rounds': rounds, 'same_buffer_limit_mb': 16,
            'values': values, 'exact_final_rows': True,
            'limit': 'Synthetic immutable pool payloads and controlled writer pause; not measured Raspberry Pi latency or proof of a particular device stall cause.'}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--worker-root', type=Path)
    p.add_argument('--baseline-root', type=Path)
    p.add_argument('--rounds', type=int, default=3)
    p.add_argument('--pause-ms', type=int, default=300)
    a = p.parse_args()
    result = worker(a.worker_root, a.rounds, a.pause_ms) if a.worker_root else run(a.baseline_root, Path(__file__).resolve().parents[1], a.rounds, a.pause_ms)
    print(json.dumps(result))
