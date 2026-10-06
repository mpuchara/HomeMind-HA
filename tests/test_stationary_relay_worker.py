import sys
import unittest

from support import ROOT
sys.path.insert(0, str(ROOT / "tools"))
from benchmark_stationary_relay import run


class StationaryRelayWorkerTests(unittest.TestCase):
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
        self.assertEqual(report["persisted_feature_contract"], 3)
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
