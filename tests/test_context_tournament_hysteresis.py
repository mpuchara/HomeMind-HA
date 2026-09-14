import unittest

from support import *
import context_tournament_promotion as promotion
from context_tournament_hysteresis import beats_with_hysteresis, install
from settings import DEFAULT_OPTIONS


class TournamentHysteresisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install()

    def _eligible_binary_model(self, shadow_correct):
        now = 400000.0
        return now, {
            'samples': 200,
            'class_totals': [100, 100],
            'active_correct_by_class': [84, 84],
            'shadow_correct_by_class': [shadow_correct, shadow_correct],
            'observation_opportunities': 200,
            'available_observations': 200,
            'first_observed_ts': now - 3.1 * 86400.0,
            'last_observed_ts': now,
            'promotion_consecutive_wins': 3,
            'promotion_completed_windows': 3,
        }

    def test_examples_require_full_hysteresis_margin(self):
        self.assertFalse(beats_with_hysteresis(0.84, 0.85, 0.03))
        # Boundary is strict: exactly +3 p.p. is still not enough to churn schema.
        self.assertFalse(beats_with_hysteresis(0.84, 0.87, 0.03))
        self.assertTrue(beats_with_hysteresis(0.84, 0.89, 0.03))

    def test_cumulative_promotion_uses_old_plus_min_gain(self):
        now, model = self._eligible_binary_model(85)
        status = promotion.promotion_eligibility(model, [0.0, 1.0], now, None, DEFAULT_OPTIONS)
        self.assertFalse(status['ready'])
        self.assertFalse(status['checks']['hysteresis'])
        self.assertAlmostEqual(status['old_score'], 0.84)
        self.assertAlmostEqual(status['new_score'], 0.85)
        self.assertAlmostEqual(status['required_new_score'], 0.87)

        now, model = self._eligible_binary_model(87)
        status = promotion.promotion_eligibility(model, [0.0, 1.0], now, None, DEFAULT_OPTIONS)
        self.assertFalse(status['ready'])
        self.assertFalse(status['checks']['gain'])
        self.assertAlmostEqual(status['hysteresis_excess'], 0.0, places=12)

        now, model = self._eligible_binary_model(89)
        status = promotion.promotion_eligibility(model, [0.0, 1.0], now, None, DEFAULT_OPTIONS)
        self.assertTrue(status['ready'])
        self.assertTrue(status['checks']['hysteresis'])
        self.assertAlmostEqual(status['gain'], 0.05)
        self.assertAlmostEqual(status['hysteresis_excess'], 0.02)

    def test_each_evaluation_window_uses_same_strict_hysteresis(self):
        options = dict(DEFAULT_OPTIONS)
        options['context_tournament_min_gain'] = 0.03
        cfg = promotion.tournament_config(options)
        model = {
            'samples': 0,
            'evaluation_started_ts': 0.0,
            'promotion_epoch_version': promotion.PROMOTION_EPOCH_VERSION,
            'promotion_window_hours': 24.0,
            'promotion_window_start_ts': 0.0,
            'promotion_window_end_ts': 86400.0,
            'promotion_window_samples': 200,
            'promotion_window_class_totals': [100, 100],
            'promotion_window_active_correct_by_class': [84, 84],
            'promotion_window_shadow_correct_by_class': [87, 87],
            'promotion_window_active_abs_error_sum': 0.0,
            'promotion_window_shadow_abs_error_sum': 0.0,
            'promotion_consecutive_wins': 2,
            'promotion_completed_windows': 2,
            'promotion_window_history': [],
        }
        promotion.advance_windows(model, [0.0, 1.0], 86400.0, cfg)
        self.assertEqual(model['promotion_consecutive_wins'], 0)
        first = model['promotion_window_history'][-1]
        self.assertFalse(first['win'])
        self.assertAlmostEqual(first['baseline_score'], 0.84)
        self.assertAlmostEqual(first['challenger_score'], 0.87)
        self.assertAlmostEqual(first['required_challenger_score'], 0.87)

        # A clear +5 p.p. window can start a new winning streak.
        model['promotion_window_samples'] = 200
        model['promotion_window_class_totals'] = [100, 100]
        model['promotion_window_active_correct_by_class'] = [84, 84]
        model['promotion_window_shadow_correct_by_class'] = [89, 89]
        promotion.advance_windows(model, [0.0, 1.0], 2 * 86400.0, cfg)
        self.assertEqual(model['promotion_consecutive_wins'], 1)
        self.assertTrue(model['promotion_window_history'][-1]['win'])


if __name__ == '__main__':
    unittest.main()
