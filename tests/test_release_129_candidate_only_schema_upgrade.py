"""0.14.129: obsolete schema repair belongs to Candidate, never Live."""
from pathlib import Path
import unittest

from support import ROOT  # noqa: F401

SRC = ROOT / "adaptive_ai" / "src"


class CandidateOnlySchemaUpgradeTests(unittest.TestCase):
    def test_correct_workflow_never_queues_live_schema_rebuild(self):
        workflow = (SRC / "agent_workflow_actions.py").read_text(encoding="utf-8")
        self.assertNotIn("def _live_schema_recovery", workflow)
        self.assertNotIn("rebuilding_live_schema", workflow)
        self.assertNotIn(
            'rebuild_reason="incompatible_persisted_model"',
            workflow,
        )

    def test_candidate_schema_wrapper_routes_copied_snapshot_to_isolated_rebuild(self):
        source = (SRC / "correct_schema_evolution.py").read_text(encoding="utf-8")
        block = source.split("def start_build(row):", 1)[1].split(
            "manager._start_build = start_build", 1
        )[0]
        self.assertIn('row.get("candidate_id")', block)
        self.assertIn("_stable_model_schema_compatible(raw_model)", block)
        self.assertIn("_SCHEMA_UPGRADE_REASON", block)
        self.assertIn("return original_start(fresh)", block)
        self.assertNotIn('row.get("parent_agent_id") or ""))\n            if parent_model', block)
        self.assertNotIn("Train/Rebuild the Live agent", block)

    def test_0128_failed_candidate_is_requeued_not_discarded(self):
        source = (SRC / "agent_candidates.py").read_text(encoding="utf-8")
        recovery = source.split("def _recover(self):", 1)[1].split(
            "def _create_candidate", 1
        )[0]
        self.assertIn("Live policy feature schema is incompatible with this release", recovery)
        self.assertIn("reason='schema_upgrade_rebuild'", recovery)
        self.assertIn("state='queued'", recovery)
        self.assertNotIn("delete_agent", recovery)
        self.assertNotIn("clear_learning", recovery)

    def test_ui_create_candidate_has_no_live_repair_side_effect(self):
        workflow_ui = (SRC / "static" / "agent_workflow_ui.js").read_text(encoding="utf-8")
        candidate_ui = (SRC / "static" / "candidate_ui.js").read_text(encoding="utf-8")
        self.assertIn("Create Candidate", workflow_ui)
        self.assertNotIn("Naprawiam Live", workflow_ui)
        self.assertNotIn("Repair & Create Candidate", candidate_ui)
        self.assertNotIn("live_schema_repair_required", workflow_ui)

    def test_durable_request_has_no_waiting_live_rebuild_state(self):
        queue = (SRC / "workflow_request_queue.py").read_text(encoding="utf-8")
        self.assertNotIn("STATE_WAITING", queue)
        self.assertNotIn("rebuilding_live_schema", queue)


if __name__ == "__main__":
    unittest.main()
