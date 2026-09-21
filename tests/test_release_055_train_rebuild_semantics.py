"""0.14.55 regressions for explicit Train/Rebuild semantics."""
import sys
import unittest

import support

sys.path.insert(0, str(support.SRC))

from training_request_semantics import train_request_mode


class Release055TrainingSemanticsTests(unittest.TestCase):
    def test_completed_trained_agent_uses_incremental_train(self):
        agent = {"training_state": "qualified", "training_cursor_ts": 1234.0, "training_progress": 1.0}
        rebuild, resumed = train_request_mode(agent, has_model=True)
        self.assertFalse(rebuild)
        self.assertTrue(resumed)

    def test_first_train_builds_from_scratch(self):
        agent = {"training_state": "waiting", "training_cursor_ts": None, "training_progress": 0.0}
        rebuild, resumed = train_request_mode(agent, has_model=False)
        self.assertTrue(rebuild)
        self.assertFalse(resumed)

    def test_needs_retrain_still_rebuilds_for_schema_safety(self):
        agent = {"training_state": "needs_retrain", "training_cursor_ts": 1234.0, "training_progress": 1.0}
        rebuild, resumed = train_request_mode(agent, has_model=True)
        self.assertTrue(rebuild)
        self.assertFalse(resumed)

    def test_candidate_layer_delegates_live_rebuild_instead_of_enqueuing_candidate(self):
        source = (support.ROOT / "adaptive_ai/src/agent_candidates.py").read_text(encoding="utf-8")
        block = source.split("def do_delete(http):", 1)[1].split("handler.do_GET = do_get", 1)[0]
        learning = block.split('path.endswith("/learning")', 1)[1]
        self.assertNotIn('self.enqueue(agent_id, "manual_rebuild")', learning)
        self.assertIn("return original_delete(http)", learning)
        self.assertIn("Discard or promote the current Candidate", learning)

    def test_feedback_undo_has_its_own_candidate_rebuild_reason(self):
        source = (support.ROOT / "adaptive_ai/src/fast_queue_main.py").read_text(encoding="utf-8")
        self.assertIn('"manual_feedback_undo_rebuild" if event == "teaching_undone"', source)
        self.assertIn('"manual_feedback_undo_rebuild" if event == "teach_rl_undone"', source)
        candidate_rebuild = (support.ROOT / "adaptive_ai/src/agent_candidate_manual_rebuild.py").read_text(encoding="utf-8")
        self.assertIn('FULL_REBUILD_REASONS = {"manual_feedback_undo_rebuild"}', candidate_rebuild)


if __name__ == "__main__":
    unittest.main()
