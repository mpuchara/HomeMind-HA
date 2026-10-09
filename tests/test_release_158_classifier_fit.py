"""Full-column reference parity, feature identity and Shadow phase boundaries."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
from binary_state_classifier import BinaryStateClassifier
from runtime_debug_log import RuntimeDebugLogService
from tools.benchmark_classifier_fit import fixture, legacy_fit, compare
from training_phase_metrics import measure_training_phase
import test_release_151_candidate_epoch as paired


class ClassifierFit158Tests(unittest.TestCase):
    def parity(self, learner):
        before = copy.deepcopy(learner.export())
        reference = copy.deepcopy(learner)
        legacy_fit(reference)
        learner.fit()
        error = compare(reference, learner)
        for key in ('rows', 'recent', 'seen'):
            self.assertEqual(learner.export()[key], before[key])
        self.assertLess(error, 1e-10)
        return reference

    def test_sparse_high_dimension_fit_matches_original_optimizer(self):
        self.parity(fixture())

    def test_dense_fit_matches_original_optimizer(self):
        self.parity(fixture(dims=128, varying=127, rows=32))

    def test_noncontiguous_feature_identities_are_restored(self):
        learner = fixture(dims=256, varying=7, rows=16)
        self.parity(learner)
        varying_ids = set(learner.rows[0][0]['x'])
        for index in set(range(learner.dims)) - varying_ids:
            self.assertEqual(learner.weights[index], 0.)
            self.assertTrue(all(row[index] == 0. for row in learner.hinge_weights))

    def test_all_constant_features_fit_bias_with_zero_coefficients(self):
        learner = fixture(dims=32, varying=0, rows=8)
        self.parity(learner)
        self.assertTrue(learner.ready)
        self.assertEqual(learner.active, ())
        self.assertEqual(learner.pending, 0)

    def test_existing_variance_threshold_is_preserved(self):
        learner = fixture(dims=16, varying=0, rows=8)
        for action in (0, 1):
            for i, row in enumerate(learner.rows[action]):
                row['x'][3] = i * 1e-7
        self.parity(learner)
        self.assertNotIn(3, learner.active)

    def test_scale_floor_and_small_varying_features_are_preserved(self):
        learner = fixture(dims=16, varying=1, rows=8)
        for action in (0, 1):
            for row in learner.rows[action]:
                row['x'] = {3: .001 * row['id'] + .004 * action}
        self.parity(learner)
        self.assertEqual(learner.scale[3], .05)
        self.assertIn(3, learner.active)

    def test_imbalanced_classes_and_example_masses_match_reference(self):
        learner = fixture(dims=64, varying=11, rows=64)
        while len(learner.rows[0]) > 8: learner.rows[0].pop()
        learner.recent[0].clear()
        for action in (0, 1):
            for row in learner.rows[action]:
                row['weight'] = .0001 if row['id'] % 3 else 8.
        self.parity(learner)

    def test_recent_tail_and_reservoir_deduplicate_existing_ids(self):
        learner = fixture(dims=32, varying=5, rows=16)
        reference = self.parity(learner)
        self.assertEqual(learner.export()['recent'], reference.export()['recent'])

    def test_recent_examples_outside_reservoir_keep_their_weight(self):
        learner = fixture(dims=32, varying=5, rows=16)
        learner.recent[1].append(dict(x={3: 2., 7: .2}, weight=7., id=100))
        learner.seen[1] = 100
        self.parity(learner)

    def test_random_shapes_keep_coefficients_and_decisions(self):
        for dims, width, seed in [(8, 3, 2), (96, 31, 14), (512, 128, 48)]:
            with self.subTest(dims=dims, width=width):
                self.parity(fixture(dims=dims, varying=width, seed=seed, rows=16))

    def test_serialized_model_restores_full_dimensions_and_predictions(self):
        learner = fixture(dims=64, varying=9, rows=16)
        learner.fit()
        raw = json.loads(json.dumps(learner.export()))
        restored = BinaryStateClassifier(64, raw)
        self.assertEqual(json.loads(json.dumps(restored.export())), raw)
        self.assertEqual(len(restored.weights), 64)
        self.assertTrue(all(len(row) == 64 for row in restored.hinge_weights))
        compare(learner, restored)

    def test_schema_remap_then_fit_keeps_original_feature_meaning(self):
        learner = fixture(dims=64, varying=9, rows=16)
        learner.fit()
        learner.remap({i: 64-i for i in range(1, 64)})
        self.parity(learner)

    def test_fit_interval_remains_sixty_four_accepted_examples(self):
        learner = fixture(dims=32, varying=5, rows=8)
        learner.fit()
        before = learner.fits
        for _ in range(63): learner.add(1, {3: .5}, .1)
        self.assertEqual(learner.fits, before)
        learner.add(1, {3: .5}, .1)
        self.assertEqual(learner.fits, before + 1)

    def test_full_weight_correction_still_fits_immediately(self):
        learner = fixture(dims=32, varying=5, rows=8)
        learner.fit()
        before = learner.fits
        learner.add(1, {3: .5}, 1.)
        self.assertEqual(learner.fits, before + 1)

    def test_insufficient_class_support_does_not_fit_or_emit_phase(self):
        learner = fixture(dims=32, varying=5, rows=3)
        before = copy.deepcopy(learner.export())
        with patch('binary_state_classifier.measure_training_phase') as measured:
            learner.fit()
        measured.assert_not_called()
        self.assertEqual(learner.export(), before)

    def test_fit_metrics_report_real_compact_matrix_shape(self):
        learner = fixture(dims=512, varying=12, rows=16)
        debug = Mock(enabled=True)
        with patch('training_phase_metrics.RUNTIME_DEBUG', debug), patch('training_phase_metrics.TELEMETRY') as telemetry:
            learner.fit()
        telemetry.observe.assert_called_once()
        self.assertEqual(telemetry.observe.call_args.args[0], 'binary_classifier_fit')
        fields = debug.end.call_args.kwargs
        self.assertEqual(fields['status'], 'ok')
        self.assertEqual(fields['dims'], 512)
        self.assertEqual(fields['varying_features'], 12)
        self.assertEqual(fields['design_columns'], 48)
        self.assertEqual(fields['rows'], 32)

    def test_failed_phase_reports_error_and_propagates_exception(self):
        debug = Mock(enabled=True)
        with patch('training_phase_metrics.RUNTIME_DEBUG', debug), patch('training_phase_metrics.TELEMETRY') as telemetry:
            with self.assertRaisesRegex(RuntimeError, 'fit failed'):
                with measure_training_phase('binary_classifier_fit', dims=128):
                    raise RuntimeError('fit failed')
        self.assertEqual(debug.end.call_args.kwargs['status'], 'error')
        self.assertEqual(telemetry.observe.call_args.args[0], 'binary_classifier_fit')

    def test_fit_failure_records_phase_without_changing_coefficients(self):
        learner = fixture(dims=32, varying=5, rows=8)
        before = copy.deepcopy(learner.export())
        with patch.object(learner, '_fit', side_effect=RuntimeError('fit failed')):
            with self.assertRaises(RuntimeError): learner.fit()
        self.assertEqual(learner.export(), before)

    def test_real_paired_outcome_retains_order_and_emits_training_phases(self):
        case = paired.CandidateEpoch151Tests('test_paired_future_outcome_trains_after_champion_and_candidate_decay')
        case.setUp()
        self.addCleanup(case.doCleanups)
        with patch('training_phase_metrics.TELEMETRY') as telemetry:
            case.test_paired_future_outcome_trains_after_champion_and_candidate_decay()
        names = [call.args[0] for call in telemetry.observe.call_args_list]
        self.assertIn('context_candidate_score', names)
        self.assertIn('context_candidate_training', names)
        self.assertIn('context_candidate_serialization', names)
        self.assertLess(names.index('context_candidate_training'), names.index('context_candidate_serialization'))

    def test_runtime_debug_lists_all_training_phase_metrics_without_sql(self):
        payload = RuntimeDebugLogService(SimpleNamespace(STORE=None, ENGINE=None), None).export_payload()
        self.assertEqual(set(payload['notes']['context_training_phase_metrics']), {
            'binary_classifier_fit', 'context_candidate_training',
            'context_candidate_serialization', 'context_candidate_score', 'context_pool_selection'})
