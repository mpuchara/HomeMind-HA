import unittest
import time
import tempfile
import json
from pathlib import Path
from unittest.mock import patch
from contextlib import contextmanager
from support import state
from context_engine import ContextEngine
from context import controllable_context_exclusions
from home_state import SharedHomeStateModel
from settings import DEFAULT_OPTIONS
from storage import Store
from engine import Engine
from history import HistoryManager
from home_bootstrap import HomeBootstrap
from telemetry import HEAVY_JOBS


class PresenceAdmissionTests(unittest.TestCase):
    def test_custom_binary_presence_survives_control_sibling(self):
        values={'binary_sensor.espen_pir':state('binary_sensor.espen_pir','off'),
                'switch.espen_led':state('switch.espen_led','on')}
        registry={eid:{'device_id':'custom','platform':'mqtt','area_id':'stairs'} for eid in values}
        c=ContextEngine(DEFAULT_OPTIONS);c.configure(values,entities=registry)
        self.assertEqual(c.admitted,{'binary_sensor.espen_pir'})

    def test_zigbee_pir_and_custom_espen_binary(self):
        ids = ['binary_sensor.sonoff', 'binary_sensor.espen_pir', 'binary_sensor.espen_ruch']
        states = {eid:state(eid,'off',**({'device_class':'motion'} if 'sonoff' in eid else {})) for eid in ids}
        c = ContextEngine(DEFAULT_OPTIONS)
        c.configure(states, entities={eid:{'area_id':'stairs','platform':'mqtt'} for eid in ids})
        self.assertEqual(c.relevant_entities(), sorted(ids))

    def test_radar_distances_and_config_do_not_hold_room_occupied(self):
        values = {'binary_sensor.kitchen_presence':state('binary_sensor.kitchen_presence','off',device_class='occupancy'),
                  'sensor.kitchen_presence_distance':state('sensor.kitchen_presence_distance',180,unit_of_measurement='cm'),
                  'sensor.kitchen_presence_still_energy':state('sensor.kitchen_presence_still_energy',90,unit_of_measurement='%'),
                  'sensor.kitchen_presence_firmware':state('sensor.kitchen_presence_firmware','2026.9'),
                  'sensor.kitchen_presence_move_threshold':state('sensor.kitchen_presence_move_threshold',75),
                  'binary_sensor.kitchen_presence_connectivity':state('binary_sensor.kitchen_presence_connectivity','on',device_class='connectivity')}
        reg = {eid:{'device_id':'radar','platform':'esphome'} for eid in values}
        c = ContextEngine(DEFAULT_OPTIONS)
        c.configure(values, entities=reg, devices=[{'id':'radar','area_id':'kitchen'}])
        # Stage 08 keeps raw activity as supporting evidence but never lets it assert a
        # calibrated occupancy probability. Distance/config/connectivity remain excluded.
        self.assertEqual(c.admitted, {'binary_sensor.kitchen_presence',
                                      'sensor.kitchen_presence_still_energy'})
        self.assertEqual(c.source_details['binary_sensor.kitchen_presence']['role'],'radar_occupancy')
        self.assertEqual(c.source_details['sensor.kitchen_presence_still_energy']['role'],'radar_activity')
        self.assertFalse(c.source_details['sensor.kitchen_presence_still_energy']['occupancy_authority'])
        for eid, value in values.items():c.observe(eid,value,100)
        belief=c.home.forecast('kitchen',100)
        self.assertLess(belief['occupancy_now'],.5)
        self.assertGreater(belief['uncertainty'],0)
        excluded,_=controllable_context_exclusions(values,reg)
        self.assertNotIn('sensor.kitchen_presence_still_energy',excluded)
        self.assertEqual(c.source_details['sensor.kitchen_presence_still_energy']['reason'],'activity_support_only')

    def test_numeric_score_without_binary_is_still_used(self):
        eid='sensor.camera_ai_detection_score';c=ContextEngine(DEFAULT_OPTIONS)
        c.configure({eid:state(eid,75,unit_of_measurement='%')}, entities={eid:{'area_id':'garden'}})
        c.observe(eid,state(eid,75),100)
        # A detection *score* is raw local evidence, not a calibrated P(occupied).
        self.assertEqual(c.source_details[eid]['role'],'auxiliary')
        self.assertFalse(c.source_details[eid]['calibrated_probability'])
        self.assertFalse(c.source_details[eid]['occupancy_authority'])
        forecast=c.forecast(eid,100)
        self.assertLess(forecast['occupancy_now'],.5)
        self.assertIn(eid,[row['entity_id'] for row in forecast['evidence_sources']])
        self.assertEqual(forecast['presence_capability']['mode'],'virtual_threshold')

    def test_binary_other_area_does_not_suppress_zone_score(self):
        states={'binary_sensor.presence':state('binary_sensor.presence','off'),
                'sensor.presence_score':state('sensor.presence_score',80,unit_of_measurement='%')}
        c=ContextEngine(DEFAULT_OPTIONS)
        c.configure(states, entities={eid:{'device_id':'multi_zone','area_id':str(i)} for i,eid in enumerate(states)})
        self.assertEqual(c.admitted,set(states))

    def test_source_diagnostics_report_missing_area_and_disabled(self):
        c=ContextEngine(DEFAULT_OPTIONS)
        states={eid:state(eid,'off',device_class='motion') for eid in ('binary_sensor.a','binary_sensor.b')}
        c.configure(states,entities={'binary_sensor.b':{'disabled_by':'user'}})
        d=c.diagnostics()
        self.assertEqual(d['unmapped_sources'],1)
        self.assertEqual(c.source_details['binary_sensor.a']['reason'],'missing_area')
        self.assertEqual(c.source_details['binary_sensor.b']['reason'],'disabled_in_ha')

    def test_no_arrival_is_not_a_transition_edge(self):
        m=SharedHomeStateModel();m.observe('a','a',1,1);m.expire(50)
        self.assertEqual(m.diagnostics(50)['edges'],0)


class BootstrapDeltaTests(unittest.TestCase):
    def test_failed_install_keeps_checkpoints_and_persisted_model(self):
        import engine as em
        import history as hm
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'test.db')
            with patch.object(em,'STORE',store),patch.object(hm,'STORE',store):
                e=Engine();eid='binary_sensor.motion';e.context.store=store
                e.state_map={eid:state(eid,'off')}
                e.context.configure(e.state_map,entities={eid:{'area_id':'room'}})
                store.archive_batch([(eid,time.time()-100,'off',{},None,'test')])
                b=HomeBootstrap(e.context,HistoryManager(e),store)
                store.meta_set('shared_home_model_v1','original')
                with store.conn() as c:c.execute("INSERT INTO home_checkpoints VALUES(1,'original')")
                original=store.conn
                @contextmanager
                def fail_install():
                    with original() as conn:
                        class Proxy:
                            def execute(self, sql, *args):
                                if sql.startswith('INSERT INTO home_checkpoints SELECT'):
                                    raise OSError('simulated storage failure')
                                return conn.execute(sql,*args)
                        yield Proxy()
                try:
                    with patch.object(store,'conn',side_effect=fail_install):
                        self.assertTrue(b.start(import_recorder=False));b.thread.join(timeout=10)
                    self.assertEqual(b.status['state'],'ERROR')
                    self.assertEqual(store.meta_get('shared_home_model_v1'),'original')
                    with store.conn() as c:self.assertEqual(c.execute('SELECT model FROM home_checkpoints').fetchone()[0],'original')
                    self.assertIsNone(e.context.bootstrap_delta)
                    self.assertIsNone(HEAVY_JOBS.owner)
                finally:
                    e.control_workers.shutdown();e.poll_worker.shutdown()

    def test_many_events_have_bounded_statistics_and_preserve_final_state(self):
        c=ContextEngine(DEFAULT_OPTIONS);eid='binary_sensor.pir'
        c.configure({eid:state(eid,'off')},entities={eid:{'area_id':'room'}})
        c.observe(eid,state(eid,'off'),100)
        c.bootstrap_started=100;c.bootstrap_delta=c.home.live_delta(100)
        for i in range(30000):
            c.observe(eid,state(eid,'on' if i%2 else 'off'),101+i)
        delta=c.bootstrap_delta
        self.assertEqual(delta.updated,30000)
        self.assertEqual(len(delta.sources),1)
        self.assertLessEqual(len(delta.graph),1)
        self.assertEqual(delta.values['room']['p'],1)
        self.assertLess(len(json.dumps(delta.export())),4000)

    def test_merge_aligns_decay_and_preserves_live_temporal_state(self):
        history=SharedHomeStateModel(1);history._record(('a',),'b',2,100,8)
        delta=SharedHomeStateModel(1);delta._record(('a',),'b',2,86500,3)
        delta.observe('c','c',1,86501,learn=False)
        history.merge_statistics(delta)
        self.assertAlmostEqual(history.graph[('a',)]['outcomes']['b'][2],7)
        self.assertEqual(delta.values['c']['p'],1)
        self.assertEqual(history.values,{})

    def test_delta_tracks_cross_cutoff_arrival(self):
        m=SharedHomeStateModel()
        m.observe('a','a',1,99)
        delta=m.live_delta(100)
        delta.observe('b','b',1,101)
        self.assertEqual(delta.graph[('a',)]['outcomes']['b'][2],1)

    def test_non_learning_poll_does_not_add_training_updates(self):
        c=ContextEngine(DEFAULT_OPTIONS);eid='binary_sensor.motion'
        c.configure({eid:state(eid,'off')},entities={eid:{'area_id':'room'}})
        c.bootstrap_started=100;c.bootstrap_delta=c.home.live_delta(100)
        c.observe(eid,state(eid,'off'),101,learn=False)
        self.assertEqual(c.bootstrap_delta.updated,0)
        self.assertEqual(c.bootstrap_delta.graph,{})

    def test_bootstrap_installs_under_high_live_activity(self):
        import engine as em
        import history as hm
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'test.db')
            with patch.object(em,'STORE',store),patch.object(hm,'STORE',store):
                e=Engine();eid='binary_sensor.motion'
                e.context.store=store
                e.state_map={eid:state(eid,'off')}
                e.context.configure(e.state_map,entities={eid:{'area_id':'room'}})
                ts=time.time()-100
                store.archive_batch([(eid,ts,'off',{},None,'test'),(eid,ts+1,'on',{},None,'test')])
                h=HistoryManager(e);bootstrap=HomeBootstrap(e.context,h,store)
                original=store.archive_iter
                def busy(*args,**kwargs):
                    start=time.time()
                    for i in range(12000):
                        e.context.observe(eid,state(eid,'on' if i%2 else 'off'),start+i*.00001)
                    yield from original(*args,**kwargs)
                try:
                    with patch.object(store,'archive_iter',side_effect=busy):
                        self.assertTrue(bootstrap.start(import_recorder=False))
                        bootstrap.thread.join(timeout=20)
                    self.assertEqual(bootstrap.status['state'],'READY',bootstrap.status)
                    self.assertEqual(e.context.home.values['room']['p'],1)
                    self.assertGreaterEqual(e.context.home.updated,12000)
                    self.assertIsNone(e.context.bootstrap_delta)
                    self.assertIsNone(HEAVY_JOBS.owner)
                    persisted=json.loads(store.meta_get(ContextEngine.ROOM_MODEL_KEY))
                    self.assertEqual(persisted['version'],2)
                    self.assertEqual(persisted['updates'],e.context.home.updated)
                    self.assertIsNone(store.meta_get(ContextEngine.LEGACY_ROOM_MODEL_KEY))
                finally:
                    e.control_workers.shutdown();e.poll_worker.shutdown()


if __name__=='__main__':unittest.main()