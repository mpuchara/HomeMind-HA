"""Stage 4 tests: frozen future holdout beside prequential adaptation."""
import unittest

from frozen_validation import (
    FrozenRidgeSnapshot,
    empty_holdout_counts,
    holdout_summary,
    prediction_is_correct,
    record_holdout_result,
)
from policy import DiagonalLinUCB


class FakePolicy:
    def __init__(self, actions=(0.0, 1.0), dims=1):
        self.actions = list(actions)
        self.dims = int(dims)
        self.heads = {1: DiagonalLinUCB(self.dims, self.actions)}


class FrozenHoldoutTests(unittest.TestCase):
    def test_snapshot_is_immutable_when_live_policy_learns_validation_rows(self):
        policy = FakePolicy()
        head = policy.heads[1]
        ts = head.last_decay_ts
        # At the boundary both arms tie, so the established deterministic mean choice is 0.
        frozen = FrozenRidgeSnapshot(policy)
        self.assertEqual(frozen.predict(1, {0: 1.0}), 0)

        for _ in range(8):
            head.update(1, {0: 1.0}, 1.0, ts)
        live_predicted = max(
            head.evaluate({0: 1.0}), key=lambda arm: arm["mean"]
        )["index"]
        self.assertEqual(live_predicted, 1)
        self.assertEqual(frozen.predict(1, {0: 1.0}), 0)

    def test_frozen_can_be_70_percent_while_prequential_is_90_percent(self):
        frozen = empty_holdout_counts()
        prequential = empty_holdout_counts()
        for index in range(10):
            actual = index % 2
            record_holdout_result(frozen, actual, index < 7)
            record_holdout_result(prequential, actual, index < 9)

        frozen_summary = holdout_summary(
            {"target_property": "power"}, [0.0, 1.0], frozen, minimum_samples=10
        )
        prequential_summary = holdout_summary(
            {"target_property": "power"}, [0.0, 1.0], prequential, minimum_samples=10
        )
        self.assertAlmostEqual(frozen_summary["overall_accuracy"], 0.7)
        self.assertAlmostEqual(prequential_summary["overall_accuracy"], 0.9)
        self.assertLess(
            frozen_summary["balanced_accuracy"],
            prequential_summary["balanced_accuracy"],
        )

    def test_missing_binary_class_reports_insufficient_evidence(self):
        stats = empty_holdout_counts()
        for _ in range(20):
            record_holdout_result(stats, 0, True)
        summary = holdout_summary(
            {"target_property": "power"}, [0.0, 1.0], stats, minimum_samples=12
        )
        self.assertFalse(summary["class_coverage"])
        self.assertEqual(summary["status"], "insufficient_evidence")
        self.assertIsNone(summary["balanced_accuracy"])
        self.assertIsNone(summary["score"])

    def test_binary_imbalance_uses_balanced_accuracy(self):
        stats = {
            "samples": 110,
            "correct": 95,
            "per_action": {
                "0": {"samples": 100, "correct": 90},
                "1": {"samples": 10, "correct": 5},
            },
        }
        summary = holdout_summary(
            {"target_property": "power"}, [0.0, 1.0], stats, minimum_samples=12
        )
        self.assertAlmostEqual(summary["overall_accuracy"], 95.0 / 110.0)
        self.assertAlmostEqual(summary["balanced_accuracy"], 0.70)
        self.assertAlmostEqual(summary["score"], 0.70)

    def test_non_binary_smoke_keeps_tolerance_contract(self):
        agent = {
            "target_property": "temperature",
            "deadband": 0.3,
            "min_value": 18.0,
            "max_value": 22.0,
        }
        actions = [18.0, 20.0, 22.0]
        self.assertTrue(prediction_is_correct(agent, actions, 1, 1))
        self.assertFalse(prediction_is_correct(agent, actions, 0, 2))
        stats = {
            "samples": 12,
            "correct": 9,
            "per_action": {
                "0": {"samples": 4, "correct": 3},
                "1": {"samples": 4, "correct": 3},
                "2": {"samples": 4, "correct": 3},
            },
        }
        summary = holdout_summary(agent, actions, stats, minimum_samples=12)
        self.assertEqual(summary["status"], "ok")
        self.assertFalse(summary["balanced"])
        self.assertAlmostEqual(summary["score"], 0.75)
        self.assertAlmostEqual(summary["balanced_accuracy"], 0.75)

    def test_prior_counts_are_copied_not_aliased(self):
        prior = {
            "samples": 1,
            "correct": 1,
            "per_action": {"0": {"samples": 1, "correct": 1}},
        }
        copied = empty_holdout_counts(prior)
        record_holdout_result(copied, 1, False)
        self.assertEqual(prior["samples"], 1)
        self.assertNotIn("1", prior["per_action"])


if __name__ == "__main__":
    unittest.main()
