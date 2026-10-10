import sys
import unittest

from support import ROOT
sys.path.insert(0, str(ROOT / "tools"))
from benchmark_stationary_relay import run


class StationaryRelayWorkerTests(unittest.TestCase):
    def test_worker_learns_short_dropout_and_preserves_brief_handwashing_visits(self):
        report = run(noisy=True, wildcard_unmapped=True)
        by_phase = {row["phase"]: row for row in report["predictions"]}
        for phase in ("empty", "entry", "stationary", "washbasin", "exit", "short_dip",
                      "short_visit_entry", "short_visit_stay"):
            self.assertEqual(by_phase[phase]["predicted"], by_phase[phase]["expected"], by_phase[phase])
        self.assertIn("sensor.espen4_stationary_target_distance", report["persisted_entities"])
        self.assertTrue(report["pass"], report["predictions"])

    def test_rebuild_full_foreign_schema_with_unmapped_local_radar(self):
        report = run(feature_contract=2, wildcard_unmapped=True)
        self.assertEqual(set(report["persisted_entities"]), {
            "sensor.espen4_stationary_energy", "sensor.espen4_moving_energy"})
        self.assertEqual(report["persisted_feature_contract"], 4)
        self.assertTrue(report["pass"], report["predictions"])

    def test_real_worker_and_persisted_policy_maintain_stationary_stay(self):
        report = run()
        self.assertGreater(report["updates"], 20)
        self.assertTrue(report["pass"], report["predictions"])
        maintenance = report["maintenance_holdout"]
        self.assertIsNotNone(maintenance)
        self.assertGreaterEqual(maintenance["samples"], 12)
        self.assertTrue(maintenance["passed"], maintenance)
        self.assertGreater(maintenance["per_action_accuracy"]["1"], .78)
        self.assertEqual(maintenance["samples"], report["frozen_onset"]["samples"])

    def test_rebuild_upgrades_old_feature_seed_in_actual_worker(self):
        report = run(feature_contract=2)
        self.assertEqual(report["persisted_feature_contract"], 4)
        self.assertTrue(report["pass"], report["predictions"])

    def test_neural_tournament_scores_whole_dwell_once(self):
        report = run(neural=True)
        self.assertTrue(report["pass"], report["predictions"])
        tournament = report["neural_tournament"]
        self.assertIsNotNone(tournament)
        self.assertIn("maintenance_holdout", tournament)
        self.assertEqual(tournament["samples"], report["maintenance_holdout"]["samples"])

    def test_numeric_pattern_survives_derived_binary_flags_below_threshold(self):
        report = run(contradictory_binary=True)
        self.assertTrue(report["pass"], report["predictions"])
        self.assertTrue(report["maintenance_holdout"]["passed"], report["maintenance_holdout"])
