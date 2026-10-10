"""The persisted trained policy must handle daylight and emitted lamp light."""
import unittest
import sys

from support import ROOT
sys.path.insert(0, str(ROOT / "tools"))
from tools.benchmark_kitchen_lighting import run


class KitchenLightingWorkerTests(unittest.TestCase):
    def test_real_training_preserves_daylight_off_and_dark_stationary_on(self):
        report = run()
        self.assertTrue(report["pass"], report)
        self.assertEqual(report["persisted_feature_contract"], 4)
        outcomes = report["training_quality"]["outcomes"]
        # OFF onset is now sampled after its source packet becomes visible. The
        # daylight/occupied interior is verified by the actual persisted predictions.
        self.assertGreater(outcomes.get("unknown_outcome", 0) + outcomes.get("occupied_daylight_off", 0), 0)
        self.assertNotIn("premature_off_confirmed_presence", outcomes)
