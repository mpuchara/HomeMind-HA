"""0.14.135 named process_agent composition and Candidate fast-bypass regressions."""
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from support import ROOT
from agent_candidates import AgentCandidateManager
from process_agent_pipeline import (
    EXPECTED_INSTALL_ORDER,
    ProcessAgentPipelineError,
    assert_process_agent_pipeline,
    install_process_agent_wrapper,
    process_agent_pipeline_snapshot,
)


SRC = ROOT / "adaptive_ai" / "src"


class ProcessAgentPipelineTests(unittest.TestCase):
    @staticmethod
    def engine(calls):
        def base(agent, state_map, changed_entities=None):
            calls.append(("base", agent.get("id")))
            return "base-result"
        return SimpleNamespace(process_agent=base)

    @staticmethod
    def layer(calls, name):
        def factory(next_handler):
            def handler(agent, state_map, changed_entities=None):
                calls.append((name, "enter"))
                result = next_handler(agent, state_map, changed_entities)
                calls.append((name, "exit"))
                return result
            return handler
        return factory

    def test_named_process_wrappers_preserve_complete_shipped_call_order(self):
        calls = []
        engine = self.engine(calls)
        for name in EXPECTED_INSTALL_ORDER:
            install_process_agent_wrapper(engine, name, self.layer(calls, name))
        result = engine.process_agent({"id": "a"}, {}, {"sensor.x"})
        self.assertEqual(result, "base-result")
        self.assertEqual(
            calls,
            [(name, "enter") for name in reversed(EXPECTED_INSTALL_ORDER)]
            + [("base", "a")]
            + [(name, "exit") for name in EXPECTED_INSTALL_ORDER],
        )
        snapshot = assert_process_agent_pipeline(engine)
        self.assertEqual(
            snapshot["install_order_inner_to_outer"], list(EXPECTED_INSTALL_ORDER)
        )
        self.assertEqual(
            snapshot["call_entry_order_outer_to_inner"],
            list(reversed(EXPECTED_INSTALL_ORDER)),
        )
        self.assertTrue(snapshot["top_handler_registered"])

    def test_duplicate_named_install_is_idempotent(self):
        calls = []
        engine = self.engine(calls)
        factory = self.layer(calls, "candidate_observation")
        first = install_process_agent_wrapper(engine, "candidate_observation", factory)
        second = install_process_agent_wrapper(engine, "candidate_observation", factory)
        self.assertIs(first, second)
        self.assertEqual(process_agent_pipeline_snapshot(engine)["registered_layers"], 1)

    def test_direct_process_agent_replacement_is_rejected(self):
        calls = []
        engine = self.engine(calls)
        for name in EXPECTED_INSTALL_ORDER:
            install_process_agent_wrapper(engine, name, self.layer(calls, name))
        engine.process_agent = lambda *args, **kwargs: None
        with self.assertRaises(ProcessAgentPipelineError):
            assert_process_agent_pipeline(engine)


class CandidateProcessFastBypassTests(unittest.TestCase):
    @staticmethod
    def manager(active):
        calls = []
        def base(agent, state_map, changed_entities=None):
            calls.append("base")
            return "result"
        engine = SimpleNamespace(process_agent=base)
        manager = AgentCandidateManager.__new__(AgentCandidateManager)
        manager.engine = engine
        manager.before_live_process = Mock(side_effect=lambda agent, states: calls.append("before"))
        manager.after_live_process = Mock(side_effect=lambda agent, states: calls.append("after"))
        manager.candidate_hot_active = active
        manager._install_process_wrapper()
        return manager, engine, calls

    def test_inactive_live_agent_bypasses_candidate_hooks(self):
        manager, engine, calls = self.manager(lambda _agent_id: False)
        result = engine.process_agent({"id": "live-no-candidate"}, {}, {"sensor.x"})
        self.assertEqual(result, "result")
        self.assertEqual(calls, ["base"])
        manager.before_live_process.assert_not_called()
        manager.after_live_process.assert_not_called()

    def test_active_candidate_preserves_before_base_after_order(self):
        manager, engine, calls = self.manager(lambda _agent_id: True)
        result = engine.process_agent({"id": "live-with-candidate"}, {}, {"sensor.x"})
        self.assertEqual(result, "result")
        self.assertEqual(calls, ["before", "base", "after"])
        manager.before_live_process.assert_called_once()
        manager.after_live_process.assert_called_once()

    def test_activity_index_failure_falls_back_to_observation_not_live_failure(self):
        def broken(_agent_id):
            raise RuntimeError("index unavailable")
        manager, engine, calls = self.manager(broken)
        self.assertEqual(engine.process_agent({"id": "a"}, {}, None), "result")
        self.assertEqual(calls, ["before", "base", "after"])


class ShippedProcessAgentContractTests(unittest.TestCase):
    def test_all_shipped_process_layers_register_through_named_pipeline(self):
        expected = {
            "manual_feedback.py": "manual_feedback_physical_equivalence",
            "context_tournament.py": "context_tournament_shadow",
            "teach_rl_rebenchmark.py": "teach_rl_rebenchmark",
            "agent_candidates.py": "candidate_observation",
            "provenance_runtime.py": "provenance",
            "observation_contract.py": "observation",
            "tiny_mlp_shadow.py": "tiny_mlp_shadow",
        }
        for filename, layer in expected.items():
            source = (SRC / filename).read_text(encoding="utf-8")
            self.assertIn("install_process_agent_wrapper(", source)
            self.assertIn(f'"{layer}"', source)

        pipeline_source = (SRC / "process_agent_pipeline.py").read_text(encoding="utf-8")
        root_source = (SRC / "runtime_composition.py").read_text(encoding="utf-8")
        self.assertIn("engine.process_agent = handler", pipeline_source)
        self.assertIn("assert_process_agent_pipeline(", root_source)
        self.assertIn('"process_agent"', root_source)
        self.assertNotIn("Candidate process_agent observation wrapper", root_source)

    def test_no_installer_directly_assigns_process_agent_outside_pipeline(self):
        offenders = []
        for path in sorted(SRC.glob("*.py")):
            if path.name in {"process_agent_pipeline.py", "manual_feedback_legacy.py"}:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                    targets = [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr == "process_agent":
                        offenders.append((path.name, getattr(node, "lineno", None)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
