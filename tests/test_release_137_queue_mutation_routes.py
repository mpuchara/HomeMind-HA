"""0.14.137 queue mutation routes use ExplicitRouteRegistry + FALLTHROUGH."""
import os
import subprocess
import sys
import unittest

from support import ROOT
from runtime_http import ExplicitRouteRegistry, FALLTHROUGH


SRC = ROOT / "adaptive_ai" / "src"


class ExplicitFallthroughContractTests(unittest.TestCase):
    def test_fallthrough_delegates_to_captured_handler_without_lower_route(self):
        calls = []
        registry = ExplicitRouteRegistry()

        class Http:
            path = "/api/agents/a"

            def require_trusted_client(self):
                return True

            def require_runtime(self):
                return True

        registry.register(
            "PATCH",
            "native.high",
            r"^/other$",
            lambda http, params: calls.append("high"),
            require_trusted=False,
            require_runtime=False,
            priority=100,
        )
        registry.register(
            "PATCH",
            "queue.guard",
            r"^/api/agents/(?P<agent_id>[^/]+)$",
            lambda http, params: FALLTHROUGH,
            require_trusted=False,
            require_runtime=False,
            priority=-10000,
        )
        registry.register(
            "PATCH",
            "should.not.run",
            r"^/api/agents/(?P<agent_id>[^/]+)$",
            lambda http, params: calls.append("lower"),
            require_trusted=False,
            require_runtime=False,
            priority=-20000,
        )

        self.assertFalse(registry.dispatch("PATCH", Http()))
        self.assertEqual(calls, [])


class QueueMutationRouteSourceContractTests(unittest.TestCase):
    def test_queue_main_owns_no_handler_do_methods(self):
        source = (SRC / "queue_main.py").read_text(encoding="utf-8")
        for token in (
            "_original_do_post",
            "_original_do_patch",
            "_original_do_delete",
            "core.Handler.do_POST =",
            "core.Handler.do_PATCH =",
            "core.Handler.do_DELETE =",
            "def do_post(",
            "def do_patch(",
            "def do_delete(",
        ):
            self.assertNotIn(token, source)

        self.assertIn("def register_mutation_routes(registry):", source)
        self.assertLess(
            source.index("register_mutation_routes(registry)"),
            source.index("_original_initialize_runtime()"),
        )
        self.assertIn("FALLTHROUGH", source)

    def test_mutation_routes_preserve_queue_and_guard_semantics_in_isolation(self):
        script = r'''
from types import SimpleNamespace

import queue_main
from runtime_http import ExplicitRouteRegistry


class Store:
    def __init__(self):
        self.agents = {
            "a": {
                "id": "a",
                "name": "Agent A",
                "training_state": "paused",
            }
        }

    def get_agent(self, agent_id):
        row = self.agents.get(agent_id)
        return dict(row) if row else None

    def get_agent_config(self, agent_id):
        return self.get_agent(agent_id)

    def get_model(self, agent_id):
        return {"version": 1} if agent_id in self.agents else None


class Teaching:
    def __init__(self):
        self.labels = []

    def status(self, agent_id):
        return {"agent_id": agent_id, "state": "ready"}

    def prepare_retrain(self, agent):
        return {"prepared": agent["id"]}

    def undo(self, agent):
        return {"undone": agent["id"]}

    def add_label(self, agent, desired, ts):
        row = {"agent_id": agent["id"], "desired": desired, "ts": ts}
        self.labels.append(row)
        return row


class Queue:
    def __init__(self):
        self.status = {}
        self.enqueued = []
        self.cancelled = []

    def status_for(self, agent_id):
        return self.status.get(agent_id)

    def enqueue(self, agent_id, **kwargs):
        row = {
            "agent_id": agent_id,
            "state": "queued",
            "position": 1,
            "ahead": 0,
            "rebuild": bool(kwargs.get("rebuild")),
            "rebuild_reason": kwargs.get("rebuild_reason"),
        }
        self.enqueued.append((agent_id, dict(kwargs)))
        return row

    def cancel(self, agent_id):
        self.cancelled.append(agent_id)


class Http:
    def __init__(self, path, payload=None):
        self.path = path
        self.payload = payload
        self.result = None
        self.trusted_checks = 0
        self.runtime_checks = 0

    def require_trusted_client(self):
        self.trusted_checks += 1
        return True

    def require_runtime(self):
        self.runtime_checks += 1
        return True

    def read_json(self):
        return self.payload

    def send_json(self, status, payload):
        self.result = (status, payload)
        return self.result


store = Store()
teaching = Teaching()
queue_main.core.STORE = store
queue_main.core.HISTORY = SimpleNamespace(status=lambda: {"phase": "idle"})
queue_main.core.ENGINE = SimpleNamespace(rl_teaching=teaching)
queue_main.train_request_decision = lambda agent, has_model: {
    "rebuild": False,
    "resumed": False,
    "rebuild_reason": None,
    "learning_path": "incremental_replay",
}

registry = ExplicitRouteRegistry()
queue_main.register_mutation_routes(registry)

names = {
    method: [row["name"] for row in registry.routes(method)]
    for method in ("POST", "PATCH", "DELETE")
}
assert names["POST"] == [
    "queue.teach_rl.add",
    "queue.teach_rl.undo",
    "queue.teach_rl.train",
    "queue.training.train",
    "queue.training.resume",
], names
assert names["PATCH"] == ["queue.training.patch_guard"], names
assert names["DELETE"] == [
    "queue.training.learning_rebuild",
    "queue.training.delete_guard",
], names

# Before TrainingQueue exists, queue-owned train/resume guards delegate to the
# captured compatibility Handler instead of fabricating a queue result.
queue_main.TRAINING_QUEUE = None
early_train = Http("/api/agents/a/train")
assert registry.dispatch("POST", early_train) is False
assert early_train.result is None

# Teach-RL was historically owned even before TrainingQueue became ready.
early_teach_train = Http("/api/agents/a/teach-rl-train")
assert registry.dispatch("POST", early_teach_train) is True
assert early_teach_train.result[0] == 502, early_teach_train.result
assert "Training queue is not ready" in early_teach_train.result[1]["error"]

queue = Queue()
queue_main.TRAINING_QUEUE = queue

train = Http("/api/agents/a/train")
assert registry.dispatch("POST", train) is True
assert train.result[0] == 202, train.result
assert queue.enqueued[-1][1]["reason"] == "training", queue.enqueued

add = Http(
    "/api/agents/a/teach-rl",
    {"sample_ts": 123.0, "desired_value": 1.0},
)
assert registry.dispatch("POST", add) is True
assert add.result == (
    200,
    {"agent_id": "a", "desired": 1.0, "ts": 123.0},
), add.result

queue.status["a"] = {"state": "queued", "position": 1}
patch_busy = Http("/api/agents/a")
assert registry.dispatch("PATCH", patch_busy) is True
assert patch_busy.result[0] == 409, patch_busy.result

queue.status.clear()
patch_idle = Http("/api/agents/a")
assert registry.dispatch("PATCH", patch_idle) is False
assert patch_idle.result is None

queue.status["a"] = {"state": "active"}
delete_active = Http("/api/agents/a")
assert registry.dispatch("DELETE", delete_active) is True
assert delete_active.result[0] == 409, delete_active.result

queue.status["a"] = {"state": "queued"}
delete_queued = Http("/api/agents/a")
assert registry.dispatch("DELETE", delete_queued) is False
assert queue.cancelled[-1] == "a", queue.cancelled

queue.status.clear()
rebuild = Http("/api/agents/a/learning")
assert registry.dispatch("DELETE", rebuild) is True
assert rebuild.result[0] == 202, rebuild.result
assert queue.enqueued[-1][1]["reason"] == "full_rebuild", queue.enqueued
assert queue.enqueued[-1][1]["rebuild_reason"] == "explicit_manual_rebuild", queue.enqueued
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
