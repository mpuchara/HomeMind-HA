import unittest
from types import SimpleNamespace

import agent_candidate_hard_correct as hard


class FakeTeaching:
    def __init__(self):
        self.rows = [{
            "id": 1,
            "agent_id": "candidate-1",
            "created_ts": 1010.0,
            "sample_ts": 1000.0,
            "desired": 1.0,
            "previous_desired": 0.0,
            "fingerprint": "unused",
            "undone_ts": None,
        }]

    def labels(self, agent_id):
        return [dict(row) for row in self.rows if row["agent_id"] == agent_id]

    def _label_context(self, agent, policy, sample_ts):
        return {0: 1.0, 1: 1.0}


class SlowRepairPolicy:
    """Needs four explicit positive updates before the marked point flips to ON."""

    actions = [0.0, 1.0]
    horizons = [1]

    def __init__(self):
        self.positive_updates = 0
        self.model_revision = "balanced-revision"

    def predict(self, features):
        value = 1.0 if self.positive_updates >= 4 else 0.0
        return {"value": value}, 0.9, [], 1, 1.0, 0.0

    def update(self, horizon, action_idx, features, reward):
        if int(action_idx) == 1 and float(reward) > 0:
            self.positive_updates += 1

    def serialize(self):
        return {
            "model_revision": self.model_revision,
            "positive_updates": self.positive_updates,
        }


class FakeStore:
    def __init__(self):
        self.saved = None

    def save_model(self, agent_id, model):
        self.saved = (agent_id, dict(model))


class HardCorrectTests(unittest.TestCase):
    def test_explicit_marked_error_is_repaired_beyond_old_three_round_limit(self):
        policy = SlowRepairPolicy()
        store = FakeStore()
        teaching = FakeTeaching()
        engine = SimpleNamespace(
            rl_teaching=teaching,
            models={"candidate-1": policy},
            policy=lambda agent: policy,
        )
        manager = SimpleNamespace(store=store, engine=engine)
        candidate = {"id": "candidate-1", "deadband": 0.5}

        original_base = hard._BASE_BALANCED_FINE_TUNE
        original_collect = hard._collect_explicit_corrections
        try:
            hard._BASE_BALANCED_FINE_TUNE = lambda manager, candidate: {
                "mode": "conservative_snapshot_finetune",
                "balance_mode": "error_only_labels_plus_context_matched_historical_anchors",
                "teach_fit_before_count": 0,
                "teach_fit_after_count": 0,
                "teach_fit_total": 1,
                "teach_fit_before": 0.0,
                "teach_fit_after": 0.0,
                "correction_rounds": 3,
                "stability_anchor_total": 1,
                "stability_anchor_retained": 1,
                "stability_anchor_shortfall": {"0": 0, "1": 0},
                "class_balance_required": True,
            }
            hard._collect_explicit_corrections = lambda manager, candidate, policy: [{
                "label": {"sample_ts": 1000.0, "desired": 1.0},
                "features": {0: 1.0, 1: 1.0},
                "desired_idx": 1,
            }]
            report = hard._hard_correct_fine_tune(manager, candidate)
        finally:
            hard._BASE_BALANCED_FINE_TUNE = original_base
            hard._collect_explicit_corrections = original_collect

        self.assertEqual(report["teach_fit_after_count"], 1)
        self.assertEqual(report["teach_fit_after"], 1.0)
        self.assertTrue(report["hard_correction_satisfied"])
        self.assertEqual(report["hard_repair_rounds"], 4)
        self.assertEqual(report["hard_repair_updates"], 4)
        self.assertEqual(policy.positive_updates, 4)
        self.assertIsNotNone(store.saved)

    def test_losing_one_soft_anchor_does_not_block_an_otherwise_good_candidate(self):
        stats = {
            "samples": 40,
            "score": .90,
            "balanced": True,
            "actual_class_coverage": 2,
            "predicted_class_coverage": 2,
            "per_action_accuracy": {"0": .90, "1": .90},
        }
        report = {
            "mode": "conservative_snapshot_finetune",
            "balance_mode": "error_only_labels_plus_context_matched_historical_anchors",
            "teach_fit_before_count": 0,
            "teach_fit_after_count": 4,
            "teach_fit_total": 4,
            "stability_anchor_total": 4,
            "stability_anchor_retained": 3,
            "stability_anchor_shortfall": {"0": 0, "1": 0},
            "class_balance_required": True,
            "correction_class_counts": {"0": 0, "1": 4},
            "stability_anchor_class_counts": {"0": 4, "1": 0},
        }

        gate = hard._hard_offline_gate({}, stats, stats, report)

        self.assertTrue(gate["passed"])
        self.assertEqual(gate["stability_anchor_retention"], .75)
        self.assertTrue(gate["stability_anchor_warning"])
        self.assertNotIn("Correct damaged selected opposite-class stability anchors", gate.get("reasons") or [])

    def test_candidate_is_blocked_if_even_one_marked_error_remains_wrong(self):
        stats = {
            "samples": 40,
            "score": .90,
            "balanced": True,
            "actual_class_coverage": 2,
            "predicted_class_coverage": 2,
            "per_action_accuracy": {"0": .90, "1": .90},
        }
        report = {
            "mode": "conservative_snapshot_finetune",
            "balance_mode": "error_only_labels_plus_context_matched_historical_anchors",
            "teach_fit_before_count": 0,
            "teach_fit_after_count": 3,
            "teach_fit_total": 4,
            "stability_anchor_total": 4,
            "stability_anchor_retained": 4,
            "stability_anchor_shortfall": {"0": 0, "1": 0},
            "class_balance_required": True,
        }

        gate = hard._hard_offline_gate({}, stats, stats, report)

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["status"], "failed")
        self.assertIn("Correct did not satisfy all marked corrections", gate["reasons"])


if __name__ == "__main__":
    unittest.main()
