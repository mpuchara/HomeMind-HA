"""Paired production read-only SQLite scopes, with identical rows and write guard."""
import argparse
import hashlib
import json
from pathlib import Path
import os
import statistics
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


def worker(root, passes):
    sys.path.insert(0, str(root / 'adaptive_ai/src'))
    with tempfile.TemporaryDirectory() as folder:
        os.environ['ADAPTIVE_AI_DATA'] = folder
        from storage import Store
        store = Store(Path(folder) / 'read.db')
        with store.conn() as c:
            c.executemany('INSERT INTO app_meta VALUES(?,?)', [(str(i), 'value-' + str(i)) for i in range(32)])
        timings, results, sql = [], [], []
        with store.connection_session() as c:
            c.set_trace_callback(sql.append)
            with patch.object(store.storage_budget, 'observe_pages', wraps=store.storage_budget.observe_pages) as pages:
                for _ in range(passes):
                    started = time.perf_counter()
                    rows = []
                    for i in range(32):
                        with store.conn() as read:
                            rows.append(tuple(read.execute('SELECT key,value FROM app_meta WHERE key=?', (str(i),)).fetchone()))
                    timings.append((time.perf_counter() - started) * 1000)
                    results.append(rows)
                probes = pages.call_count
                read_sql = len(sql)
                # Fresh guarded write still works after the complete read trace.
                with store.conn() as write:
                    write.execute("INSERT INTO app_meta VALUES('after-reads','persisted')")
                with store.conn() as read:
                    assert read.execute("SELECT value FROM app_meta WHERE key='after-reads'").fetchone()[0] == 'persisted'
            c.set_trace_callback(None)
        return {'read_scope_median_ms': statistics.median(timings), 'readonly_page_probes': probes,
                'readonly_sql_statements': read_sql, 'selects': 32 * passes,
                'rows_sha256': hashlib.sha256(json.dumps(results).encode()).hexdigest(),
                'fresh_guarded_write_persisted': True}


def run(before, after, passes):
    series = []
    for repeat in range(3):
        values = {}
        order = [('before', before), ('after', after)]
        if repeat % 2:
            order.reverse()
        for name, root in order:
            result = subprocess.run([sys.executable, __file__, '--worker-root', str(root), '--passes', str(passes)],
                                    check=True, capture_output=True, text=True)
            values[name] = json.loads(result.stdout.strip().splitlines()[-1])
        assert values['before']['rows_sha256'] == values['after']['rows_sha256']
        assert values['before']['readonly_page_probes'] == 32 * passes
        assert values['after']['readonly_page_probes'] == 0
        values['read_scope_speedup'] = values['before']['read_scope_median_ms'] / values['after']['read_scope_median_ms']
        series.append(values)
    return {'synthetic': True, 'passes': passes, 'reads_per_pass': 32, 'series': series,
            'every_row_identical': True, 'limit': 'Production Store/SQLite on test host, not measured Raspberry Pi or total inference time.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-root', type=Path)
    parser.add_argument('--worker-root', type=Path)
    parser.add_argument('--passes', type=int, default=20)
    args = parser.parse_args()
    result = worker(args.worker_root, args.passes) if args.worker_root else run(args.baseline_root, Path(__file__).resolve().parents[1], args.passes)
    print(json.dumps(result))
