"""0.14.136 queue-owned GET routes use ExplicitRouteRegistry."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

from support import ROOT


SRC = ROOT / "adaptive_ai" / "src"


class QueueReadRouteSourceContractTests(unittest.TestCase):
    def test_queue_main_no_longer_wraps_handler_get(self):
        source = (SRC / "queue_main.py").read_text(encoding="utf-8")
        self.assertNotIn("_original_do_get", source)
        self.assertNotIn("core.Handler.do_GET =", source)
        self.assertNotIn("def do_get(", source)
        self.assertIn("def register_read_routes(registry):", source)
        self.assertIn("registry = install_dispatch(core)", source)
        self.assertLess(
            source.index("TRAINING_QUEUE.start()"),
            source.index("register_read_routes(registry)"),
        )

    def test_explicit_queue_routes_dispatch_in_isolated_runtime(self):
        script = r'''
from types import SimpleNamespace

import queue_main
from runtime_http import ExplicitRouteRegistry


class Store:
    def get_agent_config(self, agent_id):
        if agent_id == "agent-1":
            return {"id": agent_id, "name": "Agent 1"}
        return None


class Queue:
    def status_for(self, agent_id):
        return {"state": "queued", "position": 2, "agent_id": agent_id}


class Teaching:
    def status(self, agent_id):
        return {"agent_id": agent_id, "state": "ready"}


class Http:
    def __init__(self, path):
        self.path = path
        self.result = None

    def require_trusted_client(self):
        return True

    def require_runtime(self):
        return True

    def static(self, name, content_type):
        self.result = ("static", name, content_type)
        return self.result

    def send_json(self, status, payload):
        self.result = ("json", status, payload)
        return self.result


queue_main.core.STORE = Store()
queue_main.core.HISTORY = SimpleNamespace(status=lambda: {"phase": "idle"})
queue_main.core.ENGINE = SimpleNamespace(rl_teaching=Teaching())
queue_main.TRAINING_QUEUE = Queue()

registry = ExplicitRouteRegistry()
queue_main.register_read_routes(registry)

names = [row["name"] for row in registry.routes("GET")]
assert names == [
    "queue.static",
    "queue.teach_rl.history",
    "queue.teach_rl.point",
    "queue.teach_rl.status",
    "queue.agents",
], names

static = Http("/queue.js")
assert registry.dispatch("GET", static) is True
assert static.result == (
    "static", "queue.js", "application/javascript; charset=utf-8"
), static.result

status = Http("/api/agents/agent-1/teach-rl-status")
assert registry.dispatch("GET", status) is True
kind, code, payload = status.result
assert kind == "json" and code == 200, status.result
assert payload["agent_id"] == "agent-1", payload
assert payload["training_queue"]["state"] == "queued", payload
assert payload["history"]["phase"] == "idle", payload

missing = Http("/api/agents/missing/teach-rl-status")
assert registry.dispatch("GET", missing) is True
assert missing.result == ("json", 404, {"error": "agent not found"}), missing.result
'''
        env = dict(os.environ)
        pythonpath = str(SRC)
        if env.get("PYTHONPATH"):
            pythonpath += os.pathsep + env["PYTHONPATH"]
        env["PYTHONPATH"] = pythonpath
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
