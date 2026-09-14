import unittest

from context_tournament_quality import (
    quality_adjusted_ranking,
    quality_from_availability,
    quality_metrics,
    quality_replacement_gate,
)


class SensorQualityMathTests(unittest.TestCase):
    def test_initial_quality_is_exactly_availability(self):
        self.assertAlmostEqual(quality_from_availability(0.70), 0.70)
        self.assertAlmostEqual(quality_from_availability(1.0), 1.0)
        self.assertAlmostEqual(quality_from_availability(-1.0), 0.0)
        self.assertAlmostEqual(quality_from_availability(2.0), 1.0)
        self.assertIsNone(quality_from_availability(None))

    def test_quality_metrics_expose_requested_statistics(self):
        now = 200000.0
        row = {
            'opportunities': 100,
            'available_count': 70,
            'unknown_count': 20,
            'unavailable_count': 10,
            'event_count': 24,
            'first_observed_ts': now - 12 * 3600.0,
            'last_event_ts': now - 600.0,
            'failure_timestamps': [now - 60.0, now - 25 * 3600.0, now - 3600.0],
        }
        metrics = quality_metrics(row, now)
        self.assertAlmostEqual(metrics['availability'], 0.70)
        self.assertAlmostEqual(metrics['unknown_rate'], 0.20)
        self.assertAlmostEqual(metrics['unavailable_rate'], 0.10)
        self.assertAlmostEqual(metrics['event_frequency'], 2.0)
        self.assertAlmostEqual(metrics['stale_time'], 600.0)
        self.assertEqual(metrics['recent_failures'], 2)
        self.assertAlmostEqual(metrics['sensor_quality'], 0.70)

    def test_predictive_ranking_is_gain_times_quality(self):
        self.assertAlmostEqual(quality_adjusted_ranking(0.05, 0.70), 0.035)
        self.assertAlmostEqual(quality_adjusted_ranking(0.05, 1.00), 0.05)
        self.assertAlmostEqual(quality_adjusted_ranking(-0.02, 0.50), -0.01)

    def test_flaky_sensor_cannot_displace_equally_relevant_stable_sensor(self):
        gate = quality_replacement_gate(
            predictive_gain=0.05,
            challenger_quality=0.70,
            incumbent_quality=1.00,
            challenger_feature_score=0.60,
            incumbent_feature_score=0.60,
            required_gain=0.03,
        )
        # 5pp predictive gain is discounted to 3.5pp, but equal predictive relevance
        # becomes 0.42 vs 0.60 after quality, so the stable incumbent stays.
        self.assertAlmostEqual(gate['quality_adjusted_gain'], 0.035)
        self.assertAlmostEqual(gate['challenger_ranking_score'], 0.42)
        self.assertAlmostEqual(gate['incumbent_ranking_score'], 0.60)
        self.assertTrue(gate['gain_passes'])
        self.assertFalse(gate['incumbent_rank_passes'])
        self.assertFalse(gate['passes'])

    def test_good_and_reliable_challenger_can_pass(self):
        gate = quality_replacement_gate(
            predictive_gain=0.08,
            challenger_quality=0.98,
            incumbent_quality=0.95,
            challenger_feature_score=0.80,
            incumbent_feature_score=0.60,
            required_gain=0.03,
        )
        self.assertGreater(gate['quality_adjusted_gain'], 0.03)
        self.assertGreater(gate['challenger_ranking_score'], gate['incumbent_ranking_score'])
        self.assertTrue(gate['passes'])

    def test_missing_quality_is_conservative_for_replacement(self):
        gate = quality_replacement_gate(
            predictive_gain=0.50,
            challenger_quality=None,
            incumbent_quality=None,
            challenger_feature_score=1.0,
            incumbent_feature_score=0.1,
            required_gain=0.03,
        )
        self.assertEqual(gate['challenger_quality'], 0.0)
        self.assertEqual(gate['incumbent_quality'], 1.0)
        self.assertFalse(gate['passes'])


if __name__ == '__main__':
    unittest.main()
