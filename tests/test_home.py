import unittest
import json
from support import *
from home_state import SharedHomeStateModel, FEATURE_NAMES
from context_engine import ContextEngine
from context import controllable_context_exclusions, electrical_context_exclusions
from settings import DEFAULT_OPTIONS


def route(model, path, ts):
    for area in ('terrace','living','kitchen','bedroom'):
        model.observe(area, area, 0, ts)
    for i, area in enumerate(path):
        model.observe(area, area, 1, ts+1+2*i)
    for area in path:
        model.observe(area, area, 0, ts+10)
    model.expire(ts+50)


class HomeTests(unittest.TestCase):
    def trained(self):
        m=SharedHomeStateModel()
        for i in range(100): route(m, ('terrace','living','kitchen'), 100*i)
        return m

    def test_learns_second_order_and_horizons(self):
        m=self.trained()
        route(m, ('terrace','living'),10000)
        # Query the active prefix independently; no future query on the just-finished route.
        m.observe('terrace','terrace',1,10100)
        m.observe('living','living',1,10102)
        f=m.forecast('kitchen',10102)
        self.assertGreater(f['occupancy_in_3s'],.85)
        self.assertEqual(f['occupancy_now'],0)
        self.assertLess(f['occupancy_in_1s'],f['occupancy_in_3s'])
        self.assertEqual(f['occupancy_in_3s'],f['occupancy_in_5s'])
        self.assertIn(('terrace','living'),m.graph)

    def test_alternative_branch_lowers_probability(self):
        m=self.trained()
        for i in range(100): route(m, ('terrace','living','bedroom'), 10000+100*i)
        m.observe('terrace','terrace',1,20100);m.observe('living','living',1,20102)
        self.assertLess(m.forecast('kitchen',20102)['arrival_probability'],.6)

    def test_branch_to_bedroom_is_not_kitchen_arrival(self):
        m=self.trained()
        m.observe('terrace','terrace',1,10000);m.observe('living','living',1,10002)
        m.observe('bedroom','bedroom',1,10004)
        self.assertEqual(m.forecast('kitchen',10004)['arrival_probability'],0)

    def test_no_arrival_trials_reduce_support(self):
        m=SharedHomeStateModel()
        for i in range(30):route(m, ('terrace','living'),100*i)
        self.assertIn('', m.graph[('terrace','living')]['outcomes'])

    def test_serialization_does_not_restore_occupancy(self):
        m=self.trained();raw=json.loads(json.dumps(m.export()))
        restored=SharedHomeStateModel(raw=raw)
        self.assertEqual(restored.graph,m.graph)
        self.assertEqual(restored.values,{})

    def test_out_of_order_does_not_rewind(self):
        m=SharedHomeStateModel();m.observe('s','a',1,20);m.observe('s','a',0,10)
        self.assertEqual(m.forecast('a',20)['occupancy_now'],1)

    def test_two_sensors_in_area(self):
        m=SharedHomeStateModel();m.observe('a','room',1,1);m.observe('b','room',0,2)
        self.assertEqual(m.forecast('room',2)['occupancy_now'],1)

    def test_sensor_remapping_does_not_leave_phantom_occupancy(self):
        m=SharedHomeStateModel();m.observe('sensor','old',1,1);m.observe('sensor','new',1,2)
        self.assertFalse(m.forecast('old',2)['known'])
        self.assertEqual(m.forecast('old',2)['occupancy_now'],0)
        self.assertEqual(m.forecast('new',2)['occupancy_now'],1)

    def test_unknown_sensor_is_not_known_empty(self):
        m=SharedHomeStateModel();m.observe('a','room',None,1)
        self.assertFalse(m.forecast('room',1)['known'])

    def test_decay_reduces_trajectory_confidence(self):
        m=self.trained();m.observe('terrace','terrace',1,10000);m.observe('living','living',1,10002)
        conf=m.forecast('kitchen',10002)['trajectory_confidence']
        # Same trajectory with old evidence, late in model lifetime.
        future=10002+45*86400
        m.arrivals.clear();m.arrivals.append(('terrace',future-2));m.arrivals.append(('living',future))
        self.assertLess(m.forecast('kitchen',future)['trajectory_confidence'],conf)

    def test_registry_priority_and_no_name_guessing(self):
        c=ContextEngine(DEFAULT_OPTIONS)
        c.options=dict(DEFAULT_OPTIONS,entity_area_mapping=json.dumps({'sensor.fallback':'manual'}))
        states={eid:state(eid,'on',device_class='occupancy') for eid in ['binary_sensor.kitchen','sensor.device','sensor.fallback']}
        c.configure(states,entities={'binary_sensor.kitchen':{'area_id':'explicit','device_id':'d'},'sensor.device':{'device_id':'d'}},devices=[{'id':'d','area_id':'device'}],areas=[])
        self.assertEqual(c.area_for('binary_sensor.kitchen'),'explicit')
        self.assertEqual(c.area_for('sensor.device'),'device')
        self.assertEqual(c.area_for('sensor.fallback'),'manual')
        self.assertIsNone(c.area_for('light.kitchen'))

    def test_esphome_config_siblings_preserved(self):
        ids=['binary_sensor.presence','sensor.still_energy','sensor.move_energy','number.threshold','select.mode','switch.engineering']
        states={eid:state(eid,'on' if eid.startswith('binary') else 50,unit_of_measurement='%' if 'energy' in eid else None) for eid in ids}
        reg={eid:{'platform':'esphome','device_id':'radar'} for eid in ids}
        excluded,_=controllable_context_exclusions(states,reg)
        self.assertNotIn('binary_sensor.presence',excluded)
        self.assertNotIn('sensor.still_energy',excluded)
        self.assertNotIn('sensor.move_energy',excluded)
        self.assertIn('number.threshold',excluded)

    def test_electrical_units_only(self):
        states={f'sensor.{unit.lower()}':state(f'sensor.{unit.lower()}',42,unit_of_measurement=unit) for unit in ('W','V','A')}
        states['sensor.still_energy']=state('sensor.still_energy',42,unit_of_measurement='%')
        excluded,_=electrical_context_exclusions(states,{})
        self.assertEqual(excluded,{'sensor.w','sensor.v','sensor.a'})

    def test_numeric_camera_and_radar_are_admitted(self):
        c=ContextEngine(DEFAULT_OPTIONS)
        states={'sensor.camera_ai_detection_score':state('sensor.camera_ai_detection_score',.8),
                'sensor.still_energy':state('sensor.still_energy',80,unit_of_measurement='%')}
        c.configure(states)
        self.assertEqual(c.admitted,set(states))
        self.assertEqual(ContextEngine.probability('sensor.still_energy',states['sensor.still_energy']),.8)

    def test_minimal_history_preserves_percentage_units(self):
        c=ContextEngine(DEFAULT_OPTIONS)
        c.configure({'sensor.still_energy':state('sensor.still_energy',1,unit_of_measurement='%')})
        self.assertEqual(c.sensor_probability('sensor.still_energy',state('sensor.still_energy',1)),.01)

    def test_invalid_mapping_does_not_break_raw_context(self):
        c=ContextEngine(dict(DEFAULT_OPTIONS,entity_area_mapping='invalid json'))
        c.configure({'binary_sensor.motion':state('binary_sensor.motion','on')})
        self.assertIn('binary_sensor.motion',c.admitted)
        self.assertTrue(c.mapping_error)

    def test_poll_unavailable_clears_known_occupancy(self):
        c=ContextEngine(DEFAULT_OPTIONS);eid='binary_sensor.motion'
        c.configure({eid:state(eid,'on')},entities={eid:{'area_id':'room'}})
        c.observe(eid,state(eid,'on'),100)
        c.configure({eid:state(eid,'unavailable')})
        c.observe(eid,state(eid,'unavailable'),101)
        self.assertFalse(c.home.forecast('room',101)['known'])


if __name__=='__main__': unittest.main()
