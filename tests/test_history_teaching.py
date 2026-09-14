import copy
import json
import time
import unittest
from unittest.mock import patch
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
from support import agent, state
import test_executor as fixtures
from teaching import Teaching
from context import ExplicitFeatureSchema


class HistoryTeachingTests(unittest.TestCase):
    setUp = fixtures.ExecutorTests.setUp
    tearDown = fixtures.ExecutorTests.tearDown
    intent = fixtures.ExecutorTests.intent

    def prepare(self, mode='shadow'):
        self.store.update_agent(self.a['id'], {'mode': mode})
        self.a = self.store.get_agent_config(self.a['id'])
        self.e.state_map['binary_sensor.motion'] = state('binary_sensor.motion', 'on', device_class='motion')
        self.model.schema = ExplicitFeatureSchema(self.model.dims, ['binary_sensor.motion'])
        self.e.runtime[self.a['id']] = {'last_prediction': 0}
        self.manager = self.e.teaching
        return self.manager

    def test_one_wrong_decision_overrules_thousands_of_old_samples(self):
        m = self.prepare()
        features, _, _ = self.model.features(self.e.state_map, self.e.temporal_history)
        for _ in range(2000):
            for h in self.model.horizons: self.model.update(h, 0, features, 1)
        for h in self.model.horizons:
            self.model.update(h, 0, features, -1)
            self.model.update(h, 1, features, 1)
        self.assertEqual(self.model.predict(features)[0]['value'], 0)
        before = copy.deepcopy(self.model.serialize())
        result = m.teach(self.e, self.a)
        self.assertEqual(result['desired_value'], 1)
        self.assertEqual(self.e.runtime[self.a['id']]['last_prediction'], 1)
        self.assertEqual(self.model.serialize(), before)
        self.service.assert_not_called()

    def test_undo_preserves_unrelated_online_learning(self):
        m = self.prepare()
        m.teach(self.e, self.a, 1)
        features, _, _ = self.model.features(self.e.state_map, self.e.temporal_history)
        self.model.update(min(self.model.horizons), 0, features, .75)
        before = copy.deepcopy(self.model.serialize())
        m.undo(self.e, self.a)
        self.assertEqual(self.model.serialize(), before)
        self.assertEqual(self.e.runtime[self.a['id']]['last_prediction'], 0)
        self.service.assert_not_called()

    def test_repeated_undo_is_per_agent_and_restores_previous_label(self):
        m = self.prepare()
        first = m.teach(self.e, self.a, 1)
        second = m.teach(self.e, self.a, 0)
        self.assertEqual(m.undo(self.e, self.a)['undone_id'], second['label_id'])
        self.assertEqual(self.e.runtime[self.a['id']]['last_prediction'], 1)
        self.assertEqual(m.undo(self.e, self.a)['undone_id'], first['label_id'])
        with self.assertRaises(ValueError): m.undo(self.e, self.a)

    def test_labels_and_undo_survive_reload(self):
        m = self.prepare()
        m.teach(self.e, self.a, 1)
        reloaded = Teaching(self.store)
        self.assertIsNotNone(reloaded.match(self.a, self.model, self.e.state_map, self.e.temporal_history, time.time()))
        reloaded.undo(self.e, self.a)
        self.assertEqual(Teaching(self.store).labels(self.a['id']), [])

    def test_motion_off_does_not_match_motion_on_label(self):
        m = self.prepare()
        m.teach(self.e, self.a, 1)
        self.e.state_map['binary_sensor.motion'] = state('binary_sensor.motion', 'off', device_class='motion')
        self.assertIsNone(m.match(self.a, self.model, self.e.state_map, self.e.temporal_history, time.time()))

    def test_new_physical_correction_retires_conflicting_button_label(self):
        m = self.prepare()
        m.teach(self.e, self.a, 1)
        self.e.runtime[self.a['id']]['previous_target'] = 1
        self.e.state_map[self.a['target_entity']]['context'] = {'user_id': 'real-user'}
        self.e.process_agent(self.a, self.e.state_map)
        self.assertEqual(m.labels(self.a['id']), [])
        self.service.assert_not_called()

    def test_shadow_process_never_sends_service_after_label(self):
        m = self.prepare()
        m.teach(self.e, self.a, 1)
        self.e.process_agent(self.a, self.e.state_map)
        self.assertEqual(self.e.runtime[self.a['id']]['last_prediction'], 1)
        self.assertEqual(self.e.state_map[self.a['target_entity']]['state'], 'off')
        self.service.assert_not_called()

    def test_control_uses_taught_desired_via_executor(self):
        m = self.prepare('control')
        m.teach(self.e, self.a, 1)
        self.e.process_agent(self.a, self.e.state_map)
        self.service.assert_called_once_with('light', 'turn_on', {'entity_id': self.a['target_entity']})
        rt = self.e.runtime[self.a['id']]
        self.assertTrue(rt['pending']['teaching_id'])
        self.assertLess(rt['last_confidence'], self.a['confidence_threshold'])
        before = self.model.serialize()
        self.e._reward_pending(self.a, rt, 1, 'completed')
        self.assertEqual(self.model.serialize(), before)

    def test_teaching_does_not_bypass_control_qualification(self):
        m = self.prepare('control')
        self.store.set_training_state(self.a['id'], 'qualified', score=.2, samples=2, detail={})
        self.store.update_agent(self.a['id'], {'mode': 'control'})
        label = m.teach(self.e, self.a, 1)
        result = self.e.executor.submit(replace(self.intent(), teaching_id=label['label_id'], teaching_revision=m.revision(self.a['id'])))
        self.assertIn('qualification:', result['reason'])
        self.service.assert_not_called()

    def test_continuous_teaching_keeps_device_quantization(self):
        m = self.prepare()
        a = self.store.create_agent(agent(target_entity='climate.test', target_property='temperature', min_value=16, max_value=26, mode='shadow'))
        self.e.state_map['climate.test'] = state('climate.test', 'heat', temperature=18, min_temp=16, max_temp=26, target_temp_step=.5)
        model = self.e.policy(a)
        model.schema = ExplicitFeatureSchema(model.dims, ['binary_sensor.motion'])
        result = m.teach(self.e, a, 19.3)
        self.assertEqual(result['desired_value'], 19.5)
        self.assertEqual(self.e.runtime[a['id']]['last_prediction'], 19.5)
        self.assertEqual(self.e.state_map['climate.test']['attributes']['temperature'], 18)
        self.service.assert_not_called()

    def test_select_option_reordering_invalidates_old_label(self):
        m = self.prepare()
        a = self.store.create_agent(agent(target_entity='select.test', target_property='option_index', min_value=0, max_value=1, mode='shadow'))
        self.e.state_map['select.test'] = state('select.test', 'Eco', options=['Eco', 'Comfort'])
        model = self.e.policy(a)
        model.schema = ExplicitFeatureSchema(model.dims, ['binary_sensor.motion'])
        m.teach(self.e, a, 1)
        self.e.state_map['select.test']['attributes']['options'] = ['Comfort', 'Eco']
        self.assertIsNone(m.match(a, model, self.e.state_map, self.e.temporal_history, time.time()))
        self.service.assert_not_called()

    def test_revoked_label_rejects_already_created_control_intent(self):
        m = self.prepare('control')
        label = m.teach(self.e, self.a, 1)
        intent = replace(self.intent(), teaching_id=label['label_id'])
        m.undo(self.e, self.a)
        result = self.e.executor.submit(intent)
        self.assertIn('teaching:', result['reason'])
        self.service.assert_not_called()

    def test_new_label_invalidates_queued_base_policy_decision(self):
        m = self.prepare('control')
        intent = self.intent()
        m.teach(self.e, self.a, 1)
        result = self.e.executor.submit(intent)
        self.assertIn('teaching:', result['reason'])
        self.service.assert_not_called()

    def test_context_mismatch_cannot_forge_label_authority(self):
        m = self.prepare('control')
        label = m.teach(self.e, self.a, 1)
        self.e.state_map['binary_sensor.motion'] = state('binary_sensor.motion', 'off', device_class='motion')
        result = self.e.executor.submit(replace(self.intent(), teaching_id=label['label_id']))
        self.assertIn('teaching:', result['reason'])
        self.service.assert_not_called()

    def archive(self):
        start = time.time()-3600
        self.store.archive_batch([
            (self.a['target_entity'], start, 'off', {}, None, 'test'),
            ('binary_sensor.motion', start, 'off', {'device_class':'motion'}, None, 'test'),
            ('binary_sensor.motion', start+100, 'on', {'device_class':'motion'}, None, 'test'),
            (self.a['target_entity'], start+101, 'on', {}, None, 'test'),
        ])
        return start

    def test_history_label_uses_as_of_context_not_current_or_future(self):
        m = self.prepare()
        start = self.archive()
        m.teach(self.e, self.a, 1, start+50)
        row = m.labels(self.a['id'])[0]
        self.assertEqual(row['source'], 'history')
        self.assertEqual(row['signature']['binary_sensor.motion:value'], -1)
        self.assertIsNone(m.match(self.a, self.model, self.e.state_map, self.e.temporal_history, time.time()))
        point = m.point(self.e, self.a, start+50)
        self.assertEqual(point['current'], 0)
        self.assertEqual(point['desired'], 1)
        self.service.assert_not_called()

    def test_replay_never_uses_live_home_forecast(self):
        m = self.prepare()
        start = self.archive()
        with patch.object(self.e.context, 'forecast', side_effect=AssertionError('future leak')):
            point = m.point(self.e, self.a, start+100.5)
            data = m.history(self.e, self.a, start, start+200)
        self.assertEqual(point['current'], 0)
        self.assertEqual(data['desired_source'], 'current_policy_replay')
        self.assertEqual(data['recorded'], [])
        self.assertLessEqual(len(data['points']), 1000)

    def test_dense_history_switches_to_bounded_as_of_sampling(self):
        m = self.prepare()
        start = self.archive()
        with patch.object(m, 'MAX_HISTORY_ROWS', 2):
            data = m.history(self.e, self.a, start, start+200)
        self.assertTrue(data['reduced'])
        self.assertLessEqual(len(data['points']), 1000)
        edge = next(p for p in data['points'] if p['ts'] == start+101)
        self.assertEqual(edge['current'], 1)

    def test_invalid_or_missing_historical_point_rejects_teaching(self):
        m = self.prepare()
        for ts in [float('nan'), time.time()+600, time.time()-86400]:
            with self.assertRaises(ValueError): m.teach(self.e, self.a, 1, ts)
        self.assertEqual(m.labels(self.a['id']), [])

    def test_training_and_replacement_of_base_model_keep_labels(self):
        m = self.prepare()
        self.store.set_training_state(self.a['id'], 'training')
        m.teach(self.e, self.a, 1)
        # No mutation of historical training matrices; a rebuilt compatible schema uses labels.
        self.e.models.pop(self.a['id'])
        new = self.e.policy(self.a)
        new.schema = ExplicitFeatureSchema(new.dims, ['binary_sensor.motion'])
        self.assertEqual(m.predict(self.a, new, self.e.state_map, self.e.temporal_history, time.time())[0], 1)

    def test_recording_is_batched_and_retains_transitions(self):
        m = self.prepare()
        now = time.time()
        m.record(self.a['id'], 0, 0, now)
        m.record(self.a['id'], 0, 0, now+.1)
        m.record(self.a['id'], 0, 1, now+.2)
        self.assertEqual(len(m.buffer), 2)
        m.flush()
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM decision_history').fetchone()[0], 2)

    def test_live_endpoint_reads_current_during_training_write_transaction(self):
        import main
        self.prepare()
        handler = main.Handler.__new__(main.Handler)
        handler.path = '/api/live'
        handler.require_trusted_client = Mock(return_value=True)
        handler.require_runtime = Mock(return_value=True)
        handler.send_json = Mock()
        with patch.object(main, 'STORE', self.store), patch.object(main, 'ENGINE', self.e), \
             patch.object(self.e, 'runtime_for', side_effect=AssertionError('heavy diagnostics')):
            with self.store.lock, self.store.conn() as c:
                c.execute("UPDATE agents SET training_progress=.5 WHERE id=?", (self.a['id'],))
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(handler.do_GET).result(timeout=1)
        self.assertEqual(handler.send_json.call_args.args[0], 200)
        self.assertEqual(handler.send_json.call_args.args[1]['agents'][0]['current_value'], 0)
