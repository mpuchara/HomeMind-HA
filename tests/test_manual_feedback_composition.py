"""Regression tests for the stage-06 single-wrapper composition.

These tests intentionally inspect the public compatibility shim rather than depending on
implementation details of Engine.  The shim may enable Candidate-only semantics, but it
must not define another process_agent wrapper of its own.
"""
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import manual_feedback_live_isolation
import manual_feedback_workflow


class ManualFeedbackWorkflowSignatureTests(unittest.TestCase):
    def test_correct_wrapper_preserves_durable_request_id_keyword(self):
        original_commit = Mock(return_value={
            "ok": True,
            "child_generation_id": "candidate:g1",
        })
        manager = SimpleNamespace(
            workflow_add_correct_label=Mock(),
            workflow_undo_correct_label=Mock(),
            workflow_correct_commit=original_commit,
            workflow_change_decision=Mock(),
            workflow_correct_point=Mock(),
            store=SimpleNamespace(get_model=lambda _agent_id: {"version": 1}),
            engine=SimpleNamespace(models={}, manual_feedback_journal=None),
        )
        generation = {
            "generation_id": "live:g0",
            "root_agent_id": "agent-1",
        }
        agent = {"id": "agent-1"}

        with patch.object(
            manual_feedback_workflow,
            "_resolve_generation",
            return_value=(generation, agent),
        ):
            manual_feedback_workflow.install(manager)
            result = manager.workflow_correct_commit(
                "live:g0", request_id="req-correct-1"
            )

        original_commit.assert_called_once_with(
            "live:g0", request_id="req-correct-1"
        )
        self.assertEqual(result["child_generation_id"], "candidate:g1")


    def test_correct_wrapper_does_not_treat_store_bookkeeping_as_parent_policy_mutation(self):
        model = {
            "model_checksum": "stable-policy-id",
            "model_revision": "r1",
            "_history_watermark": 10,
        }

        class Store:
            def get_model(self, _agent_id):
                return dict(model)

        def original_commit(ref, request_id=None):
            self.assertEqual(ref, "live:g0")
            self.assertEqual(request_id, "req-bookkeeping")
            # Store-owned bookkeeping is intentionally outside policy identity and may
            # advance while the async Correct request is being committed.
            model["_history_watermark"] = 11
            return {"ok": True, "child_generation_id": "candidate:g1"}

        manager = SimpleNamespace(
            workflow_add_correct_label=Mock(),
            workflow_undo_correct_label=Mock(),
            workflow_correct_commit=original_commit,
            workflow_change_decision=Mock(),
            workflow_correct_point=Mock(),
            store=Store(),
            engine=SimpleNamespace(models={}, manual_feedback_journal=None),
        )
        generation = {
            "generation_id": "live:g0",
            "root_agent_id": "agent-1",
        }
        agent = {"id": "agent-1"}

        with patch.object(
            manual_feedback_workflow,
            "_resolve_generation",
            return_value=(generation, agent),
        ):
            manual_feedback_workflow.install(manager)
            result = manager.workflow_correct_commit(
                "live:g0", request_id="req-bookkeeping"
            )

        self.assertEqual(result["child_generation_id"], "candidate:g1")
        self.assertEqual(model["_history_watermark"], 11)


class ManualFeedbackCompositionTests(unittest.TestCase):
    def test_live_isolation_is_declarative_shim_not_second_process_wrapper(self):
        source = inspect.getsource(manual_feedback_live_isolation)
        self.assertIn("manual_feedback_candidate_only", source)
        self.assertIn("install_runtime_physical_equivalence", source)
        self.assertNotIn("def process_agent", source)
        self.assertNotIn("engine.process_agent =", source)


if __name__ == "__main__":
    unittest.main()
