"""Local UI fixture: fake agents, real experiment settings, no HA connection."""
import json
import os
from pathlib import Path
import sys
import tempfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
DATA = tempfile.TemporaryDirectory(prefix='homemind-preview-')
os.environ['ADAPTIVE_AI_DATA'] = DATA.name
sys.path.insert(0, str(ROOT/'adaptive_ai/src'))
from storage import STORE
from experiments import Experiments

EXPERIMENTS = Experiments(STORE)
AGENT = dict(id='preview', name='Kuchnia · światło testowe', target_entity='light.kitchen',
    target_property='power', min_value=0, max_value=1, training_state='qualified',
    mode='control', enabled=True, benchmark_score=.95, benchmark_samples=200,
    confidence_threshold=.78, action_interval=.25, deadband=.5)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT/'adaptive_ai/src/static'), **kwargs)

    def send_json(self, code, value):
        raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers(); self.wfile.write(raw)

    def do_GET(self):
        path = self.path.partition('?')[0]
        if path == '/api/status':
            return self.send_json(200, dict(version='0.10.0 PREVIEW', ha_connected=True, state_count=3,
                agent_count=1, realtime=dict(connected=True), history=dict(phase='manual_ready'),
                home_intelligence={}, telemetry={}))
        if path == '/api/agents':
            return self.send_json(200, [AGENT | dict(runtime=dict(current_value=0, last_prediction=0,
                last_confidence=.92, decision_state='hold', decision_reason='duplicate: desired value already set',
                experiments=EXPERIMENTS.status(AGENT['id'])))])
        if path == '/api/agents/preview/experiments':
            return self.send_json(200, EXPERIMENTS.status(AGENT['id']))
        if path.startswith('/api/'):
            return self.send_json(200, [])
        return super().do_GET()

    def do_POST(self):
        if self.path != '/api/agents/preview/experiments':
            return self.send_json(404, dict(error='Preview only supports experiment settings'))
        try:
            value = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            return self.send_json(200, EXPERIMENTS.configure(AGENT, value))
        except ValueError as exc:
            return self.send_json(400, dict(error=str(exc)))


if __name__ == '__main__':
    print('Local fixture: http://127.0.0.1:8873 (no Home Assistant services)', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 8873), Handler).serve_forever()
