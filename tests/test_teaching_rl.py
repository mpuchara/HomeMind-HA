import json
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import *
from context import TemporalHistory
from policy import MultiHorizonPolicy
from storage import Store
from teaching_rl import RLTeaching, fingerprint, _feature_evidence


class FakeTeaching:
    def __init__(self, states_by_ts):
        self.states_by_ts = states_by_ts

    def point_context(self, engine, agent, timestamp, policy=None):
        ts = min(self.states_by_ts, key=lambda x: abs(float(x)-float(timestamp)))
        states = {eid: dict(st) for eid, st in self.states_by_ts[ts].items()}
        temporal = TemporalHistory()
        for eid, st in states.items():
            temporal.add(eid, float(timestamp), st)
        return states, temporal, True


class FakeEngine:
    def __init__(self, store, states, registry):
        self.store = store
        self.state_map = states
        self.entity_registry = registry
        self.context_relevance = {}
        self.lock = threading.RLock()
        self.models = {}
        self.runtime = {}
        self.wake_event = threading.Event()
        self.teaching = None

    def policy(self, agent):
        if agent['id'] not in self.models:
            raw = self.store.get_model(agent['id'])
            self.models[agent['id']] = MultiHorizonPolicy(
                agent, self.state_map, self.entity_registry, set(), model=raw
            )
        return self.models[agent['id']]


def make_agent(store):
    a = store.create_agent({
        'name': 'Kitchen', 'target_entity': 'light.kitchen', 'target_property': 'power',
        'min_value': 0, 'max_value': 1, 'deadband': .5, 'confidence_threshold': .78,
        'action_interval': 1, 'exploration_step': 1, 'input_entities': ['*'],
    })
    store.set_training_state(a['id'], 'qualified', score=1.0, samples=80,
                             source='test', detail={'counts': {'samples': 80, 'correct': 80}})
    return store.get_agent_config(a['id'])


class TeachRLTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='teach-rl-')
        self.store = Store(Path(self.temp.name)/'test.db')
        self.now = time.time()
        self.good = 'binary_sensor.espn4_presence'
        self.bad = 'binary_sensor.kitchen_motion'
        self.battery = 'sensor.espn4_battery_level'
        self.states = {
            'light.kitchen': state('light.kitchen', 'off'),
            self.good: state(self.good, 'off', device_class='occupancy'),
            self.bad: state(self.bad, 'off', device_class='motion'),
            self.battery: state(self.battery, '80', device_class='battery', unit_of_measurement='%'),
        }
        self.registry = {
            'light.kitchen': {'area_id': 'kitchen'},
            self.bad: {'area_id': 'kitchen'},
            self.good: {'area_id': 'bathroom', 'platform': 'esphome'},
            self.battery: {'area_id': 'bathroom', 'platform': 'esphome'},
        }
        self.agent = make_agent(self.store)
        self.engine = FakeEngine(self.store, self.states, self.registry)
        self.service = RLTeaching(self.store, self.engine)

    def tearDown(self):
        self.temp.cleanup()

    def _insert_labels_and_history(self, samples):
        fp = fingerprint(self.agent)
        with self.store.lock, self.store.conn() as c:
            c.executemany(
                "INSERT INTO teaching_rl_labels(agent_id,created_ts,sample_ts,desired,previous_desired,fingerprint) VALUES(?,?,?,?,?,?)",
                [(self.agent['id'], self.now, ts, desired, 0, fp) for ts, desired, _, _ in samples],
            )
        rows = []
        for ts, desired, good_state, bad_state in samples:
            rows.extend([
                (self.good, ts, good_state, {'device_class': 'occupancy'}, None, 'test'),
                (self.bad, ts, bad_state, {'device_class': 'motion'}, None, 'test'),
                (self.battery, ts, 90 if desired else 10,
                 {'device_class': 'battery', 'unit_of_measurement': '%'}, None, 'test'),
            ])
        self.store.archive_batch(rows)
        return samples

    def _labels_and_history(self):
        # Twelve balanced labels over 66 hours satisfy the production feature gate:
        # >=12 total, >=5 per binary class and >=2 observation days.
        samples = []
        for i in range(12):
            ts = self.now - (11-i) * 6 * 3600
            desired = 1 if i % 2 == 0 else 0
            good_state = 'on' if desired else 'off'
            bad_state = 'on' if i < 6 else 'off'
            samples.append((ts, desired, good_state, bad_state))
        return self._insert_labels_and_history(samples)

    def test_feature_evidence_gate_requires_twelve_labels(self):
        labels = [
            {'sample_ts': self.now-3*86400, 'desired': 0.0},
            {'sample_ts': self.now, 'desired': 1.0},
        ]
        eligible, stats = _feature_evidence(self.agent, labels)
        self.assertFalse(eligible)
        self.assertEqual(stats['min_labels'], 12)
        self.assertIn('12', stats['reason'])

    def test_binary_feature_evidence_requires_five_per_class(self):
        labels = []
        for i in range(12):
            desired = 1.0 if i < 8 else 0.0  # 8 ON / 4 OFF: total is enough, balance is not.
            labels.append({'sample_ts': self.now-(11-i)*6*3600, 'desired': desired})
        eligible, stats = _feature_evidence(self.agent, labels)
        self.assertFalse(eligible)
        self.assertEqual(stats['binary_class_counts']['0.0'], 4)
        self.assertEqual(stats['binary_class_counts']['1.0'], 8)
        self.assertIn('each binary class', stats['reason'])

    def test_feature_evidence_requires_two_observation_days(self):
        labels = [
            {'sample_ts': self.now-(11-i)*3600, 'desired': float(i % 2)}
            for i in range(12)
        ]
        eligible, stats = _feature_evidence(self.agent, labels)
        self.assertFalse(eligible)
        self.assertLess(stats['observation_days'], 2)
        self.assertIn('observation days', stats['reason'])

    def test_continuous_feature_evidence_requires_three_action_bins(self):
        continuous = dict(self.agent)
        continuous.update(target_property='brightness_pct', min_value=0, max_value=100)
        two_ranges = [
            {'sample_ts': self.now-(11-i)*6*3600, 'desired': 0.0 if i % 2 == 0 else 100.0}
            for i in range(12)
        ]
        eligible, stats = _feature_evidence(continuous, two_ranges)
        self.assertFalse(eligible)
        self.assertEqual(stats['desired_action_bins'], 2)

        three_ranges = [
            {'sample_ts': self.now-(11-i)*6*3600, 'desired': (0.0, 50.0, 100.0)[i % 3]}
            for i in range(12)
        ]
        eligible, stats = _feature_evidence(continuous, three_ranges)
        self.assertTrue(eligible)
        self.assertGreaterEqual(stats['desired_action_bins'], 3)

    def test_supervised_scores_do_not_start_from_two_contrasting_labels(self):
        samples = [
            (self.now-3*86400, 0, 'off', 'off'),
            (self.now, 1, 'on', 'on'),
        ]
        self._insert_labels_and_history(samples)
        scores, stats = self.service.supervised_scores(self.agent)
        self.assertEqual(scores, {})
        self.assertFalse(stats['feature_selection_eligible'])
        self.assertIn('12', stats['reason'])

    def test_insufficient_teach_evidence_preserves_existing_schema(self):
        limited_agent = dict(self.agent)
        limited_agent['input_entities'] = [self.bad]
        policy = MultiHorizonPolicy(limited_agent, self.states, self.registry, set())
        self.store.save_model(self.agent['id'], policy.serialize())
        self.engine.models[self.agent['id']] = policy
        before = list(policy.schema.entities)

        samples = [
            (self.now-3*86400, 0, 'off', 'off'),
            (self.now, 1, 'on', 'on'),
        ]
        self._insert_labels_and_history(samples)
        selected, meta, scores = self.service.select_features(self.agent)
        self.assertEqual(selected, before)
        self.assertEqual(scores, {})
        self.assertFalse(meta['teach_rl_feature_selection_eligible'])
        self.assertNotIn(self.good, selected)

    def test_full_context_scores_hidden_sensor_and_excludes_diagnostics(self):
        self._labels_and_history()
        scores, stats = self.service.supervised_scores(self.agent)
        self.assertEqual(stats['labels'], 12)
        self.assertTrue(stats['feature_selection_eligible'])
        self.assertIn(self.good, scores)
        self.assertGreater(scores[self.good], .7)
        self.assertNotIn(self.battery, scores)

    def test_feature_selection_can_promote_sensor_outside_current_schema(self):
        self._labels_and_history()
        self.engine.context_relevance[self.agent['id']] = {self.bad: .35}
        with patch.dict('settings.OPTIONS', {'fast_max_context_entities': 2, 'teach_rl_feature_score': .60}):
            selected, meta, scores = self.service.select_features(self.agent)
        self.assertIn(self.good, selected)
        self.assertIn(self.good, meta['teach_rl_scores'])
        self.assertTrue(meta['teach_rl_feature_selection_eligible'])
        self.assertGreater(scores[self.good], .7)

    def test_prepare_retrain_defers_selection_until_recorder_context_is_ready(self):
        self._labels_and_history()
        with patch.dict('settings.OPTIONS', {'fast_max_context_entities': 2, 'teach_rl_feature_score': .60}):
            queued = self.service.prepare_retrain(self.agent)
        # HTTP/queue admission must stay cheap: no temporary selector is installed yet.
        self.assertEqual(self.store.get_agent_config(self.agent['id'])['input_entities'], ['*'])
        self.assertEqual(queued['stage'], 'queued')
        self.assertEqual(queued['selected'], [])

        # The queue thread first refreshes Recorder context, then re-scores the full
        # eligible universe and only then constrains the deterministic Rebuild.
        refreshed = {'chunks': 2, 'rows': 8, 'windows': 2, 'candidates': 3}
        with patch.object(self.service, 'refresh_label_context', return_value=refreshed), \
             patch.dict('settings.OPTIONS', {'fast_max_context_entities': 2, 'teach_rl_feature_score': .60}):
            report = self.service.prepare_context_selection(self.agent)
        temporary = self.store.get_agent_config(self.agent['id'])['input_entities']
        self.assertNotEqual(temporary, ['*'])
        self.assertIn(self.good, temporary)
        self.assertIn(self.good, report['selected'])
        self.assertEqual(report['context_refresh'], refreshed)
        self.assertTrue(report['feature_selection_eligible'])
        self.assertEqual(report['stage'], 'features_selected')
        self.assertFalse(self.service.needs_context_selection(self.agent['id']))
        with self.store.conn() as c:
            row = c.execute('SELECT original_inputs_json,state FROM teaching_rl_jobs WHERE agent_id=?',
                            (self.agent['id'],)).fetchone()
        self.assertEqual(json.loads(row['original_inputs_json']), ['*'])
        self.assertEqual(row['state'], 'selected')

    def test_prepare_context_selection_skips_schema_rescreen_when_evidence_is_insufficient(self):
        limited_agent = dict(self.agent)
        limited_agent['input_entities'] = [self.bad]
        policy = MultiHorizonPolicy(limited_agent, self.states, self.registry, set())
        self.store.save_model(self.agent['id'], policy.serialize())
        self.engine.models[self.agent['id']] = policy
        before = list(policy.schema.entities)
        samples = [
            (self.now-3*86400, 0, 'off', 'off'),
            (self.now, 1, 'on', 'on'),
        ]
        self._insert_labels_and_history(samples)
        self.service.prepare_retrain(self.agent)
        with patch.object(self.service, 'refresh_label_context') as refresh:
            report = self.service.prepare_context_selection(self.agent)
        refresh.assert_not_called()
        self.assertFalse(report['feature_selection_eligible'])
        self.assertEqual(report['selected'], before)
        self.assertEqual(report['added'], [])
        self.assertEqual(report['removed'], [])
        self.assertTrue(report['context_refresh']['skipped'])

    def test_finalize_changes_base_policy_and_restores_selector(self):
        t0, t1 = self.now-100, self.now-50
        on_states = {**self.states, self.good: state(self.good, 'on', device_class='occupancy')}
        off_states = {**self.states, self.good: state(self.good, 'off', device_class='occupancy')}
        self.engine.teaching = FakeTeaching({t0: on_states, t1: off_states})
        fp = fingerprint(self.agent)
        with self.store.lock, self.store.conn() as c:
            c.execute("INSERT INTO teaching_rl_labels(agent_id,created_ts,sample_ts,desired,previous_desired,fingerprint) VALUES(?,?,?,?,?,?)",
                      (self.agent['id'], self.now, t0, 1, 0, fp))
            c.execute("INSERT INTO teaching_rl_labels(agent_id,created_ts,sample_ts,desired,previous_desired,fingerprint) VALUES(?,?,?,?,?,?)",
                      (self.agent['id'], self.now, t1, 0, 1, fp))
        temp_agent = dict(self.agent);temp_agent['input_entities']=[self.good]
        policy = MultiHorizonPolicy(temp_agent, on_states, self.registry, set())
        self.store.save_model(self.agent['id'], policy.serialize())
        self.service._set_inputs_direct(self.agent['id'], [self.good])
        with self.store.lock, self.store.conn() as c:
            c.execute("INSERT OR REPLACE INTO teaching_rl_jobs(agent_id,requested_ts,state,original_inputs_json,pre_schema_json,selected_inputs_json,report_json) VALUES(?,?,?,?,?,?,?)",
                      (self.agent['id'], self.now, 'training', '["*"]', '[]', json.dumps([self.good]), '{}'))
        report = self.service.finalize_retrain(self.agent['id'])
        self.assertEqual(report['labels_applied'], 2)
        self.assertEqual(self.store.get_agent_config(self.agent['id'])['input_entities'], ['*'])
        raw = self.store.get_model(self.agent['id'])
        learned = MultiHorizonPolicy(self.store.get_agent_config(self.agent['id']), on_states, self.registry, set(), model=raw)
        for ts, desired_states, desired in ((t0, on_states, 1), (t1, off_states, 0)):
            temporal = TemporalHistory()
            for eid, st in desired_states.items():
                temporal.add(eid, ts, st)
            features = learned.features(desired_states, temporal, at_ts=ts)[0]
            self.assertEqual(learned.predict(features)[0]['value'], desired)

    def test_undo_only_marks_rl_label_and_does_not_touch_wrong_decision_table(self):
        self._labels_and_history()
        before = len(self.service.labels(self.agent['id']))
        result = self.service.undo(self.agent)
        self.assertEqual(len(self.service.labels(self.agent['id'])), before-1)
        self.assertTrue(result['undone_id'])
        with self.store.conn() as c:
            exists = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='teaching_labels'").fetchone()
        self.assertFalse(exists)


class TeachRLFrontendContract(unittest.TestCase):
    def test_teach_defaults_to_ten_minutes_and_uses_separate_endpoints(self):
        text = (ROOT/'adaptive_ai/src/static/manual_feedback.js').read_text(encoding='utf-8')
        self.assertIn('range={start:end-600,end}', text)
        self.assertIn('/teach-rl-history?', text)
        self.assertIn('/teach-rl-point?', text)
        self.assertIn("teachPost('teach-rl-train')", text)

    def test_selection_is_visible_on_dark_chart(self):
        text = (ROOT/'adaptive_ai/src/static/manual_feedback.js').read_text(encoding='utf-8')
        self.assertIn('fill="#9aa0a6"', text)
        self.assertIn('fill-opacity="0.32"', text)
        self.assertIn('data-selection', text)

    def test_wrong_decision_keeps_existing_teaching_endpoint(self):
        text = (ROOT/'adaptive_ai/src/static/manual_feedback.js').read_text(encoding='utf-8')
        wrong = text.split('window.wrongDecision=',1)[1].split('window.undoTeaching=',1)[0]
        self.assertIn('await submit(id,desired===undefined?{}:{desired_value:desired},button)', wrong)
        submit = text.split('async function submit',1)[1].split('window.wrongDecision=',1)[0]
        self.assertIn('/teaching`', submit)
        self.assertNotIn('teach-rl', wrong)


if __name__ == '__main__':
    unittest.main()
