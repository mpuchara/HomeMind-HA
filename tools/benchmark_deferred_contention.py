"""Controlled SQLite writer lock with WAL maintenance admission announced."""
import argparse
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


def worker(root, rounds):
    sys.path.insert(0, str(root / 'adaptive_ai/src'))
    with tempfile.TemporaryDirectory() as directory:
        os.environ['ADAPTIVE_AI_DATA'] = directory
        import sqlite3
        from storage import Store
        from context_row_buffer import ContextRowBuffer
        results = []
        for index in range(rounds):
            store = Store(Path(directory) / f'{index}.db')
            store.start_wal_keeper()
            try:
                with store.conn() as c:
                    c.execute('CREATE TABLE rows(a TEXT,e TEXT,n INTEGER,p TEXT,PRIMARY KEY(a,e))')
                buffer = ContextRowBuffer(store, 'rows',
                    'INSERT INTO rows VALUES(?,?,?,?) ON CONFLICT(a,e) DO UPDATE SET n=excluded.n,p=excluded.p')
                payload = json.dumps({'samples': list(range(500))})
                buffer.submit([('agent', 'sensor', 1, payload)])
                # Simulate the native writer lock held by TRUNCATE, without relying on
                # filesystem fsync timing. Both versions receive the same announcement.
                if not hasattr(store, '_wal_reclaiming'):
                    store._wal_reclaiming = threading.Event()
                blocker = sqlite3.connect(store.path)
                try:
                    blocker.execute('BEGIN IMMEDIATE')
                    store._wal_reclaiming.set()
                    started = time.perf_counter()
                    with store.background_sqlite():
                        try:
                            buffer.flush()
                        except sqlite3.OperationalError as exc:
                            assert exc.sqlite_errorcode & 0xff == sqlite3.SQLITE_BUSY
                        else:
                            raise AssertionError('Contended writer unexpectedly committed')
                    elapsed = (time.perf_counter() - started) * 1000
                    assert buffer.snapshot()['pending'] == 1
                    buffer.submit([('agent', 'sensor', 9, payload)])
                finally:
                    blocker.rollback(); blocker.close()
                    store._wal_reclaiming.clear()
                buffer.flush()
                with store.conn() as c:
                    rows = [tuple(row) for row in c.execute('SELECT * FROM rows ORDER BY a,e').fetchall()]
                    assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                assert rows == [('agent', 'sensor', 9, payload)]
                results.append({'contended_flush_ms': elapsed,
                                'final_sha256': hashlib.sha256(json.dumps(rows).encode()).hexdigest(),
                                'writer_deferrals': store.sqlite_snapshot().get('wal_writer_deferrals', 0)})
            finally:
                store.stop_wal_keeper()
        return {'results': results, 'median_contended_flush_ms': statistics.median(r['contended_flush_ms'] for r in results)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker-root', type=Path)
    parser.add_argument('--baseline-root', type=Path)
    parser.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args()
    if args.worker_root:
        output = worker(args.worker_root, args.rounds)
    else:
        output = {'values': {}, 'controlled_native_writer_lock': True,
                  'limit': 'Announced WAL maintenance with a controlled native BEGIN IMMEDIATE writer lock; not Raspberry Pi latency, CPU or proof that every reported error came from reclamation.'}
        for label, root in [('before', args.baseline_root), ('after', Path(__file__).resolve().parents[1])]:
            value = subprocess.run([sys.executable, __file__, '--worker-root', str(root), '--rounds', str(args.rounds)],
                                   check=True, text=True, capture_output=True)
            output['values'][label] = json.loads(value.stdout)
        for before, after in zip(output['values']['before']['results'], output['values']['after']['results']):
            assert before['final_sha256'] == after['final_sha256']
            assert before['writer_deferrals'] == 0 and after['writer_deferrals'] == 1
        output['exact_final_rows'] = True
    print(json.dumps(output, indent=2))
