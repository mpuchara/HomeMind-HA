"""0.14.140 final Candidate/Live Handler fallback migration regressions."""
import threading
import unittest

from support import ROOT
from candidate_http_routes import register_routes as register_candidate_routes
from manual_feedback_static import install as install_manual_feedback_static
from manual_feedback_static import uninstall_legacy as uninstall_manual_feedback_static
from runtime_http import CONTINUE, ExplicitRouteRegistry


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


class Store:
    def __init__(self):
        self._candidate_ids_ram_lock = threading.RLock()
        self._candidate_ids_ram_ready = True
        self._candidate_ids_ram = set()
        self.agent = {"id": "a", "name": "Agent A"}

    def get_agent_config(self, agent_id):
        return dict(self.agent) if agent_id == "a" else None


class Teaching:
    def status(self, agent_id):
        return {"agent_id": agent_id, "state": "ready"}


class Queue:
    def status_for(self, agent_id):
        return {"state": "idle", "agent_id": agent_id}


class History:
    def status(self):
        return {"phase": "idle"}


class Manager:
    def __init__(self):
        self.blocking_candidate = None
        self.calls = []

    def list_status(self):
        return [{"parent_agent_id": "a", "state": "ready"}]

    def status(self, agent_id):
        return {"parent_agent_id": agent_id, "state": "ready"}

    def request_build(self, agent_id, reason):
        self.calls.append(("request_build", agent_id, reason))
        return {"state": "queued", "queue": {"state": "queued"}}

    def generation_history(self, ref, start, end):
        self.calls.append(("history", ref, start, end))
        return {"ref": ref, "start": start, "end": end}

    def generation_comparison(self, ref):
        self.calls.append(("comparison", ref))
        return {"ref": ref, "paired": True}

    def live_snapshots(self):
        return [{"root_agent_id": "a", "candidate_desired": 1.0}]

    def discard(self, agent_id):
        self.calls.append(("discard", agent_id))
        return {"ok": True, "agent_id": agent_id}

    def _candidate_row(self, agent_id):
        return self.blocking_candidate if agent_id == "a" else None


class Core:
    def __init__(self):
        self.STORE = Store()
        self.ENGINE = type("Engine", (), {"rl_teaching": Teaching()})()
        self.TRAINING_QUEUE = Queue()
        self.HISTORY = History()
        self.live_agent_payload = lambda include_configs=False: {
            "agents": [{"id": "a"}],
            "configs": ([{"id": "a"}] if include_configs else None),
        }


class ContinueContractTests(unittest.TestCase):
    def test_continue_evaluates_lower_explicit_route(self):
        calls = []
        registry = ExplicitRouteRegistry()
        registry.register(
            "DELETE",
            "guard",
            r"^/api/agents/(?P<agent_id>[^/]+)/learning$",
            lambda http, params: CONTINUE,
            require_trusted=False,
            require_runtime=False,
            priority=100,
        )
        registry.register(
            "DELETE",
            "lower",
            r"^/api/agents/(?P<agent_id>[^/]+)/learning$",
            lambda http, params: calls.append(params["agent_id"]),
            require_trusted=False,
            require_runtime=False,
            priority=0,
        )
        http = Http("/api/agents/a/learning")
        self.assertTrue(registry.dispatch("DELETE", http))
        self.assertEqual(calls, ["a"])


class CandidateExplicitRouteTests(unittest.TestCase):
    def setUp(self):
        self.core = Core()
        self.manager = Manager()
        self.registry = ExplicitRouteRegistry()
        register_candidate_routes(self.registry, self.core, self.manager)

    def test_static_list_live_and_generation_routes(self):
        candidate_ui = Http("/candidate_ui.js")
        self.assertTrue(self.registry.dispatch("GET", candidate_ui))
        self.assertEqual(
            candidate_ui.result,
            ("static", "candidate_ui.js", "application/javascript; charset=utf-8"),
        )

        preference_ui = Http("/candidate_preference_ui.js")
        self.assertTrue(self.registry.dispatch("GET", preference_ui))
        self.assertEqual(
            preference_ui.result,
            ("static", "candidate_preference_ui.js", "application/javascript; charset=utf-8"),
        )

        candidates = Http("/api/candidates")
        self.assertTrue(self.registry.dispatch("GET", candidates))
        self.assertEqual(candidates.result[0:2], ("json", 200))
        self.assertEqual(candidates.result[2]["candidates"][0]["state"], "ready")

        comparison = Http("/api/candidate-generations/gen%201/comparison")
        self.assertTrue(self.registry.dispatch("GET", comparison))
        self.assertEqual(comparison.result, ("json", 200, {"ref": "gen 1", "paired": True}))

        history = Http("/api/candidate-generations/gen%201/history?start=10&end=20")
        self.assertTrue(self.registry.dispatch("GET", history))
        self.assertEqual(history.result[0:2], ("json", 200))
        self.assertEqual(self.manager.calls[-1], ("history", "gen 1", 10.0, 20.0))

        candidate_live = Http("/api/candidate-live")
        self.assertTrue(self.registry.dispatch("GET", candidate_live))
        self.assertEqual(candidate_live.result[0:2], ("json", 200))

        live = Http("/api/live?bootstrap=1")
        self.assertTrue(self.registry.dispatch("GET", live))
        self.assertEqual(live.result[0:2], ("json", 200))
        self.assertEqual(live.result[2]["configs"], [{"id": "a"}])

    def test_candidate_teach_routes_override_lower_queue_compatibility(self):
        self.registry.register(
            "GET",
            "queue.status",
            r"^/api/agents/(?P<agent_id>[^/]+)/teach-rl-status$",
            lambda http, params: http.send_json(599, {"owner": "queue"}),
            priority=180,
        )
        status = Http("/api/agents/a/teach-rl-status")
        self.assertTrue(self.registry.dispatch("GET", status))
        self.assertEqual(status.result[0:2], ("json", 200))
        self.assertEqual(status.result[2]["candidate"]["state"], "ready")
        self.assertEqual(status.result[2]["training_queue"]["state"], "idle")

        self.registry.register(
            "POST",
            "queue.train",
            r"^/api/agents/(?P<agent_id>[^/]+)/teach-rl-train$",
            lambda http, params: http.send_json(599, {"owner": "queue"}),
            priority=-10000,
        )
        train = Http("/api/agents/a/teach-rl-train")
        self.assertTrue(self.registry.dispatch("POST", train))
        self.assertEqual(train.result[0:2], ("json", 202))
        self.assertEqual(self.manager.calls[-1], ("request_build", "a", "teach_train"))

    def test_learning_guard_blocks_active_candidate_or_continues_to_queue(self):
        self.registry.register(
            "DELETE",
            "queue.training.learning_rebuild",
            r"^/api/agents/(?P<agent_id>[^/]+)/learning$",
            lambda http, params: http.send_json(202, {"owner": "queue"}),
            priority=-9999,
        )

        self.manager.blocking_candidate = {"candidate_id": "cand-a"}
        blocked = Http("/api/agents/a/learning")
        self.assertTrue(self.registry.dispatch("DELETE", blocked))
        self.assertEqual(blocked.result[0:2], ("json", 409))

        self.manager.blocking_candidate = None
        allowed = Http("/api/agents/a/learning")
        self.assertTrue(self.registry.dispatch("DELETE", allowed))
        self.assertEqual(allowed.result, ("json", 202, {"owner": "queue"}))

    def test_candidate_discard_is_explicit(self):
        discard = Http("/api/agents/a/candidate")
        self.assertTrue(self.registry.dispatch("DELETE", discard))
        self.assertEqual(discard.result, ("json", 200, {"ok": True, "agent_id": "a"}))


class FinalCompositionOwnershipTests(unittest.TestCase):
    def test_final_root_opts_out_of_legacy_candidate_handler_ownership(self):
        root = (SRC / "runtime_composition.py").read_text(encoding="utf-8")
        self.assertIn("core._final_explicit_http_only = True", root)
        self.assertIn("register_candidate_routes(router, self.core, manager)", root)
        self.assertIn("register_manual_feedback_static_routes(router, self.core)", root)
        self.assertIn("uninstall_manual_feedback_static_legacy(core)", root)

        for filename in (
            "agent_candidates.py",
            "agent_candidate_teach_status.py",
            "agent_candidate_shadow_runtime.py",
            "agent_candidate_atomic_promote.py",
            "agent_candidate_user_promotion.py",
            "agent_candidate_card_summary.py",
            "agent_live_card_refresh.py",
            "agent_candidate_preference_metrics.py",
        ):
            source = (SRC / filename).read_text(encoding="utf-8")
            self.assertIn("_final_explicit_http_only", source, filename)

    def test_manual_feedback_legacy_wrapper_is_reversible_before_server_bind(self):
        calls = []

        class Handler:
            def do_GET(self):
                calls.append("base")

        class FakeCore:
            pass

        core = FakeCore()
        core.Handler = Handler
        base = Handler.do_GET
        install_manual_feedback_static(core)
        self.assertIsNot(Handler.do_GET, base)
        self.assertTrue(uninstall_manual_feedback_static(core))
        self.assertIs(Handler.do_GET, base)


if __name__ == "__main__":
    unittest.main()
