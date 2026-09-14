"""Local teaching UI fixture with real SQLite/policy and simulated device commands.

No HA connection or background control workers are started. Never uses user data.
"""
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import parse_qs
import tempfile
from types import SimpleNamespace
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
DATA = tempfile.TemporaryDirectory(prefix="homemind-teaching-preview-")
os.environ["ADAPTIVE_AI_DATA"] = DATA.name
sys.path.insert(0, str(ROOT / "adaptive_ai/src"))
from storage import STORE
from engine import Engine
from manual_context_learning import install as install_context
from manual_feedback import apply_ui_correction, teach_desired
from settings import APP_VERSION

ENGINE = Engine()
CORE = SimpleNamespace(ENGINE=ENGINE, STORE=STORE)
install_context(CORE)
AGENT = STORE.create_agent(dict(name="Schody · test Shadow", mode="shadow", enabled=True,
    target_entity="light.preview", target_property="power", min_value=0, max_value=1,
    deadband=.5, input_entities=["*"], confidence_threshold=.78, action_interval=1))
STORE.set_training_state(AGENT["id"], "qualified", score=1, samples=80, detail={'balanced':True,'counts':{'samples':80,'correct':80,'per_action':{'0':{'samples':40,'correct':40},'1':{'samples':40,'correct':40}}}})
ENGINE.state_map = {
    "light.preview": dict(entity_id="light.preview", state="off", attributes={}),
    "binary_sensor.motion": dict(entity_id="binary_sensor.motion", state="on", attributes={"device_class": "motion"}),
}
ENGINE.runtime[AGENT["id"]] = {"last_prediction": 0, "last_confidence": .8}
ENGINE.context.configure(ENGINE.state_map)
start=time.time()-86400
STORE.archive_batch([(eid,start+i*600,'on' if (i%6 in (1,2)) else 'off',{'device_class':'motion'} if eid.startswith('binary_sensor') else {},None,'preview') for i in range(145) for eid in ('light.preview','binary_sensor.motion')])


def simulated_service(domain, service, data):
    assert domain == "light" and data["entity_id"] == "light.preview"
    ENGINE.state_map["light.preview"]["state"] = "on" if service == "turn_on" else "off"
    return []


ENGINE.executor._service = simulated_service


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "adaptive_ai/src/static"), **kwargs)

    def send_json(self, code, value):
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.partition("?")[0]
        params=parse_qs(self.path.partition('?')[2])
        if path == '/api/live':
            return self.send_json(200, {'ts':time.time(),'agents':[{'id':AGENT['id'],'current_value':int(ENGINE.state_map['light.preview']['state']=='on'), 'last_prediction':ENGINE.runtime[AGENT['id']].get('last_prediction'), 'teaching_id':ENGINE.runtime[AGENT['id']].get('teaching_id')}]})
        if path.endswith('/teaching-history') or path.endswith('/teaching-point'):
            try:
                a=STORE.get_agent_config(AGENT['id'])
                data=ENGINE.teaching.point(ENGINE,a,params['ts'][0]) if path.endswith('/teaching-point') else ENGINE.teaching.history(ENGINE,a,params['start'][0],params['end'][0])
                return self.send_json(200,data)
            except (ValueError,KeyError) as exc:
                return self.send_json(400,{'error':str(exc)})
        if path == "/api/status":
            return self.send_json(200, dict(version=APP_VERSION + " LOCAL PREVIEW", startup={"ready": True},
                agent_count=1, history={}, ha_connected=False, home_intelligence={}))
        if path == "/api/agents":
            agent = STORE.get_agent_config(AGENT["id"])
            return self.send_json(200, [agent | {"runtime": ENGINE.runtime[AGENT["id"]] | {
                "current_value": int(ENGINE.state_map["light.preview"]["state"] == "on")}}])
        if path.startswith("/api/"):
            return self.send_json(200, [])
        return super().do_GET()

    def do_POST(self):
        operations = {'teaching':True,'undo-teaching':True}
        prefix = "/api/agents/" + AGENT["id"] + "/"
        if not self.path.startswith(prefix) or self.path[len(prefix):] not in operations:
            return self.send_json(404, {"error": "Only the two teaching operations are supported in this fixture"})
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            a=STORE.get_agent_config(AGENT['id'])
            result = ENGINE.teaching.undo(ENGINE,a) if self.path.endswith('/undo-teaching') else ENGINE.teaching.teach(ENGINE,a,payload.get('desired_value'),payload.get('sample_ts'))
            if a['mode']=='control':ENGINE.process_agent(a,ENGINE.state_map)
            return self.send_json(200, result)
        except ValueError as exc:
            return self.send_json(400, {"error": str(exc)})

    def do_PATCH(self):
        payload=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
        a=STORE.update_agent(AGENT['id'],payload)
        return self.send_json(200,a)

    def log_message(self,*args):
        pass

if __name__ == "__main__":
    print("Teaching fixture: http://127.0.0.1:8874 — simulated device only", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8874), Handler).serve_forever()
