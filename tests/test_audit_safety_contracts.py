import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import agent_candidate_preference_metrics as pref
from agent_candidate_atomic_promote import install as install_atomic_promote
from agent_candidate_shadow_runtime import ensure_shadow_tables
from agent_candidate_user_promotion import install as install_user_promotion
import test_atomic_promote_lifecycle as atomic_fixture
import test_candidate_preference_metrics as preference_fixture
import test_experiments as experiment_fixture


EXPECTED_FAST_GATES = {
    'data_freshness', 'version_configuration', 'offline_gate', 'action_coverage',
    'quality_regression', 'false_early', 'corrections', 'timing',
    'preference_evidence', 'execution_prerequisites',
}


class CandidatePromotionSafetyRegressionTests(unittest.TestCase):
    def _summary(self, candidate_false_early=0, live_false_early=0, pair_count=40):
        fixture = preference_fixture.CandidatePreferenceMetricTests()
        fixture.setUp()
        for i in range(pair_count):
            fixture._pair(200 + i * 200, i % 2, True, True, 1, 1)

        parent = {
            **fixture.parent,
            'min_value': 0.0, 'max_value': 1.0, 'confidence_threshold': 0.8,
            'deadband': 0.5, 'action_interval': 0.25, 'exploration_step': 1.0,
            'exploration_interval': 900.0, 'input_entities': ['binary_sensor.presence'],
        }
        child = {**parent, 'id': 'child', 'training_state': 'qualified'}
        row = {
            **fixture.row, 'state': 'comparing', 'feedback_revision': 1,
            'build_revision': 1, 'dirty': 0,
        }
        base = {
            'promotable': False,
            'samples': pair_count,
            'fresh_feedback_revision': True,
            'offline_gate_passed': True,
            'candidate_false_early': candidate_false_early,
            'live_false_early': live_false_early,
        }

        class Handler:
            def do_GET(self):
                pass
            def static(self, *args):
                pass

        fixture.store.get_model = lambda aid: {'version': 10}
        manager = SimpleNamespace(
            store=fixture.store,
            _comparison_summary=lambda *a, **kw: dict(base),
            status=Mock(return_value=None),
            list_status=Mock(return_value=[]),
            core=SimpleNamespace(Handler=Handler),
        )
        pref.install(manager)
        return fixture, manager._comparison_summary(row, parent, child)

    def test_1000_vs_0_false_early_is_blocked_despite_40_correct_pairs(self):
        fixture, result = self._summary(candidate_false_early=1000, live_false_early=0)
        try:
            self.assertGreaterEqual(result['preference_confidence'], result['preference_confidence_threshold'])
            self.assertTrue(result['promotion_gates']['action_coverage']['passed'])
            self.assertTrue(result['promotion_gates']['quality_regression']['passed'])
            self.assertFalse(result['promotion_gates']['false_early']['passed'])
            self.assertFalse(result['promotable'])
        finally:
            fixture.store.db.close()

    def test_correct_candidate_passes_all_named_standard_gates(self):
        fixture, result = self._summary(candidate_false_early=0, live_false_early=0)
        try:
            self.assertEqual(set(result['promotion_gates']), EXPECTED_FAST_GATES)
            self.assertTrue(all(g['passed'] for g in result['promotion_gates'].values()), result['promotion_gates'])
            self.assertEqual(result['promotion_vetoes'], [])
            self.assertTrue(result['promotable'])
        finally:
            fixture.store.db.close()

    def test_one_veto_is_sufficient_and_reports_reason(self):
        fixture, result = self._summary(candidate_false_early=5, live_false_early=0)
        try:
            self.assertEqual([v['gate'] for v in result['promotion_vetoes']], ['false_early'])
            self.assertIn('Candidate 5 > Live 0 + margin 4', result['promotion_veto_reasons'][0])
            self.assertFalse(result['promotable'])
        finally:
            fixture.store.db.close()

    def test_false_early_boundary_for_40_samples_is_exact(self):
        self.assertTrue(pref._false_early_safety({
            'samples': 40, 'candidate_false_early': 4, 'live_false_early': 0,
        }))
        self.assertFalse(pref._false_early_safety({
            'samples': 40, 'candidate_false_early': 5, 'live_false_early': 0,
        }))


class ProductionPromotionCompositionTests(unittest.TestCase):
    """Compose the same preference -> atomic -> custom layers used by production."""

    def setUp(self):
        self.fixture = atomic_fixture.AtomicPromoteTests()
        self.fixture.setUp()
        ensure_shadow_tables(self.fixture.store)
        with self.fixture.store.conn() as c:
            child = c.execute(
                'SELECT generation_id,parent_generation_id FROM agent_candidate_generations WHERE agent_id=?',
                (self.fixture.candidate['id'],),
            ).fetchone()
            for i in range(40):
                outcome = float(i % 2)
                c.execute(
                    '''INSERT INTO candidate_generation_pairs
                       (root_agent_id,parent_generation_id,child_generation_id,prediction_event_id,
                        prediction_ts,outcome_ts,outcome,parent_prediction,child_prediction,
                        parent_confidence,child_confidence,parent_correct,child_correct,paired_result,
                        parent_lead_seconds,child_lead_seconds,lead_gain_seconds)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (
                        self.fixture.root['id'], child['parent_generation_id'], child['generation_id'],
                        f'event:{i}', 1000.0 + i * 10, 1001.0 + i * 10, outcome, outcome, outcome,
                        .9, .9, 1, 1, 'both_correct', 1.0, 1.0, 0.0,
                    ),
                )

        manager = atomic_fixture.FakeManager(self.fixture.store, self.fixture.engine, self.fixture.root['id'])
        manager.core.Handler.static = lambda self, *args: None
        manager.before_live_process = lambda agent, state_map: None
        manager.after_live_process = lambda agent, state_map: None
        base = {
            'promotable': False,
            'samples': 40,
            'fresh_feedback_revision': True,
            'offline_gate_passed': True,
            'candidate_false_early': 1000,
            'live_false_early': 0,
        }
        manager._comparison_summary = lambda *a, **kw: dict(base)

        def base_status(parent_id):
            row = manager._candidate_row(parent_id)
            return {
                'parent_agent_id': str(parent_id),
                'candidate_id': row['candidate_id'],
                'root_agent_id': self.fixture.root['id'],
                'state': 'ready',
                'training_state': 'qualified',
                'offline_gate': {'status': 'passed', 'passed': True},
                'comparison': dict(base),
                'promotable': True,
            }

        manager.status = base_status
        manager.list_status = lambda: [base_status(self.fixture.root['id'])]
        manager.lineage_status = lambda ref: base_status(self.fixture.root['id'])

        manager = pref.install(manager)
        manager = install_atomic_promote(manager)
        manager = install_user_promotion(manager)
        self.manager = manager

    def tearDown(self):
        self.fixture.tearDown()

    def test_ui_status_and_atomic_promote_share_the_same_false_early_veto(self):
        status = self.manager.status(self.fixture.root['id'])
        self.assertFalse(status['promotable'])
        self.assertEqual([v['gate'] for v in status['promotion_vetoes']], ['false_early'])
        self.assertEqual(status['promotion_vetoes'], status['comparison']['promotion_vetoes'])
        with self.assertRaisesRegex(ValueError, 'needs more future paired evidence'):
            self.manager.promote(self.fixture.root['id'])
        status_after = self.manager.status(self.fixture.root['id'])
        self.assertEqual(status_after['promotion_vetoes'], status['promotion_vetoes'])

    def test_custom_promote_cannot_override_false_early_hard_gate(self):
        with self.assertRaisesRegex(ValueError, 'false_early'):
            self.manager.promote_custom(
                self.fixture.root['id'], 'shadow',
                {'min_future_samples': 0, 'min_per_binary_action': 0,
                 'max_future_regression_pp': None, 'allow_offline_gate_override': True},
            )


class ExperimentOutcomeSafetyRegressionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = experiment_fixture.ExperimentTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_background_presence_sensor_cannot_confirm_local_trial(self):
        f = self.fixture
        f.states['binary_sensor.other_room'] = {
            'entity_id': 'binary_sensor.other_room',
            'state': 'off',
            'attributes': {'device_class': 'motion'},
        }
        f.policy.schema = SimpleNamespace(entities=['binary_sensor.other_room'])

        trial = f.start()
        self.assertIn('binary_sensor.pir', trial['outcome_confirmers'])
        self.assertNotIn('binary_sensor.other_room', trial['outcome_confirmers'])
        f.ack()

        f.states['binary_sensor.other_room']['state'] = 'on'
        f.e.observe(f.a, f.states, 1.)
        result = f.e.status(f.a['id'])['last_outcome']

        self.assertIsNone(result['reward'])
        self.assertEqual(result['reason'], 'context changed; ordinary control resumes')

    def test_selected_presence_sensor_from_wrong_area_cannot_confirm(self):
        f = self.fixture
        registry = {
            'light.kitchen': {'area_id': 'kitchen'},
            'binary_sensor.pir': {'area_id': 'hall'},
        }

        trial = f.start(registry=registry)
        self.assertNotIn('binary_sensor.pir', trial['outcome_confirmers'])
        f.ack()

        f.states['binary_sensor.pir']['state'] = 'on'
        f.e.observe(f.a, f.states, 1.)
        result = f.e.status(f.a['id'])['last_outcome']

        self.assertIsNone(result['reward'])
        self.assertEqual(result['reason'], 'context changed; ordinary control resumes')

    def test_local_selected_presence_sensor_still_confirms_trial(self):
        f = self.fixture
        registry = {
            'light.kitchen': {'area_id': 'kitchen'},
            'binary_sensor.pir': {'area_id': 'kitchen'},
        }

        trial = f.start(registry=registry)
        self.assertIn('binary_sensor.pir', trial['outcome_confirmers'])
        f.ack()

        f.states['binary_sensor.pir']['state'] = 'on'
        f.e.observe(f.a, f.states, 1.)
        result = f.e.status(f.a['id'])['last_outcome']

        self.assertEqual(result['reward'], .6)
        self.assertEqual(result['reason'], 'presence confirmed after decision')


if __name__ == '__main__':
    unittest.main()
