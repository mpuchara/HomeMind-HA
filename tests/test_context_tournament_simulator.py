import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from simulate_context_tournament import run_all


class ContextTournamentSimulatorTests(unittest.TestCase):
    def test_release_scenarios_good_sensor_noise_and_concept_drift(self):
        report = run_all()
        self.assertTrue(report['A']['promoted'])
        self.assertGreaterEqual(report['A']['gain'], 0.03)
        self.assertGreaterEqual(report['A']['consecutive_wins'], 3)
        self.assertEqual(report['B']['noise_sensors'], 300)
        self.assertEqual(report['B']['promotions'], 0)
        self.assertTrue(report['B']['schema_unchanged'])
        self.assertTrue(report['C']['automatic'])
        self.assertFalse(report['C']['manual_rebuild_called'])
        self.assertIn('sensor_a -> binary_sensor.sensor_b', report['C']['transition'])


if __name__ == '__main__':
    unittest.main()
