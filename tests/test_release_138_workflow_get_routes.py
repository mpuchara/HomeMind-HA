"""0.14.138 Workflow/Correct/Explore/Confidence GET reads use ExplicitRouteRegistry."""
import unittest
from pathlib import Path

from support import ROOT
from runtime_http import ExplicitRouteRegistry
from agent_workflow_actions import register_read_routes as register_workflow_read_routes
from agent_explore import register_read_routes as register_explore_read_routes
from confidence_contract import register_read_routes as register_confidence_read_routes


SRC = ROOT / "adaptive_ai" / "src"


class Http:
    def __init__(self, path):
        self.path = path
        self.result = None
        self.trusted_checks = 0
        self.runtime_checks = 0

    def require_trusted_client(self):
        self.trusted_checks += 1
        return True

    def require_runtime(self):
        self.runtime_checks += 1
        return True

    def send_json(self, status, payload):
        self.result = ("json", status, payload)
        return self.result

    def static(self, name, content_type):
        self.result = ("static", name, content_type)
        return self.result


class Manager:
    def __init__(self):
        self.calls = []

    def workflow_subject(self, ref):
        self.calls.append(("status", ref))
        return {"ref": ref, "kind": "status"}

    def workflow_correct_history(self, ref, start, end, compact=False):
        self.calls.append(("history", ref, start, end, compact))
        return {
            "ref": ref,
            "start": start,
            "end": end,
            "compact": compact,
            "source": "optimized_generation_history",
        }

    def workflow_correct_point(self, ref, ts):
        self.calls.append(("point", ref, ts))
        return {"ref": ref, "ts": ts}

    def workflow_explore_status(self, ref):
        self.calls.append(("explore", ref))
        return {"ref": ref, "mode": "idle"}


class WorkflowReadRouteTests(unittest.TestCase):
    def setUp(self):
        self.registry = ExplicitRouteRegistry()
        self.manager = Manager()
        register_workflow_read_routes(self.registry, self.manager)
        register_explore_read_routes(self.registry, self.manager)
        register_confidence_read_routes(self.registry, object())

    def test_named_get_routes_and_static_assets_dispatch(self):
        names = [row["name"] for row in self.registry.routes("GET")]
        self.assertEqual(
            names,
            [
                "workflow.static",
                "workflow.status",
                "workflow.correct_history",
                "workflow.correct_point",
                "workflow.explore_status",
                "confidence.static",
            ],
        )

        workflow_ui = Http("/agent_workflow_ui.js")
        self.assertTrue(self.registry.dispatch("GET", workflow_ui))
        self.assertEqual(
            workflow_ui.result,
            ("static", "agent_workflow_ui.js", "application/javascript; charset=utf-8"),
        )

        confidence_ui = Http("/confidence_contract_ui.js")
        self.assertTrue(self.registry.dispatch("GET", confidence_ui))
        self.assertEqual(
            confidence_ui.result,
            ("static", "confidence_contract_ui.js", "application/javascript; charset=utf-8"),
        )

    def test_workflow_correct_and_explore_reads_preserve_semantics(self):
        status = Http("/api/agent-workflow/root%201/status")
        self.assertTrue(self.registry.dispatch("GET", status))
        self.assertEqual(status.result, ("json", 200, {"ref": "root 1", "kind": "status"}))

        history = Http(
            "/api/agent-workflow/root%201/correct-history?start=10&end=20&compact=1"
        )
        self.assertTrue(self.registry.dispatch("GET", history))
        self.assertEqual(history.result[0:2], ("json", 200))
        self.assertEqual(history.result[2]["source"], "optimized_generation_history")
        self.assertTrue(history.result[2]["compact"])
        self.assertEqual(
            self.manager.calls[-1],
            ("history", "root 1", 10.0, 20.0, True),
        )

        point = Http("/api/agent-workflow/root%201/correct-point?ts=12.5")
        self.assertTrue(self.registry.dispatch("GET", point))
        self.assertEqual(point.result, ("json", 200, {"ref": "root 1", "ts": 12.5}))

        explore = Http("/api/agent-workflow/root%201/explore")
        self.assertTrue(self.registry.dispatch("GET", explore))
        self.assertEqual(explore.result, ("json", 200, {"ref": "root 1", "mode": "idle"}))


class FinalCompositionGetOwnershipTests(unittest.TestCase):
    def test_final_root_disables_legacy_get_static_wrappers(self):
        root = (SRC / "runtime_composition.py").read_text(encoding="utf-8")
        self.assertIn("install_agent_workflow_actions(manager, legacy_get=False)", root)
        self.assertIn("install_correct_generation_history(manager, legacy_get=False)", root)
        self.assertIn("install_agent_explore(manager, legacy_get=False)", root)
        self.assertIn("install_confidence_contract(manager, legacy_http=False)", root)
        self.assertIn("register_workflow_read_routes(router, manager)", root)
        self.assertIn("register_explore_read_routes(router, manager)", root)
        self.assertIn("register_confidence_read_routes(router, self.core)", root)

        index = (SRC / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('src="confidence_contract_ui.js?v=0.14.138"', index)


if __name__ == "__main__":
    unittest.main()
