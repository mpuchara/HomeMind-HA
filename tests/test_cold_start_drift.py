import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

from support import ROOT  # noqa: F401 - installs adaptive_ai/src on sys.path
from agent_candidates import ensure_tables as ensure_candidate_tables
from cold_start_drift import (
    AdaptationService,
    CONTRACT_VERSION,
    POST_PROMOTION_EPISODES,
    contract_descriptor,
    decay_contract,
    ensure_tables as ensure_adaptation_tables,
    install as install_adaptation,
)
from storage import Store


class _Context:
    registry_revision = 1
    home = type('Home', (), {'sources': {}})()

    def relevant_entities(self):
        return []

    def area_for(self, eid):
        return None

    def evidence_metadata(self, eid):
        return {}


class _Executor:
    def __init__(self):
        self.release_calls = 0
        self.take_calls = 0

    @contextmanager
    def target_lock(self, target):
        yield

    def release_control(self, agent, reason=None):
        self.release_calls += 1

    def take_control(self, agent, refresh=True):
        self.take_calls += 1


class _Engine:
    def __init__(self):
        self.state_map = {}
        self.context = _Context()
        self.executor = _Executor()
        self.models = {}
        self.runtime = {}


class _Manager:
    def __init__(self, store):
        self.store = store
        self.engine = _Engine()
        self.enqueue_calls = []
        self.promote_calls = 0

    def enqueue(self, parent_id, reason='feedback'):
        self.enqueue_calls.append((str(parent_id), str(reason)))
        now = 1000.0 + len(self.enqueue_calls)
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS agent_candidate_generations (
                    generation_id TEXT PRIMARY KEY,
                    root_agent_id TEXT NOT NULL,
                    agent_id TEXT,
                    parent_generation_id TEXT,
                    generation_number INTEGER NOT NULL,
                    generation_type TEXT NOT NULL,
                    lifecycle_state TEXT NOT NULL,
                    created_ts REAL NOT NULL,
                    updated_ts REAL NOT NULL
                )"""
            )
            c.execute(
                """INSERT OR REPLACE INTO agent_candidate_generations
                   (generation_id,root_agent_id,agent_id,parent_generation_id,generation_number,
                    generation_type,lifecycle_state,created_ts,updated_ts)
                   VALUES(?,?,?,?,1,'candidate','queued',?,?)""",
                (f'candidate:{parent_id}', str(parent_id), 'candidate-agent', f'root:{parent_id}', now, now),
            )
        return {'candidate_id': 'candidate-agent', 'generation_id': f'candidate:{parent_id}'}

    def after_live_process(self, agent, state_map):
        return {'agent_id': agent['id']}

    def _comparison_summary(self, row, parent=None, candidate=None):
        return {'promotable': True, 'promotion_gates': {}, 'promotion_vetoes': []}

    def status(self, parent_id):
        return {'parent_agent_id': str(parent_id), 'comparison': self._comparison_summary({'parent_agent_id': str(parent_id)})}

    def list_status(self):
        return []

    def promote(self, *args, **kwargs):
        self.promote_calls += 1
        raise AssertionError('Adaptation monitor must never promote automatically')


def _agent(store, *, name='Light'):
    return store.create_agent({
        'name': name,
        'target_entity': 'light.kitchen',
        'target_property': 'power',
        'min_value': 0,
        'max_value': 1,
        'confidence_threshold': .78,
        'deadband': 0,
        'action_interval': .25,
        'exploration_step': 1,
        'input_entities': ['*'],
    })


class ColdStartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='cold-start-drift-')
        self.store = Store(Path(self.tmp.name) / 'test.db')
        self.manager = _Manager(self.store)
        self.agent = _agent(self.store)
        self.service = AdaptationService(self.manager)

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_home_without_history_reports_missing_evidence_without_relaxing_safety(self):
        report = self.service.cold_start(self.agent)
        self.assertEqual(report['contract_version'], CONTRACT_VERSION)
        self.assertEqual(report['status'], 'missing_evidence')
        self.assertIn('target_history', report['missing_evidence'])
        self.assertFalse(report['safety']['thresholds_relaxed'])
        self.assertFalse(report['safety']['control_without_history'])
        self.assertIn(report['recommended_mode'], ('fallback_collect_evidence', 'fallback_plus_shadow_learning'))
        self.assertLessEqual(len(report['optional_questions']), 2)
        self.assertTrue(all(item['optional'] for item in report['optional_questions']))
        self.assertTrue(all(item['auto_dispatch'] is False for item in report['optional_questions']))
        self.assertTrue(report['safety']['stage13_final_evaluation_required'])
        self.assertTrue(report['safety']['optional_answers_do_not_bypass_promotion_gates'])
        self.assertTrue(all(item.get('selection_reason') for item in report['optional_questions']))

    def test_no_recognized_sensors_stays_cold_start_not_sensor_failure(self):
        for ts in range(1, 7):
            env = self.service._environment_from_runtime(self.agent, {})
            self.service.record_environment(self.agent['id'], ts, **env)
        detection = self.service.detect(self.agent['id'])
        self.assertFalse(detection['detected'])
        self.assertEqual(detection['environment_shift']['new_sensor_count'], 0)

    def test_decay_contract_never_expires_persistent_instruction_or_trains_regression_anchor(self):
        contract = decay_contract()
        self.assertFalse(contract['persistent_instruction']['decays'])
        self.assertEqual(contract['regression_anchor']['training_weight'], 0.0)
        self.assertGreater(contract['policy_learning']['half_life_days'], 0)
        self.assertGreater(contract['future_evaluation']['half_life_episodes'], 0)


class DriftClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='drift-classify-')
        self.store = Store(Path(self.tmp.name) / 'test.db')
        self.manager = _Manager(self.store)
        self.agent = _agent(self.store)
        self.aid = self.agent['id']
        self.service = AdaptationService(self.manager)
        self.service._state(self.aid)

    def tearDown(self):
        self.tmp.cleanup()

    def _env(self, ts, *, health=1.0, unavailable=0.0, signature='A', pref=0):
        self.service.record_environment(
            self.aid, ts,
            sensor_health=health,
            unavailable_fraction=unavailable,
            topology_signature=signature,
            topology=[{'entity_id': 'binary_sensor.motion', 'area_id': signature}],
            registry_revision=1,
            preference_revision=pref,
        )

    def test_moved_sensor_is_topology_change_and_creates_only_isolated_candidate(self):
        for ts in (1, 2, 3):
            self._env(ts, signature='kitchen')
        for ts in (4, 5, 6):
            self._env(ts, signature='hall')
        detection = self.service.detect(self.aid)
        self.assertTrue(detection['detected'])
        self.assertEqual(detection['kind'], 'topology_change')
        status = self.service._start_adaptation(self.aid, detection)
        self.assertEqual(status['state']['status'], 'candidate_active')
        self.assertEqual(len(self.manager.enqueue_calls), 1)
        self.assertTrue(self.manager.enqueue_calls[0][1].startswith('drift:topology_change'))
        self.assertEqual(self.manager.promote_calls, 0)
        self.assertFalse(status['promotion']['automatic'])
        self.assertTrue(status['promotion']['uses_existing_stage13_gate'])

    def test_activity_time_shift_with_quality_drop_is_new_habit(self):
        for i in range(12):
            self.service.record_episode(
                self.aid, f'base-{i}', 100 + i, quality=.95,
                activity_hour=8.0, context_bucket='routine',
            )
        for i in range(6):
            self.service.record_episode(
                self.aid, f'recent-{i}', 1000 + i, quality=.55,
                activity_hour=18.0, context_bucket='routine',
            )
        detection = self.service.detect(self.aid)
        self.assertTrue(detection['detected'])
        self.assertEqual(detection['kind'], 'new_habit')
        self.assertGreaterEqual(detection['episode_shift']['hour_shift'], 2.5)

    def test_new_household_pattern_is_context_distribution_shift_not_new_preference(self):
        for i in range(12):
            self.service.record_episode(
                self.aid, f'single-{i}', 100 + i, quality=.95,
                activity_hour=9.0, context_bucket='occupied_rooms:1',
            )
        for i in range(6):
            self.service.record_episode(
                self.aid, f'multi-{i}', 1000 + i, quality=.60,
                activity_hour=9.0, context_bucket='occupied_rooms:2+',
            )
        detection = self.service.detect(self.aid)
        self.assertTrue(detection['detected'])
        self.assertEqual(detection['kind'], 'new_habit')
        self.assertGreaterEqual(detection['episode_shift']['context_tv'], .35)

    def test_persistent_preference_after_long_silence_does_not_decay_away(self):
        for ts in (1, 2, 3):
            self._env(ts, pref=0)
        for ts in (10000, 10001, 10002):
            self._env(ts, pref=1)
        detection = self.service.detect(self.aid)
        self.assertTrue(detection['detected'])
        self.assertEqual(detection['kind'], 'new_preference')
        self.assertFalse(detection['decay']['persistent_instruction']['decays'])

    def test_single_transient_sensor_failure_does_not_trigger_candidate(self):
        self._env(1, health=1.0, unavailable=0.0)
        self._env(2, health=1.0, unavailable=0.0)
        self._env(3, health=1.0, unavailable=0.0)
        self._env(4, health=1.0, unavailable=0.0)
        self._env(5, health=.2, unavailable=.8)
        self._env(6, health=1.0, unavailable=0.0)
        detection = self.service.detect(self.aid)
        self.assertFalse(detection['detected'])
        self.assertEqual(self.manager.enqueue_calls, [])

    def test_sustained_sensor_failure_is_distinguished_from_behavior_drift(self):
        for ts in (1, 2, 3):
            self._env(ts, health=1.0, unavailable=0.0)
        for ts in (4, 5, 6):
            self._env(ts, health=.2, unavailable=.8)
        detection = self.service.detect(self.aid)
        self.assertTrue(detection['detected'])
        self.assertEqual(detection['kind'], 'sensor_failure')


class RegressionAnchorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='drift-anchor-')
        self.store = Store(Path(self.tmp.name) / 'test.db')
        self.manager = _Manager(self.store)
        self.parent = _agent(self.store, name='Parent')
        self.candidate = _agent(self.store, name='Candidate')
        self.service = AdaptationService(self.manager)
        self.aid = self.parent['id']
        now = time.time()
        with self.store.lock, self.store.conn() as db:
            for idx, desired in enumerate((0.0, 1.0), start=1):
                db.execute(
                    """INSERT INTO adaptation_regression_anchors
                       (agent_id,episode_id,reason,retained_ts,training_weight,
                        anchor_ts,desired_action,label_source)
                       VALUES(?,?,?,?,0,?,?,?)""",
                    (
                        self.aid, f'anchor-{idx}', 'test', now,
                        float(100 + idx), desired, 'manual_feedback:test',
                    ),
                )

    def tearDown(self):
        self.tmp.cleanup()

    def test_zero_weight_anchor_replay_detects_candidate_regression(self):
        def replay(agent_id, ts):
            desired_by_ts = {101.0: 0.0, 102.0: 1.0}
            desired = desired_by_ts[float(ts)]
            if str(agent_id) == str(self.aid):
                return {'complete': True, 'prediction': desired}
            # Candidate loses the ON anchor.
            return {'complete': True, 'prediction': 0.0}
        self.service._replay_prediction = replay
        report = self.service.regression_anchor_report(
            self.aid, self.candidate['id'], 'candidate:g1'
        )
        self.assertTrue(report['gate_applicable'])
        self.assertFalse(report['passed'])
        self.assertEqual(report['parent_correct'], 2)
        self.assertEqual(report['candidate_correct'], 1)
        self.assertEqual(report['training_weight'], 0.0)
        self.assertIn('not_stage13_future_calibration', report['evidence_semantics'])

    def test_regression_anchor_gate_is_additional_and_non_overridable(self):
        service = install_adaptation(self.manager).adaptation_service
        service._update_state(
            self.aid,
            status='candidate_active',
            candidate_generation_id='candidate:g1',
            candidate_agent_id=self.candidate['id'],
        )
        service.regression_anchor_report = lambda *args, **kwargs: {
            'gate_applicable': True,
            'passed': False,
            'training_weight': 0.0,
            'evidence_semantics': 'offline_regression_replay_not_future_calibration',
        }
        summary = self.manager._comparison_summary(
            {'parent_agent_id': self.aid, 'candidate_id': self.candidate['id']},
            self.parent, self.candidate,
        )
        gate = summary['promotion_gates']['drift_regression_anchors']
        self.assertFalse(gate['passed'])
        self.assertEqual(gate['custom_override'], 'never')
        self.assertFalse(summary['promotable'])

    def test_unreplayable_anchors_do_not_replace_stage13_future_gate(self):
        self.service._replay_prediction = lambda *args, **kwargs: {
            'complete': False, 'reason': 'historical_context_incomplete'
        }
        report = self.service.regression_anchor_report(
            self.aid, self.candidate['id'], 'candidate:g1'
        )
        self.assertFalse(report['gate_applicable'])
        self.assertIsNone(report['passed'])
        self.assertIn('stage13_still_required', report['recommendation'])


class AdaptationMigrationTests(unittest.TestCase):
    def test_v1_tables_upgrade_additively(self):
        tmp = tempfile.TemporaryDirectory(prefix='drift-migration-')
        try:
            store = Store(Path(tmp.name) / 'legacy.db')
            with store.lock, store.conn() as db:
                db.executescript(
                    """CREATE TABLE adaptation_state (
                       agent_id TEXT PRIMARY KEY, contract_version INTEGER NOT NULL,
                       status TEXT NOT NULL, episodes_to_recover INTEGER,
                       preference_revision_seen INTEGER NOT NULL DEFAULT 0,
                       updated_ts REAL NOT NULL);
                       CREATE TABLE adaptation_regression_anchors (
                       agent_id TEXT NOT NULL, episode_id TEXT NOT NULL,
                       reason TEXT NOT NULL, retained_ts REAL NOT NULL,
                       training_weight REAL NOT NULL DEFAULT 0,
                       PRIMARY KEY(agent_id,episode_id));"""
                )
                db.execute(
                    "INSERT INTO adaptation_state(agent_id,contract_version,status,updated_ts) VALUES('a',1,'monitoring',1)"
                )
                db.execute(
                    "INSERT INTO adaptation_regression_anchors(agent_id,episode_id,reason,retained_ts,training_weight) VALUES('a','e','old',1,0)"
                )
            ensure_adaptation_tables(store)
            with store.conn() as db:
                state_cols = {row['name'] for row in db.execute('PRAGMA table_info(adaptation_state)').fetchall()}
                anchor_cols = {row['name'] for row in db.execute('PRAGMA table_info(adaptation_regression_anchors)').fetchall()}
                state = dict(db.execute("SELECT * FROM adaptation_state WHERE agent_id='a'").fetchone())
                anchor = dict(db.execute("SELECT * FROM adaptation_regression_anchors WHERE agent_id='a'").fetchone())
            self.assertIn('recovery_seconds', state_cols)
            self.assertIn('anchor_ts', anchor_cols)
            self.assertIn('desired_action', anchor_cols)
            self.assertIn('label_source', anchor_cols)
            self.assertEqual(state['status'], 'monitoring')
            self.assertEqual(anchor['training_weight'], 0.0)
            self.assertIsNone(anchor['desired_action'])
        finally:
            tmp.cleanup()


class AdaptationContractParityTests(unittest.TestCase):
    def test_runtime_build_info_and_contract_share_stage14_v2_semantics(self):
        contract = contract_descriptor()
        self.assertEqual(contract['version'], CONTRACT_VERSION)
        self.assertEqual(
            contract['recovery_metrics'],
            ['episodes_to_recover', 'seconds_to_recover'],
        )
        self.assertEqual(contract['regression_anchors']['training_weight'], 0.0)
        self.assertTrue(contract['regression_anchors']['not_final_calibration'])

        build = json.loads(
            (ROOT / 'adaptive_ai' / 'BUILD_INFO.json').read_text(encoding='utf-8')
        )
        self.assertEqual(build['controlled_adaptation_contract_version'], CONTRACT_VERSION)
        self.assertEqual(
            build['drift_recovery_metrics'],
            ['episodes_to_recover', 'seconds_to_recover'],
        )
        self.assertIn('zero-training-weight', build['drift_regression_anchors'])
        self.assertIn('Stage-13 v2', build['drift_promotion'])

        composition = (
            ROOT / 'adaptive_ai' / 'src' / 'runtime_composition.py'
        ).read_text(encoding='utf-8')
        self.assertIn('"controlled_adaptation"', composition)
        self.assertIn('zero_weight_cached_offline_replay_guard', composition)
        self.assertIn('Stage13_v2_future_holdout_remains_authoritative', composition)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='drift-recovery-')
        self.store = Store(Path(self.tmp.name) / 'test.db')
        ensure_candidate_tables(self.store)
        self.manager = _Manager(self.store)
        self.agent = _agent(self.store)
        self.aid = self.agent['id']
        self.service = AdaptationService(self.manager)
        self.service._state(self.aid)

    def tearDown(self):
        self.tmp.cleanup()

    def test_quality_recovery_reports_episode_count(self):
        self.service._update_state(
            self.aid,
            status='promoted_monitoring',
            promoted_ts=100.0,
            baseline_quality=.80,
        )
        for i in range(POST_PROMOTION_EPISODES):
            self.service.record_episode(
                self.aid, f'post-{i}', 101 + i, quality=.82,
                activity_hour=8.0, context_bucket='stable',
            )
        result = self.service._post_promotion_monitor(self.aid)
        self.assertEqual(result, 'recovered')
        state = self.service.status(self.aid)['state']
        self.assertEqual(state['status'], 'recovered')
        self.assertEqual(state['episodes_to_recover'], POST_PROMOTION_EPISODES)
        self.assertEqual(state['recovery_seconds'], 6.0)
        self.assertEqual(self.service.status(self.aid)['recovery']['seconds_to_recover'], 6.0)
        self.assertIsNotNone(state['recovered_ts'])

    def test_post_promotion_regression_restores_previous_model(self):
        old_model = {'version': 10, 'model_revision': 'old', 'schema': {'version': 11}}
        new_model = {'version': 10, 'model_revision': 'new', 'schema': {'version': 11}}
        self.store.save_model(self.aid, old_model)
        root = self.store.get_agent_config(self.aid)
        now = time.time()
        with self.store.lock, self.store.conn() as c:
            row = c.execute(
                """INSERT INTO agent_generation_backups
                   (agent_id,generation,created_ts,expires_ts,model_json,agent_json,comparison_json)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    self.aid, 0, now, now + 86400.0, json.dumps(old_model),
                    json.dumps(root, default=str), '{}',
                ),
            )
            backup_id = int(row.lastrowid)
        self.store.save_model(self.aid, new_model)
        self.service._update_state(
            self.aid,
            status='promoted_monitoring',
            promoted_ts=100.0,
            baseline_quality=.90,
            rollback_backup_id=backup_id,
        )
        for i in range(POST_PROMOTION_EPISODES):
            self.service.record_episode(
                self.aid, f'bad-{i}', 101 + i, quality=.40,
                activity_hour=8.0, context_bucket='bad',
            )
        result = self.service._post_promotion_monitor(self.aid)
        self.assertEqual(result, 'rolled_back')
        self.assertEqual(self.store.get_model(self.aid)['model_revision'], 'old')
        self.assertEqual(self.service.status(self.aid)['state']['status'], 'rolled_back')


if __name__ == '__main__':
    unittest.main()
