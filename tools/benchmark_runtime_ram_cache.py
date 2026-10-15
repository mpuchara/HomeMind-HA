"""Paired SQL component benchmark through the actual inference dispatcher.

Both modes execute every SQL read. The only difference is retaining bounded
SQLite page/statement caches between decisions. No HA latency claim is made.
"""
from pathlib import Path
from types import SimpleNamespace
import argparse
import hashlib
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'adaptive_ai/src'))
_scratch = tempfile.TemporaryDirectory(prefix='runtime-ram-benchmark-')
os.environ['ADAPTIVE_AI_DATA'] = _scratch.name
import engine as engine_module
from engine import Engine
from storage import Store
from runtime_sqlite_cache import RuntimeSQLiteCache
from agent_candidate_lineage import ensure_lineage_tables, _ensure_root
from agent_candidate_shadow_runtime import _generation
from agent_explore import ensure_explore_tables, _active_free_for_live


def run_mode(mode, passes=128):
    with tempfile.TemporaryDirectory(prefix='runtime-cache-pair-') as directory:
        store = Store(Path(directory) / 'test.db')
        store.start_wal_keeper()
        try:
            ensure_lineage_tables(store)
            ensure_explore_tables(store)
            a = store.create_agent(dict(name='Paired cache light', target_entity='light.example',
                target_property='power', min_value=0, max_value=1, input_entities=['sensor.radar']))
            _ensure_root(store, a['id'], generation_number=0)
            with store.conn() as c:
                c.execute("UPDATE agents SET mode='shadow' WHERE id=?", (a['id'],))
                c.execute('CREATE TABLE benchmark_context(entity_id TEXT PRIMARY KEY, sample TEXT)')
                c.executemany('INSERT INTO benchmark_context VALUES(?,?)',
                              [(f'sensor.context_{i}', str(i) * 200) for i in range(96)])
            outputs = []
            def observe(*_):
                before = _generation(store, agent_id=a['id'])['lifecycle_state']
                explore = _active_free_for_live(store, a['id'])
                config = store.get_agent_config(a['id'])['enabled']
                reads = []
                with store.background_sqlite(), store.connection_session():
                    for i in range(24):
                        with store.conn() as c:
                            reads.append(tuple(c.execute('SELECT entity_id,length(sample) FROM benchmark_context WHERE entity_id=?',
                                                       (f'sensor.context_{i}',)).fetchone()))
                after = _generation(store, agent_id=a['id'])['lifecycle_state']
                outputs.append((before, explore, config, reads, after))
            runtime = Engine.__new__(Engine)
            runtime.process_agent = observe
            runtime._inference_tls = threading.local()
            runtime.stop_event = threading.Event()
            runtime.wake_event = threading.Event()
            runtime.state_revision = 1
            snapshot = ({}, 1, {}, 0)
            if mode == 'current':
                store._runtime_sqlite_cache = RuntimeSQLiteCache(memory=lambda: (8192, 6144))
            samples = []
            with patch.object(engine_module, 'STORE', store):
                runtime.process_target([a], {'sensor.radar'}, snapshot)
                assert len(outputs) == 1, 'warm inference did not complete'
                outputs.clear()
                for _ in range(passes):
                    started = time.perf_counter()
                    runtime.process_target([a], {'sensor.radar'}, snapshot)
                    samples.append((time.perf_counter() - started) * 1000)
            assert len(outputs) == passes
            assert all(row == outputs[0] for row in outputs)
            with store.conn() as c:
                assert c.execute("SELECT count(*) FROM events WHERE kind='agent_error'").fetchone()[0] == 0
            stats = store._runtime_sqlite_cache.snapshot() if mode == 'current' else None
            return dict(median_ms=statistics.median(samples), total_ms=sum(samples), passes=passes,
                        result_sha256=hashlib.sha256(json.dumps(outputs, sort_keys=True).encode()).hexdigest(),
                        runtime_cache=stats, sqlite_connections_in_timed_inference=0 if stats else passes)
        finally:
            store.stop_runtime_cache()
            store.stop_wal_keeper()


def run(passes=128):
    series = []
    for repeat in range(3):
        pair = {}
        for mode in (('previous', 'current') if repeat % 2 == 0 else ('current', 'previous')):
            pair[mode] = run_mode(mode, passes)
        assert pair['previous']['result_sha256'] == pair['current']['result_sha256']
        series.append(dict(**pair, total_speedup=pair['previous']['total_ms'] / pair['current']['total_ms']))
    return dict(synthetic=True, source_baseline='0.14.199 connection_session lifetime', series=series,
                median_component_speedup=statistics.median(row['total_speedup'] for row in series),
                every_read_executes_sql=True, same_results=True,
                limit='Host SQL component, warm sequential inference and 96 context rows. Excludes model CPU, HA and concurrent checkpoint I/O; not device-wide latency or RSS measurement.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--passes', type=int, default=128)
    args = parser.parse_args()
    if args.passes < 1:
        parser.error('passes must be positive')
    print(json.dumps(run(args.passes), indent=2))
