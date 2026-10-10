"""Frozen 0.14.171 versus target-owned retries; actual dispatch and SQLite writes."""
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
import argparse
import json
import os
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'adaptive_ai/src'))
sys.path.insert(0, str(ROOT))
_scratch = tempfile.TemporaryDirectory(prefix='homemind-target-retries-')
os.environ['ADAPTIVE_AI_DATA'] = _scratch.name
import engine as engine_module
from engine import Engine
from storage import Store
from tools.fixtures.inference_retry_014171 import previous_process


class ControlledWorkers:
    """Deterministically overlap target workers; each completion runs actual code."""
    def __init__(self):
        self.tasks = []

    def submit(self, fn, *args):
        future = Future()
        self.tasks.append((future, fn, args))
        return future

    def complete_next(self):
        task = next((t for t in self.tasks if not t[0].done()), None)
        if task is None:
            return False
        future, fn, args = task
        try:
            fn(*args)
        except Exception as exc:
            future.set_exception(exc)
            raise
        else:
            future.set_result(None)
        return True


def fixture(store, mode='current', targets=2):
    r = Engine.__new__(Engine)
    r.lock = threading.RLock()
    r._inference_tls = threading.local()
    r.stop_event = threading.Event()
    r.wake_event = threading.Event()
    r.in_flight = {}
    r.pending_target_changes = {}
    r.ready_target_changes = {}
    r.resubmit_targets = set()
    r.dirty_entities = set()
    r.control_workers = ControlledWorkers()
    r.context = SimpleNamespace(home=SimpleNamespace(revision=0))
    r.state_revision = 0
    r.state_map = {'binary_sensor.shared': {'state': 'off'}}
    r.entity_revisions = {'binary_sensor.shared': 0}
    r.entity_event_received_perf = {'binary_sensor.shared': time.perf_counter()}
    r.agent_configs = {str(i): dict(id=str(i), target_entity=f'light.target_{i}', mode='shadow') for i in range(targets)}
    r.active_agents_by_target = {a['target_entity']: [aid] for aid, a in r.agent_configs.items()}
    r.dependency_agents = {'binary_sensor.shared': set(r.agent_configs)}
    r.dependency_agents.update({a['target_entity']: {aid} for aid, a in r.agent_configs.items()})
    r._refresh_agent_index = lambda *a, **kw: None
    r._active_agents_for_changes = lambda changed: [a for aid, a in r.agent_configs.items()
        if not changed or any(aid in r.dependency_agents.get(eid, ()) for eid in changed)]
    r.process = (previous_process if mode == 'previous' else Engine.process).__get__(r, Engine)
    r.outputs = []
    def record(a, states, changed):
        result = (a['id'], states['binary_sensor.shared']['state'])
        r.outputs.append(result)
        with store.conn() as c:
            c.execute('INSERT INTO app_meta(key,value) VALUES(?,?)',
                      (f'decision:{len(r.outputs)}', json.dumps(result)))
    r.process_agent = record
    return r


def drain(r):
    dirty = set(r.dirty_entities)
    r.dirty_entities.clear()
    r.wake_event.clear()
    if dirty or r.ready_target_changes:
        r.process(r.state_map, dirty)


def run_mode(mode, completions=64, targets=2):
    with tempfile.TemporaryDirectory(prefix='target-retries-bench-') as directory:
        store = Store(Path(directory) / 'test.db')
        store.start_wal_keeper()
        try:
            r = fixture(store, mode, targets)
            started = time.perf_counter()
            with patch.object(engine_module, 'STORE', store):
                r.process(r.state_map, {'binary_sensor.shared'})
                # A second real event arrives while every target is still busy.
                r.state_map['binary_sensor.shared'] = {'state': 'on'}
                r.state_revision = 1
                r.entity_revisions['binary_sensor.shared'] = 1
                r.entity_event_received_perf['binary_sensor.shared'] = time.perf_counter()
                r.process(r.state_map, {'binary_sensor.shared'})
                for _ in range(completions):
                    if not r.control_workers.complete_next():
                        break
                    drain(r)
            elapsed = (time.perf_counter() - started) * 1000.
            final = {}
            for aid, value in r.outputs:
                final[aid] = value
            assert final == {str(i): 'on' for i in range(targets)}
            assert all({value for owner, value in r.outputs if owner == str(i)} == {'off', 'on'} for i in range(targets))
            if mode == 'current':
                assert len(r.outputs) == targets * 2
                assert not r.ready_target_changes and not r.pending_target_changes
                assert all(t[0].done() for t in r.control_workers.tasks)
            else:
                assert len(r.outputs) == completions
                assert any(not t[0].done() for t in r.control_workers.tasks)
            with store.conn() as c:
                durable = [json.loads(row[0]) for row in c.execute(
                    "SELECT value FROM app_meta WHERE key LIKE 'decision:%' ORDER BY CAST(substr(key,10) AS INTEGER)")]
                assert durable == [list(result) for result in r.outputs]
                assert not list(c.execute("SELECT id FROM events WHERE kind='agent_error'"))
            return dict(total_ms=elapsed, decisions=len(r.outputs), queued=len(r.control_workers.tasks),
                unresolved=sum(not task[0].done() for task in r.control_workers.tasks), final_desired=final,
                durable_decisions=len(durable), every_real_event_delivered=True)
        finally:
            store.stop_wal_keeper()


def run(completions=64, targets=2):
    assert targets >= 2 and completions >= targets * 2
    series = []
    for repeat in range(3):
        result = {}
        for mode in (('previous', 'current') if repeat % 2 == 0 else ('current', 'previous')):
            result[mode] = run_mode(mode, completions, targets)
        assert result['previous']['final_desired'] == result['current']['final_desired']
        series.append(result)
    return dict(synthetic=True, genuine_events=2, targets=targets, completion_limit=completions,
                same_final_desired=True, every_real_event_delivered_current=True, series=series,
                limit='Deterministic overlapping workers; actual Engine dispatcher, process_target and SQLite writes. Excludes policy CPU, HA and timers (none due during this short scenario). Not an HA latency forecast.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--completions', type=int, default=64)
    parser.add_argument('--targets', type=int, default=2)
    parser.add_argument('--compact', action='store_true')
    args = parser.parse_args()
    if args.targets < 2 or args.completions < args.targets * 2:
        parser.error('targets >= 2 and completions >= 2*targets required')
    print(json.dumps(run(args.completions, args.targets), indent=None if args.compact else 2))
