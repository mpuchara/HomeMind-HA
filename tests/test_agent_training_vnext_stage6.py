"""Agent Training vNext Stage 6: practical gain + paired significance."""
from pathlib import Path
import unittest

from policy_tiny_mlp_training import (
    exact_paired_mlp_win_p_value,
    tournament_result,
)


def counts(samples, correct_each, samples_each):
    return {
        "samples": samples,
        "correct": sum(correct_each),
        "per_action": {
            str(index): {"samples": samples_each[index], "correct": correct_each[index]}
            for index in range(len(correct_each))
        },
    }


def mlp_metrics(score, samples, correct_each, samples_each, paired):
    correct = sum(correct_each)
    return {
        "samples": samples,
        "correct": correct,
        "per_action": {
            str(index): {"samples": samples_each[index], "correct": correct_each[index]}
            for index in range(len(correct_each))
        },
        "score": float(score),
        "class_coverage": True,
        "per_action_accuracy": {
            str(index): float(correct_each[index]) / samples_each[index]
            for index in range(len(correct_each))
        },
        "paired_comparison": {
            "contract": "paired_holdout_correctness_v1",
            "samples": samples,
            "complete": True,
            **paired,
        },
    }


class Stage6TournamentTests(unittest.TestCase):
    def setUp(self):
        self.agent = {
            "target_property": "power",
            "deadband": .5,
            "min_value": 0,
            "max_value": 1,
        }

    def result(self, ridge, mlp, *, minimum_gain=.03, alpha=.05):
        return tournament_result(
            agent=self.agent,
            actions=(0.0, 1.0),
            ridge_stats=ridge,
            mlp_metrics=mlp,
            threshold=.78,
            minimum_samples=12,
            minimum_gain=minimum_gain,
            significance_alpha=alpha,
            parameter_count=4000,
            serialized_bytes=80000,
        )

    def test_exact_one_sided_p_values_are_deterministic(self):
        self.assertAlmostEqual(exact_paired_mlp_win_p_value(8, 0), 1.0 / 256.0)
        self.assertAlmostEqual(exact_paired_mlp_win_p_value(4, 1), 6.0 / 32.0)
        self.assertEqual(exact_paired_mlp_win_p_value(0, 0), 1.0)

    def test_large_accuracy_gain_without_paired_significance_keeps_ridge(self):
        ridge = counts(20, [7, 8], [10, 10])
        mlp = mlp_metrics(
            .90, 20, [9, 9], [10, 10],
            {
                "both_correct": 14,
                "mlp_only_correct": 4,
                "ridge_only_correct": 1,
                "both_wrong": 1,
                "discordant": 5,
            },
        )
        result = self.result(ridge, mlp)
        self.assertGreater(result["gain"], .03)
        self.assertFalse(result["significance_passed"])
        self.assertEqual(result["reason"], "mlp_paired_improvement_not_significant")
        self.assertEqual(result["selected_backend"], "diagonal_linucb")

    def test_statistically_clear_but_tiny_gain_keeps_ridge(self):
        ridge = counts(1000, [490, 490], [500, 500])
        mlp = mlp_metrics(
            .99, 1000, [495, 495], [500, 500],
            {
                "both_correct": 980,
                "mlp_only_correct": 10,
                "ridge_only_correct": 0,
                "both_wrong": 10,
                "discordant": 10,
            },
        )
        result = self.result(ridge, mlp, minimum_gain=.03)
        self.assertLess(result["significance_p_value"], .05)
        self.assertFalse(result["gain_passed"])
        self.assertEqual(result["reason"], "mlp_gain_below_practical_minimum")

    def test_practical_and_significant_gain_selects_mlp(self):
        ridge = counts(40, [15, 15], [20, 20])
        mlp = mlp_metrics(
            .95, 40, [19, 19], [20, 20],
            {
                "both_correct": 30,
                "mlp_only_correct": 8,
                "ridge_only_correct": 0,
                "both_wrong": 2,
                "discordant": 8,
            },
        )
        result = self.result(ridge, mlp)
        self.assertTrue(result["gain_passed"])
        self.assertTrue(result["significance_passed"])
        self.assertTrue(result["passed"])
        self.assertEqual(result["selected_backend"], "tiny_mlp")
        self.assertEqual(
            result["contract"], "ridge_vs_tiny_mlp_paired_holdout_v2"
        )

    def test_missing_paired_row_evidence_never_selects_mlp(self):
        ridge = counts(40, [15, 15], [20, 20])
        mlp = {
            "samples": 40,
            "correct": 38,
            "per_action": {
                "0": {"samples": 20, "correct": 19},
                "1": {"samples": 20, "correct": 19},
            },
            "score": .95,
            "class_coverage": True,
            "per_action_accuracy": {"0": .95, "1": .95},
        }
        result = self.result(ridge, mlp)
        self.assertFalse(result["same_holdout_rows"])
        self.assertEqual(
            result["reason"], "paired_holdout_evidence_missing_or_incomplete"
        )

    def test_history_wires_frozen_ridge_correctness_and_stage6_thresholds(self):
        root = Path(__file__).resolve().parents[1]
        history = (root / "adaptive_ai" / "src" / "history.py").read_text()
        settings = (root / "adaptive_ai" / "src" / "settings.py").read_text()
        self.assertIn('"ridge_correct": bool(frozen_correct)', history)
        self.assertIn('"paired_holdout_id": int(old.get("history_id") or 0)', history)
        self.assertIn(
            'OPTIONS.get("tiny_mlp_tournament_significance_alpha", 0.05)',
            history,
        )
        self.assertIn('"tiny_mlp_tournament_min_gain": 0.03', settings)
        self.assertIn('"tiny_mlp_tournament_significance_alpha": 0.05', settings)


if __name__ == "__main__":
    unittest.main()
