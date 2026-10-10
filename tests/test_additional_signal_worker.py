import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
from additional_signal import normalize
from benchmark_kitchen_lighting import LIGHT, run


class AdditionalSignalWorkerTests(unittest.TestCase):
    def test_motion_only_history_and_explicit_daylight_goal(self):
        config=normalize({"entity_id":LIGHT,"purpose":"avoid_bright_on","threshold":40,
                          "hysteresis":5,"max_age_seconds":60})
        report=run(additional_signal=config,motion_only=True)
        self.assertTrue(report["pass"],report["predictions"])
        daylight=next(x for x in report["predictions"] if x["phase"]=="occupied_daylight")
        self.assertEqual(daylight["raw_prediction"],1)
        self.assertEqual(daylight["predicted"],0)
        self.assertGreater((report["additional_signal_objective"] or {}).get("daylight",{}).get("samples",0),0)
