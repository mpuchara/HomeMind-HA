"""Controlled writer/event/registry comparison. No Home Assistant or device claims."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch


def run(root):
    sys.path[:0] = [str(root / 'adaptive_ai/src'), str(root / 'tests')]
    import support
    import test_context_tournament_events as event_fixture
    from context_tournament import ContextTournament
    from context_tournament_events import install_context_events
    from context_engine import ContextEngine
    from engine import Engine
    from settings import OPTIONS
    from storage import Store

    result = {}
    with tempfile.TemporaryDirectory() as temp:
        store = Store(Path(temp) / 'writer.db')
        connections = []
        native = sqlite3.connect
        def connect(*args, **kwargs):
            if threading.current_thread().name == 'adaptive-ai-context-shadow-writer':
                connections.append(1)
            return native(*args, **kwargs)
        engine = SimpleNamespace(stop_event=threading.Event())
        started = time.perf_counter()
        with patch('storage.sqlite3.connect', side_effect=connect):
            service = ContextTournament(store, engine)
            try:
                for i in range(1, 21):
                    service._save_shadow_model('a', 'sensor', dict(version=1, action_count=2,
                        samples=i, counts={'x': [i, 0]}))
                    service._shadow_flush_event.set()
                    deadline = time.monotonic() + 10.
                    while service.shadow_persistence_snapshot()['flushed'] < i:
                        if time.monotonic() >= deadline:
                            raise RuntimeError('Writer did not commit its batch')
                        time.sleep(.001)
            finally:
                engine.stop_event.set()
                service._shadow_flush_event.set()
                service._shadow_writer_thread.join(10.)
        if service._shadow_writer_thread.is_alive():
            raise RuntimeError('Writer did not stop')
        elapsed = (time.perf_counter() - started) * 1000.
        with store.conn() as c:
            rows = [tuple(r) for r in c.execute(
                'SELECT agent_id,challenger_entity,model_json FROM context_tournament_shadow ORDER BY agent_id,challenger_entity')]
        result['writer'] = dict(connections=len(connections), batches=20, elapsed_ms=elapsed,
            final_sha=hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest())

        engine = Engine.__new__(Engine)
        engine.lock = threading.RLock()
        engine.context = ContextEngine(OPTIONS, store)
        engine.state_map = {}
        engine.entity_registry = {}
        engine._entity_registry_raw = None
        engine.registry_refresh_stats = dict(duplicates=0, entity_updates=0, max_duration_ms=0.)
        engine.agent_index_at, engine.agent_index_revision = 1., 1
        payload = [dict(entity_id=f'sensor.fixture_{i}', area_id='room', modified_at=1.) for i in range(1333)]
        engine._registry_update('entity', payload)
        revision = engine.context.registry_revision
        started = time.perf_counter()
        for i in range(10):
            payload.reverse()
            payload[0]['modified_at'] = i + 2.
            engine._registry_update('entity', payload)
        result['registry'] = dict(metadata_refreshes=10,
            topology_rebuilds=engine.context.registry_revision - revision,
            elapsed_ms=(time.perf_counter() - started) * 1000.,
            entities=len(engine.entity_registry), mapping_sha=hashlib.sha256(
                json.dumps(engine.context.mapping, sort_keys=True).encode()).hexdigest())

    service = event_fixture.FakeService()
    try:
        source = copy.deepcopy(service.models['sensor.new'])
        for i in range(4):
            service.models[f'sensor.{i}'] = copy.deepcopy(source)
        service.current['challenger_features'] = [f'sensor.{i}' for i in range(4)]
        install_context_events(service)
        service.observe_shadow(service.agent)
        trace = []
        service.store.db.set_trace_callback(trace.append)
        for _ in range(20):
            service.observe_shadow(service.agent)
        reads = [q for q in trace if q.startswith('SELECT') and 'context_tournament_event_state' in q]
        result['event_state'] = dict(observations=20, reads=len(reads),
            events_sha=hashlib.sha256(json.dumps(service.store.events, sort_keys=True).encode()).hexdigest(),
            models_sha=hashlib.sha256(json.dumps(service.models, sort_keys=True).encode()).hexdigest())
    finally:
        service.store.db.close()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path)
    parser.add_argument('--baseline-root', type=Path)
    parser.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args()
    if args.root:
        print(json.dumps(run(args.root.resolve())))
        return
    if not args.baseline_root:
        parser.error('--baseline-root is required')
    root = Path(__file__).resolve().parents[1]
    pairs = []
    for _ in range(args.rounds):
        pair = {}
        for name, folder in [('before', args.baseline_root), ('after', root)]:
            raw = subprocess.check_output([sys.executable, __file__, '--root', str(folder)], text=True)
            pair[name] = json.loads(raw)
        for field, hashes in [('writer', ['final_sha']), ('registry', ['mapping_sha']),
                              ('event_state', ['events_sha', 'models_sha'])]:
            for key in hashes:
                if pair['before'][field][key] != pair['after'][field][key]:
                    raise AssertionError(f'{field}/{key} parity mismatch')
        pairs.append(pair)
    print(json.dumps(dict(pairs=pairs, source_parity=True, device_latency_claim=False), indent=2))


if __name__ == '__main__':
    main()
