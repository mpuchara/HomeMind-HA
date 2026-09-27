"""Stage 1 regression tests for Agent Training vNext balance diagnostics."""
import copy
import unittest

from training_balance_audit import TrainingBalanceAudit


class FakeHead:
    def __init__(self, counts=(0.0, 0.0), reward_sums=(0.0, 0.0), total_updates=0.0):
        self.counts = list(counts)
        self.reward_sums = list(reward_sums)
        self.total_updates = float(total_updates)

    def sample_weight(self, sample_ts=None):
        # Deterministic stand-in for the existing time-decay contract.
        return 0.5 if float(sample_ts or 0.0) < 50.0 else 1.0


class FakePolicy:
    def __init__(self):
        self.actions = [0.0, 1.0]
        self.agent = {"target_property": "power"}
        self.heads = {
            1: FakeHead((2.0, 5.0), (1.0, 3.0), 7.0),
            60: FakeHead((1.0, 4.0), (0.5, 2.0), 5.0),
        }


class TrainingBalanceAuditTests(unittest.TestCase):
    def test_persistence_asymmetry_is_visible_without_mutating_policy(self):
        policy = FakePolicy()
        before = copy.deepcopy(policy.__dict__)
        audit = TrainingBalanceAudit({"id": "lamp"}, policy)
        audit.record_dwell(0, 20.0)
        audit.record_sample("onset", 0, 1.0, 100.0, policy.heads[1], horizon=1)
        audit.record_dwell(1, 240.0)
        audit.record_sample("onset", 1, 1.0, 100.0, policy.heads[1], horizon=1)
        for ts in (110.0, 150.0, 220.0):
            audit.record_sample("persistence", 1, 1.0, ts, policy.heads[1], horizon=1)

        summary = audit.finalize(policy)

        self.assertEqual(policy.__dict__, before)
        self.assertEqual(summary["samples"]["onset"]["total"], 2)
        self.assertEqual(summary["samples"]["persistence"]["total"], 3)
        self.assertEqual(
            summary["derived"]["persistence_to_onset_ratio"]["value"], 1.5
        )
        self.assertEqual(
            summary["derived"]["effective_update_ratio"]["status"], "ok"
        )
        self.assertGreater(
            summary["derived"]["effective_update_ratio"]["value"], 1.0
        )

    def test_zero_denominator_is_explicit(self):
        policy = FakePolicy()
        audit = TrainingBalanceAudit({"id": "lamp"}, policy)
        audit.record_sample("persistence", 1, 1.0, 100.0, policy.heads[1], horizon=1)
        summary = audit.finalize(policy)
        ratio = summary["derived"]["persistence_to_onset_ratio"]
        self.assertIsNone(ratio["value"])
        self.assertEqual(ratio["status"], "zero_denominator")

    def test_dwell_quantiles_benchmark_and_mlp_balance_are_reported(self):
        policy = FakePolicy()
        audit = TrainingBalanceAudit({"id": "lamp"}, policy)
        for duration in (10.0, 20.0, 30.0, 40.0, 50.0):
            audit.record_dwell(0, duration)
        for duration in (100.0, 200.0, 300.0):
            audit.record_dwell(1, duration)

        artifact = {
            "trainer": {
                "samples": 4,
                "class_counts": {"0": 1, "1": 3},
                "class_weight": {"0": 2.0, "1": 2.0 / 3.0},
            },
            "tournament": {"selected_backend": "tiny_mlp", "gain": 0.08},
        }
        summary = audit.finalize(
            policy,
            benchmark={
                "samples": 5,
                "correct": 4,
                "per_action": {
                    "0.0": {"samples": 2, "correct": 1},
                    "1.0": {"samples": 3, "correct": 3},
                },
            },
            neural_train_rows=[
                {"action_idx": 0},
                {"action_idx": 1},
                {"action_idx": 1},
                {"action_idx": 1},
            ],
            neural_holdout_rows=[{"action_idx": 0}, {"action_idx": 1}],
            neural_artifact=artifact,
        )

        self.assertEqual(summary["dwell"]["0.0"]["median_seconds"], 30.0)
        self.assertEqual(summary["dwell"]["1.0"]["p50_seconds"], 200.0)
        self.assertAlmostEqual(summary["qualification"]["balanced_accuracy"], 0.75)
        self.assertEqual(summary["tiny_mlp"]["class_counts"], {"0": 1, "1": 3})
        self.assertEqual(summary["tiny_mlp"]["holdout_sample_count"], 2)
        self.assertEqual(summary["tiny_mlp"]["tournament"]["gain"], 0.08)
        self.assertEqual(summary["derived"]["mlp_class_ratio"]["value"], 3.0)

    def test_provenance_and_reward_mass_are_separate_diagnostics(self):
        policy = FakePolicy()
        audit = TrainingBalanceAudit({"id": "lamp"}, policy)
        audit.record_sample(
            "onset", 0, -1.0, 20.0, policy.heads[1],
            provenance="manual", horizon=1,
        )
        audit.record_sample(
            "upstream", 1, 0.35, 100.0, policy.heads[1],
            provenance="automation_assisted", horizon=1,
        )
        audit.record_excluded("own_command")

        summary = audit.finalize(policy)
        self.assertEqual(summary["excluded_samples"]["own_command"], 1)
        self.assertAlmostEqual(summary["mass"]["negative_reward_mass"], 0.5)
        self.assertAlmostEqual(
            summary["mass_by_provenance"]["automation_assisted"]["positive_reward_mass"],
            0.35,
        )
        # Upstream currently has full evidence mass even though reward was scaled.
        self.assertAlmostEqual(
            summary["mass_by_source"]["upstream"]["time_decayed_sample_mass"], 1.0
        )

    def test_state_can_accumulate_across_training_chunks(self):
        policy = FakePolicy()
        first = TrainingBalanceAudit({"id": "lamp"}, policy)
        first.begin_chunk()
        first.record_dwell(0, 12.0)
        first.record_sample("onset", 0, 1.0, 100.0, policy.heads[1], horizon=1)

        second = TrainingBalanceAudit(
            {"id": "lamp"}, policy, prior_state=first.export_state()
        )
        second.begin_chunk()
        second.record_dwell(1, 20.0)
        second.record_sample("onset", 1, 1.0, 100.0, policy.heads[1], horizon=1)
        summary = second.finalize(policy)

        self.assertEqual(summary["chunks"], 2)
        self.assertEqual(summary["dwell"]["0.0"]["count"], 1)
        self.assertEqual(summary["dwell"]["1.0"]["count"], 1)
        self.assertEqual(summary["samples"]["onset"]["total"], 2)


    def test_short_long_long_short_and_symmetric_dwell_fixtures(self):
        policy = FakePolicy()
        fixtures = {
            "short_on_long_off": ((1, 10.0), (0, 300.0)),
            "long_on_short_off": ((1, 300.0), (0, 10.0)),
            "symmetric": ((1, 120.0), (0, 120.0)),
        }
        expected = {
            "short_on_long_off": 10.0 / 300.0,
            "long_on_short_off": 300.0 / 10.0,
            "symmetric": 1.0,
        }
        for name, dwells in fixtures.items():
            with self.subTest(name=name):
                audit = TrainingBalanceAudit({"id": name}, policy)
                for action_idx, seconds in dwells:
                    audit.record_dwell(action_idx, seconds)
                summary = audit.finalize(policy)
                ratio = summary["derived"]["action_time_ratio"]
                # Binary ratio is action[1] / action[0].
                self.assertEqual(ratio["status"], "ok")
                self.assertAlmostEqual(ratio["value"], expected[name])

    def test_non_binary_target_smoke(self):
        class NonBinaryPolicy:
            def __init__(self):
                self.actions = [18.0, 20.0, 22.0]
                self.agent = {"target_property": "temperature"}
                self.heads = {
                    1: FakeHead(
                        counts=(2.0, 3.0, 1.0),
                        reward_sums=(1.0, 2.0, 0.5),
                        total_updates=6.0,
                    )
                }

        policy = NonBinaryPolicy()
        audit = TrainingBalanceAudit({"id": "setpoint"}, policy)
        for idx, duration in enumerate((90.0, 120.0, 60.0)):
            audit.record_dwell(idx, duration)
            audit.record_sample(
                "onset", idx, 1.0, 100.0, policy.heads[1],
                provenance="manual", horizon=1,
            )
        summary = audit.finalize(
            policy,
            benchmark={
                "samples": 3,
                "correct": 2,
                "per_action": {
                    "18.0": {"samples": 1, "correct": 1},
                    "20.0": {"samples": 1, "correct": 1},
                    "22.0": {"samples": 1, "correct": 0},
                },
            },
        )
        self.assertEqual(summary["ridge"]["action_count"], 3)
        self.assertIsNone(summary["qualification"]["balanced_accuracy"])
        self.assertEqual(summary["samples"]["onset"]["total"], 3)


if __name__ == "__main__":
    unittest.main()
