"""0.14.139 Workflow mutation + durable Correct transport explicit-route regressions."""
import unittest

from support import ROOT
from runtime_http import ExplicitRouteRegistry
from agent_workflow_actions import register_mutation_routes as register_workflow_mutation_routes
from agent_explore import register_mutation_routes as register_explore_mutation_routes
from workflow_request_queue import register_routes as register_workflow_request_routes


SRC = ROOT / "adaptive_ai" / "src"


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
        self.result = ("json", status, payload)
        return self.result


class DurableQueue:
    def __init__(self):
        self.accepted = []
        self.statuses = {}

    def enqueue_correct(self, ref, request_id=None):
        row = {
            "ok": True,
            "request_id": request_id or "generated",
            "generation_ref": ref,
            "action": "correct",
            "state": "accepted",
            "durable": True,
        }
        self.accepted.append((ref, request_id))
        self.statuses[row["request_id"]] = row
        return row

    def status(self, request_id):
        return self.statuses.get(request_id)


class Manager:
    def __init__(self):
        self.calls = []
        self.workflow_requests = DurableQueue()

    def workflow_autonomous(self, ref):
        self.calls.append(("autonomous", ref))
        return {"action": "autonomous", "ref": ref}

    def workflow_add_correct_label(self, ref, desired, ts):
        self.calls.append(("correct-label", ref, desired, ts))
        return {"ref": ref, "desired": desired, "ts": ts}

    def workflow_undo_correct_label(self, ref):
        self.calls.append(("correct-undo", ref))
        return {"ref": ref, "undone": True}

    def workflow_change_decision(self, ref, desired):
        self.calls.append(("change-decision", ref, desired))
        return {"ref": ref, "desired": desired}

    def workflow_explore(self, ref, payload):
        self.calls.append(("explore", ref, payload))
        return {"ref": ref, "payload": payload}


class WorkflowMutationRouteTests(unittest.TestCase):
    def setUp(self):
        self.registry = ExplicitRouteRegistry()
        self.manager = Manager()
        register_workflow_mutation_routes(self.registry, self.manager)
        register_workflow_request_routes(self.registry, self.manager)
        register_explore_mutation_routes(self.registry, self.manager)

    def test_named_routes_and_durable_correct_ownership(self):
        names = [row["name"] for row in self.registry.routes("POST")]
        self.assertEqual(
            names,
            [
                "workflow_request.correct",
                "workflow.autonomous",
                "workflow.correct_label",
                "workflow.correct_undo",
                "workflow.change_decision",
                "workflow.explore",
            ],
        )

        correct = Http(
            "/api/agent-workflow/root%201/correct",
            {"request_id": "req-1"},
        )
        self.assertTrue(self.registry.dispatch("POST", correct))
        self.assertEqual(correct.result[0:2], ("json", 202))
        self.assertTrue(correct.result[2]["durable"])
        self.assertEqual(
            self.manager.workflow_requests.accepted,
            [("root 1", "req-1")],
        )
        self.assertNotIn(
            "correct",
            [call[0] for call in self.manager.calls],
        )

        status = Http("/api/agent-workflow-requests/req-1")
        self.assertTrue(self.registry.dispatch("GET", status))
        self.assertEqual(status.result[0:2], ("json", 200))
        self.assertEqual(status.result[2]["state"], "accepted")

    def test_workflow_and_explore_mutations_preserve_status_codes(self):
        autonomous = Http("/api/agent-workflow/a/autonomous", {})
        self.assertTrue(self.registry.dispatch("POST", autonomous))
        self.assertEqual(autonomous.result[0:2], ("json", 202))

        label = Http(
            "/api/agent-workflow/a/correct-label",
            {"desired_value": 1.0, "sample_ts": 12.5},
        )
        self.assertTrue(self.registry.dispatch("POST", label))
        self.assertEqual(label.result[0:2], ("json", 200))

        undo = Http("/api/agent-workflow/a/correct-undo", {})
        self.assertTrue(self.registry.dispatch("POST", undo))
        self.assertEqual(undo.result[0:2], ("json", 200))

        change = Http(
            "/api/agent-workflow/a/change-decision",
            {"desired_value": 0.0},
        )
        self.assertTrue(self.registry.dispatch("POST", change))
        self.assertEqual(change.result[0:2], ("json", 202))

        explore = Http(
            "/api/agent-workflow/a/explore",
            {"mode": "free", "intensity": 0.25},
        )
        self.assertTrue(self.registry.dispatch("POST", explore))
        self.assertEqual(explore.result[0:2], ("json", 202))
        self.assertEqual(
            self.manager.calls[-1],
            ("explore", "a", {"mode": "free", "intensity": 0.25}),
        )

    def test_missing_durable_request_is_404(self):
        status = Http("/api/agent-workflow-requests/missing")
        self.assertTrue(self.registry.dispatch("GET", status))
        self.assertEqual(
            status.result,
            ("json", 404, {"error": "workflow request not found"}),
        )


class FinalCompositionMutationOwnershipTests(unittest.TestCase):
    def test_final_root_disables_workflow_request_explore_handler_post_wrappers(self):
        root = (SRC / "runtime_composition.py").read_text(encoding="utf-8")
        self.assertIn(
            "manager, legacy_get=False, legacy_post=False",
            root,
        )
        self.assertIn(
            "install_workflow_request_queue(manager, legacy_http=False)",
            root,
        )
        self.assertIn(
            "register_workflow_mutation_routes(router, manager)",
            root,
        )
        self.assertIn(
            "register_workflow_request_routes(router, manager)",
            root,
        )
        self.assertIn(
            "register_explore_mutation_routes(router, manager)",
            root,
        )
        self.assertNotIn(
            '"workflow/explore POST compatibility routes"',
            root,
        )


if __name__ == "__main__":
    unittest.main()
