"""Fresh-process tests of run.sh's actual final entrypoint, with no HA/network workers."""
import os
import subprocess
import sys
import tempfile
import unittest
from support import ROOT


class PackagedStartupTests(unittest.TestCase):
    def run_isolated(self, script):
        with tempfile.TemporaryDirectory() as data:
            env = dict(os.environ, ADAPTIVE_AI_DATA=data,
                       PYTHONPATH=str(ROOT/'adaptive_ai/src'), PYTHONIOENCODING='utf-8')
            env.pop('SUPERVISOR_TOKEN', None)
            env.pop('HA_TOKEN', None)
            result = subprocess.run([sys.executable, '-c', script], cwd=ROOT,
                                    env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_final_entrypoint_import_is_http_lightweight_and_does_not_touch_database(self):
        self.run_isolated('''
import sys
import trial_queue_main as entry
assert entry.core.STORE is None
for name in (
    'storage', 'engine', 'policy', 'manual_context_learning',
    'agent_candidate_preference_metrics', 'episode_evaluator', 'observation_contract',
    'cold_start_drift', 'performance_f22', 'trial_knowledge',
):
    assert name not in sys.modules, (name, sorted(x for x in sys.modules if x == name))
assert entry.RUNTIME_COMPOSITION_ROOT.descriptor()['state'] == 'bound_not_prepared'
''')

    def test_packaged_entrypoint_binds_then_initializes_all_extensions(self):
        self.run_isolated('''
import http.server
import runpy
import threading
from unittest.mock import Mock, patch

order = []
server = Mock()
server.serve_forever.side_effect = KeyboardInterrupt

def bind(*args, **kwargs):
    import main
    assert main.STORE is None
    order.append('http')
    return server

def start(thread):
    import main
    if thread.name == 'adaptive-ai-runtime-init':
        assert order == ['http']
        order.append('database_and_extensions')
        thread._target()
    else:
        engine = main.ENGINE
        assert engine._manual_feedback_equivalence_installed
        assert engine._manual_lifecycle_events_installed
        assert main._FAST_RUNTIME_INSTALLED
        order.append(thread.name)

with patch.object(http.server, 'ThreadingHTTPServer', side_effect=bind), \
     patch.object(threading.Thread, 'start', start):
    runpy.run_path('adaptive_ai/src/trial_queue_main.py', run_name='__main__')

import main, engine, executor, settings
assert main.startup_snapshot()['ready'], main.startup_snapshot()
assert main.startup_snapshot()['error'] is None
assert order[:2] == ['http', 'database_and_extensions']
assert 'adaptive-ai-engine' in order
assert 'adaptive-ai-training-queue' in order
with main.STORE.conn() as db:
    assert db.execute("SELECT name FROM sqlite_master WHERE name='manual_context_feedback'").fetchone()
assert main.APP_VERSION == engine.APP_VERSION == settings.APP_VERSION
from pathlib import Path
assert ('version: "'+main.APP_VERSION+'"') in Path('adaptive_ai/config.yaml').read_text()
vacuum = {'entity_id':'vacuum.test','state':'docked','attributes':{'supported_features':8192|16}}
assert executor.target_call('vacuum.test', 'power', 1, vacuum)[1] == 'start'
server.server_close.assert_called_once()
''')

    def test_extension_failure_is_reported_by_status_instead_of_killing_http(self):
        self.run_isolated('''
from unittest.mock import patch
import trial_queue_main as entry
core = entry.core
with patch.object(core, 'prepare_runtime_extensions', side_effect=RuntimeError('extension failure')):
    core.run_initialize_runtime()
handler = core.Handler.__new__(core.Handler)
payload = handler.status_payload()
assert payload['startup']['error'] == 'extension failure', payload
assert not payload['startup']['ready']
assert core.ENGINE is None
# Errors thrown by wrappers outside initialize_runtime's own try must be visible too.
with patch.object(core, 'initialize_runtime', side_effect=RuntimeError('queue failure')):
    core.run_initialize_runtime()
assert core.startup_snapshot()['error'] == 'queue failure'
''')

    def test_http_remains_responsive_while_final_extensions_initialize(self):
        self.run_isolated('''
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import trial_queue_main as entry
import training_queue

core = entry.core
entered, release = threading.Event(), threading.Event()
original = core.prepare_runtime_extensions
def prepare():
    entered.set()
    assert release.wait(5)
    original()
    # Import AFTER adapters, then prevent all HA/background workers in this fixture.
    import engine, history
    engine.Engine.start = lambda self: None
    engine.HAEventStream.start = lambda self: None
    history.HistoryManager.start = lambda self: None
    training_queue.TrainingQueue.start = lambda self: None
core.prepare_runtime_extensions = prepare
server = ThreadingHTTPServer(('127.0.0.1', 0), core.Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
runtime = threading.Thread(target=core.run_initialize_runtime, daemon=True)
runtime.start()
url = 'http://127.0.0.1:' + str(server.server_port)
def get(path):
    with urllib.request.urlopen(url+path, timeout=2) as response:
        return response.read()
try:
    assert entered.wait(5)
    assert not json.loads(get('/health'))['ready']
    assert json.loads(get('/api/status'))['startup']['state'] == 'loading_runtime'
    assert b'window.wrongDecision' in get('/manual_feedback.js?v=test')
    release.set()
    runtime.join(10)
    assert not runtime.is_alive()
    assert json.loads(get('/health'))['ready'], core.startup_snapshot()
    assert json.loads(get('/api/agents')) == []
finally:
    release.set()
    core.shutdown_runtime()
    server.shutdown()
    server.server_close()
    thread.join(2)
''')
