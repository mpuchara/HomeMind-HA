"""0.14.14+ regressions for the real shipped startup/initial-training path."""
import os
import subprocess
import sys
import tempfile
import threading
import unittest

from support import ROOT, agent


class Release014StartupTrainTests(unittest.TestCase):
    def run_isolated(self, script):
        with tempfile.TemporaryDirectory() as data:
            env = dict(os.environ, ADAPTIVE_AI_DATA=data,
                       PYTHONPATH=str(ROOT/'adaptive_ai/src'), PYTHONIOENCODING='utf-8')
            env.pop('SUPERVISOR_TOKEN', None)
            env.pop('HA_TOKEN', None)
            result = subprocess.run([sys.executable, '-c', script], cwd=ROOT,
                                    env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_partial_engine_can_never_make_startup_status_call_engine_status(self):
        self.run_isolated(r'''
import trial_queue_main as entry
core = entry.core
class ExplodingEngine:
    def status(self):
        raise AssertionError('ENGINE.status must not run before startup ready')
core.ENGINE = ExplodingEngine()
core.STORE = object()
core.AUTOMATION_KNOWLEDGE = object()
core.set_startup('building_runtime', 2, 'still building', ready=False)
handler = core.Handler.__new__(core.Handler)
payload = handler.status_payload()
assert payload['startup']['ready'] is False, payload
assert payload['history']['phase'] == 'starting', payload
# Internal installers historically use runtime_available() once the three core objects
# exist, before startup.ready. The release guard must not change that contract.
assert core.runtime_available() is True
assert core.startup_train_guard_contract['internal_runtime_available_semantics'] == 'preserved_for_extension_installers'
''')

    def test_half_built_runtime_is_not_http_ready(self):
        self.run_isolated(r'''
import trial_queue_main as entry
core = entry.core
core.ENGINE = object()
core.STORE = object()
core.AUTOMATION_KNOWLEDGE = object()
core.set_startup('building_runtime', 2, 'still building', ready=False)
handler = core.Handler.__new__(core.Handler)
sent = {}
handler.send_json = lambda code, payload: sent.update(code=code, payload=payload)
assert core.runtime_available() is True
assert handler.require_runtime() is False
assert sent['code'] == 503, sent
assert sent['payload']['error'] == 'runtime_starting', sent
core.set_startup('ready', 7, 'ready', ready=True)
assert handler.require_runtime() is True
''')

    def test_plain_train_claims_idle_slot_without_queue_worker_schedule(self):
        self.run_isolated(r'''
import threading
import trial_queue_main  # installs the final release guard
from telemetry import HEAVY_JOBS
from training_queue import TrainingQueue

for owner in ('discovery', 'home_bootstrap', 'agent:fresh'):
    HEAVY_JOBS.release(owner)

class Store:
    def __init__(self):
        self.row = {
            'id':'fresh','name':'Fresh','mode':'shadow','training_state':'waiting',
            'benchmark_score':None,'benchmark_samples':0,'benchmark_source':None,
            'benchmark_detail':{},
        }
        self.events=[]
    def get_agent(self, agent_id): return dict(self.row) if agent_id=='fresh' else None
    def set_training_state(self, agent_id, state, score=None, samples=0, source=None, detail=None):
        self.row['training_state']=state
    def event(self, *args): self.events.append(args)
    def meta_set(self, key, value): pass
class Executor:
    def release_control(self, agent, reason='training'): pass
class Engine:
    executor=Executor()
    rl_teaching=None
class History:
    def __init__(self, store):
        self.store=store; self.agent_jobs=set(); self.agent_jobs_lock=threading.RLock(); self.started=[]
    def _manual_lightweight_cycle(self, *args): return None
    def _fetch_history_resilient(self, *args, **kwargs): return []
    def _start(self, agent_id, rebuild):
        with self.agent_jobs_lock: self.agent_jobs.add(agent_id)
        self.store.row['training_state']='training'
        self.started.append((agent_id,rebuild))
        return True
    def request_agent_resume(self, agent_id): return self._start(agent_id,False)
    def request_agent_rebuild(self, agent_id): return self._start(agent_id,True)

store=Store(); history=History(store)
queue=TrainingQueue(history,store,Engine(),poll_seconds=.02)
# Deliberately DO NOT queue.start(). A user Train must still launch when the slot is idle.
result=queue.enqueue(
    'fresh', rebuild=True, reason='training', rebuild_reason='initial_model_build'
)
assert history.started == [('fresh',True)], history.started
assert result['state'] == 'active', result
assert result['rebuild_reason'] == 'initial_model_build', result
assert store.row['training_state'] == 'training', store.row
''')

    def test_final_entrypoint_installs_queue_before_history_discovery(self):
        self.run_isolated(r'''
import inspect
import trial_queue_main as entry
import startup_train_guard
source = inspect.getsource(startup_train_guard.install)
assert 'FIFO training queue ready before background discovery' in source
assert source.index('queue.start()') < source.index('return original_history_start(history_self')
assert getattr(entry.core, '_startup_train_guard_installed', False)
contract = entry.core.startup_train_guard_contract
assert contract['training_queue_order'] == 'before_history_discovery'
assert contract['explicit_train_idle_slot'] == 'immediate_admission_attempt'
''')

    def test_auto_discovered_agents_remain_waiting_for_manual_selection(self):
        self.run_isolated(r'''
from startup_train_guard import _install_initial_training_bridge

class Store:
    def __init__(self):
        self.events = []
    def event(self, *args):
        self.events.append(args)

class Queue:
    def __init__(self):
        self.calls = []
    def enqueue(self, agent_id, rebuild=False, reason="training"):
        self.calls.append((agent_id, rebuild, reason))
        raise AssertionError("discovery must not enqueue training")

class History:
    pass

store=Store(); queue=Queue(); history=History()
_install_initial_training_bridge(history, queue, store)
assert queue.calls == [], queue.calls
assert history.initial_training_enqueued == []
assert history._initial_training_bridge_installed is True
assert any(row[2] == "manual_initial_training_ready" for row in store.events), store.events
''')

    def test_frontend_fetch_guard_bounds_startup_reads_without_aborting_mutations(self):
        source = (ROOT/'adaptive_ai/src/static/home.js').read_text(encoding='utf-8')
        index = (ROOT/'adaptive_ai/src/static/index.html').read_text(encoding='utf-8')
        self.assertIn('window.__adaptiveAiFetchTimeoutGuard', source)
        self.assertIn('new AbortController()', source)
        self.assertIn("if(method!=='GET'&&method!=='HEAD')return nativeFetch(input,fetchInit);", source)
        self.assertIn('Adaptive AI read timeout', source)
        self.assertLess(index.index('home.js'), index.index('app.js'))


if __name__ == '__main__':
    unittest.main()
