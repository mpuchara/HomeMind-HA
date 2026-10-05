"""0.14.134 named Engine.on_state_changed composition regressions."""
import unittest
from pathlib import Path
from types import SimpleNamespace

from state_event_pipeline import (
    EXPECTED_INSTALL_ORDER,
    StateEventPipelineError,
    assert_state_event_pipeline,
    install_state_event_wrapper,
    state_event_pipeline_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]


class StateEventPipelineTests(unittest.TestCase):
    def engine(self, calls):
        def base(data):
            calls.append(("base", data.get("entity_id")))
            return "base-result"
        return SimpleNamespace(on_state_changed=base)

    @staticmethod
    def layer(calls, name):
        def factory(next_handler):
            def handler(data):
                calls.append((name, "enter"))
                result = next_handler(data)
                calls.append((name, "exit"))
                return result
            return handler
        return factory

    def test_named_layers_preserve_wrapper_call_semantics(self):
        calls = []
        engine = self.engine(calls)
        for name in EXPECTED_INSTALL_ORDER:
            install_state_event_wrapper(engine, name, self.layer(calls, name))

        result = engine.on_state_changed({"entity_id": "light.kitchen"})
        self.assertEqual(result, "base-result")
        self.assertEqual(
            calls,
            [
                ("observation", "enter"),
                ("provenance", "enter"),
                ("candidate_shadow", "enter"),
                ("manual_feedback_lifecycle", "enter"),
                ("base", "light.kitchen"),
                ("manual_feedback_lifecycle", "exit"),
                ("candidate_shadow", "exit"),
                ("provenance", "exit"),
                ("observation", "exit"),
            ],
        )
        snapshot = assert_state_event_pipeline(engine)
        self.assertEqual(
            snapshot["install_order_inner_to_outer"], list(EXPECTED_INSTALL_ORDER)
        )
        self.assertEqual(
            snapshot["call_entry_order_outer_to_inner"],
            list(reversed(EXPECTED_INSTALL_ORDER)),
        )

    def test_duplicate_named_install_is_noop_and_does_not_stack(self):
        calls = []
        engine = self.engine(calls)
        factory_calls = []
        def factory(next_handler):
            factory_calls.append(1)
            return self.layer(calls, "provenance")(next_handler)
        first = install_state_event_wrapper(engine, "provenance", factory)
        second = install_state_event_wrapper(engine, "provenance", factory)
        self.assertIs(first, second)
        self.assertEqual(factory_calls, [1])
        self.assertEqual(state_event_pipeline_snapshot(engine)["registered_layers"], 1)

    def test_unregistered_top_handler_replacement_blocks_next_install(self):
        calls = []
        engine = self.engine(calls)
        install_state_event_wrapper(
            engine, "manual_feedback_lifecycle",
            self.layer(calls, "manual_feedback_lifecycle"),
        )
        engine.on_state_changed = lambda data: None
        with self.assertRaises(StateEventPipelineError):
            install_state_event_wrapper(
                engine, "candidate_shadow", self.layer(calls, "candidate_shadow")
            )

    def test_final_assertion_rejects_wrong_order_or_direct_override(self):
        calls = []
        engine = self.engine(calls)
        for name in ("manual_feedback_lifecycle", "provenance", "candidate_shadow", "observation"):
            install_state_event_wrapper(engine, name, self.layer(calls, name))
        with self.assertRaises(StateEventPipelineError):
            assert_state_event_pipeline(engine)

        calls = []
        engine = self.engine(calls)
        for name in EXPECTED_INSTALL_ORDER:
            install_state_event_wrapper(engine, name, self.layer(calls, name))
        engine.on_state_changed = lambda data: None
        with self.assertRaises(StateEventPipelineError):
            assert_state_event_pipeline(engine)


class ShippedStateEventCompositionContractTests(unittest.TestCase):
    def test_shipped_layers_use_named_pipeline_and_final_root_asserts_it(self):
        src = ROOT / "adaptive_ai" / "src"
        expected = {
            "manual_feedback_lifecycle.py": "manual_feedback_lifecycle",
            "agent_candidate_shadow_runtime.py": "candidate_shadow",
            "provenance_runtime.py": "provenance",
            "observation_contract.py": "observation",
        }
        for filename, layer in expected.items():
            text = (src / filename).read_text(encoding="utf-8")
            self.assertIn("install_state_event_wrapper", text)
            self.assertIn(f'"{layer}"', text)
        root = (src / "runtime_composition.py").read_text(encoding="utf-8")
        self.assertIn("assert_state_event_pipeline(", root)
        self.assertIn("EXPECTED_STATE_EVENT_INSTALL_ORDER", root)
        self.assertIn('"state_events"', root)


if __name__ == "__main__":
    unittest.main()
