import ast
import json
import unittest
from pathlib import Path

from support import ROOT
from promotion_validation import merge_named_results, named_result, PromotionValidationService
from runtime_http import ExplicitRouteRegistry, install_dispatch
from runtime_composition import bind_final_composition


SRC = ROOT / "adaptive_ai/src"


def _runtime_overlay_map():
    """Characterize install-time method/function mutation in the actual source tree."""
    found = []
    for path in sorted(SRC.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not (node.name == "install" or node.name.startswith("install_") or node.name.startswith("bind_")):
                continue
            for child in ast.walk(node):
                targets = []
                if isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                for target in targets:
                    if isinstance(target, ast.Attribute):
                        try:
                            text = ast.unparse(target)
                        except Exception:
                            text = target.attr
                        found.append({"file": path.name, "installer": node.name, "target": text})
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "setattr":
                    if len(child.args) >= 2:
                        try:
                            target = f"setattr({ast.unparse(child.args[0])},{ast.unparse(child.args[1])})"
                        except Exception:
                            target = "setattr(?)"
                        found.append({"file": path.name, "installer": node.name, "target": target})
    return found


class _Store:
    def get_model(self, _agent_id):
        return {"version": 11}


class _Manager:
    def __init__(self):
        self.store = _Store()


class PromotionValidationContractTests(unittest.TestCase):
    def test_duplicate_named_gate_uses_and_so_module_order_cannot_remove_veto(self):
        fail = named_result("hard_safety", False, "veto", source="first")
        later_pass = named_result("hard_safety", True, "later pass", source="second")
        forward = merge_named_results([fail], [later_pass])
        reverse = merge_named_results([later_pass], [fail])
        self.assertFalse(forward[0]["passed"])
        self.assertFalse(reverse[0]["passed"])
        self.assertEqual(forward[0]["custom_override"], "never")
        self.assertEqual(reverse[0]["custom_override"], "never")

    def test_custom_override_can_waive_evidence_but_never_hard_veto(self):
        service = PromotionValidationService(_Manager(), clock=object(), repository=object())
        row = {"feedback_revision": 1, "build_revision": 1, "dirty": 0}
        parent = {"id": "live"}
        candidate = {"id": "candidate", "training_state": "qualified"}
        base = {
            "promotable": True,
            "user_promotion_override": True,
            "promotion_gates": {
                "evidence": {"passed": False, "reason": "small n", "custom_override": "custom_evidence"},
                "hard": {"passed": True, "reason": "safe", "custom_override": "never"},
            },
        }
        result = service.decorate_summary(row, parent, candidate, base)
        self.assertTrue(result["promotable"])
        by_name = {item["name"]: item for item in result["promotion_validations"]}
        self.assertFalse(by_name["evidence"]["passed"])
        self.assertTrue(by_name["evidence"]["effective_passed"])

        base["promotion_gates"]["hard"]["passed"] = False
        result = service.decorate_summary(row, parent, candidate, base)
        self.assertFalse(result["promotable"])
        self.assertIn("hard", [item["gate"] for item in result["promotion_vetoes"]])

    def test_legacy_candidate_bool_is_decomposed_into_named_results(self):
        service = PromotionValidationService(_Manager(), clock=object(), repository=object())
        summary = {
            "samples": 40, "required_future_samples": 40,
            "live_accuracy": 0.9, "candidate_accuracy": 0.9,
            "live_false_early": 0, "candidate_false_early": 0,
            "fresh_feedback_revision": True, "per_action_ready": True,
            "promotable": True,
        }
        out = service.decorate_summary(
            {"feedback_revision": 2, "build_revision": 2, "dirty": 0},
            {"id": "live"}, {"id": "candidate", "training_state": "qualified"}, summary,
        )
        names = [item["name"] for item in out["promotion_validations"]]
        self.assertEqual(names, [
            "data_freshness", "action_coverage", "quality_regression",
            "false_early", "execution_prerequisites",
        ])
        self.assertTrue(out["promotable"])


class ExplicitRouteRegistryTests(unittest.TestCase):
    def test_same_named_route_is_replaced_in_place_not_installed_twice(self):
        registry = ExplicitRouteRegistry()
        calls = []
        registry.register("POST", "feedback.correct", r"^/x$", lambda http, params: calls.append("old"))
        registry.register("POST", "feedback.correct", r"^/x$", lambda http, params: calls.append("new"))
        self.assertEqual(len(registry.routes("POST")), 1)

        class Http:
            path = "/x"
            def require_trusted_client(self): return True
            def require_runtime(self): return True

        self.assertTrue(registry.dispatch("POST", Http()))
        self.assertEqual(calls, ["new"])

    def test_two_route_instances_do_not_modify_each_other(self):
        left = ExplicitRouteRegistry()
        right = ExplicitRouteRegistry()
        left.register("POST", "left", r"^/same$", lambda http, params: setattr(http, "owner", "left"))
        right.register("POST", "right", r"^/same$", lambda http, params: setattr(http, "owner", "right"))

        class Http:
            path = "/same"
            owner = None
            def require_trusted_client(self): return True
            def require_runtime(self): return True

        a, b = Http(), Http()
        left.dispatch("POST", a)
        right.dispatch("POST", b)
        self.assertEqual(a.owner, "left")
        self.assertEqual(b.owner, "right")
        self.assertEqual([x["name"] for x in left.routes()], ["left"])
        self.assertEqual([x["name"] for x in right.routes()], ["right"])

    def test_server_bound_dispatch_is_instance_owned_even_with_shared_base_handler(self):
        class Handler:
            def __init__(self, path, server):
                self.path = path
                self.server = server
                self.calls = []
            def require_trusted_client(self): return True
            def require_runtime(self): return True
            def do_GET(self): self.calls.append("get-fallback")
            def do_POST(self): self.calls.append("post-fallback")
            def do_PATCH(self): self.calls.append("patch-fallback")
            def do_DELETE(self): self.calls.append("delete-fallback")

        class Server:
            def __init__(self):
                self.RequestHandlerClass = Handler

        class Core:
            pass

        left_core, right_core = Core(), Core()
        left_core.Handler = right_core.Handler = Handler
        left_core.HTTP_SERVER, right_core.HTTP_SERVER = Server(), Server()

        left = install_dispatch(left_core)
        left_handler = left_core.HTTP_SERVER.RequestHandlerClass
        # Repeated installation for one runtime is idempotent and does not stack a new class.
        self.assertIs(install_dispatch(left_core), left)
        self.assertIs(left_core.HTTP_SERVER.RequestHandlerClass, left_handler)

        right = install_dispatch(right_core)
        right_handler = right_core.HTTP_SERVER.RequestHandlerClass
        self.assertIsNot(left_handler, right_handler)
        self.assertIs(left_handler.__bases__[0], Handler)
        self.assertIs(right_handler.__bases__[0], Handler)
        self.assertNotIn("_explicit_http_dispatch_installed", Handler.__dict__)
        self.assertNotIn("_explicit_http_route_registry", Handler.__dict__)

        left.register("POST", "left", r"^/same$", lambda http, params: http.calls.append("left"))
        right.register("POST", "right", r"^/same$", lambda http, params: http.calls.append("right"))

        a = left_handler("/same", left_core.HTTP_SERVER)
        b = right_handler("/same", right_core.HTTP_SERVER)
        a.do_POST()
        b.do_POST()
        self.assertEqual(a.calls, ["left"])
        self.assertEqual(b.calls, ["right"])
        self.assertEqual(left.descriptor()["binding"]["mode"], "server_instance_handler_subclass")
        self.assertEqual(right.descriptor()["binding"]["mode"], "server_instance_handler_subclass")

    def test_single_dispatch_install_is_idempotent_and_falls_back_for_unmigrated_route(self):
        class Handler:
            def __init__(self, path):
                self.path = path
                self.calls = []
            def require_trusted_client(self): return True
            def require_runtime(self): return True
            def do_GET(self): self.calls.append("get-fallback")
            def do_POST(self): self.calls.append("post-fallback")
            def do_PATCH(self): self.calls.append("patch-fallback")
            def do_DELETE(self): self.calls.append("delete-fallback")

        class Core:
            pass

        core = Core()
        core.Handler = Handler
        first = install_dispatch(core)
        second = install_dispatch(core)
        self.assertIs(first, second)
        first.register("POST", "explicit", r"^/explicit$", lambda http, params: http.calls.append("explicit"))

        explicit = Handler("/explicit")
        explicit.do_POST()
        self.assertEqual(explicit.calls, ["explicit"])
        fallback = Handler("/legacy")
        fallback.do_POST()
        self.assertEqual(fallback.calls, ["post-fallback"])


class CompositionRootIsolationTests(unittest.TestCase):
    def test_repeated_binding_is_idempotent_but_distinct_cores_stay_isolated(self):
        class Core:
            def __init__(self):
                self.prepare_engine_extensions = lambda: None

        class Runtime:
            def __init__(self):
                self.core = Core()

        left, right = Runtime(), Runtime()
        left_root = bind_final_composition(left)
        self.assertIs(bind_final_composition(left), left_root)
        right_root = bind_final_composition(right)

        self.assertIsNot(left_root, right_root)
        self.assertIs(left.core.RUNTIME_COMPOSITION_ROOT, left_root)
        self.assertIs(right.core.RUNTIME_COMPOSITION_ROOT, right_root)
        self.assertIs(left.core.prepare_engine_extensions.__self__, left_root)
        self.assertIs(right.core.prepare_engine_extensions.__self__, right_root)


class FinalRuntimeCharacterizationTests(unittest.TestCase):
    def test_shipped_entrypoint_and_image_reach_exact_composition_root(self):
        run = (SRC / "run.sh").read_text(encoding="utf-8")
        trial = (SRC / "trial_queue_main.py").read_text(encoding="utf-8")
        preference = (SRC / "preference_queue_main.py").read_text(encoding="utf-8")
        fast = (SRC / "fast_queue_main.py").read_text(encoding="utf-8")
        queue = (SRC / "queue_main.py").read_text(encoding="utf-8")
        main = (SRC / "main.py").read_text(encoding="utf-8")
        docker = (ROOT / "adaptive_ai/Dockerfile").read_text(encoding="utf-8")
        root = (SRC / "runtime_composition.py").read_text(encoding="utf-8")
        transport = (SRC / "runtime_http.py").read_text(encoding="utf-8")

        self.assertIn("exec python3 -u /app/trial_queue_main.py", run)
        self.assertIn('CMD ["/app/run.sh"]', docker)
        self.assertIn("import preference_queue_main as runtime", trial)
        self.assertIn("bind_final_composition(runtime)", trial)
        self.assertNotIn("core.prepare_engine_extensions = prepare_engine_extensions", trial)
        self.assertIn("import fast_queue_main as runtime", preference)
        self.assertIn("import queue_main as queued_runtime", fast)
        self.assertIn("import main as core", queue)
        self.assertIn("HTTP_SERVER = server", main)
        self.assertIn("ENTRYPOINT_CHAIN", root)
        self.assertIn('"promotion_validations[]"', root)
        self.assertIn("server.RequestHandlerClass = bound", transport)
        self.assertIn("server_instance_handler_subclass", transport)

    def test_final_root_declares_all_requested_service_contracts(self):
        source = (SRC / "runtime_composition.py").read_text(encoding="utf-8")
        for name in (
            '"context"', '"policy"', '"feedback"', '"episode_evaluation"',
            '"candidates"', '"promotion_gates"', '"execution"',
        ):
            self.assertIn(name, source)
        self.assertIn("register_feedback_routes", source)
        self.assertIn("register_promotion_routes", source)
        self.assertNotIn("executor._service", source)

    def test_characterization_map_captures_remaining_overlays_for_staged_removal(self):
        overlays = _runtime_overlay_map()
        keys = {(row["file"], row["installer"], row["target"]) for row in overlays}
        self.assertTrue(any(row[0] == "agent_candidates.py" for row in keys))
        self.assertTrue(any(row[0] == "manual_feedback.py" for row in keys))
        self.assertTrue(any("process_agent" in row[2] for row in keys))
        # Kept visible in CI so the audit document can be compared to the real source map.
        print("F23_OVERLAY_MAP=" + json.dumps(overlays, sort_keys=True))

    def test_existing_candidate_shadow_invariant_remains_in_characterized_stack(self):
        atomic = (SRC / "agent_candidate_atomic_promote.py").read_text(encoding="utf-8")
        candidates = (SRC / "agent_candidates.py").read_text(encoding="utf-8")
        self.assertIn('result["candidate_physical_mode"] = "shadow"', atomic)
        self.assertIn("candidate_control", candidates)
        self.assertIn("forbidden_by_runtime_enumeration", candidates)


if __name__ == "__main__":
    unittest.main()
