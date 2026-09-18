import json
import tempfile
import unittest
from pathlib import Path

from support import ROOT
from storage import Store
from confidence_contract import (
    CONTRACT_VERSION,
    DEFAULT_FINAL_EPISODES,
    DEFAULT_MIN_PER_ACTION,
    DEFAULT_SELECTION_EPISODES,
    DEFAULT_FINAL_MAX_REGRESSION,
    EvaluationEpochJournal,
    ProbabilityCalibrationJournal,
    action_quality_report,
    contract_descriptor,
    paired_future_quality_report,
    probability_calibration,
)


def pair(i, outcome, correct=True, confidence=.9, scope='room-a', cluster=None,
         parent_correct=True, eligible=True, evidence_kind='manual_user_target_change'):
    return {
        'root_agent_id': scope,
        'scope_id': scope,
        'prediction_event_id': f'ep-{i}',
        'outcome_ts': float(i * 60),
        'outcome': float(outcome),
        'parent_correct': 1 if parent_correct else 0,
        'child_correct': 1 if correct else 0,
        'parent_confidence': .8,
        'child_confidence': float(confidence),
        'evidence_kind': evidence_kind,
        'calibration_eligible': 1 if eligible else 0,
        'dependency_cluster': cluster or f'cluster-{i}',
    }


class ProbabilityCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='confidence-contract-')
        self.store = Store(Path(self.tmp.name) / 'test.db')
        self.journal = ProbabilityCalibrationJournal(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_replaying_same_episode_does_not_increase_effective_evidence(self):
        kwargs = dict(metric_id='occupancy_3s', model_key='room-v2', scope_id='kitchen',
                      episode_id='arrival-1', ts=10, prediction=.8, observed=1,
                      source_kind='manual_ground_truth', independent=True)
        self.assertTrue(self.journal.record(**kwargs))
        self.assertFalse(self.journal.record(**kwargs))
        report = self.journal.report('occupancy_3s', 'room-v2', 'kitchen')
        self.assertEqual(report['episodes'], 1)
        self.assertAlmostEqual(report['effective_n'], 1.0)

    def test_training_fit_labels_are_not_independent_calibration(self):
        self.journal.record(metric_id='occupancy_3s', model_key='room-v2', scope_id='kitchen',
                            episode_id='fit-1', ts=10, prediction=.9, observed=1,
                            source_kind='training_fit', independent=True)
        report = self.journal.report('occupancy_3s', 'room-v2', 'kitchen')
        self.assertEqual(report['episodes'], 0)
        self.assertIsNone(report['brier_score'])

    def test_overconfident_probability_is_detected_with_brier_and_reliability_bins(self):
        rows = []
        for i in range(12):
            rows.append({'episode_id': f'p-{i}', 'scope_id': 'kitchen', 'model_key': 'm1',
                         'ts': i * 60, 'prediction': .95, 'observed': 1 if i < 2 else 0,
                         'source_kind': 'independent_presence_label', 'independent': True,
                         'dependency_cluster': f'c-{i}'})
        report = probability_calibration(rows, scope_id='kitchen', model_key='m1')
        self.assertGreater(report['brier_score'], .5)
        self.assertTrue(report['overconfident'])
        self.assertEqual(len(report['reliability_bins']), 10)
        high = report['reliability_bins'][9]
        self.assertGreater(high['mean_prediction'], high['observed_frequency'])


class ActionQualityTests(unittest.TestCase):
    def test_off_only_never_qualifies_on_safety(self):
        rows = [pair(i, 0, True) for i in range(20)]
        report = action_quality_report(rows, scope_id='room-a', min_total=12, min_per_action=4)
        self.assertTrue(report['per_action']['OFF']['sufficient_evidence'])
        self.assertFalse(report['per_action']['ON']['sufficient_evidence'])
        self.assertFalse(report['sufficient_evidence'])
        self.assertEqual(report['recommendation'], 'abstain_insufficient_independent_evidence')

    def test_twelve_easy_episodes_in_one_room_do_not_prove_new_room(self):
        rows = [pair(i, i % 2, True, scope='room-a') for i in range(12)]
        learned = action_quality_report(rows, scope_id='room-a', min_total=12, min_per_action=4)
        unseen = action_quality_report(rows, scope_id='room-b', min_total=12, min_per_action=4)
        self.assertTrue(learned['sufficient_evidence'])
        self.assertEqual(unseen['effective_n'], 0)
        self.assertFalse(unseen['sufficient_evidence'])

    def test_dependent_burst_has_smaller_effective_n_than_raw_count(self):
        rows = [pair(i, i % 2, True, cluster='same-burst') for i in range(20)]
        report = action_quality_report(rows, scope_id='room-a', min_total=12, min_per_action=4)
        self.assertLess(report['effective_n'], 12)
        self.assertFalse(report['sufficient_evidence'])

    def test_overstated_decision_strength_is_flagged_without_calling_it_probability(self):
        rows = [pair(i, i % 2, correct=(i % 3 == 0), confidence=.97) for i in range(18)]
        report = action_quality_report(rows, scope_id='room-a', min_total=12, min_per_action=4)
        self.assertTrue(report['decision_strength_overstated'])
        self.assertFalse(report['probability_claim'])
        self.assertIn('not_probability', report['decision_strength_semantics'])


class FixedFutureEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='confidence-epoch-')
        self.store = Store(Path(self.tmp.name) / 'test.db')
        self.epochs = EvaluationEpochJournal(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_selection_and_final_evaluation_are_disjoint_and_final_end_locks(self):
        selection = [pair(i, i % 2, True) for i in range(12)]
        epoch = self.epochs.ensure('g0', 'g1', 'rev-a', 'diagonal_linucb:v10', selection,
                                   selection_target=12, final_target=12, min_per_action=4)
        self.assertIsNotNone(epoch)
        self.assertEqual(epoch['selection_cutoff_ts'], selection[-1]['outcome_ts'])
        collecting = self.epochs.final_report(epoch, selection, scope_id='room-a')
        self.assertFalse(collecting['sufficient_evidence'])
        future = selection + [pair(20+i, i % 2, True) for i in range(12)]
        completed = self.epochs.final_report(epoch, future, scope_id='room-a')
        self.assertTrue(completed['sufficient_evidence'])
        self.assertEqual(completed['status'], 'complete_passed')
        self.assertTrue(completed['promotion_quality_passed'])
        locked_end = completed['final_end_ts']
        later = future + [pair(100, 0, False), pair(101, 1, False)]
        still_locked = self.epochs.final_report(self.epochs.get('g0','g1','rev-a'), later, scope_id='room-a')
        self.assertEqual(still_locked['final_end_ts'], locked_end)
        self.assertEqual(still_locked['episodes'], completed['episodes'])

    def test_external_automation_transitions_do_not_complete_final_calibration(self):
        selection = [pair(i, i % 2, True) for i in range(12)]
        epoch = self.epochs.ensure('g0', 'g1', 'rev-a', 'diagonal_linucb:v11', selection)
        self.assertIsNotNone(epoch)
        external = selection + [
            pair(20+i, i % 2, True, eligible=False, evidence_kind='external_target_transition')
            for i in range(24)
        ]
        report = self.epochs.final_report(epoch, external, scope_id='room-a')
        self.assertFalse(report['sufficient_evidence'])
        self.assertFalse(report['promotion_quality_passed'])
        self.assertEqual(report['status'], 'collecting_fixed_future_test')
        self.assertIsNone(report['final_end_ts'])

    def test_failed_future_quality_locks_and_later_easy_rows_cannot_heal_it(self):
        selection = [pair(i, i % 2, True) for i in range(12)]
        epoch = self.epochs.ensure('g0', 'g1', 'rev-a', 'diagonal_linucb:v11', selection)
        self.assertIsNotNone(epoch)

        # Parent is right on every independent user label; child misses four of twelve,
        # including both ON and OFF contexts. Evidence is sufficient, quality is not.
        future_rows = []
        for i in range(12):
            child_ok = i not in {1, 4, 7, 10}
            future_rows.append(pair(20+i, i % 2, child_ok, parent_correct=True))
        failed = self.epochs.final_report(epoch, selection + future_rows, scope_id='room-a')
        self.assertTrue(failed['sufficient_evidence'])
        self.assertFalse(failed['promotion_quality_passed'])
        self.assertEqual(failed['status'], 'complete_failed_quality')
        self.assertFalse(failed['paired_delta']['non_regression_passed'])
        locked_end = failed['final_end_ts']
        self.assertIsNotNone(locked_end)

        later = selection + future_rows + [
            pair(100+i, i % 2, True, parent_correct=True) for i in range(20)
        ]
        still_failed = self.epochs.final_report(
            self.epochs.get('g0', 'g1', 'rev-a'), later, scope_id='room-a'
        )
        self.assertEqual(still_failed['final_end_ts'], locked_end)
        self.assertEqual(still_failed['episodes'], failed['episodes'])
        self.assertFalse(still_failed['promotion_quality_passed'])
        self.assertEqual(still_failed['status'], 'complete_failed_quality')

    def test_paired_future_report_requires_separate_on_off_non_regression(self):
        rows = [pair(i, i % 2, True, parent_correct=True) for i in range(12)]
        report = paired_future_quality_report(
            rows, scope_id='room-a', min_total=12, min_per_action=4,
            max_regression=DEFAULT_FINAL_MAX_REGRESSION,
        )
        self.assertTrue(report['sufficient_evidence'])
        self.assertTrue(report['promotion_quality_passed'])
        self.assertTrue(report['per_action_delta']['OFF']['non_regression_passed'])
        self.assertTrue(report['per_action_delta']['ON']['non_regression_passed'])
        self.assertEqual(report['evidence_contract'], 'future_manual_user_preference_labels_only')

    def test_backend_or_model_revision_requires_new_calibration_epoch(self):
        selection = [pair(i, i % 2, True) for i in range(12)]
        first = self.epochs.ensure('g0', 'g1', 'rev-a', 'diagonal_linucb:v10', selection)
        self.assertIsNotNone(first)

        # Backend-only change must create a distinct evaluation identity even when a
        # producer accidentally reuses the source model revision string.
        backend_only = self.epochs.ensure('g0', 'g1', 'rev-a', 'full_ridge_linucb:v1', selection)
        self.assertIsNotNone(backend_only)
        self.assertNotEqual(first['model_revision'], backend_only['model_revision'])
        self.assertNotEqual(first['backend_key'], backend_only['backend_key'])

        self.assertIsNone(self.epochs.get('g0', 'g1', 'rev-b'))
        second = self.epochs.ensure('g0', 'g1', 'rev-b', 'full_ridge_linucb:v1', selection)
        self.assertIsNotNone(second)
        self.assertNotEqual(first['model_revision'], second['model_revision'])


class ContractParityTests(unittest.TestCase):
    def test_runtime_build_info_and_ui_share_stage13_contract(self):
        descriptor = contract_descriptor()
        self.assertEqual(descriptor['version'], CONTRACT_VERSION)
        self.assertEqual(descriptor['selection_min_independent_episodes'], DEFAULT_SELECTION_EPISODES)
        self.assertEqual(descriptor['final_min_independent_episodes'], DEFAULT_FINAL_EPISODES)
        self.assertEqual(descriptor['final_min_per_action'], DEFAULT_MIN_PER_ACTION)
        self.assertEqual(descriptor['final_max_allowed_regression'], DEFAULT_FINAL_MAX_REGRESSION)
        self.assertIn('manual_user_target_change', descriptor['final_calibration_evidence_kinds'])
        self.assertIn('backend identity', descriptor['backend_recalibration'])
        self.assertIn('screening_only', descriptor['automation_replay'])

        build = json.loads((ROOT / 'adaptive_ai' / 'BUILD_INFO.json').read_text(encoding='utf-8'))
        self.assertEqual(build['confidence_contract_version'], CONTRACT_VERSION)
        self.assertEqual(build['confidence_selection_min_independent_episodes'], DEFAULT_SELECTION_EPISODES)
        self.assertEqual(build['confidence_final_min_independent_episodes'], DEFAULT_FINAL_EPISODES)
        self.assertEqual(build['confidence_final_min_per_action'], DEFAULT_MIN_PER_ACTION)
        self.assertEqual(build['confidence_final_max_allowed_regression'], DEFAULT_FINAL_MAX_REGRESSION)
        self.assertEqual(build['confidence_final_evidence'], 'manual_user_target_change')

        ui = (ROOT / 'adaptive_ai' / 'src' / 'static' / 'confidence_contract_ui.js').read_text(encoding='utf-8')
        self.assertIn('Decision strength', ui)
        self.assertIn('Preference alignment', ui)
        self.assertIn('Final empirical quality', ui)
        self.assertIn('OFF future safety', ui)
        self.assertIn('ON future safety', ui)
        self.assertIn('Paired quality delta', ui)
        self.assertIn('automation replay is screening only', ui)

        entrypoint = (ROOT / 'adaptive_ai' / 'src' / 'trial_queue_main.py').read_text(encoding='utf-8')
        runtime = (ROOT / 'adaptive_ai' / 'src' / 'runtime_composition.py').read_text(encoding='utf-8')
        self.assertIn('bind_final_composition', entrypoint)
        self.assertIn('install_confidence_contract', runtime)
        self.assertIn('confidence_contract_ready', runtime)


if __name__ == '__main__':
    unittest.main()
