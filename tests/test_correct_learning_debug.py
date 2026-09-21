import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from correct_learning_debug import (
    DEFAULT_LABELS,
    MAX_DEBUG_ENTITIES,
    MAX_LABELS,
    MAX_RAW_ROWS_PER_LABEL,
    _bounded_float,
    _bounded_int,
    _feature_rows,
    register_correct_learning_debug_route,
)
from runtime_http import ExplicitRouteRegistry
from support import ROOT


SRC = ROOT / "adaptive_ai/src"


class CorrectLearningDebugContractTests(unittest.TestCase):
    def test_debug_limits_are_hard_bounded(self):
        self.assertEqual(_bounded_int("9999", DEFAULT_LABELS, 1, MAX_LABELS), MAX_LABELS)
        self.assertEqual(_bounded_int("-4", DEFAULT_LABELS, 1, MAX_LABELS), 1)
        self.assertEqual(_bounded_int("bad", DEFAULT_LABELS, 1, MAX_LABELS), DEFAULT_LABELS)
        self.assertEqual(_bounded_float("9999", 120, 5, 600), 600)
        self.assertEqual(_bounded_float("bad", 120, 5, 600), 120)
        self.assertLessEqual(MAX_DEBUG_ENTITIES, 128)
        self.assertLessEqual(MAX_RAW_ROWS_PER_LABEL, 1024)

    def test_feature_export_always_keeps_home_tail(self):
        policy = SimpleNamespace(dims=12)
        rows = _feature_rows(
            policy,
            {0: 1.0, 5: 0.25, 11: 0.75},
            {0: ["bias"], 5: ["sensor.presence:state"], 11: ["home:trajectory_confidence"]},
        )
        by_index = {row["index"]: row for row in rows}
        for index in range(5, 12):
            self.assertIn(index, by_index)
        self.assertEqual(by_index[11]["label"], "home:trajectory_confidence")
        self.assertEqual(by_index[11]["value"], 0.75)

    def test_named_route_is_trusted_runtime_only_and_non_mutating_get(self):
        class Core:
            STORE = object()
            ENGINE = object()

        manager = SimpleNamespace()
        registry = ExplicitRouteRegistry()
        service = register_correct_learning_debug_route(registry, Core(), manager)
        routes = registry.routes("GET")
        names = {route["name"]: route for route in routes}
        self.assertEqual(
            set(names),
            {
                "debug.correct_learning",
                "debug.correct_learning.job_status",
                "debug.correct_learning.job_download",
            },
        )
        route = names["debug.correct_learning"]
        self.assertTrue(route["require_trusted"])
        self.assertTrue(route["require_runtime"])
        self.assertEqual(route["priority"], 250)
        self.assertFalse(names["debug.correct_learning.job_status"]["require_runtime"])
        self.assertFalse(names["debug.correct_learning.job_download"]["require_runtime"])
        post_routes = registry.routes("POST")
        self.assertEqual([row["name"] for row in post_routes], ["debug.correct_learning.start"])
        self.assertIs(manager.correct_learning_debug, service)
        self.assertIn("read_only", manager.correct_learning_debug_contract)

    def test_async_export_job_is_single_flight_and_caches_serialized_result(self):
        class Core:
            STORE = object()
            ENGINE = object()

        manager = SimpleNamespace()
        registry = ExplicitRouteRegistry()
        service = register_correct_learning_debug_route(registry, Core(), manager)

        def fake_export(ref, **kwargs):
            progress = kwargs.get("progress")
            if callable(progress):
                progress(0.5, "half")
            time.sleep(0.03)
            return {"ok": True, "ref": ref}

        service.export = fake_export
        first = service.start_export("agent-one")
        same = service.start_export("agent-one")
        self.assertEqual(first["job_id"], same["job_id"])
        with self.assertRaises(RuntimeError):
            service.start_export("agent-two")

        deadline = time.time() + 2.0
        state = None
        while time.time() < deadline:
            state = service.job_status(first["job_id"])
            if state and state["state"] == "done":
                break
            time.sleep(0.01)
        self.assertIsNotNone(state)
        self.assertEqual(state["state"], "done")
        self.assertEqual(state["progress"], 1.0)
        public, payload = service.job_bytes(first["job_id"])
        self.assertEqual(public["state"], "done")
        self.assertIn(b'"ok": true', payload)
        self.assertIn(b'"agent-one"', payload)

    def test_final_runtime_composition_wires_debug_after_dispatch(self):
        source = (SRC / "runtime_composition.py").read_text(encoding="utf-8")
        self.assertIn("register_correct_learning_debug_route", source)
        self.assertIn('"correct_learning_debug"', source)
        dispatch = source.index("router = install_dispatch(self.core)")
        route = source.index("register_correct_learning_debug_route(router, self.core, manager)")
        self.assertGreater(route, dispatch)


if __name__ == "__main__":
    unittest.main()
