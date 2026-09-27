"""Stage 2 tests: one bounded persistence budget per dwell and horizon."""
import unittest

from policy import DiagonalLinUCB
from replay import DeferredUpdates
from training_evidence import normalized_dwell_sample_mass


class MassAwarePolicy:
    def __init__(self):
        self.calls = []

    def update(
        self, horizon, action_idx, features, reward, sample_ts=None, sample_mass=1.0
    ):
        self.calls.append({
            "horizon": int(horizon),
            "action_idx": int(action_idx),
            "features": dict(features),
            "reward": float(reward),
            "sample_ts": float(sample_ts),
            "sample_mass": float(sample_mass),
        })


class LegacyPolicy:
    def __init__(self):
        self.calls = []

    def update(self, horizon, action_idx, features, reward, sample_ts=None):
        self.calls.append((horizon, action_idx, dict(features), reward, sample_ts))


class PerDwellTrainingBudgetTests(unittest.TestCase):
    def test_zero_one_two_three_persistence_samples_have_bounded_total_mass(self):
        self.assertEqual(normalized_dwell_sample_mass(0), 0.0)
        expected = {1: 1.0, 2: 0.5, 3: 1.0 / 3.0}
        for count, per_sample in expected.items():
            with self.subTest(count=count):
                mass = normalized_dwell_sample_mass(count)
                self.assertAlmostEqual(mass, per_sample)
                self.assertAlmostEqual(mass * count, 1.0)

    def test_ridge_one_two_three_contexts_have_same_total_update_mass(self):
        snapshots = []
        for count in (1, 2, 3):
            head = DiagonalLinUCB(2, [0.0, 1.0])
            ts = head.last_decay_ts
            sample_mass = normalized_dwell_sample_mass(count)
            for _ in range(count):
                head.update(
                    1, {0: 1.0, 1: 0.5}, 1.0, ts,
                    sample_mass=sample_mass,
                )
            snapshots.append({
                "count": head.counts[1],
                "reward_sum": head.reward_sums[1],
                "a0": head.a[1][0],
                "a1": head.a[1][1],
                "b0": head.b[1][0],
                "b1": head.b[1][1],
                "total": head.total_updates,
            })

        for snapshot in snapshots:
            self.assertAlmostEqual(snapshot["count"], 1.0)
            self.assertAlmostEqual(snapshot["reward_sum"], 1.0)
            self.assertAlmostEqual(snapshot["a0"], 2.0)
            self.assertAlmostEqual(snapshot["a1"], 1.25)
            self.assertAlmostEqual(snapshot["b0"], 1.0)
            self.assertAlmostEqual(snapshot["b1"], 0.5)
            self.assertAlmostEqual(snapshot["total"], 1.0)

    def test_default_sample_mass_is_exactly_legacy_full_mass(self):
        legacy = DiagonalLinUCB(2, [0.0, 1.0])
        explicit = DiagonalLinUCB(2, [0.0, 1.0])
        ts = min(legacy.last_decay_ts, explicit.last_decay_ts)
        legacy.last_decay_ts = ts
        explicit.last_decay_ts = ts
        legacy.update(1, {0: 1.0, 1: 0.25}, 0.6, ts)
        explicit.update(
            1, {0: 1.0, 1: 0.25}, 0.6, ts, sample_mass=1.0
        )
        self.assertEqual(legacy.counts, explicit.counts)
        self.assertEqual(legacy.reward_sums, explicit.reward_sums)
        self.assertEqual(legacy.a, explicit.a)
        self.assertEqual(legacy.b, explicit.b)
        self.assertEqual(legacy.ctx_sum, explicit.ctx_sum)
        self.assertEqual(legacy.ctx_sq, explicit.ctx_sq)

    def test_deferred_prequential_update_carries_persistence_mass(self):
        policy = MassAwarePolicy()
        deferred = DeferredUpdates({"agent": policy})
        deferred.append(
            (policy, 1, 1, {3: 0.5}, 1.0, 200.0, 1.0 / 3.0)
        )
        self.assertEqual(len(deferred), 1)
        self.assertEqual(len(policy.calls), 1)
        self.assertAlmostEqual(policy.calls[0]["sample_mass"], 1.0 / 3.0)

    def test_legacy_six_field_deferred_update_still_uses_old_call_shape(self):
        policy = LegacyPolicy()
        deferred = DeferredUpdates({"agent": policy})
        deferred.append((policy, 1, 1, {3: 0.5}, 0.35, 200.0))
        self.assertEqual(len(policy.calls), 1)
        self.assertEqual(policy.calls[0][-1], 200.0)

    def test_audit_mass_can_express_three_distinct_contexts_with_one_total_budget(self):
        masses = [normalized_dwell_sample_mass(3) for _ in range(3)]
        self.assertEqual(len(masses), 3)
        self.assertAlmostEqual(sum(masses), 1.0)
        self.assertTrue(all(mass > 0.0 for mass in masses))


if __name__ == "__main__":
    unittest.main()
