"""UI transport must work before runtime composition, with no legacy action fallback."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from support import ROOT


def referenced_scripts():
    text = (ROOT / 'adaptive_ai/src/static/index.html').read_text(encoding='utf-8')
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
    return re.findall(r'<script src="([^?]+)\?', text)


class UiAssets160Tests(unittest.TestCase):
    def test_every_index_script_loads_before_runtime_readiness(self):
        import main
        for name in referenced_scripts():
            with self.subTest(asset=name):
                handler = main.Handler.__new__(main.Handler)
                handler.path = '/' + name + '?v=startup'
                handler.require_trusted_client = lambda: True
                handler.require_runtime = lambda: self.fail('A UI script entered the backend gate')
                replies = []
                handler.send_bytes = lambda *reply: replies.append(reply)
                handler.send_json = lambda *reply: self.fail(str(reply))
                main.Handler.do_GET(handler)
                self.assertEqual(replies, [(200,
                    (ROOT / 'adaptive_ai/src/static' / name).read_bytes(),
                    'application/javascript; charset=utf-8')])

    def test_asset_catalog_matches_index_without_broad_file_exposure(self):
        import main
        self.assertEqual(set(referenced_scripts()), set(main.UI_SCRIPT_ASSETS))
        self.assertNotIn('settings.py', main.UI_SCRIPT_ASSETS)

    def test_asset_transport_keeps_trusted_client_boundary(self):
        import main
        handler = main.Handler.__new__(main.Handler)
        handler.path = '/agent_workflow_ui.js'
        handler.require_trusted_client = lambda: False
        handler.static = lambda *_: self.fail('Untrusted client reached static transport')
        main.Handler.do_GET(handler)

    def test_unknown_assets_and_path_traversal_cannot_use_early_catalog(self):
        import main
        for path in ['/settings.py', '/unknown.js', '/../app.js', '//app.js', '/%2e%2e/app.js']:
            with self.subTest(path=path):
                handler = main.Handler.__new__(main.Handler)
                handler.path = path
                handler.require_trusted_client = lambda: True
                handler.require_runtime = lambda: False
                handler.static = lambda *_: self.fail('Unexpected file access')
                main.Handler.do_GET(handler)

    def test_shipped_entrypoint_serves_full_ui_with_initialization_suspended(self):
        # Run the real entrypoint/Handler on a private random port. Keeping initialization
        # suspended reproduces the original startup race rather than relying on fast boot.
        child = r'''
import json, os, threading
from pathlib import Path
import trial_queue_main as entry
original = entry.core.ThreadingHTTPServer
def server(_address, handler):
    instance = original(('127.0.0.1', 0), handler)
    Path(os.environ['UI_TEST_PORT_FILE']).write_text(str(instance.server_port))
    return instance
entry.core.ThreadingHTTPServer = server
entry.core.run_initialize_runtime = lambda: threading.Event().wait()
entry.core.main()
'''
        with tempfile.TemporaryDirectory() as folder:
            port_file = Path(folder) / 'port.txt'
            env = dict(os.environ, ADAPTIVE_AI_DATA=folder, UI_TEST_PORT_FILE=str(port_file))
            env.pop('SUPERVISOR_TOKEN', None)
            with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as output:
                proc = subprocess.Popen([sys.executable, '-u', '-c', child],
                    cwd=ROOT / 'adaptive_ai/src', env=env, stdout=output, stderr=output)
                try:
                    deadline = time.monotonic() + 15
                    while not port_file.exists():
                        if proc.poll() is not None or time.monotonic() > deadline:
                            output.seek(0)
                            self.fail(output.read())
                        time.sleep(.02)
                    base = 'http://127.0.0.1:' + port_file.read_text()
                    with urllib.request.urlopen(base + '/health', timeout=3) as response:
                        self.assertFalse(json.load(response)['ready'])
                    for name in referenced_scripts():
                        with self.subTest(asset=name), urllib.request.urlopen(base + '/' + name + '?v=startup', timeout=3) as response:
                            self.assertEqual(response.status, 200)
                            self.assertTrue(response.headers['Content-Type'].startswith('application/javascript'))
                            self.assertEqual(response.read(), (ROOT / 'adaptive_ai/src/static' / name).read_bytes())
                finally:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)

