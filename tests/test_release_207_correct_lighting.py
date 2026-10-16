"""Observed Current, long-ON photometry and consistent learned OFF confirmation."""
from types import SimpleNamespace
import copy
import threading
import unittest
from unittest.mock import Mock, patch

import support
from context import TemporalHistory
from observation_contract import FeatureSchemaV12, _feature_observation
from shared_inference_context import shared_inference_temporal
from fast_runtime import stabilize_fast_light_power_decision


class CurrentHistoryTests(unittest.TestCase):
    def setUp(self):
        from test_correct_multigeneration_history import CorrectGenerationHistoryTests
        self.f = CorrectGenerationHistoryTests()
        self.f.setUp(); self.addCleanup(self.f.tearDown)
        self.at = self.f.ts + 10
        with self.f.store.conn() as c:
            c.execute('DELETE FROM entity_history')
            c.execute('DELETE FROM decision_history')
        self.f.store.archive_batch([('light.history', self.at-10, 'off', {}, None, 'test')])

    def record(self, offset, current, desired):
        with self.f.store.conn() as c:
            c.execute('INSERT INTO decision_history VALUES(?,?,?,?)',
                      (self.f.root['id'], self.at+offset, current, desired))

    def history(self):
        from agent_correct_generation_history import build_correct_history
        return build_correct_history(self.f.manager, self.f.g0['generation_id'],
                                     self.at-1, self.at+5, Mock(side_effect=AssertionError('no replay')))

    def test_current_uses_observed_on_even_when_raw_archive_still_says_off(self):
        self.record(0, 1, 0); self.record(1, 1, 1); self.record(2, 1, 0)
        h = self.history()
        points = h['series']['current']['points']
        self.assertTrue(all(p['value'] == 1 for p in points if p['ts'] >= self.at))
        self.assertEqual({p['value'] for p in h['series']['live_desired']['points']}, {0, 1})
        self.f.engine.policy.assert_not_called()

    def test_later_real_off_event_beats_earlier_runtime_on_observation(self):
        self.record(0, 1, 1)
        self.f.store.archive_batch([('light.history', self.at+2, 'off', {}, None, 'test')])
        self.assertEqual(self.history()['series']['current']['points'][-1]['value'], 0)

    def test_exact_ha_event_wins_a_timestamp_tie(self):
        self.record(0, 1, 1)
        self.f.store.archive_batch([('light.history', self.at, 'off', {}, None, 'test')])
        self.assertEqual(self.history()['series']['current']['points'][-1]['value'], 0)

    def test_pending_observed_current_is_visible_without_writer_flush(self):
        self.f.engine.teaching = SimpleNamespace(lock=threading.Lock(), buffer=[
            (self.f.root['id'], self.at, 1, 0)])
        self.assertEqual(self.history()['series']['current']['points'][-1]['value'], 1)

    def test_desired_is_never_substituted_for_physical_current(self):
        self.record(0, 0, 1)
        self.assertEqual({p['value'] for p in self.history()['series']['current']['points']}, {0})

    def test_newer_archive_on_beats_older_runtime_off(self):
        self.record(0, 0, 1)
        self.f.store.archive_batch([('light.history', self.at+2, 'on', {}, None, 'test')])
        self.assertEqual(self.history()['series']['current']['points'][-1]['value'], 1)

    def test_correct_point_uses_same_observed_physical_current(self):
        from agent_correct_generation_history import build_correct_point
        self.record(0, 1, 0)
        p = build_correct_point(self.f.manager, self.f.g0['generation_id'], self.at+1,
                                Mock(side_effect=AssertionError('no replay')))
        self.assertEqual(p['current'], 1)

    def test_candidate_observed_current_is_independent_of_candidate_desired(self):
        from agent_correct_generation_history import build_correct_history, build_correct_point
        self.f.insert_observed(self.f.g2['generation_id'], self.at, 1, 0, 'g2')
        h = build_correct_history(self.f.manager, self.f.g2['generation_id'],
                                   self.at-1, self.at+5, Mock())
        self.assertEqual(h['series']['current']['points'][-1]['value'], 1)
        self.assertEqual(build_correct_point(self.f.manager, self.f.g2['generation_id'],
                                            self.at+1, Mock())['current'], 1)


class LongOnPhotometryTests(unittest.TestCase):
    def setUp(self):
        from test_observation_contract import sensor_state, add_live
        self.state, self.add_live = sensor_state, add_live
        self.at = 1800000000.
        self.h = TemporalHistory(maxlen=24)
        self.a = support.agent(target_entity='light.kitchen')
        self.schema = FeatureSchemaV12(128, ['sensor.room_lux'])
        self.states = {}
        self.add('light.kitchen', 'off', -3)
        self.add('sensor.room_lux', 20, -2)
        self.add('light.kitchen', 'on', 0, changed=0)
        self.add('sensor.room_lux', 200, 1)

    def add(self, eid, value, offset, *, changed=None, received=None):
        st = self.state(eid, value, self.at+offset,
                        last_changed=self.at+changed if changed is not None else None,
                        unit='lx' if eid.endswith('lux') else None)
        self.add_live(self.h, st, self.at+offset, self.at+received if received is not None else None)
        self.states[eid] = st

    def observe(self, offset, *, h=None, knowledge=None):
        return _feature_observation(self.schema, 'sensor.room_lux', self.states['sensor.room_lux'],
            self.a, self.states, h or self.h, self.at+offset,
            self.at+(offset if knowledge is None else knowledge))

    def roll(self):
        for i in range(2, 100):
            self.add('sensor.room_lux', 180 + i%20, i)
            self.add('light.kitchen', 'on', i, changed=0)

    def test_twenty_four_sample_rollover_keeps_same_observed_pre_on_value(self):
        expected = self.observe(1)
        self.roll()
        self.assertEqual(self.observe(100), expected)
        self.assertEqual(self.observe(100)[0]['physical_value'], 20)
        self.assertEqual(len(self.h.samples['sensor.room_lux']), 24)

    def test_shared_inference_frames_keep_the_observed_anchor(self):
        self.observe(1, h=shared_inference_temporal(self.h, self.at+1))
        self.roll()
        value, _ = self.observe(100, h=shared_inference_temporal(self.h, self.at+100))
        self.assertEqual(value['physical_value'], 20)

    def test_no_observed_baseline_is_invented_if_first_read_is_after_rollover(self):
        self.roll()
        self.assertFalse(self.observe(100)[0]['valid'])

    def test_new_on_epoch_cannot_borrow_baseline_from_previous_visit(self):
        self.observe(1); self.roll()
        self.add('light.kitchen', 'off', 101)
        self.add('sensor.room_lux', 160, 102)
        self.add('light.kitchen', 'on', 103, changed=103)
        self.add('sensor.room_lux', 240, 104)
        self.assertEqual(self.observe(104)[0]['physical_value'], 160)

    def test_new_run_without_off_sensor_sample_remains_unknown(self):
        self.observe(1); self.roll()
        self.add('light.kitchen', 'off', 101)
        self.add('light.kitchen', 'on', 103, changed=103)
        self.h.samples['sensor.room_lux'].clear()
        self.add('sensor.room_lux', 240, 104)
        self.assertFalse(self.observe(104)[0]['valid'])

    def test_cached_future_knowledge_is_not_available_to_an_earlier_cutoff(self):
        self.observe(1); self.roll()
        self.assertFalse(self.observe(100, knowledge=.5)[0]['valid'])

    def test_future_received_pre_on_sample_never_becomes_the_anchor(self):
        self.h = TemporalHistory(maxlen=24)
        self.add('light.kitchen', 'off', -3)
        self.add('sensor.room_lux', 20, -2)
        self.add('sensor.room_lux', 250, -1, received=200)
        self.add('light.kitchen', 'on', 0, changed=0)
        self.add('sensor.room_lux', 200, 1)
        self.assertEqual(self.observe(1)[0]['physical_value'], 20)

    def test_startup_already_on_does_not_create_a_baseline(self):
        self.h = TemporalHistory(maxlen=24)
        self.add('light.kitchen', 'on', 0, changed=-500)
        self.add('sensor.room_lux', 200, 1)
        self.assertFalse(self.observe(1)[0]['valid'])

    def test_missing_epoch_marker_cannot_retain_a_baseline(self):
        for _, st in self.h.samples['light.kitchen']:
            st.pop('last_changed', None)
        self.observe(1); self.roll()
        self.assertFalse(self.observe(100)[0]['valid'])

    def test_retention_is_bounded_and_returns_owned_observations(self):
        from photometric_baseline import PhotometricBaselines
        memory = PhotometricBaselines()
        with patch('photometric_baseline.MAX_ENTRIES', 2):
            for key in range(3):
                memory.put(key, 10., 10., 9., 11., {'valid': 1., 'physical_value': key})
            self.assertEqual(memory.snapshot()['entries'], 2)
            self.assertIsNone(memory.get(0, 10., 11., 11.))
            obs, _ = memory.get(2, 10., 11., 11.)
            obs['physical_value'] = 100
            self.assertEqual(memory.get(2, 10., 11., 11.)[0]['physical_value'], 2)

    def test_deepcopied_history_owns_its_samples_and_retained_anchors(self):
        self.observe(1); self.roll()
        cloned = copy.deepcopy(self.h)
        self.assertEqual(self.observe(100, h=cloned)[0]['physical_value'], 20)
        self.assertIsNot(cloned.photometric_baselines(), self.h.photometric_baselines())
        cloned.photometric_baselines().entries.clear()
        self.assertEqual(self.observe(100)[0]['physical_value'], 20)


class ContinuedPresenceTests(unittest.TestCase):
    def model(self, role='radar_occupancy'):
        from home_state import RoomBeliefModel
        m = RoomBeliefModel()
        m.observe('binary_sensor.kitchen', 'kitchen', 1., 10., evidence={'role': role})
        return m

    def test_new_same_on_events_keep_stateful_presence_fresh_for_ten_minutes(self):
        for role in ('radar_occupancy', 'occupancy_binary'):
            m = self.model(role)
            for t in range(11, 611):
                m.observe('binary_sensor.kitchen', 'kitchen', 1., t, evidence={'role': role})
            f = m.forecast('kitchen', 610.)
            self.assertGreater(f['occupancy_now'], .8)
            self.assertEqual(f['evidence_sources'][0]['evidence_freshness'], 1.)
            self.assertEqual(m.sources['binary_sensor.kitchen']['state_since_ts'], 10.)

    def test_old_event_poll_confirmation_does_not_renew_presence(self):
        m = self.model()
        m.observe('binary_sensor.kitchen', 'kitchen', 1., 1010.,
                  evidence={'role':'radar_occupancy'}, event_ts=10., received_ts=1010.)
        f = m.forecast('kitchen', 1010.)
        self.assertLess(f['occupancy_now'], .5)
        self.assertLess(f['evidence_sources'][0]['evidence_freshness'], .2)

    def test_repeated_stuck_pir_on_still_decays(self):
        m = self.model('pir')
        for t in range(11, 611):
            m.observe('binary_sensor.kitchen', 'kitchen', 1., t, evidence={'role':'pir'})
        self.assertLess(m.forecast('kitchen', 610.)['occupancy_now'], .5)

    def test_off_and_unavailable_clear_stateful_presence(self):
        for value in (0., None):
            m = self.model()
            m.observe('binary_sensor.kitchen', 'kitchen', value, 11., evidence={'role':'radar_occupancy'})
            f = m.forecast('kitchen', 11.)
            if value is None:
                self.assertFalse(f['known'])
                self.assertEqual(f['uncertainty'], 1.)
            else:
                self.assertLess(f['occupancy_now'], .5)

    def test_future_confirmation_is_not_available_at_an_earlier_query(self):
        m = self.model()
        m.observe('binary_sensor.kitchen', 'kitchen', 1., 1010., evidence={'role':'radar_occupancy'})
        self.assertFalse(m.forecast('kitchen', 1000.)['known'])


class TrainedKitchenLongStayTests(unittest.TestCase):
    def test_persisted_trained_policy_stays_on_for_ten_minutes_in_dark_occupied_room(self):
        import sys
        sys.path.insert(0, str(support.ROOT / 'tools'))
        from tools.benchmark_kitchen_lighting import run
        report = run(long_stay=True)
        self.assertTrue(report['pass'], report)
        self.assertEqual(report['long_stay']['seconds'], 600)
        self.assertEqual(report['long_stay']['raw_history_limit'], 24)
        self.assertEqual(report['long_stay']['off_predictions'], 0)
        self.assertEqual(report['long_stay']['invalid_illumination'], 0)


class LearnedOffConfirmationTests(unittest.TestCase):
    def test_neural_short_off_pulses_are_confirmed_like_other_learned_predictions(self):
        rt = {}; a = support.agent()
        for at in (10., 10.1, 11.):
            self.assertEqual(stabilize_fast_light_power_decision(
                a, rt, 1, 0, 'hybrid_tiny_mlp_ridge_guard', at), (1., True))
        self.assertEqual(stabilize_fast_light_power_decision(
            a, rt, 1, 1, 'hybrid_tiny_mlp_ridge_guard', 11.1), (1., False))
        self.assertNotIn('fast_off_candidate_since', rt)

    def test_neural_sustained_off_still_completes_at_six_seconds(self):
        rt = {}; a = support.agent()
        stabilize_fast_light_power_decision(a, rt, 1, 0, 'hybrid_tiny_mlp_ridge_guard', 10.)
        self.assertEqual(stabilize_fast_light_power_decision(
            a, rt, 1, 0, 'hybrid_tiny_mlp_ridge_guard', 16.), (0., False))

    def test_explicit_instructions_and_physical_off_remain_immediate(self):
        for source, current in [('scoped_instruction:one_time', 1), ('preference_model', 1),
                                ('experiment', 1), ('hybrid_tiny_mlp_ridge_guard', 0)]:
            self.assertEqual(stabilize_fast_light_power_decision(
                support.agent(), {}, current, 0, source, 10.), (0., False))


if __name__ == '__main__':
    unittest.main()
