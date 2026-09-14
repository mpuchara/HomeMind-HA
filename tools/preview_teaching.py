"""Local teaching UI fixture with real SQLite/policy and simulated device commands.

No HA connection or background control workers are started. Never uses user data.
"""
import json
import os
from pathlib import Path
import sys
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
STORE.set_training_state(AGENT["id"], "qualified")
ENGINE.state_map = {
    "light.preview": dict(entity_id="light.preview", state="off", attributes={}),
    "binary_sensor.motion": dict(entity_id="binary_sensor.motion", state="on", attributes={"device_class": "motion"}),
}
ENGINE.runtime[AGENT["id"]] = {"last_prediction": 0, "last_confidence": .8}
ENGINE.context.configure(ENGINE.state_map)


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
        operations = {"teach-desired": teach_desired, "manual-correction": apply_ui_correction}
        prefix = "/api/agents/" + AGENT["id"] + "/"
        if not self.path.startswith(prefix) or self.path[len(prefix):] not in operations:
            return self.send_json(404, {"error": "Only the two teaching operations are supported in this fixture"})
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            result = operations[self.path[len(prefix):]](CORE, STORE.get_agent_config(AGENT["id"]), payload.get("desired_value"))
            return self.send_json(200, result)
        except ValueError as exc:
            return self.send_json(400, {"error": str(exc)})


if __name__ == "__main__":
    print("Teaching fixture: http://127.0.0.1:8874 — simulated device only", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8874), Handler).serve_forever()
