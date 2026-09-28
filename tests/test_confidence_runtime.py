import tempfile
import unittest
from pathlib import Path

from support import ROOT  # noqa: F401 - installs adaptive_ai/src on sys.path
from confidence_contract import CONTRACT_VERSION, ProbabilityCalibrationJournal
from confidence_runtime import ConfidenceCalibrationService, install_runtime_semantics
from storage import Store


class _Home:
    VERSION = 2


class _Context:
    home = _Home()


class _Engine:
    def __init__(self):
        self.context = _Context()
        self._confidence_runtime_installed = False

    def runtime_for(self, agent):
        return {
            'last_confidence': .81,
            'last_expected_reward': .22,
            'historical_support': .64,
            'validation_accuracy': .75,
            'validation_lower_bound': .55,
            'validation_samples': 12,
            'home_forecast': {'occupancy_in_3s': .70, 'uncertainty': .18},
        }

    def status(self):
        return {'average_confidence': .72}


class ConfidenceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='confidence-runtime-')
        self.store = Store(Path(self.tmp.name) / 'test.db')
        self.journal = ProbabilityCalibrationJournal(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_live_runtime_exposes_separate_semantics_without_reinterpreting_legacy_value(self):
        engine = _Engine()
        install_runtime_semantics(engine, self.journal)
        runtime = engine.runtime_for({'id': 'light'})
        self.assertEqual(runtime['last_confidence'], .81)
        self.assertEqual(runtime['decision_strength'], .81)
        self.assertIn('not_probability', runtime['decision_strength_semantics'])
        self.assertEqual(runtime['expected_action_utility'], .22)
        self.assertEqual(runtime['data_coverage'], .64)
        self.assertEqual(runtime['presence_probability'], .70)
        self.assertEqual(runtime['forecast_uncertainty'], .18)
        self.assertEqual(runtime['confidence_contract']['version'], CONTRACT_VERSION)
        self.assertIn('manual_user_target_change', runtime['confidence_contract']['final_calibration_evidence_kinds'])
        status = engine.status()
        self.assertEqual(status['average_decision_strength'], .72)
        self.assertIn('not_probability', status['average_confidence_semantics'])

    def test_presence_probability_calibration_requires_stable_episode_and_deduplicates(self):
        engine = _Engine()
        install_runtime_semantics(engine, self.journal)
        service = engine.confidence_calibration
        with self.assertRaises(ValueError):
            service.record_presence(area_id='kitchen', horizon_seconds=3, episode_id='',
                                    prediction=.8, observed=1, source_kind='manual_ground_truth')
        self.assertTrue(service.record_presence(
            area_id='kitchen', horizon_seconds=3, episode_id='arrival-42', ts=100,
            prediction=.8, observed=1, source_kind='manual_ground_truth'))
        self.assertFalse(service.record_presence(
            area_id='kitchen', horizon_seconds=3, episode_id='arrival-42', ts=100,
            prediction=.8, observed=1, source_kind='manual_ground_truth'))
        report = service.presence_report(area_id='kitchen', horizon_seconds=3)
        self.assertEqual(report['episodes'], 1)
        self.assertAlmostEqual(report['effective_n'], 1.0)
        self.assertAlmostEqual(report['brier_score'], .04)

    def test_training_label_is_persisted_as_non_independent_and_not_counted(self):
        engine = _Engine()
        service = ConfidenceCalibrationService(engine, self.journal)
        self.assertTrue(service.record_presence(
            area_id='kitchen', horizon_seconds=3, episode_id='fit-1', ts=100,
            prediction=.9, observed=1, source_kind='training_fit'))
        report = service.presence_report(area_id='kitchen', horizon_seconds=3)
        self.assertEqual(report['episodes'], 0)
        self.assertIsNone(report['brier_score'])


if __name__ == '__main__':
    unittest.main()
