import unittest
from training_quality_benchmark import run


class PreferenceRolloutTests(unittest.TestCase):
    def test_learned_policy_improves_independent_preference_scenarios(self):
        report = run()
        self.assertTrue(report["pass"], report)
        self.assertEqual(report["paired_cost_gain"]["pairs"], 15)
        self.assertGreater(report["paired_cost_gain"]["minimum"], 0)
        self.assertEqual({row["scenario"] for row in report["trials"]}, {
            "entry_still_exit", "false_motion", "rare_manual_need", "sensor_failure", "late_entry"
        })

    def test_no_scenario_can_hide_regression_behind_average_gain(self):
        for row in run()["trials"]:
            self.assertLessEqual(row["agent"]["premature_off_seconds"], row["baseline"]["premature_off_seconds"])
            self.assertLessEqual(row["agent"]["false_activations"], row["baseline"]["false_activations"])
            self.assertGreaterEqual(row["cost_gain"], 0)
