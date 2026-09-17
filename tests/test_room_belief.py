import json
import unittest

from context_engine import ContextEngine
from home_sources import select_sources
from home_state import RoomBeliefModel
from settings import DEFAULT_OPTIONS
from support import state


RADAR = {'role': 'radar_occupancy', 'value_semantics': 'stationary_occupancy'}
PIR = {'role': 'pir', 'value_semantics': 'event_presence'}
RAW = {'role': 'radar_activity', 'value_semantics': 'activity_likelihood'}
OCC = {'role': 'occupancy_binary', 'value_semantics': 'binary_occupancy'}


def route(model, path, start):
    for area in ('entry', 'hall', 'kitchen', 'bedroom'):
        model.observe(area, area, 0.0, start, evidence=OCC)
    for i, area in enumerate(path):
        model.observe(area, area, 1.0, start + 1 + i * 2, evidence=OCC)
    for area in path:
        model.observe(area, area, 0.0, start + 12, evidence=OCC)
    model.expire(start + 50)


class RoomBeliefTests(unittest.TestCase):
    def test_two_people_can_occupy_two_rooms_without_identity_inference(self):
        m = RoomBeliefModel()
        m.observe('radar.a', 'kitchen', 1.0, 1, evidence=RADAR)
        m.observe('radar.b', 'office', 1.0, 2, evidence=RADAR)
        kitchen = m.forecast('kitchen', 2)
        office = m.forecast('office', 2)
        self.assertGreater(kitchen['occupancy_now'], .5)
        self.assertGreater(office['occupancy_now'], .5)
        self.assertGreaterEqual(len(m.hypotheses), 2)
        self.assertTrue(all('person' not in item for item in kitchen['movement_hypotheses']))
        self.assertGreater(kitchen['uncertainty'], 0)

    def test_stationary_radar_survives_stillness_but_stuck_on_eventually_decays(self):
        m = RoomBeliefModel()
        m.observe('radar', 'room', 1.0, 0, evidence=RADAR)
        still = m.forecast('room', 100)
        stale = m.forecast('room', 1000)
        self.assertGreater(still['occupancy_now'], .5)
        self.assertLess(stale['occupancy_now'], .5)
        # Transport reliability is not inferred from state age.
        self.assertEqual(stale['evidence_sources'][0]['communication_reliability'], 1.0)
        self.assertLess(stale['evidence_sources'][0]['evidence_freshness'], .2)

    def test_pir_stuck_on_decays_faster_than_radar(self):
        m = RoomBeliefModel()
        m.observe('pir', 'room', 1.0, 0, evidence=PIR)
        self.assertGreater(m.forecast('room', 1)['occupancy_now'], .5)
        self.assertLess(m.forecast('room', 180)['occupancy_now'], .5)

    def test_raw_radar_activity_is_not_a_calibrated_probability_or_arrival(self):
        m = RoomBeliefModel()
        m.observe('raw', 'room', 1.0, 1, evidence=RAW)
        f = m.forecast('room', 1)
        self.assertLess(f['occupancy_now'], .5)
        self.assertGreater(f['uncertainty'], .5)
        self.assertEqual(f['movement_hypotheses'], [])
        self.assertEqual(f['evidence_sources'][0]['value_semantics'], 'activity_likelihood')

    def test_unavailable_and_reconnect_separate_transport_reliability_from_state_age(self):
        m = RoomBeliefModel()
        m.observe('radar', 'room', 1.0, 1, evidence=RADAR)
        m.observe('radar', 'room', None, 2, evidence=RADAR)
        offline = m.forecast('room', 2)
        self.assertFalse(offline['known'])
        self.assertEqual(offline['observability'], 0)
        self.assertEqual(offline['uncertainty'], 1)
        m.observe('radar', 'room', 1.0, 3, evidence=RADAR)
        reconnect = m.forecast('room', 3)
        self.assertAlmostEqual(reconnect['evidence_sources'][0]['communication_reliability'], .60)
        self.assertGreater(reconnect['uncertainty'], .3)
        # A later received sample raises communication confidence even if state is unchanged.
        m.observe('radar', 'room', 1.0, 4, evidence=RADAR)
        self.assertAlmostEqual(m.forecast('room', 4)['evidence_sources'][0]['communication_reliability'], .75)

    def test_delayed_packet_never_rewinds_room_state(self):
        m = RoomBeliefModel()
        m.observe('radar', 'room', 1.0, 20, evidence=RADAR)
        self.assertFalse(m.observe('radar', 'room', 0.0, 10, evidence=RADAR))
        self.assertGreater(m.forecast('room', 20)['occupancy_now'], .5)
        # Querying before the accepted sample does not use future state.
        before = m.forecast('room', 10)
        self.assertFalse(before['known'])
        self.assertEqual(before['uncertainty'], 1)

    def test_branching_route_returns_ambiguous_distribution_not_identity(self):
        m = RoomBeliefModel()
        for i in range(80):
            route(m, ('entry', 'hall', 'kitchen'), i * 100)
        for i in range(80):
            route(m, ('entry', 'hall', 'bedroom'), 10000 + i * 100)
        m.observe('entry', 'entry', 1.0, 20000, evidence=OCC)
        m.observe('hall', 'hall', 1.0, 20002, evidence=OCC)
        kitchen = m.forecast('kitchen', 20002)
        bedroom = m.forecast('bedroom', 20002)
        self.assertGreater(kitchen['arrival_probability'], .15)
        self.assertGreater(bedroom['arrival_probability'], .15)
        self.assertLess(abs(kitchen['arrival_probability'] - bedroom['arrival_probability']), .20)
        self.assertGreater(kitchen['trajectory_confidence'], .5)

    def test_no_entry_is_an_explicit_negative_transition_opportunity(self):
        m = RoomBeliefModel()
        m.observe('entry', 'entry', 1.0, 1, evidence=OCC)
        m.expire(40)
        self.assertGreater(m.graph[('entry',)]['outcomes'][''][6], 0)
        self.assertEqual(m.diagnostics(40)['edges'], 0)

    def test_checkpoint_is_versioned_and_never_restores_live_on(self):
        m = RoomBeliefModel()
        m.observe('radar', 'room', 1.0, 10, evidence=RADAR)
        m._record(('hall',), 'kitchen', 2, 10, 3)
        raw = json.loads(json.dumps(m.export()))
        self.assertEqual(raw['version'], 2)
        restored = RoomBeliefModel(raw=raw)
        self.assertEqual(restored.graph, m.graph)
        self.assertEqual(restored.values, {})
        self.assertEqual(restored.sources, {})
        self.assertEqual(restored.hypotheses, [])

    def test_v1_checkpoint_migration_is_explicit_and_exports_v2(self):
        raw = {'version': 1, 'updates': 7, 'last_decay_ts': 10,
               'graph': [{'context': ['hall'], 'ts': 10,
                          'outcomes': {'kitchen': [0, 0, 2, 0, 0, 0, 0]}}],
               'dwell': {}}
        m = RoomBeliefModel(raw=raw)
        self.assertEqual(m.migrated_from, 1)
        self.assertEqual(m.export()['version'], 2)
        self.assertEqual(m.graph[('hall',)]['outcomes']['kitchen'][2], 2)

    def test_calibration_accepts_only_independent_labels_and_does_not_train_state(self):
        m = RoomBeliefModel()
        before = m.export()
        metrics = m.record_calibration_label(.8, True, 'manual_ground_truth')
        self.assertEqual(metrics['independent_labels'], 1)
        self.assertAlmostEqual(metrics['brier_score'], .04)
        self.assertEqual(m.graph, before['graph'] and m.graph or {})
        self.assertEqual(m.values, {})
        with self.assertRaises(ValueError):
            m.record_calibration_label(.8, True, 'model:self_prediction')


class RoomSourceRoleTests(unittest.TestCase):
    def test_radar_binary_raw_door_and_pir_have_explicit_roles(self):
        states = {
            'binary_sensor.pir': state('binary_sensor.pir', 'off', device_class='motion'),
            'binary_sensor.presence': state('binary_sensor.presence', 'off', device_class='occupancy'),
            'sensor.still_energy': state('sensor.still_energy', 80, unit_of_measurement='%'),
            'binary_sensor.door': state('binary_sensor.door', 'off', device_class='door'),
        }
        registry = {
            'binary_sensor.pir': {'device_id': 'pir', 'area_id': 'hall'},
            'binary_sensor.presence': {'device_id': 'radar', 'area_id': 'room'},
            'sensor.still_energy': {'device_id': 'radar', 'area_id': 'room'},
            'binary_sensor.door': {'device_id': 'door', 'area_id': 'hall'},
        }
        mapping = {eid: row['area_id'] for eid, row in registry.items()}
        admitted, details = select_sources(states, registry, mapping, set())
        self.assertEqual(admitted, set(states))
        self.assertEqual(details['binary_sensor.pir']['role'], 'pir')
        self.assertEqual(details['binary_sensor.presence']['role'], 'radar_occupancy')
        self.assertEqual(details['sensor.still_energy']['role'], 'radar_activity')
        self.assertFalse(details['sensor.still_energy']['calibrated_probability'])
        self.assertEqual(details['binary_sensor.door']['role'], 'door')

    def test_context_engine_uses_v2_key_and_preserves_legacy_key_for_rollback(self):
        # Contract-level check without database fixture: the key names themselves are
        # versioned and legacy remains a separate read fallback.
        self.assertEqual(ContextEngine.ROOM_MODEL_KEY, 'room_belief_model_v2')
        self.assertEqual(ContextEngine.LEGACY_ROOM_MODEL_KEY, 'shared_home_model_v1')


if __name__ == '__main__':
    unittest.main()
