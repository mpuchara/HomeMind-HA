"""Paired real target dispatcher and Candidate/Explore SQL; excludes policy CPU."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import argparse
import hashlib
import json
import os
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'adaptive_ai/src'))
_scratch = tempfile.TemporaryDirectory(prefix='homemind-inference-sql-')
os.environ['ADAPTIVE_AI_DATA'] = _scratch.name
import engine as engine_module
from engine import Engine
from storage import Store
from agent_candidate_lineage import ensure_lineage_tables, _ensure_root
from agent_candidate_shadow_runtime import _generation
from agent_explore import ensure_explore_tables, _active_free_for_live


def run_mode(mode, passes, shadow_queries):
    with tempfile.TemporaryDirectory(prefix='inference-sql-bench-') as directory:
        store = Store(Path(directory) / 'test.db')
        store.start_wal_keeper()
        try:
            ensure_lineage_tables(store)
            ensure_explore_tables(store)
            a = store.create_agent(dict(name='Paired light', target_entity='light.example',
                target_property='power', min_value=0, max_value=1, input_entities=['sensor.radar']))
            with store.conn() as c:
                c.execute("UPDATE agents SET mode='shadow' WHERE id=?", (a['id'],))
            _ensure_root(store, a['id'], generation_number=0)
            with store.conn() as c:
                c.execute('CREATE TABLE benchmark_shadow_pool(agent_id TEXT,entity_id TEXT, PRIMARY KEY(agent_id,entity_id))')
                c.executemany('INSERT INTO benchmark_shadow_pool VALUES(?,?)',
                              [(a['id'], f'sensor.context_{i}') for i in range(96)])
            outputs = []
            def process(*_):
                # Actual helper reads seen in the user's trace: generation before
                # and after Live, Explore table check + active session query.
                before = _generation(store, agent_id=a['id'])['lifecycle_state']
                explore = _active_free_for_live(store, a['id'])
                after = _generation(store, agent_id=a['id'])['lifecycle_state']
                counts = []
                with store.background_sqlite(), store.connection_session():
                    for _ in range(shadow_queries):
                        with store.conn() as c:
                            counts.append(c.execute(
                                'SELECT count(*) FROM (SELECT 1 FROM benchmark_shadow_pool WHERE agent_id=? LIMIT 96)',
                                (a['id'],)).fetchone()[0])
                outputs.append((before, explore, after, counts))
            r = Engine.__new__(Engine)
            r.process_agent = process
            r._inference_tls = threading.local()
            r.stop_event = threading.Event()
            r.wake_event = threading.Event()
            r.state_revision = 1
            snapshot = ({}, 1, {}, 0)
            # Frozen 0.14.170 scope: the outer pipeline opens no session;
            # only the existing nested Shadow block owns a connection.
            dispatch_store = store if mode == 'current' else SimpleNamespace(
                connection_session=nullcontext, event=store.event)
            samples = []
            with patch.object(engine_module, 'STORE', dispatch_store):
                process()  # Warm both fixtures identically, outside timing.
                outputs.clear()
                for _ in range(passes):
                    started = time.perf_counter()
                    r.process_target([a], {'sensor.radar'}, snapshot)
                    samples.append((time.perf_counter() - started) * 1000.)
                assert len(outputs) == passes
                assert outputs == [('live', None, 'live', [96] * shadow_queries)] * passes
                with store.conn() as c:
                    assert not list(c.execute("SELECT id FROM events WHERE kind='agent_error'"))
                # Measure connection count separately: mock overhead is not timed.
                real_connect = sqlite3.connect
                connections = 0
                owner = threading.get_ident()
                def measured_connect(*args, **kwargs):
                    nonlocal connections
                    # Full-suite fixtures may have unrelated background writers.
                    if threading.get_ident() == owner and Path(args[0]) == Path(store.path):
                        connections += 1
                    return real_connect(*args, **kwargs)
                with patch('storage.sqlite3.connect', side_effect=measured_connect):
                    r.process_target([a], {'sensor.radar'}, snapshot)
                expected = 1 if mode == 'current' else 5
                assert connections == expected, connections
            digest = hashlib.sha256(json.dumps(outputs, sort_keys=True).encode()).hexdigest()
            return dict(median_ms=statistics.median(samples), total_ms=sum(samples),
                        connections_per_decision=expected, result_sha256=digest)
        finally:
            store.stop_wal_keeper()


def run(passes=128, shadow_queries=32):
    series = []
    for repeat in range(3):
        result = {}
        for mode in (('previous', 'current') if repeat % 2 == 0 else ('current', 'previous')):
            result[mode] = run_mode(mode, passes, shadow_queries)
        assert result['previous']['result_sha256'] == result['current']['result_sha256']
        series.append(dict(**result, total_speedup=result['previous']['total_ms'] / result['current']['total_ms']))
    return dict(synthetic=True, passes=passes, shadow_queries=shadow_queries,
                same_fresh_sql_and_every_result=True, includes_connection_setup_and_close=True,
                series=series, limit='SQL component with actual Engine dispatcher and Candidate/Explore helpers; excludes model validation, features, HA, concurrent checkpoint I/O. Not an HA latency forecast.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--passes', type=int, default=128)
    parser.add_argument('--shadow-queries', type=int, default=32)
    parser.add_argument('--compact', action='store_true')
    args = parser.parse_args()
    if args.passes < 1 or args.shadow_queries < 1:
        parser.error('passes and shadow queries must be positive')
    print(json.dumps(run(args.passes, args.shadow_queries), indent=None if args.compact else 2))
