"""0.14.57 regressions for Discard cycle reset and deep-lineage Correct robustness."""
import unittest

from support import ROOT


class Release057DiscardLineageResetTests(unittest.TestCase):
    def test_live_workflow_does_not_assume_enqueue_always_returns_candidate_id(self):
        source = (ROOT / "adaptive_ai/src/agent_workflow_actions.py").read_text(encoding="utf-8")
        block = source.split('elif parent_generation.get("generation_type") == "live":', 1)[1].split("    else:", 1)[0]
        self.assertIn('status.get("candidate_id")', block)
        self.assertIn('status.get("generation_id")', block)
        self.assertNotIn('status["candidate_id"]', block)

    def test_user_discard_is_full_candidate_cycle_not_parent_rollback(self):
        source = (ROOT / "adaptive_ai/src/agent_candidate_lineage.py").read_text(encoding="utf-8")
        self.assertIn('discard_requested=2', source)
        self.assertIn('"discard_scope"] = "candidate_cycle"', source)
        self.assertIn("agent_candidate_cycle_discarded", source)
        self.assertIn("_repair_legacy_discard_rollbacks()", source)

    def test_new_candidate_refreshes_live_parent_model_snapshot(self):
        source = (ROOT / "adaptive_ai/src/agent_candidate_lineage.py").read_text(encoding="utf-8")
        self.assertIn("def _refresh_live_generation_snapshot", source)
        register = source.split("def register_created(parent, row, reason=None):", 1)[1].split("    def create_candidate", 1)[0]
        self.assertIn("_refresh_live_generation_snapshot(manager.store, parent_gen)", register)


if __name__ == "__main__":
    unittest.main()
