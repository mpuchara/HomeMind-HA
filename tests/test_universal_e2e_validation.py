"""Original production V2 receipts + V3 through REAL offline fit/gate/publication.

All trials and operator assertions are synthetic, not home/Pi acceptance.
Training configuration and default ValidationPolicy are fixed before evaluation.
"""
import copy
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import support
sys.path.insert(0,str(support.ROOT/'tools'))
from storage import Store
from manual_feedback_contract import ManualFeedbackJournal
from universal_e2e_fixture import OriginalEvidenceScenario
from universal_checkpoint_lifecycle import CheckpointLifecycle,NativeDatasetSource
from universal_scorer_training import TrainingConfig,context_from_sample,train_scorer
from universal_scorer_validation import ValidationPolicy,validate_core_artifact
from universal_training_data import chronological_split,_digest
from universal_neural_encoder import SharedSetEncoder
from universal_action_scorer import UniversalActionScorer


CONFIG=TrainingConfig(epochs=24)
POLICY=ValidationPolicy()


class OriginalE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=tempfile.TemporaryDirectory();cls.template=Path(cls.folder.name)/'original.db'
        store=Store(cls.template)
        with OriginalEvidenceScenario(store) as scenario:
            cls.source=scenario.collect(128);cls.split=scenario.split();cls.end=scenario.clock()
            cls.frames=list(scenario.frames)
            if len(scenario.intents)!=128 or scenario.ha.call_count:raise AssertionError('runtime safety boundary')
            cls.collector_stats=scenario.collector.diagnostics()
        with store.conn() as c:
            c.execute('BEGIN');cls.original_data=cls.source.dataset(c)
        if len(cls.original_data.samples)!=128 or cls.original_data.rejected:raise AssertionError(cls.original_data.rejected)
        service=CheckpointLifecycle(store)
        cls.result=service.rebuild(cls.source,parent_seed=1,encoder_seed=41,**cls.split,config=CONFIG)
        if cls.result['status']!='optimization_complete':raise AssertionError(cls.result)
        cls.core=cls.result['checkpoint']
        # No quality evaluation here: publication reserves the holdout FIRST.
        with store.conn() as c:c.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    @classmethod
    def tearDownClass(cls):cls.folder.cleanup()

    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.path=Path(folder.name)/'experiment.db';shutil.copyfile(self.template,self.path)
        self.store=Store(self.path);self.service=CheckpointLifecycle(self.store)

    def data(self,source=None):
        with self.store.conn() as c:
            c.execute('BEGIN');return (source or self.source).dataset(c)

    def publish(self,core=None,source=None):
        result=self.service.publish(core or self.core,source or self.source,policy=POLICY)
        self.assertEqual(result['status'],'published',result)
        return result['artifact_id']

    def artifact(self,aid):
        with self.store.conn() as c:
            return json.loads(c.execute('SELECT artifact FROM universal_validated_checkpoints WHERE id=?',(aid,)).fetchone()[0])

    def undo(self,index=0):
        ManualFeedbackJournal(self.store,clock=lambda:self.end+1).undo(f'e2e-correct:{index}')

    def delete(self,entity):
        with self.store.conn() as c:c.execute('DELETE FROM feature_observation_events WHERE entity_id=?',(entity,))

    def test_a_original_v2_native_source_and_causal_v3_dataset(self):
        doc=json.loads(self.source.raw)
        self.assertEqual(doc['coverage']['version'],3)
        self.assertEqual(doc['coverage']['historical_sensor_universe_count'],5)
        self.assertEqual(doc['coverage']['entity_ids'],['binary_sensor.motion'])
        with self.store.conn() as c:
            rows=c.execute('SELECT id,body FROM universal_collection_decisions ORDER BY at').fetchall()
            self.assertEqual(c.execute('SELECT COUNT(*) FROM provenance_decisions').fetchone()[0],128)
        self.assertEqual(len(rows),128)
        for item,(did,raw),sample in zip(doc['decisions'],rows,self.data().samples):
            receipt=item['receipt'];native=json.loads(raw);payload=json.loads(receipt['payload_json'])
            self.assertEqual(receipt['version'],2)
            self.assertEqual(receipt,native['receipt'])
            evidence=payload['predecision_evidence'];affected=payload['affected_sensor_evidence']
            self.assertEqual(evidence['decision_id'],did)
            self.assertEqual(affected['decision_id'],did)
            self.assertEqual(payload['legal_mask'],native['predecision']['legal_mask'])
            self.assertEqual(len(payload['legal_mask']['actions']),2)
            self.assertLessEqual(evidence['available_ts'],evidence['inference_started_ts'])
            self.assertLessEqual(affected['information_available_ts'],evidence['inference_started_ts'])
            self.assertEqual([t.entity_id for t in sample.home_observation],['binary_sensor.motion'])
            self.assertTrue(all(t.event_ts<=sample.observation_timestamp and t.received_ts<=sample.observation_timestamp
                                for t in sample.home_observation))
        self.assertEqual(self.collector_stats['dropped'],0)

    def test_b_real_weight_updates_frozen_encoder_and_future_manifest(self):
        core=self.core;parent=UniversalActionScorer(seed=1);encoder=SharedSetEncoder(seed=41)
        self.assertGreater(core['updates'],0)
        self.assertEqual(core['parent_id'],_digest(parent.checkpoint()))
        self.assertNotEqual(core['scorer'],parent.checkpoint())
        self.assertEqual(core['encoder'],encoder.checkpoint())
        self.assertEqual(core['encoder_id'],_digest(encoder.checkpoint()))
        self.assertEqual((core['dataset_id'],core['supervision_revision']),
                         (self.data().dataset_id,self.data().supervision_revision))
        train,holdout=chronological_split(self.data(),**self.split)
        self.assertEqual((len(train),len(holdout)),(85,43))
        self.assertFalse({s.episode_id for s in train}&{s.episode_id for s in holdout})
        self.assertEqual(core['before']['holdout']['status'],'withheld')
        self.assertEqual(core['after']['holdout']['status'],'withheld')
        self.assertFalse(core['holdout_evaluated'])
        self.assertEqual({r['sample_id'] for r in core['samples']},{s.sample_id for s in (*train,*holdout)})
        self.assertEqual(parent.checkpoint(),UniversalActionScorer(seed=1).checkpoint())

    def test_c_original_evidence_to_real_passed_artifact_and_guarded_restart(self):
        configs=self.store.list_agent_configs()
        aid=self.publish();artifact=self.artifact(aid);report=artifact['report']
        self.assertEqual(report['status'],'passed',report)
        self.assertEqual(report['policy'],asdict(POLICY))
        self.assertEqual(report['holdout_units'],43)
        self.assertGreater(report['holdout']['mean_paired_gain'],0)
        self.assertLessEqual(report['holdout']['improvement_p_value_upper_bound'],POLICY.family_alpha)
        for key in ('artifact_id','core_id','encoder_id','dataset_id','supervision_revision','source_id','policy_id','holdout_id','seal'):
            self.assertTrue(artifact[key],key)
        restored=CheckpointLifecycle(Store(self.path)).load(aid,NativeDatasetSource(self.source.raw))
        ranked=restored.rank_context(context_from_sample(self.data().samples[-1]))
        self.assertEqual(ranked,self.result['model'].rank_context(context_from_sample(self.data().samples[-1])))
        self.assertFalse(restored.control_eligible);self.assertFalse(restored.promotion_eligible)
        self.assertEqual(configs,self.store.list_agent_configs())

    def test_undo_before_training_excludes_fact_and_rebuilds_fresh(self):
        self.undo();data=self.data()
        self.assertEqual(len(data.samples),127)
        self.assertNotIn('feedback-v1:e2e-correct:0',{s.supervision_event_id for s in data.samples})
        fresh=self.service.rebuild(self.source,parent_seed=1,encoder_seed=41,**self.split,config=CONFIG)
        self.assertEqual(fresh['status'],'optimization_complete')
        self.assertEqual(fresh['checkpoint']['dataset_id'],data.dataset_id)
        self.assertEqual(fresh['checkpoint']['parent_id'],self.core['parent_id'])
        self.assertNotEqual(fresh['checkpoint']['supervision_revision'],self.core['supervision_revision'])

    def test_undo_during_training_prevents_stale_publication(self):
        def fit(*args,**kwargs):
            self.undo();return train_scorer(*args,**kwargs)
        with patch('universal_checkpoint_lifecycle.train_scorer',side_effect=fit):
            result=self.service.rebuild(self.source,parent_seed=1,encoder_seed=41,**self.split,config=CONFIG)
        self.assertEqual(result['status'],'optimization_complete')
        with self.assertRaisesRegex(ValueError,'revision changed'):
            self.service.publish(result['checkpoint'],self.source)

    def test_undo_after_validation_cannot_commit_artifact(self):
        def evaluate(*args,**kwargs):
            report=validate_core_artifact(*args,**kwargs);self.undo();return report
        with patch('universal_checkpoint_lifecycle.validate_core_artifact',side_effect=evaluate):
            with self.assertRaisesRegex(ValueError,'supervision changed'):self.service.publish(self.core,self.source)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM universal_validated_checkpoints').fetchone()[0],0)
            self.assertGreater(c.execute('SELECT COUNT(*) FROM universal_checkpoint_holdouts').fetchone()[0],0)

    def test_undo_after_publication_and_clean_rebuild_burn_old_holdout(self):
        aid=self.publish();loaded=self.service.load(aid,self.source);self.undo()
        with self.assertRaises(ValueError):loaded.rank_context(context_from_sample(self.original_data.samples[-1]))
        with self.assertRaises(ValueError):CheckpointLifecycle(Store(self.path)).load(aid,self.source)
        fresh=self.service.rebuild(self.source,parent_seed=1,encoder_seed=41,predecessor_id=aid,**self.split,config=CONFIG)
        self.assertEqual(fresh['status'],'optimization_complete')
        self.assertEqual(fresh['checkpoint']['rebuild_of'],aid)
        with self.assertRaisesRegex(ValueError,'holdout already exposed'):
            self.service.publish(fresh['checkpoint'],self.source)

    def test_retention_before_training_required_and_unrelated(self):
        self.delete('sensor.outdoor')
        self.assertEqual(self.data().dataset_id,self.original_data.dataset_id)
        fresh=self.service.rebuild(self.source,parent_seed=1,encoder_seed=41,**self.split,config=CONFIG)
        self.assertEqual(fresh['status'],'optimization_complete')
        self.delete('binary_sensor.motion')
        with self.assertRaisesRegex(ValueError,'retention'):
            self.service.rebuild(self.source,parent_seed=1,encoder_seed=41,**self.split,config=CONFIG)

    def test_required_history_loss_during_training_prevents_publish(self):
        def fit(*args,**kwargs):
            self.delete('binary_sensor.motion');return train_scorer(*args,**kwargs)
        with patch('universal_checkpoint_lifecycle.train_scorer',side_effect=fit):
            result=self.service.rebuild(self.source,parent_seed=1,encoder_seed=41,**self.split,config=CONFIG)
        self.assertEqual(result['status'],'optimization_complete')
        with self.assertRaisesRegex(ValueError,'retention'):self.service.publish(result['checkpoint'],self.source)

    def test_required_seed_loss_before_validation_rejects(self):
        doc=json.loads(self.source.raw);key=doc['coverage']['seed_event_keys'][0][1]
        with self.store.conn() as c:c.execute('DELETE FROM feature_observation_events WHERE event_key=?',(key,))
        with self.assertRaisesRegex(ValueError,'retention'):self.service.publish(self.core,self.source)

    def test_required_loss_after_validation_blocks_atomic_publish(self):
        def evaluate(*args,**kwargs):
            report=validate_core_artifact(*args,**kwargs);self.delete('binary_sensor.motion');return report
        with patch('universal_checkpoint_lifecycle.validate_core_artifact',side_effect=evaluate):
            with self.assertRaisesRegex(ValueError,'retention'):self.service.publish(self.core,self.source)

    def test_unrelated_loss_after_validation_and_publish_survives_restart(self):
        def evaluate(*args,**kwargs):
            report=validate_core_artifact(*args,**kwargs);self.delete('sensor.outdoor');return report
        with patch('universal_checkpoint_lifecycle.validate_core_artifact',side_effect=evaluate):aid=self.publish()
        self.delete('sensor.effect')
        loaded=CheckpointLifecycle(Store(self.path)).load(aid,self.source)
        self.assertFalse(loaded.rank_context(context_from_sample(self.data().samples[-1]))['control_eligible'])
        self.delete('binary_sensor.motion')
        with self.assertRaisesRegex(ValueError,'retention'):loaded.rank_context(context_from_sample(self.original_data.samples[-1]))
        with self.assertRaises(ValueError):CheckpointLifecycle(Store(self.path)).load(aid,self.source)

    def test_recovery_during_v3_validation_cannot_resurrect_attempt(self):
        def evaluate(*args,**kwargs):
            report=validate_core_artifact(*args,**kwargs)
            self.assertEqual(CheckpointLifecycle(Store(self.path)).recover(),1)
            return report
        with patch('universal_checkpoint_lifecycle.validate_core_artifact',side_effect=evaluate):
            with self.assertRaisesRegex(ValueError,'attempt no longer active'):self.service.publish(self.core,self.source)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT status FROM universal_checkpoint_attempts').fetchone()[0],'interrupted')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM universal_validated_checkpoints').fetchone()[0],0)

    def test_recomputed_checksums_cannot_substitute_historical_mask_or_registry(self):
        for field in ('mask','registry'):
            doc=json.loads(self.source.raw)
            if field=='mask':
                payload=json.loads(doc['decisions'][0]['receipt']['payload_json'])
                payload['legal_mask']['actions'][0]['reason']='substituted original mask'
                payload['predecision_evidence']['legal_mask']=payload['legal_mask']
                doc['decisions'][0]['receipt']['payload_json']=json.dumps(payload)
            else:
                doc['epochs'][0]['registry']['devices']['measurements']['area_id']='forged-area'
            source=NativeDatasetSource(json.dumps(doc).encode())
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'native .* (receipt|registry).*mismatch'):
                self.service.publish(self.core,source)

    def test_native_provenance_or_original_receipt_loss_rejects_exported_json(self):
        with self.store.conn() as c:c.execute('DELETE FROM provenance_decisions WHERE decision_id=(SELECT id FROM universal_collection_decisions ORDER BY at LIMIT 1)')
        with self.assertRaisesRegex(ValueError,'native decision provenance'):self.data()

    def test_holdout_labels_do_not_fit_weights_or_normalization(self):
        sample=self.original_data.samples[-1]
        self.undo(127)
        ManualFeedbackJournal(self.store,clock=lambda:self.end+2).record(agent_id=sample.agent_id,
            selected_ts=sample.observation_timestamp,source='correct',correct_action=1.-sample.desired_action,
            feedback_id='counterfactual-future-correct',generation_id=sample.generation_id,
            episode_id=sample.episode_id)
        fresh=self.service.rebuild(self.source,parent_seed=1,encoder_seed=41,**self.split,config=CONFIG)
        self.assertEqual(fresh['status'],'optimization_complete')
        self.assertEqual(fresh['checkpoint']['scorer'],self.core['scorer'])
        self.assertEqual(fresh['checkpoint']['normalization'],self.core['normalization'])
        self.assertEqual(fresh['checkpoint']['encoder'],self.core['encoder'])
        self.assertNotEqual(fresh['checkpoint']['supervision_revision'],self.core['supervision_revision'])

    def test_real_quality_failure_and_invalid_or_invalidated_checkpoint(self):
        bad=copy.deepcopy(self.core);bad['checksum']='wrong'
        self.assertEqual(validate_core_artifact(bad,self.data())['status'],'invalid_artifact')
        with self.assertRaises(ValueError):self.service.publish(bad,self.source)
        bad=copy.deepcopy(self.core)
        for layer in bad['scorer']['layers'].values():
            layer['weights']=[[0. for _ in row] for row in layer['weights']]
            layer['bias']=[0. for _ in layer['bias']]
        bad.pop('checksum');bad['checksum']=_digest(bad)
        result=self.service.publish(bad,self.source)
        self.assertEqual(result['status'],'validation_failed',result)
        self.undo()
        self.assertEqual(validate_core_artifact(self.core,self.data())['status'],'dataset_invalidated')

    def test_substituted_artifact_hmac_and_supervision_revision_reject(self):
        bad=copy.deepcopy(self.core);bad['supervision_revision']='0'*64
        bad.pop('checksum');bad['checksum']=_digest(bad)
        with self.assertRaisesRegex(ValueError,'revision changed'):self.service.publish(bad,self.source)
        aid=self.publish();artifact=self.artifact(aid)
        artifact['core']['scorer']['layers']['dense1']['weights'][0][0]+=1
        with self.store.conn() as c:
            c.execute('UPDATE universal_validated_checkpoints SET artifact=? WHERE id=?',
                      (json.dumps(artifact).encode(),aid))
        with self.assertRaisesRegex(ValueError,'seal'):self.service.load(aid,self.source)
        with self.assertRaises(ValueError):CheckpointLifecycle(Store(self.path)).load(aid,self.source)

    def test_later_config_and_assertion_do_not_rewrite_valid_history(self):
        doc=json.loads(self.source.raw);payload=json.loads(doc['decisions'][0]['receipt']['payload_json'])
        agent=payload['agent']
        self.store.update_agent(agent['id'],{'confidence_threshold':.91})
        from universal_affected_sensors import AffectedSensorDeclarations
        with patch('time.time',return_value=self.end+1):
            AffectedSensorDeclarations(self.store).declare(agent['id'],
                logical_device_id=payload['descriptor']['logical_device_id'],
                topology_revision=doc['coverage']['topology_revision'],affected_sensor_ids=None,
                complete=False,approved_by='synthetic operator',rationale='future withdrawal')
        self.assertEqual(self.data().dataset_id,self.original_data.dataset_id)
        self.publish()


class OriginalBoundaryTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.store=Store(Path(folder.name)/'boundary.db')
        self.scenario=OriginalEvidenceScenario(self.store);self.addCleanup(self.scenario.close)

    def dataset(self,source):
        with self.store.conn() as c:c.execute('BEGIN');return source.dataset(c)

    def test_default_policy_reports_insufficient_support(self):
        s=self.scenario;source=s.collect(8);service=CheckpointLifecycle(self.store)
        trained=service.rebuild(source,parent_seed=1,encoder_seed=41,**s.split(),config=CONFIG)
        self.assertEqual(trained['status'],'optimization_complete')
        result=service.publish(trained['checkpoint'],source,policy=POLICY)
        self.assertEqual(result['status'],'insufficient_evidence',result)
        self.assertEqual(result['report']['required_global_units'],30)
        with self.assertRaisesRegex(ValueError,'consumed'):service.publish(trained['checkpoint'],source)

    def test_repeated_episode_cannot_manufacture_independent_units(self):
        s=self.scenario
        for i in range(8):s.episode(i,episode_id='one-indivisible-trial')
        source=s.source();data=self.dataset(source)
        self.assertEqual(len(data.samples),8)
        result=CheckpointLifecycle(self.store).rebuild(source,parent_seed=1,encoder_seed=41,**s.split(),config=CONFIG)
        self.assertEqual(result['status'],'insufficient_evidence')

    def test_predictions_and_automations_are_not_correct_labels(self):
        s=self.scenario
        for i in range(8):s.episode(i,source='shadow_prediction' if i%2 else 'automation')
        self.assertFalse(self.dataset(s.source()).samples)
        self.assertEqual(len(s.intents),8);self.assertEqual(s.ha.call_count,0)

    def test_prepared_context_changes_fail_closed_without_extra_intents(self):
        # Separate disposable Store per mutation: never rewind an existing stream.
        mutations={
            'config':lambda s,a:s.store.update_agent(a['id'],{'confidence_threshold':.9}),
            'override':lambda s,a:s.engine.set_manual_hold(a,s.engine.runtime.setdefault(a['id'],{}),s.clock()),
            'declaration':lambda s,a:s.declare(a,affected_sensor_ids=None,complete=False),
            'mapping':lambda s,a:s.engine.executor.device_agents.set_explicit_mapping(a['target_entity'],
                logical_device_id='synthetic-new-device',property_name='power'),
            'mask':lambda s,a:s.evidence.prepared[a['id']]['legal_mask']['actions'].pop(),
        }
        def generation(s,a):
            with s.store.conn() as c:c.execute('UPDATE agent_candidate_generations SET generation_id=? WHERE agent_id=?',('synthetic:g2',a['id']))
        def lease(s,a):
            key=s.engine.executor.device_agents.resource_keys_for_agent(a)[0]
            with s.store.conn() as c:c.execute('INSERT OR REPLACE INTO device_resource_state(resource_key,lease_until,updated_ts) VALUES(?,?,?)',(key,s.clock()+60,s.clock()))
        def area(s,a):
            devices=copy.deepcopy(s.devices);devices[0]['area_id']='new-room'
            s.collector.apply_registry('device',s.engine.update_device_registry,devices,s.clock(),True,s.collector.epoch)
            s.drain()
        mutations.update(generation=generation,lease=lease,area=area)
        # Close the setUp fixture before patching globals for another producer.
        self.scenario.close()
        for name,mutation in mutations.items():
            with self.subTest(name=name),tempfile.TemporaryDirectory() as folder:
                store=Store(Path(folder)/'case.db')
                with OriginalEvidenceScenario(store) as s:
                    s.episode(0,before_predict=mutation)
                    with store.conn() as c:
                        rows=c.execute('SELECT body FROM universal_collection_decisions').fetchall()
                    self.assertEqual(len(rows),1)
                    body=json.loads(rows[0][0]);self.assertTrue(body['receipt'] is None,(name,body.get('missing_proof')))
                    self.assertEqual(len(s.intents),1);self.assertEqual(s.ha.call_count,0)

    def test_ws_gap_and_collector_restart_do_not_extend_old_history(self):
        from universal_history_collector import HistoricalTrainingCollector,source_from_collection
        s=self.scenario;source=s.collect(8);identity=self.dataset(source).dataset_id
        s.collector.connection(False);s.drain()
        self.assertEqual(self.dataset(source).dataset_id,identity)
        s.clock.set(s.clock()+1)
        replacement=HistoricalTrainingCollector(self.store,s.journal,clock=s.clock)
        replacement.connection(True);replacement.barrier()
        while replacement.pending or replacement.pending_gap is not None:self.assertTrue(replacement.drain())
        with self.store.conn() as c:
            c.execute('BEGIN')
            with self.assertRaisesRegex(ValueError,'incomplete replay coverage'):
                source_from_collection(c,[a['id'] for a in s.agents],start=s.start,through=s.clock())
        self.assertEqual(self.dataset(source).dataset_id,identity)


if __name__=='__main__':unittest.main()
