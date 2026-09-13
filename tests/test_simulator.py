import unittest
from support import *
sys.path.insert(0,str(ROOT/'tools'))
from simulate_anticipation import run


class SimulatorTests(unittest.TestCase):
    def test_real_policy_anticipates_and_negative_branch_clears(self):
        report=run()
        self.assertEqual(report['shadow']['intent']['status'],'SHADOW')
        self.assertEqual(report['control']['intent']['status'],'ACCEPTED')
        self.assertEqual(report['control']['forecast']['occupancy_now'],0)
        self.assertGreater(report['control']['forecast']['occupancy_in_3s'],.8)
        self.assertEqual(report['negative_branch']['desired_value'],0)
