"""Stage 3 tests: reward utility and evidence reliability are independent."""
import unittest

from policy import DiagonalLinUCB
from replay import DeferredUpdates
from training_evidence import evidence_weight_for


class EvidenceAwarePolicy:
    def __init__(self):
        self.calls = []

    def update(
        self, horizon, action_idx, features, reward, sample_ts=None,
        sample_mass=1.0, evidence_weight=1.0,
    ):
        self.calls.append({
            "reward": float(reward),
            "sample_mass": float(sample_mass),
            "evidence_weight": float(evidence_weight),
        })


class EvidenceWeightTests(unittest.TestCase):
    def head(self):
        head = DiagonalLinUCB(1, [0.0, 1.0])
        return head, head.last_decay_ts

    def test_positive_reward_with_full_and_weak_evidence(self):
        full, ts = self.head()
        weak, _ = self.head()
        weak.last_decay_ts = ts
        full.update(1, {0: 1.0}, 1.0, ts, evidence_weight=1.0)
        weak.update(1, {0: 1.0}, 1.0, ts, evidence_weight=0.35)
        self.assertAlmostEqual(full.counts[1], 1.0)
        self.assertAlmostEqual(weak.counts[1], 0.35)
        self.assertAlmostEqual(full.reward_sums[1], 1.0)
        self.assertAlmostEqual(weak.reward_sums[1], 0.35)

    def test_negative_reward_can_be_strong_or_weak_evidence(self):
        full, ts = self.head()
        weak, _ = self.head()
        weak.last_decay_ts = ts
        full.update(1, {0: 1.0}, -1.0, ts, evidence_weight=1.0)
        weak.update(1, {0: 1.0}, -1.0, ts, evidence_weight=0.35)
        self.assertAlmostEqual(full.counts[1], 1.0)
        self.assertAlmostEqual(weak.counts[1], 0.35)
        self.assertAlmostEqual(full.reward_sums[1], -1.0)
        self.assertAlmostEqual(weak.reward_sums[1], -0.35)

    def test_reward_magnitude_no_longer_defines_evidence_count(self):
        weak_reward, ts = self.head()
        strong_reward, _ = self.head()
        strong_reward.last_decay_ts = ts
        weak_reward.update(1, {0: 1.0}, 0.15, ts, evidence_weight=1.0)
        strong_reward.update(1, {0: 1.0}, 1.0, ts, evidence_weight=0.35)
        self.assertAlmostEqual(weak_reward.counts[1], 1.0)
        self.assertAlmostEqual(strong_reward.counts[1], 0.35)
        self.assertAlmostEqual(weak_reward.reward_sums[1], 0.15)
        self.assertAlmostEqual(strong_reward.reward_sums[1], 0.35)

    def test_time_decay_multiplies_evidence(self):
        head, ts = self.head()
        old_ts = ts - 30.0 * 86400.0
        head.update(1, {0: 1.0}, 1.0, old_ts, evidence_weight=0.35)
        self.assertAlmostEqual(head.counts[1], 0.175, places=4)

    def test_persistence_budget_multiplies_provenance_reliability(self):
        head, ts = self.head()
        head.update(
            1, {0: 1.0}, 1.0, ts,
            sample_mass=1.0 / 3.0, evidence_weight=0.8,
        )
        self.assertAlmostEqual(head.counts[1], 0.8 / 3.0, places=6)

    def test_upstream_moves_point_35_from_reward_to_evidence_without_changing_b(self):
        old, ts = self.head()
        new, _ = self.head()
        new.last_decay_ts = ts
        old.update(1, {0: 2.0}, 0.7 * 0.35, ts)
        new.update(1, {0: 2.0}, 0.7, ts, evidence_weight=0.35)
        self.assertAlmostEqual(old.b[1][0], new.b[1][0])
        self.assertAlmostEqual(old.reward_sums[1], new.reward_sums[1])
        self.assertAlmostEqual(old.counts[1], 1.0)
        self.assertAlmostEqual(new.counts[1], 0.35)

    def test_provenance_defaults_are_conservative_and_own_command_is_zero(self):
        self.assertEqual(evidence_weight_for("user", "onset"), 1.0)
        self.assertEqual(evidence_weight_for("unknown", "onset"), 0.25)
        self.assertEqual(evidence_weight_for("user_intent", "persistence"), 1.0)
        self.assertEqual(evidence_weight_for("unknown", "persistence"), 0.25)
        self.assertAlmostEqual(evidence_weight_for("unknown", "upstream"), 0.0875)
        self.assertEqual(evidence_weight_for("own_command", "onset"), 0.0)

    def test_deferred_update_carries_sample_and_evidence_weights(self):
        policy = EvidenceAwarePolicy()
        deferred = DeferredUpdates({"agent": policy})
        deferred.append(
            (policy, 1, 1, {0: 1.0}, -1.0, 100.0, 0.5, 0.35)
        )
        self.assertEqual(len(policy.calls), 1)
        self.assertEqual(policy.calls[0]["reward"], -1.0)
        self.assertEqual(policy.calls[0]["sample_mass"], 0.5)
        self.assertEqual(policy.calls[0]["evidence_weight"], 0.35)


if __name__ == "__main__":
    unittest.main()
