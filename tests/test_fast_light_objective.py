import unittest

from fast_light_objective import (
    TIMING_METRIC_MODE,
    _patch_promotion_for_timing,
    benchmark_summary,
    timing_metric_row_factory,
    timing_utility,
)
from rewards import RewardEngine
import context_tournament_metrics as metrics
import context_tournament_promotion as promotion


class FastLightRewardTests(unittest.TestCase):
    def test_confirmed_on_lead_is_positive(self):
        result = RewardEngine().evaluate(
            timing_direction="on", baseline_delay=2.0, timing_window=8.0
        )
        self.assertGreater(result.value, 0.25)
        self.assertGreater(result.components["timing_improvement"], 0.25)

    def test_confirmed_off_saved_time_is_positive(self):
        result = RewardEngine().evaluate(
            timing_direction="off", baseline_delay=30.0, timing_window=120.0
        )
        self.assertGreater(result.value, 0.25)
        self.assertGreater(result.components["timing_improvement"], 0.25)

    def test_false_early_on_is_penalized(self):
        result = RewardEngine().evaluate(timing_direction="on", false_timing=True)
        self.assertLess(result.value, 0)
        self.assertEqual(result.components["false_timing"], -0.70)

    def test_premature_off_is_maximum_negative_reward(self):
        result = RewardEngine().evaluate(timing_direction="off", premature_off=True)
        self.assertEqual(result.value, -1.0)
        self.assertEqual(result.components["premature_off"], -1.0)

    def test_quick_retrigger_is_strongly_penalized(self):
        result = RewardEngine().evaluate(timing_direction="off", retrigger=True)
        self.assertLessEqual(result.value, -0.85)


class FastLightTournamentMetricTests(unittest.TestCase):
    def setUp(self):
        self.base = metrics.metric_row
        self.metric = timing_metric_row_factory(self.base)

    def model(self, **extra):
        row = {
            "metric_mode": TIMING_METRIC_MODE,
            "timing_samples": 40,
            "timing_active_utility_sum": 8.0,
            "timing_shadow_utility_sum": 20.0,
            "timing_active_failures": 1,
            "timing_shadow_failures": 1,
            "samples": 40,
            "class_totals": [20, 20],
            "active_correct_by_class": [19, 19],
            "shadow_correct_by_class": [19, 19],
        }
        row.update(extra)
        return row

    def test_timing_gain_rewards_earlier_challenger(self):
        row = self.metric(self.model(), [0.0, 1.0])
        self.assertEqual(row["metric"], "fast_timing_utility")
        self.assertAlmostEqual(row["baseline_score"], 0.60, places=6)
        self.assertAlmostEqual(row["challenger_score"], 0.75, places=6)
        self.assertAlmostEqual(row["gain"], 0.15, places=6)
        self.assertTrue(row["safety_ok"])

    def test_same_instant_accuracy_is_safety_not_objective(self):
        row = self.metric(self.model(
            active_correct_by_class=[20, 20],
            shadow_correct_by_class=[19, 20],
        ), [0.0, 1.0])
        self.assertGreater(row["gain"], 0.10)
        self.assertTrue(row["safety_ok"], "small accuracy loss may not erase real safe timing gain")

    def test_excess_challenger_failures_block_safety(self):
        row = self.metric(self.model(timing_shadow_failures=5), [0.0, 1.0])
        self.assertGreater(row["gain"], 0)
        self.assertFalse(row["safety_ok"], "12.5% timing failures must block promotion")

    def test_timing_utility_is_normalized_by_direction_window(self):
        self.assertAlmostEqual(timing_utility(4, 8), 0.5)
        self.assertAlmostEqual(timing_utility(60, 120), 0.5)
        self.assertEqual(timing_utility(999, 8), 1.0)

    def test_benchmark_summary_reports_real_seconds(self):
        summary = benchmark_summary({
            "on_samples": 10, "on_success": 8, "on_lead_seconds_sum": 12.0,
            "off_samples": 10, "off_success": 5, "off_saved_seconds_sum": 100.0,
            "false_early_on": 1, "premature_off": 0, "false_early_off": 1,
            "retriggers": 0, "reward_sum": 5.0,
        })
        self.assertAlmostEqual(summary["mean_on_lead_seconds"], 1.5)
        self.assertAlmostEqual(summary["mean_off_saved_seconds"], 20.0)
        self.assertEqual(summary["samples"], 20)


class FastLightPromotionWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        timing_metric = timing_metric_row_factory(metrics.metric_row)
        metrics.metric_row = timing_metric
        _patch_promotion_for_timing(timing_metric)

    def test_promotion_window_consumes_timing_samples_not_state_samples(self):
        model = {
            "metric_mode": TIMING_METRIC_MODE,
            "timing_samples": 0,
            "timing_active_utility_sum": 0.0,
            "timing_shadow_utility_sum": 0.0,
            "timing_active_failures": 0,
            "timing_shadow_failures": 0,
            "samples": 200,
            "class_totals": [100, 100],
            "active_correct_by_class": [90, 90],
            "shadow_correct_by_class": [90, 90],
            "evaluation_started_ts": 1000.0,
            "first_observed_ts": 1000.0,
            "last_observed_ts": 1000.0 + 4 * 86400,
        }
        cfg = {
            "enabled": True, "min_samples": 4, "min_days": 0.0,
            "min_gain": 0.03, "consecutive_wins": 1,
            "evaluation_hours": 24.0, "cooldown_hours": 0.0,
        }
        promotion.ensure_promotion_epoch(model, 2, 1000.0, cfg)
        model.update({
            "timing_samples": 4,
            "timing_active_utility_sum": 0.0,
            "timing_shadow_utility_sum": 2.0,
            "timing_last_scored_ts": 1100.0,
        })
        absorbed = promotion.absorb_new_scored_evidence(model, [0.0, 1.0], 1100.0, cfg)
        self.assertEqual(absorbed, 4)
        self.assertEqual(model["promotion_window_timing_samples"], 4)
        self.assertEqual(model["promotion_window_samples"], 4)

    def test_timing_safety_is_required_for_promotion(self):
        model = {
            "metric_mode": TIMING_METRIC_MODE,
            "timing_samples": 40,
            "timing_active_utility_sum": 0.0,
            "timing_shadow_utility_sum": 20.0,
            "timing_active_failures": 0,
            "timing_shadow_failures": 5,
            "timing_last_scored_ts": 1000.0,
            "samples": 40,
            "class_totals": [20, 20],
            "active_correct_by_class": [19, 19],
            "shadow_correct_by_class": [19, 19],
            "first_observed_ts": 0.0,
            "last_observed_ts": 4 * 86400.0,
            "promotion_consecutive_wins": 3,
            "promotion_completed_windows": 3,
            "promotion_epoch_version": promotion.PROMOTION_EPOCH_VERSION,
            "promotion_window_hours": 24.0,
            "promotion_window_start_ts": 3 * 86400.0,
            "promotion_window_end_ts": 4 * 86400.0,
        }
        result = promotion.promotion_eligibility(
            model, [0.0, 1.0], 4 * 86400.0,
            options={
                "context_tournament_enabled": True,
                "context_tournament_min_samples": 40,
                "context_tournament_min_days": 3,
                "context_tournament_min_gain": 0.03,
                "context_tournament_consecutive_wins": 3,
                "context_tournament_evaluation_hours": 24,
                "context_tournament_cooldown_hours": 0,
            },
        )
        self.assertFalse(result["ready"])
        self.assertFalse(result["checks"]["timing_safety"])


class FastLightArchitectureTests(unittest.TestCase):
    def test_fast_light_objective_never_dispatches_services(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "adaptive_ai/src/fast_light_objective.py").read_text()
        self.assertNotIn("executor.submit(", src)
        self.assertNotIn("HA.service(", src)
        self.assertNotIn("._service(", src)


if __name__ == "__main__":
    unittest.main()
