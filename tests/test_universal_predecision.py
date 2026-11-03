"""Original DeviceAgent snapshots and native U05a path, with real clock order."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support
from test_device_agents import _Engine, _agent
from device_agents import DeviceAgentService
from storage import Store
from observation_contract import FeatureJournal
from universal_history_collector import HistoricalTrainingCollector, source_from_collection
from universal_predecision import PredecisionEvidence, revision, verify_for_write, validate_binding
from agent_candidate_lineage import ensure_lineage_tables, _ensure_root
from intent import ActionIntent


class PredecisionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name)/'data.db')
        created = _agent(self.store, 'light.kitchen', 'power')
        self.agent = self.store.get_agent_config(created['id'])
        ensure_lineage_tables(self.store)
        self.generation = _ensure_root(self.store, self.agent['id'], generation_number=0)['generation_id']
        self.state = support.state('light.kitchen', 'off')
        entities = {'light.kitchen': {'device_id': 'light-device'},
                    'binary_sensor.motion': {'device_id': 'sensor-device'}}
        devices = {'light-device': {'area_id': 'kitchen'}, 'sensor-device': {'area_id': 'kitchen'}}
        self.engine = _Engine(entities, devices, {'light.kitchen': self.state})
        self.engine.context.registry_revision = 1
        self.engine.agent_configs = {self.agent['id']: self.agent}
        self.service = DeviceAgentService(self.engine, self.store)
        self.agent = self.store.get_agent_config(self.agent['id'])
        self.engine.agent_configs = {self.agent['id']: self.agent}
        self.journal = FeatureJournal(self.store)
        self.collector = HistoricalTrainingCollector(self.store, self.journal)
        self.evidence = PredecisionEvidence(self.engine, self.store, self.collector)
        self.collector.predecision = self.evidence
        self.collector.connection(True)
        for name, rows in [('entity', [dict(entity_id=eid, **row) for eid,row in entities.items()]),
                           ('device', [dict(id=did, **row) for did,row in devices.items()]),
                           ('area', [dict(area_id='kitchen')])]:
            self.collector.registry(name, rows)
        requested = time.time()
        self.collector.snapshot({'binary_sensor.motion': dict(entity_id='binary_sensor.motion', state='on',
            last_updated=datetime.now(timezone.utc).isoformat(), attributes={'device_class':'motion'})},
            requested_ts=requested, received_ts=time.time(), epoch=self.collector.epoch)
        self.collector.barrier(); self.drain()

    def drain(self):
        while self.collector.pending or self.collector.pending_gap is not None:
            self.assertTrue(self.collector.drain(), self.collector.diagnostics())

    def prepare(self):
        self.evidence.request(self.agent['id']); self.evidence.prepare_one()
        self.assertIn(self.agent['id'], self.evidence.prepared)

    def bind(self, ticket=None):
        if ticket is None:
            ticket = self.evidence.begin(self.agent, self.state, [0., 1.])
        self.assertIsNotNone(ticket)
        intent = ActionIntent.create(agent_id=self.agent['id'], target_entity='light.kitchen',
            target_property='power', desired_value=1., created_at=time.time(), policy_version=12,
            model_revision='original-model', decision_source='policy',confidence=.8,support=.8,novelty=.1,
            prediction_horizon=1.,ttl=2.,context_revision=1,target_revision=0,reason='original test decision')
        bound = self.evidence.bind(ticket, intent, self.agent, self.state, [0., 1.])
        self.assertIsNotNone(bound)
        return intent, bound

    def test_original_full_mask_no_live_mutation_and_producer_no_sql(self):
        self.prepare()
        prepared = self.evidence.prepared[self.agent['id']]
        expected = self.service.legal_action_mask(self.agent, self.state, [0.,1.], now=prepared['created_ts'])
        self.assertEqual(prepared['legal_mask'], expected)
        with patch.object(self.store, 'conn', side_effect=AssertionError('hot SQL')):
            intent, evidence = self.bind()
            validate_binding(evidence, vars(intent), self.agent, self.state)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM device_resource_state').fetchone()[0], 0)
        self.assertEqual(evidence['values'], [0., 1.])
        self.assertEqual(evidence['generation_id'], self.generation)
        self.assertLessEqual(evidence['available_ts'], evidence['inference_started_ts'])

    def test_runtime_state_registry_actions_and_epoch_invalidations(self):
        self.prepare()
        original = deepcopy(self.evidence.prepared[self.agent['id']])
        for kind in ('hold','state','registry','actions','epoch','config'):
            with self.subTest(kind=kind):
                self.evidence.prepared[self.agent['id']] = deepcopy(original)
                with patch.dict(self.engine.runtime, {'peer': {'manual_override_until':time.time()+60}}) if kind=='hold' else patch.dict({}, {}):
                    state = dict(self.state, state='on') if kind=='state' else self.state
                    agent = dict(self.agent, min_value=.5) if kind=='config' else self.agent
                    if kind=='registry': self.engine.context.registry_revision += 1
                    if kind=='epoch': self.collector.epoch += 1
                    try:
                        self.assertIsNone(self.evidence.begin(agent, state, [0.] if kind=='actions' else [0.,1.]))
                    finally:
                        self.engine.context.registry_revision=original['registry_revision']
                        self.collector.epoch=original['epoch']

    def test_change_during_prediction_and_expiry_rejected(self):
        self.prepare()
        ticket = self.evidence.begin(self.agent,self.state,[0.,1.])
        self.engine.runtime['peer'] = {'manual_override_until':time.time()+60}
        intent = SimpleNamespace(created_at=time.time())
        self.assertIsNone(self.evidence.bind(ticket,intent,self.agent,self.state,[0.,1.]))
        self.engine.runtime.clear()
        self.evidence.prepared[self.agent['id']]['valid_until_ts'] = time.time()-1
        self.assertIsNone(self.evidence.begin(self.agent,self.state,[0.,1.]))

    def test_external_sqlite_writes_and_aba_invalidate_atomically(self):
        for table, sql in [('device_resource_state', "INSERT OR REPLACE INTO device_resource_state(resource_key,lease_until,updated_ts) VALUES('device:light-device',0,0)"),
                           ('device_explicit_mappings', "INSERT OR REPLACE INTO device_explicit_mappings(entity_id,logical_device_id,updated_ts) VALUES('light.kitchen','d2',0)"),
                           ('app_meta', "INSERT OR REPLACE INTO app_meta VALUES('manual_hold:peer','999')"),
                           ('agents', "UPDATE agents SET name=name"),
                           ('agent_candidate_generations', "UPDATE agent_candidate_generations SET updated_ts=updated_ts")]:
            with self.subTest(table=table):
                self.prepare(); intent, evidence = self.bind()
                with closing(sqlite3.connect(self.store.path)) as other, other:
                    other.execute(sql)
                with self.store.conn() as c:
                    c.execute('BEGIN IMMEDIATE')
                    with self.assertRaisesRegex(ValueError,'revision changed'):
                        verify_for_write(c,evidence,vars(intent),self.agent,self.state)

    def test_original_manual_hold_materialization_only_changes_copy(self):
        peer = _agent(self.store,'light.kitchen','brightness_pct', maximum=100)
        self.engine.runtime[peer['id']] = {'manual_override_until': time.time()+60}
        self.prepare()
        prepared = self.evidence.prepared[self.agent['id']]
        self.assertTrue(all(not r['legal'] for r in prepared['legal_mask']['actions']))
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM device_resource_state').fetchone()[0],0)

    def test_partial_mask_backdated_identity_and_unverified_evidence_rejected(self):
        self.prepare(); intent, evidence = self.bind()
        for key, value in [('values',[1.]),('available_ts',intent.created_at+1),
                           ('decision_id','other'),('generation_id',None)]:
            malformed = dict(evidence, **{key:value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_binding(malformed,vars(intent),self.agent,self.state)

    def native_source(self, intent, evidence, *, affected=(),required=('binary_sensor.motion',)):
        from provenance import ProvenanceJournal
        from manual_feedback_contract import ManualFeedbackJournal
        from teaching_rl import RLTeaching
        from correct_data_foundation import _ensure_schema
        self.collector.decision(intent,self.agent,self.state,required=required,
                                affected=affected,predecision=evidence)
        self.drain()
        ProvenanceJournal(self.store).record_decision(decision_id=intent.intent_id,created_time=intent.created_at,
            agent_id=self.agent['id'],generation_id=self.generation,model_version=12,model_revision='original-model')
        RLTeaching(self.store,SimpleNamespace()); _ensure_schema(self.store)
        feedback = ManualFeedbackJournal(self.store)
        feedback.record(agent_id=self.agent['id'],selected_ts=intent.created_at,source='correct',correct_action=1.,feedback_id='original-correct')
        self.collector.barrier(); self.drain()
        with self.store.conn() as c:
            start,through=c.execute('SELECT start,through FROM universal_collection_segments ORDER BY start DESC LIMIT 1').fetchone()
            c.execute('BEGIN')
            source=source_from_collection(c,[self.agent['id']],start=start,through=through)
            dataset=source.dataset(c)
        return dataset, source, feedback

    def test_real_clocks_original_mask_native_dataset_and_undo(self):
        from universal_affected_sensors import AffectedSensorDeclarations
        AffectedSensorDeclarations(self.store).declare(self.agent['id'],
            logical_device_id=self.service.descriptor(self.agent)['logical_device_id'],
            topology_revision=self.collector.topology,affected_sensor_ids=[],complete=True,
            approved_by='fixture operator',rationale='independent motion fixture',
            empty_reason='no dependent monitored sensors in this fixture scope')
        self.prepare(); intent,evidence=self.bind()
        dataset, source, feedback = self.native_source(intent,evidence)
        self.assertEqual(len(dataset.samples),1,dataset.rejected)
        self.assertEqual(dataset.samples[0].available_actions.count,2)
        from universal_checkpoint_lifecycle import NativeDatasetSource
        for changed in ('verified_revision','legal_mask','values','event_ts','topology','mask_contract'):
            doc=json.loads(source.raw)
            receipt=doc['decisions'][0]['receipt']
            payload=json.loads(receipt['payload_json'])
            evidence=payload['predecision_evidence']
            if changed=='verified_revision': evidence.pop('verified_revision')
            elif changed=='legal_mask': evidence['legal_mask']['actions']=evidence['legal_mask']['actions'][:1]
            elif changed=='event_ts': receipt['event_ts']-=.01
            elif changed=='topology': evidence['topology']='other'
            elif changed=='mask_contract': evidence['mask_contract']='post_executor_mask'
            else:evidence['values']=[1.]
            receipt['payload_json']=json.dumps(payload)
            malformed=NativeDatasetSource(json.dumps(doc).encode())
            with self.subTest(changed=changed), self.store.conn() as c:
                c.execute('BEGIN'); self.assertFalse(malformed.dataset(c).samples)
        feedback.undo('original-correct')
        with self.store.conn() as c:
            c.execute('BEGIN'); self.assertFalse(source.dataset(c).samples)

    def test_missing_evidence_or_unknown_affected_stays_ineligible(self):
        self.prepare(); intent,evidence=self.bind()
        dataset,_,_=self.native_source(intent,evidence,affected=None)
        self.assertFalse(dataset.samples)
        self.assertIn('affected_declaration_missing',str(dataset.rejected))
        with self.store.conn() as c:
            body=json.loads(c.execute('SELECT body FROM universal_collection_decisions').fetchone()[0])
        self.assertIsNotNone(body['predecision'])
        self.assertEqual(body['missing_proof'],'affected_declaration_missing')

    def test_late_arbiter_change_preserves_decision_but_rejects_training(self):
        self.prepare();intent,evidence=self.bind()
        self.store.meta_set('manual_hold:peer',str(time.time()+60))
        dataset,_,_=self.native_source(intent,evidence)
        self.assertFalse(dataset.samples)
        with self.store.conn() as c:
            body=json.loads(c.execute('SELECT body FROM universal_collection_decisions').fetchone()[0])
        self.assertEqual(body['intent']['intent_id'],intent.intent_id)
        self.assertEqual(body['missing_proof'],'stale_or_invalid_predecision_evidence')

    def test_lease_expiry_is_a_validity_boundary_without_any_write(self):
        deadline=time.time()+2.
        with self.store.conn() as c:
            c.execute('INSERT INTO device_resource_state(resource_key,lease_until,updated_ts) VALUES(?,?,?)',
                      (self.service.resource_keys_for_agent(self.agent)[0],deadline,time.time()))
        self.prepare()
        evidence=self.evidence.prepared[self.agent['id']]
        self.assertEqual(evidence['valid_until_ts'],deadline)
        self.assertTrue(all(not r['legal'] for r in evidence['legal_mask']['actions']))
        with patch('universal_predecision.time.time',return_value=deadline+.001):
            self.assertIsNone(self.evidence.begin(self.agent,self.state,[0.,1.]))

    def test_missing_trigger_and_restart_cannot_reuse_a_ram_witness(self):
        self.prepare()
        replacement=PredecisionEvidence(self.engine,self.store,self.collector)
        self.assertIsNone(replacement.begin(self.agent,self.state,[0.,1.]))
        with self.store.conn() as c:c.execute('DROP TRIGGER uc_revision_agents_UPDATE')
        self.evidence.request(self.agent['id'])
        with self.assertRaisesRegex(ValueError,'revision contract'):self.evidence.prepare_one()

    def test_ram_first_manual_metadata_is_an_original_mask_input(self):
        peer=_agent(self.store,'light.kitchen','brightness_pct',maximum=100)
        key='manual_hold_source:'+peer['id']
        with self.store.conn() as c:
            c.execute('INSERT OR REPLACE INTO app_meta VALUES(?,?)',(key,'explicit_user_v8'))
            c.execute('INSERT OR REPLACE INTO app_meta VALUES(?,?)',('manual_hold:'+peer['id'],str(time.time()+60)))
        # Direct durable SQL does not refresh Store.meta_get in this process.
        self.prepare()
        prepared=self.evidence.prepared[self.agent['id']]
        original=self.service.legal_action_mask(self.agent,self.state,[0.,1.],now=prepared['created_ts'])
        self.assertEqual(prepared['legal_mask'],original)
        self.store._meta_cache[key]='explicit_user_v8'
        self.assertIsNone(self.evidence.begin(self.agent,self.state,[0.,1.]))

    def test_engine_consumes_before_predict_and_missing_evidence_keeps_one_intent(self):
        from test_executor import ExecutorTests
        runtime=ExecutorTests('test_real_event_does_not_wait_for_periodic_throttle')
        runtime.setUp()
        try:
            runtime.store.update_agent(runtime.a['id'],{'mode':'shadow'})
            agent=runtime.store.get_agent_config(runtime.a['id'])
            engine=runtime.e
            engine.agent_configs={agent['id']:agent}
            ensure_lineage_tables(runtime.store)
            _ensure_root(runtime.store,agent['id'],generation_number=0)
            collector=HistoricalTrainingCollector(runtime.store,FeatureJournal(runtime.store))
            for name,rows in [('entity',[{'entity_id':agent['target_entity']}]),('device',[]),('area',[])]:
                collector.registry(name,rows)
            while collector.pending or collector.pending_gap is not None:self.assertTrue(collector.drain())
            evidence=PredecisionEvidence(engine,runtime.store,collector)
            collector.predecision=evidence
            engine.universal_training_collector=collector
            from universal_affected_sensors import AffectedSensorDeclarations
            AffectedSensorDeclarations(runtime.store).declare(agent['id'],
                logical_device_id=engine.executor.device_agents.descriptor(agent)['logical_device_id'],
                topology_revision=collector.topology,affected_sensor_ids=[],complete=True,
                approved_by='fixture operator',rationale='empty monitored fixture universe',
                empty_reason='this fixture topology contains no sensors')
            evidence.request(agent['id']);evidence.prepare_one()
            self.assertIn(agent['id'],evidence.prepared)
            order=[]
            begin=evidence.begin;predict=runtime.model.predict
            def traced_begin(*args):order.append('evidence');return begin(*args)
            def traced_predict(*args):order.append('predict');return predict(*args)
            with patch.object(evidence,'begin',side_effect=traced_begin), patch.object(runtime.model,'predict',side_effect=traced_predict), \
                 patch.object(engine.executor,'submit',wraps=engine.executor.submit) as submitted:
                engine.process_agent(agent,engine.state_map,{agent['target_entity']})
                self.assertEqual(order[:2],['evidence','predict'])
                self.assertEqual(evidence.stats['consumed'],1)
                original=submitted.call_args.args[0]
                self.assertIsInstance(original,ActionIntent)
                while collector.pending:self.assertTrue(collector.drain())
                evidence.prepared.clear();evidence.prepared_bytes=0
                engine.process_agent(agent,engine.state_map,{agent['target_entity']})
                self.assertEqual(submitted.call_count,2)
                self.assertEqual(submitted.call_args.args[0].desired_value,original.desired_value)
                self.assertEqual(evidence.stats['consumed'],1)
            runtime.service.assert_not_called()
            while collector.pending:self.assertTrue(collector.drain())
            with runtime.store.conn() as c:
                bodies=[json.loads(r[0]) for r in c.execute('SELECT body FROM universal_collection_decisions ORDER BY at')]
            self.assertEqual(len(bodies),2)
            self.assertEqual(bodies[0]['predecision']['decision_id'],original.intent_id)
            self.assertEqual(bodies[0]['affected_sensor_status'],'complete')
            self.assertEqual(bodies[0]['affected_sensor_evidence']['decision_id'],original.intent_id)
            self.assertIsNone(bodies[1]['predecision'])
        finally:
            for name in ('housekeeping_worker','registry_worker'):
                worker=getattr(runtime.e,name,None)
                if worker is not None:worker.shutdown(wait=True,cancel_futures=True)
            runtime.tearDown()

    def test_generic_review_global_inputs_fail_closed_without_live_mask_reads(self):
        created=_agent(self.store,'number.limit','value',maximum=10)
        agent=self.store.get_agent_config(created['id'])
        self.engine.agent_configs[agent['id']]=agent
        self.engine.state_map['number.limit']=support.state('number.limit','1',min=0,max=10,step=1)
        self.evidence.request(agent['id'])
        with patch('device_agents.legal_value',side_effect=AssertionError('live global review input')):
            self.evidence.prepare_one()
        self.assertNotIn(agent['id'],self.evidence.prepared)
        self.assertEqual(self.evidence.stats['unsupported'],1)


if __name__=='__main__': unittest.main()
