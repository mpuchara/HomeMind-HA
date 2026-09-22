from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CandidateCorrectVisibilityContractTests(unittest.TestCase):
    def source(self, name):
        return (ROOT / "adaptive_ai" / "src" / name).read_text(encoding="utf-8")

    def test_correct_has_visible_candidate_worker_queue(self):
        source = self.source("agent_candidate_conservative_correct.py")
        self.assertIn('"backend": "candidate_worker"', source)
        self.assertIn('result["queue"] = synthetic_queue', source)
        self.assertIn('result["training_backend"] = "candidate_worker"', source)
        self.assertIn('result["training_progress"] = max(', source)
        self.assertIn('phase="waiting_for_heavy_slot"', source)

    def test_correct_serializes_with_shared_heavy_work(self):
        source = self.source("agent_candidate_conservative_correct.py")
        self.assertIn('if not HEAVY_JOBS.acquire(owner):', source)
        self.assertIn('HEAVY_JOBS.release(owner)', source)
        self.assertIn('finally:', source)
        self.assertIn('blocked_by=HEAVY_JOBS.owner', source)

    def test_correct_uses_cooperative_training_budget(self):
        source = self.source("agent_candidate_conservative_correct.py")
        self.assertIn('_BUDGET_THREAD_NAME = "adaptive-ai-index-candidate-correct"', source)
        self.assertIn('TRAINING_BUDGET.begin(thread_name=_BUDGET_THREAD_NAME)', source)
        self.assertIn('"candidate_correct_context"', source)
        self.assertIn('"candidate_correct_update"', source)
        self.assertIn('"candidate_correct_offline_score"', source)
        self.assertIn('TRAINING_BUDGET.end()', source)

    def test_existing_candidate_card_surfaces_queue_and_progress_without_new_poller(self):
        source = (
            ROOT / "adaptive_ai" / "src" / "static" / "candidate_ui.js"
        ).read_text(encoding="utf-8")
        self.assertIn("queue #${q.position||1}", source)
        self.assertIn("q.state==='active'", source)
        self.assertIn("c.state==='building'?", source)
        self.assertIn("c.training_progress||0", source)


if __name__ == "__main__":
    unittest.main()
