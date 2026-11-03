"""Scoped causal assertions through original predecision/native training receipts."""
from copy import deepcopy
from datetime import datetime,timezone
import json
import time
import unittest
from unittest.mock import patch

import support
import test_universal_predecision as fixtures
from test_device_agents import _agent
from agent_candidate_lineage import _ensure_root
from universal_affected_sensors import AffectedSensorDeclarations, validate, TABLE
from universal_history_collector import digest,encoded
from universal_checkpoint_lifecycle import NativeDatasetSource


class AffectedSensorTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.PredecisionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.api=AffectedSensorDeclarations(self.f.store)

    def declare(self,ids,**overrides):
        f=self.f
        args=dict(logical_device_id=f.service.descriptor(f.agent)['logical_device_id'],
            topology_revision=f.collector.topology,affected_sensor_ids=ids,complete=True,
            approved_by='test operator',rationale='explicit fixture dependency assessment')
        args.update(overrides)
        return self.api.declare(f.agent['id'],**args)

    def topology(self,extra):
        f=self.f
        for eid,attrs in extra.items():
            f.engine.context.entities[eid]={'device_id':'sensor-device'}
            f.engine.state_map[eid]=support.state(eid,'12',**attrs)
        f.engine.context.registry_revision+=1
        entities=[dict(entity_id=eid,**r) for eid,r in f.engine.context.entities.items()]
        f.collector.registry('entity',entities)
        f.collector.registry('device',[dict(id=eid,**r) for eid,r in f.engine.context.devices.items()])
        f.collector.registry('area',[dict(area_id='kitchen')])
        f.drain()
        requested=time.time()
        states={'binary_sensor.motion':support.state('binary_sensor.motion','on',device_class='motion')}
        states.update({eid:st for eid,st in f.engine.state_map.items() if eid.startswith(('sensor.','binary_sensor.'))})
        for st in states.values():st['last_updated']=datetime.now(timezone.utc).isoformat()
        f.collector.snapshot(states,requested_ts=requested,received_ts=time.time(),epoch=f.collector.epoch)
        f.collector.barrier();f.drain()

    def dataset(self,**kwargs):
        self.f.prepare();intent,evidence=self.f.bind()
        data,source,feedback=self.f.native_source(intent,evidence,**kwargs)
        return data,source,feedback,intent,evidence

    def test_original_events_multiple_affected_power_lux_and_undo(self):
        self.topology({'sensor.effect':{'device_class':'temperature'},'sensor.lux':{'device_class':'illuminance'},
                       'sensor.power':{'device_class':'power'}})
        revision=self.declare(['sensor.effect','sensor.lux','sensor.power'])
        # An original received HA event, not a later replay of today's state.
        at=time.time()
        self.f.collector.event('sensor.effect',support.state('sensor.effect','13',device_class='temperature'),event_ts=at,received_ts=time.time())
        self.f.drain()
        data,source,feedback,intent,evidence=self.dataset(required=('binary_sensor.motion','sensor.effect','sensor.lux','sensor.power'))
        self.assertEqual(len(data.samples),1,data.rejected)
        self.assertEqual([t.entity_id for t in data.samples[0].home_observation],['binary_sensor.motion'])
        proof=evidence['affected_sensor_evidence']
        self.assertEqual(proof['declaration_revision'],revision)
        self.assertEqual(proof['decision_id'],intent.intent_id)
        self.assertLessEqual(proof['information_available_ts'],evidence['inference_started_ts'])
        receipt=json.loads(source.raw)['decisions'][0]['receipt']
        self.assertEqual(receipt['version'],2)
        feedback.undo('original-correct')
        with self.f.store.conn() as c:
            c.execute('BEGIN');self.assertFalse(source.dataset(c).samples)

    def test_power_and_lux_remain_excluded_even_if_not_declared_affected(self):
        self.topology({'sensor.effect':{'device_class':'temperature'},'sensor.lux':{'device_class':'illuminance'},
                       'sensor.power':{'device_class':'power'}})
        self.declare(['sensor.effect'])
        data,*_=self.dataset(required=('binary_sensor.motion','sensor.effect','sensor.lux','sensor.power'))
        self.assertEqual(len(data.samples),1,data.rejected)
        self.assertEqual([t.entity_id for t in data.samples[0].home_observation],['binary_sensor.motion'])

    def test_single_affected_sensor_and_explicit_known_empty_are_distinct(self):
        self.declare(['binary_sensor.motion'])
        self.f.prepare();intent,evidence=self.f.bind()
        self.assertEqual(validate(evidence['affected_sensor_evidence'],evidence),('binary_sensor.motion',))
        self.declare([],empty_reason='only independent motion is monitored in this fixture')
        data,source,*_=self.dataset()
        self.assertEqual(len(data.samples),1,data.rejected)
        payload=json.loads(json.loads(source.raw)['decisions'][0]['receipt']['payload_json'])
        self.assertEqual(payload['affected_sensor_ids'],[])

    def test_missing_mapping_does_not_infer_shared_area_or_names(self):
        self.topology({'sensor.light_kitchen_effect':{'device_class':'temperature'}})
        data,_,_,_,evidence=self.dataset()
        self.assertFalse(data.samples)
        self.assertIn('affected_declaration_missing',str(data.rejected))
        self.assertIsNone(evidence['affected_sensor_evidence'])

    def test_partial_declaration_and_hypothesis_are_not_complete(self):
        for source in ('operator_declaration','automatic_hypothesis'):
            with self.subTest(source=source):
                self.declare(['binary_sensor.motion'],complete=False,evidence_source=source)
                self.f.prepare();_,evidence=self.f.bind()
                self.assertEqual(evidence['affected_sensor_status'],'affected_declaration_incomplete')
                with self.assertRaisesRegex(ValueError,'incomplete'):validate(evidence['affected_sensor_evidence'],evidence)
        with self.assertRaisesRegex(ValueError,'hypothesis'):
            self.declare(['binary_sensor.motion'],evidence_source='automatic_hypothesis')

    def test_empty_unknown_and_unknown_sensors_cannot_attest_completeness(self):
        for ids,kwargs in [(None,{}),([],{}),([],{ 'complete':False}),(['sensor.missing'],{})]:
            with self.subTest(ids=ids,kwargs=kwargs),self.assertRaises(ValueError):self.declare(ids,**kwargs)
        self.declare(None,complete=False)
        data,*_=self.dataset()
        self.assertFalse(data.samples)
        self.assertIn('affected_declaration_incomplete',str(data.rejected))

    def test_later_declaration_does_not_qualify_earlier_decision(self):
        data,source,_,intent,_=self.dataset()
        self.assertFalse(data.samples)
        self.declare([],empty_reason='independent motion fixture')
        with self.f.store.conn() as c:
            c.execute('BEGIN');again=source.dataset(c)
        self.assertFalse(again.samples)
        self.assertIn('affected_declaration_missing',str(again.rejected))
        self.f.prepare();_,new=self.f.bind()
        self.assertGreater(new['affected_sensor_evidence']['information_available_ts'],intent.created_at)

    def test_replacement_preserves_old_historical_assertion_without_fallback(self):
        first=self.declare([],empty_reason='independent monitored motion')
        data,source,*_=self.dataset()
        self.assertEqual(len(data.samples),1,data.rejected)
        self.declare(None,complete=False,rationale='withdraw earlier completeness assertion')
        self.f.prepare();_,new=self.f.bind()
        self.assertEqual(new['affected_sensor_status'],'affected_declaration_incomplete')
        with self.f.store.conn() as c:
            c.execute('BEGIN');self.assertEqual(len(source.dataset(c).samples),1)
            self.assertEqual(c.execute(f'SELECT COUNT(*) FROM {TABLE}').fetchone()[0],2)
            self.assertEqual(c.execute(f'SELECT revision FROM {TABLE} ORDER BY seq LIMIT 1').fetchone()[0],first)

    def test_change_after_prepare_invalidates_native_revision(self):
        self.declare([],empty_reason='independent fixture')
        self.f.prepare();intent,evidence=self.f.bind()
        self.declare(None,complete=False)
        data,*_=self.f.native_source(intent,evidence)
        self.assertFalse(data.samples)
        with self.f.store.conn() as c:
            body=json.loads(c.execute('SELECT body FROM universal_collection_decisions').fetchone()[0])
        self.assertEqual(body['missing_proof'],'stale_or_invalid_predecision_evidence')

    def test_topology_configuration_mapping_and_generation_changes_reject(self):
        self.declare([],empty_reason='independent fixture')
        self.topology({'sensor.new':{'device_class':'temperature'}})
        self.f.prepare();_,new=self.f.bind()
        self.assertEqual(new['affected_sensor_status'],'affected_topology_mismatch')
        self.declare([],empty_reason='independent fixture after topology review')
        self.f.store.update_agent(self.f.agent['id'],{'name':'changed configuration'})
        self.f.agent=self.f.store.get_agent_config(self.f.agent['id'])
        self.f.engine.agent_configs[self.f.agent['id']]=self.f.agent
        self.f.prepare();_,new=self.f.bind()
        self.assertIn('identity_or_configuration',new['affected_sensor_status'])

    def test_two_devices_same_room_have_separate_declarations(self):
        self.topology({'sensor.a':{'device_class':'temperature'},'sensor.b':{'device_class':'temperature'}})
        created=_agent(self.f.store,'light.other','power')
        other=self.f.store.get_agent_config(created['id'])
        _ensure_root(self.f.store,other['id'],generation_number=0)
        self.f.engine.agent_configs[other['id']]=other
        self.f.engine.state_map['light.other']=support.state('light.other','off')
        self.f.engine.context.entities['light.other']={'device_id':'other'}
        self.f.engine.context.devices['other']={'area_id':'kitchen'}
        self.topology({})
        self.declare(['sensor.a'])
        self.api.declare(other['id'],logical_device_id=self.f.service.descriptor(other)['logical_device_id'],
            topology_revision=self.f.collector.topology,affected_sensor_ids=['sensor.b'],complete=True,
            approved_by='operator',rationale='independent second device dependency assessment')
        self.f.prepare()
        self.f.evidence.request(other['id']);self.f.evidence.prepare_one()
        a=self.f.evidence.prepared[self.f.agent['id']]['affected_sensor_evidence']
        b=self.f.evidence.prepared[other['id']]['affected_sensor_evidence']
        self.assertEqual(a['affected_sensor_ids'],['sensor.a'])
        self.assertEqual(b['affected_sensor_ids'],['sensor.b'])

    def test_device_identity_and_generation_invalidate_declarations(self):
        self.declare([],empty_reason='independent fixture')
        self.f.service.set_explicit_mapping('light.kitchen','replacement-device')
        self.f.agent=self.f.store.get_agent_config(self.f.agent['id'])
        self.f.engine.agent_configs[self.f.agent['id']]=self.f.agent
        self.f.prepare();_,proof=self.f.bind()
        self.assertIn('identity_or_configuration',proof['affected_sensor_status'])
        self.declare([],empty_reason='reviewed replacement device')
        with self.f.store.conn() as c:
            c.execute('UPDATE agent_candidate_generations SET generation_id=? WHERE agent_id=?',('replacement-generation',self.f.agent['id']))
        self.f.prepare();_,proof=self.f.bind()
        self.assertIn('identity_or_configuration',proof['affected_sensor_status'])

    def test_corrupt_checksum_missing_revision_and_contract_rejected(self):
        self.declare([],empty_reason='independent fixture')
        with self.f.store.conn() as c:
            original=c.execute(f'SELECT revision,body,checksum FROM {TABLE}').fetchone()
        for kind in ('checksum','revision','version'):
            raw=original[1];revision=original[0];checksum=original[2]
            if kind=='checksum':checksum='wrong'
            elif kind=='revision':revision=''
            else:
                doc=json.loads(raw);doc['contract_version']=99;raw=encoded(doc);revision=checksum=digest(raw)
            with self.subTest(kind=kind):
                with self.f.store.conn() as c:
                    c.execute(f'UPDATE {TABLE} SET revision=?,body=?,checksum=?',(revision,raw,checksum))
                self.f.prepare();_,evidence=self.f.bind()
                self.assertIsNone(evidence['affected_sensor_evidence'])
                self.assertNotEqual(evidence['affected_sensor_status'],'complete')

    def test_exported_u05a_rejects_mutated_causal_fields_and_future_clocks(self):
        self.declare([],empty_reason='independent monitored motion')
        data,source,*_=self.dataset()
        self.assertEqual(len(data.samples),1,data.rejected)
        for kind in ('checksum','revision','decision','topology','configuration','empty','trust','future','universe','missing','downgrade',
                     'configuration_clock','topology_clock','approval_clock','topology_clock_past'):
            doc=json.loads(source.raw);receipt=doc['decisions'][0]['receipt'];payload=json.loads(receipt['payload_json'])
            proof=payload['affected_sensor_evidence'];declaration=json.loads(proof['declaration_json'])
            if kind=='checksum':proof['declaration_checksum']='wrong'
            elif kind=='revision':proof['declaration_revision']=''
            elif kind=='decision':proof['decision_id']='other'
            elif kind=='future':proof['information_available_ts']=receipt['decision_ts']+1
            elif kind=='missing':proof=None
            elif kind=='downgrade':receipt['version']=1
            elif kind.endswith('_clock') or kind=='topology_clock_past':
                key={'configuration_clock':'configuration_information_ts','topology_clock':'topology_information_ts',
                     'approval_clock':'approved_ts','topology_clock_past':'topology_information_ts'}[kind]
                declaration[key]=declaration[key]-.1 if kind=='topology_clock_past' else receipt['decision_ts']+1
                raw=encoded(declaration);proof.update(declaration_json=raw,declaration_revision=digest(raw),declaration_checksum=digest(raw))
            else:
                key={'topology':'topology_revision','configuration':'config_revision','empty':'empty_reason',
                     'trust':'trust_level','universe':'sensor_universe_revision'}[kind]
                declaration[key]=None if kind=='empty' else [] if kind=='universe' else 'other'
                raw=encoded(declaration);proof.update(declaration_json=raw,declaration_revision=digest(raw),declaration_checksum=digest(raw))
            payload['affected_sensor_evidence']=proof
            payload['predecision_evidence']['affected_sensor_evidence']=deepcopy(proof)
            receipt['payload_json']=json.dumps(payload)
            bad=NativeDatasetSource(json.dumps(doc).encode())
            with self.subTest(kind=kind),self.f.store.conn() as c:
                c.execute('BEGIN');self.assertFalse(bad.dataset(c).samples)

    def test_hot_capture_has_no_sql_and_never_waits_for_preparation(self):
        self.declare([],empty_reason='independent monitored motion')
        self.f.prepare()
        with patch.object(self.f.store,'conn',side_effect=AssertionError('producer SQL')):
            intent,evidence=self.f.bind()
            self.assertTrue(self.f.collector.decision(intent,self.f.agent,self.f.state,required=('binary_sensor.motion',),predecision=evidence))
            self.f.evidence.prepared.clear()
            self.assertIsNone(self.f.evidence.begin(self.f.agent,self.f.state,[0.,1.]))


if __name__=='__main__':unittest.main()
