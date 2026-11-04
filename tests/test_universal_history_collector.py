"""Native FeatureJournal integration; no physical HA acceptance claimed."""
from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import time
import threading
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock

import support
from storage import Store
from observation_contract import FeatureJournal
from universal_history_collector import HistoricalTrainingCollector,source_from_collection,install,digest
from universal_training_data import HistoricalDecision
from test_universal_action_runtime import FakeDeviceAgent


def iso(at):return datetime.fromtimestamp(at,timezone.utc).isoformat()


class CollectorTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.store=Store(Path(folder.name)/'data.db')
        self.journal=FeatureJournal(self.store,clock=lambda:1000.)
        self.collector=HistoricalTrainingCollector(self.store,self.journal,clock=lambda:1000.)
        self.store._universal_training_retention_changed=self.collector.retention_changed

    def drain(self):
        while self.collector.pending or self.collector.pending_gap is not None:
            self.assertTrue(self.collector.drain(),self.collector.stats)

    def seed(self,sensors=5,devices=1,area='kitchen'):
        ids=[f'binary_sensor.motion{i}' for i in range(sensors)]
        rows=[dict(entity_id=eid,device_id='d0') for eid in ids]
        self.collector.connection(True,at=2.)
        self.collector.registry('entity',rows,at=1.)
        self.collector.registry('device',[dict(id=f'd{i}',area_id=area) for i in range(devices)],at=1.)
        self.collector.registry('area',[dict(area_id=area,name=area)],at=1.)
        states={eid:dict(state='on',last_updated=iso(10.),attributes={'device_class':'motion'}) for eid in ids}
        self.collector.snapshot(states,received_ts=20.,requested_ts=19.,epoch=self.collector.epoch)
        self.collector.barrier(40.)
        self.drain()
        return ids,states

    def source(self,start=20.,through=40.):
        with self.store.conn() as c:
            c.execute('BEGIN')
            return source_from_collection(c,['a'],start=start,through=through)

    def test_cardinality_and_actual_old_seed_clocks(self):
        for sensors,devices in [(5,1),(50,10),(200,30),(1000,200)]:
            with self.subTest(sensors=sensors,devices=devices):
                self.setUp()  # Independent collection, never backdate into a sealed prior segment.
                self.seed(sensors,devices)
                source=self.source()
                proof=json.loads(source.raw)['coverage']
                self.assertEqual(proof['version'],3)
                self.assertEqual(len(proof['entity_ids']),sensors)
                self.assertTrue(all(r[1:]==[10.,20.] for r in proof['seed_receipts']))

    def test_disabled_install_has_no_thread_or_schema(self):
        import settings
        with patch.dict(settings.OPTIONS,universal_training_collector_enabled=False):
            self.assertIsNone(install(SimpleNamespace(),None))

    def test_completed_indexed_future_does_not_starve_idle_preparation(self):
        from concurrent.futures import Future
        from universal_history_collector import foreground_idle
        future=Future()
        engine=SimpleNamespace(in_flight={'light.a':future},last_event_received=0.)
        self.assertFalse(foreground_idle(engine))
        future.set_result(None)
        self.assertTrue(foreground_idle(engine))
        engine.last_event_received=time.perf_counter()
        self.assertFalse(foreground_idle(engine))

    def test_enabled_initialization_failure_cannot_break_runtime_startup(self):
        import settings
        fake=SimpleNamespace(feature_journal=self.journal,executor=SimpleNamespace(device_agents=SimpleNamespace()))
        with patch.dict(settings.OPTIONS,universal_training_collector_enabled=True),patch(
                'universal_history_collector.HistoricalTrainingCollector',side_effect=OSError('disk full')):
            self.assertIsNone(install(fake,self.store))
        self.assertIsNone(fake.universal_training_collector)
        self.assertEqual(fake.universal_training_collector_error,'OSError')

    def test_shared_native_event_key_avoids_a_parallel_sensor_history(self):
        self.seed()
        state=dict(state='on',last_updated=iso(21.),context={'id':'native-context'},
                   attributes={'device_class':'motion','friendly_name':'Motion'})
        self.journal.record('binary_sensor.motion0',state,event_time=21.,received_time=22.,source='ha_state_changed')
        self.collector.event('binary_sensor.motion0',state,event_ts=21.,received_ts=22.)
        self.drain()
        with self.store.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM feature_observation_events WHERE entity_id='binary_sensor.motion0' AND event_time=21").fetchone()[0],1)

    def test_producer_methods_do_no_sql_and_overflow_latches_a_gap(self):
        self.collector.capacity=1
        with patch.object(self.store,'conn',side_effect=AssertionError('producer SQLite')):
            self.assertTrue(self.collector.event('binary_sensor.motion',dict(state='on'),event_ts=10.,received_ts=11.))
            self.assertFalse(self.collector.event('binary_sensor.motion',dict(state='off'),event_ts=12.,received_ts=13.))
        self.assertEqual(len(self.collector.pending),0)
        self.drain()
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT reason FROM universal_collection_gaps ORDER BY id DESC LIMIT 1').fetchone()[0],'queue_overflow')

    def test_websocket_gap_does_not_extend_old_coverage(self):
        self.seed();self.collector.connection(False);self.collector.barrier(60.);self.drain()
        self.source(20.,40.)
        with self.assertRaisesRegex(ValueError,'incomplete'):self.source(20.,60.)

    def test_partial_snapshot_and_missing_registry_never_qualify(self):
        self.collector.connection(True)
        self.collector.registry('entity',[dict(entity_id='sensor.missing')],at=1.)
        self.collector.registry('device',[],at=1.);self.collector.registry('area',[],at=1.)
        self.collector.snapshot({},received_ts=20.);self.collector.barrier(40.);self.drain()
        with self.assertRaisesRegex(ValueError,'incomplete'):self.source()

    def test_malformed_registry_and_sensor_identity_are_explicit_gaps(self):
        self.assertFalse(self.collector.registry('entity',[{'entity_id':'sensor.a'},{}],at=1.))
        self.assertEqual(self.collector.pending_gap,'invalid_registry')
        self.assertFalse(self.collector.registry('device',[{'id':'a'},{'id':'a'}],at=1.))
        self.assertFalse(self.collector.event('sensor.a',{'entity_id':'sensor.other','state':'1'},event_ts=10.,received_ts=11.))
        self.assertEqual(self.collector.pending_gap,'invalid_sensor_identity')
        self.drain()
        with self.assertRaisesRegex(ValueError,'incomplete'):self.source()

    def test_registry_move_preserves_historical_versions(self):
        ids,states=self.seed()
        original=self.source()
        self.collector.gap('registry_announced');self.drain()
        self.collector.registry('entity',[dict(entity_id=eid,device_id='d0') for eid in ids],at=50.)
        self.collector.registry('device',[dict(id='d0',area_id='bedroom')],at=50.)
        self.collector.registry('area',[dict(area_id='bedroom')],at=50.)
        self.collector.snapshot(states,received_ts=60.,requested_ts=59.,epoch=self.collector.epoch);self.collector.barrier(80.);self.drain()
        new=self.source(60.,80.)
        self.assertNotEqual(original.source_id,new.source_id)
        oldreg=json.loads(original.raw)['epochs'][0]['registry']
        newreg=json.loads(new.raw)['epochs'][0]['registry']
        self.assertEqual(oldreg['devices']['d0']['area_id'],'kitchen')
        self.assertEqual(newreg['devices']['d0']['area_id'],'bedroom')
        with self.assertRaisesRegex(ValueError,'incomplete'):self.source(20.,80.)

    def test_rest_request_must_begin_after_subscription_and_registry(self):
        ids,states=self.seed()
        self.collector.connection(True,at=50.)
        self.collector.snapshot(states,received_ts=60.,requested_ts=45.,epoch=self.collector.epoch)
        self.collector.barrier(80.);self.drain()
        with self.assertRaisesRegex(ValueError,'incomplete'):self.source(60.,80.)
        self.collector.snapshot(states,received_ts=62.,requested_ts=61.,epoch=self.collector.epoch)
        self.collector.barrier(80.);self.drain()
        self.source(62.,80.)
        before=self.collector.epoch;self.collector.gap('queue_overflow')
        self.assertFalse(self.collector.snapshot(states,received_ts=90.,requested_ts=85.,epoch=before))
        self.assertEqual(self.collector.pending_gap,'queue_overflow')
        self.drain()
        with self.store.conn() as c:
            self.assertTrue(c.execute("SELECT 1 FROM universal_collection_gaps WHERE reason='rejected_snapshot_crossed_gap'").fetchone())

    def test_coalesced_gap_cannot_erase_registry_invalidation(self):
        ids,states=self.seed()
        self.collector.gap('registry_announced');self.collector.gap('queue_overflow')
        self.collector.snapshot(states,received_ts=60.,requested_ts=59.,epoch=self.collector.epoch)
        self.collector.barrier(80.);self.drain()
        with self.assertRaisesRegex(ValueError,'incomplete'):self.source(60.,80.)

    def test_restart_before_pending_barrier_cannot_advance_watermark(self):
        self.seed();self.collector.barrier(90.)  # RAM data deliberately abandoned.
        script='''import sys,os
sys.path.insert(0,sys.argv[1])
from storage import Store
from observation_contract import FeatureJournal
from universal_history_collector import HistoricalTrainingCollector
from storage_budget import BudgetConnection
from unittest.mock import patch
store=Store(sys.argv[2]);journal=FeatureJournal(store,clock=lambda:1000.)
collector=HistoricalTrainingCollector(store,journal,clock=lambda:1000.)
collector.connection(True,at=2.)
collector.registry('entity',[dict(entity_id='binary_sensor.restart')],at=70.)
collector.registry('device',[],at=70.);collector.registry('area',[],at=70.)
collector.snapshot({'binary_sensor.restart':dict(state='on',last_updated='1970-01-01T00:00:10+00:00')},received_ts=80.,requested_ts=79.,epoch=collector.epoch)
collector.barrier(90.)
original=BudgetConnection.execute
def execute(self,sql,*a,**k):
    if sql.startswith('INSERT INTO universal_collection_segments'):os._exit(77)
    return original(self,sql,*a,**k)
with patch.object(BudgetConnection,'execute',execute):collector.drain()
'''
        result=subprocess.run([sys.executable,'-c',script,str(support.ROOT/'adaptive_ai/src'),str(self.store.path)],
                              capture_output=True,text=True,timeout=60)
        self.assertEqual(result.returncode,77,result.stderr)
        replacement=HistoricalTrainingCollector(self.store,self.journal,clock=lambda:1001.)
        self.addCleanup(replacement.stop)
        self.source(20.,40.)
        with self.assertRaisesRegex(ValueError,'incomplete'):self.source(20.,90.)
        with self.assertRaisesRegex(ValueError,'incomplete'):self.source(80.,90.)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT reason FROM universal_collection_sessions WHERE id=?',(self.collector.session,)).fetchone()[0],'restart')

    def test_native_pruning_invalidates_proof_in_same_transaction(self):
        self.seed()
        source=self.source()
        self.journal.retention_hours=0
        self.journal.prune()
        with self.assertRaisesRegex(ValueError,'retention'):self.source()
        # Persisted NativeDatasetSource must also recheck the witness every use.
        with self.store.conn() as c:
            c.execute('BEGIN')
            from manual_feedback_contract import ManualFeedbackJournal
            # Preparation with no labels still checks native collection authority.
            with self.assertRaisesRegex(ValueError,'retention witness'):
                from universal_history_collector import verify_collection_coverage
                from universal_training_data import ReplayCoverage
                doc=json.loads(source.raw)['coverage'];doc['entity_ids']=frozenset(doc['entity_ids'])
                verify_collection_coverage(c,ReplayCoverage(**doc))

    def test_corrupt_topology_and_coverage_fail_closed(self):
        self.seed()
        with self.store.conn() as c:c.execute("UPDATE universal_collection_topology SET body='{}'")
        with self.assertRaisesRegex(ValueError,'corrupt historical topology'):self.source()
        with self.store.conn() as c:c.execute('UPDATE universal_collection_segments SET through=9999')
        with self.assertRaisesRegex(ValueError,'corrupt collection'):self.source()

    def intent(self):
        return SimpleNamespace(intent_id='decision-a',agent_id='a',target_entity='light.kitchen',
            target_property='power',desired_value=1.,created_at=30.,policy_version=12,
            model_revision='model-r1',decision_source='policy')

    def test_real_mask_tap_does_not_recompute_or_grant_predecision_authority(self):
        from device_agents import DeviceAgentService
        service=DeviceAgentService.__new__(DeviceAgentService)
        service.universal_training_collector=self.collector
        agent=support.agent(id='a');state=support.state('light.kitchen','off')
        desc=FakeDeviceAgent().descriptor(agent)
        for name,value in [('descriptor',desc),('control_eligibility',{'allowed':True}),
                           ('shared_manual_hold_until',0),('active_lease',[]),
                           ('_resource_rows',{}),('min_dwell_seconds',0)]:
            setattr(service,name,Mock(return_value=value))
        self.collector.tls.decision_id='decision-a'
        mask=service.legal_action_mask(agent,state,[0.,1.],now=31.)
        self.assertEqual(service.descriptor.call_count,1)
        self.assertEqual(len(mask['actions']),2)
        self.drain()
        with self.store.conn() as c:
            body=json.loads(c.execute('SELECT body FROM universal_collection_masks').fetchone()[0])
        self.assertEqual(body['mask'],mask);self.assertEqual(body['phase'],'executor_guard')
        self.assertEqual(body['decision_id'],'decision-a')

    def test_missing_mask_is_explicit_and_adapter_does_not_fabricate_it(self):
        ids,_=self.seed()
        self.collector.decision(self.intent(),support.agent(id='a'),support.state('light.kitchen'),required=ids,affected=())
        self.drain()
        self.assertEqual(json.loads(self.source().raw)['decisions'],[])
        with self.store.conn() as c:
            body=json.loads(c.execute('SELECT body FROM universal_collection_decisions').fetchone()[0])
        self.assertEqual(body['missing_proof'],'no_predecision_legal_mask')

    def test_causal_receipt_adapter_uses_native_u05a_and_undo(self):
        from manual_feedback_contract import ManualFeedbackJournal
        from teaching_rl import RLTeaching
        from correct_data_foundation import _ensure_schema
        ids,_=self.seed()
        agent=support.agent(id='a');state=support.state('light.kitchen');service=FakeDeviceAgent()
        with self.store.conn() as c:
            topo,at=c.execute('SELECT id,at FROM universal_collection_topology').fetchone()
        receipt=HistoricalDecision.capture(decision_ts=30.,event_ts=30.,received_ts=30.,
            registry_information_ts=at,registry_revision=topo,agent=agent,descriptor=service.descriptor(agent),
            legal_mask=service.legal_action_mask(agent,state,[0.,1.],now=30.),state=state,
            required_sensor_ids=ids,affected_sensor_ids=())
        self.collector.decision(self.intent(),agent,state,required=ids,affected=(),receipt=receipt);self.drain()
        journal=ManualFeedbackJournal(self.store,clock=lambda:31.)
        RLTeaching(self.store,SimpleNamespace());_ensure_schema(self.store)
        journal.record(agent_id='a',selected_ts=30.,source='correct',correct_action=1.,feedback_id='correct-a')
        from provenance import ProvenanceJournal
        ProvenanceJournal(self.store).record_decision(decision_id='decision-a',created_time=30.,agent_id='a',
                                                     model_version=12,model_revision='model-r1')
        source=self.source()
        with self.store.conn() as c:
            c.execute('BEGIN');data=source.dataset(c)
        self.assertEqual(len(data.samples),1,data.rejected)
        self.assertEqual(data.samples[0].home_observation[0].event_ts,10.)
        journal.undo('correct-a')
        with self.store.conn() as c:
            c.execute('BEGIN');after=source.dataset(c)
        self.assertFalse(after.samples)

    def test_write_failure_keeps_old_trusted_watermark(self):
        self.seed();self.collector.barrier(80.)
        with patch.object(self.store,'conn',side_effect=OSError('disk failure')):
            self.assertFalse(self.collector.drain())
        with self.assertRaisesRegex(ValueError,'incomplete'):self.source(20.,80.)

    def test_worker_stop_drains_and_closes_session(self):
        self.seed();self.collector.start();self.assertTrue(self.collector.ready.wait(1.))
        self.assertTrue(self.collector.stop(timeout=3.))
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT reason FROM universal_collection_sessions WHERE id=?',(self.collector.session,)).fetchone()[0],'clean_shutdown')

    def test_sustained_foreground_does_not_starve_native_writer(self):
        self.collector.idle_ready=lambda:False
        self.collector.event('binary_sensor.motion',dict(state='on'),event_ts=10.,received_ts=11.)
        committed=threading.Event()
        original=self.collector._write
        def write(*args):
            result=original(*args)
            committed.set()
            return result
        with patch.object(self.collector,'_write',write):
            self.collector.start();self.assertTrue(self.collector.ready.wait(1.))
            self.assertTrue(committed.wait(3.))
        self.assertTrue(self.collector.stop(3.))
        self.assertGreater(self.collector.stats['foreground_batches'],0)
        with self.store.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM feature_observation_events WHERE entity_id='binary_sensor.motion'").fetchone()[0],1)

    def test_busy_writer_retains_events_and_trusted_boundary_for_retry(self):
        self.seed()
        self.collector.event('binary_sensor.motion0',dict(state='off'),event_ts=41.,received_ts=42.)
        self.collector.barrier(60.)
        with self.store.conn() as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            self.assertFalse(self.collector.drain(foreground=True))
            self.assertEqual(len(self.collector.pending),2)
            self.assertEqual(self.collector.stats['errors'],0)
            self.assertEqual(self.collector.stats['dropped'],0)
        with self.assertRaisesRegex(ValueError,'incomplete'):self.source(20.,60.)
        self.assertTrue(self.collector.drain(foreground=True))
        self.source(20.,60.)
        with self.store.conn() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM feature_observation_events WHERE event_time=41").fetchone()[0],1)

    def test_native_foreground_write_does_not_wait_for_unrelated_store_owner(self):
        held=threading.Event();release=threading.Event()
        def hold():
            with self.store.lock:
                held.set();release.wait(3.)
        worker=threading.Thread(target=hold);worker.start()
        self.addCleanup(worker.join,3.);self.addCleanup(release.set)
        self.assertTrue(held.wait(1.))
        self.collector.event('sensor.a',dict(state='1'),event_ts=10.,received_ts=11.)
        self.assertTrue(self.collector.drain(foreground=True))
        self.assertFalse(self.collector.pending)
        with self.store.conn() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM feature_observation_events WHERE entity_id='sensor.a'").fetchone()[0],1)

    def test_deferred_native_pruning_keeps_v3_invalidation(self):
        self.seed();self.journal.retention_hours=0
        held=threading.Event();release=threading.Event()
        def hold():
            with self.store.lock:held.set();release.wait(30.)
        worker=threading.Thread(target=hold);worker.start()
        self.addCleanup(worker.join,3.);self.addCleanup(release.set)
        self.assertTrue(held.wait(1.))
        for i in range(128):
            self.collector.event('sensor.a',dict(state=str(i)),event_ts=41.+i,received_ts=41.+i)
            if i%32==31:self.assertTrue(self.collector.drain(foreground=True))
        self.assertTrue(self.journal._prune_pending)
        self.source(20.,40.)
        release.set();worker.join(3.)
        self.assertTrue(self.journal.flush_pending_prune())
        self.assertFalse(self.journal._prune_pending)
        with self.assertRaisesRegex(ValueError,'retention'):self.source(20.,40.)

    def test_foreground_batch_is_bounded_and_per_kind_counts_are_explicit(self):
        for i in range(100):
            self.collector.event('sensor.a',dict(state=str(i)),event_ts=10.+i,received_ts=11.+i)
        self.assertTrue(self.collector.drain(foreground=True))
        self.assertEqual(len(self.collector.pending),36)
        self.collector.gap('websocket_disconnected')
        stats=self.collector.diagnostics()
        self.assertEqual(stats['by_kind']['event'],dict(queued=100,written=64,dropped=36,rejected=0))
        with self.store.conn() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM feature_observation_events WHERE entity_id='sensor.a'").fetchone()[0],64)

    def test_concurrent_gap_during_busy_retry_cannot_restore_stale_boundary(self):
        self.seed();self.collector.barrier(70.)
        def fail(*args):
            self.collector.gap('websocket_disconnected')
            raise sqlite3.OperationalError('database is locked')
        with patch.object(self.collector,'_write',side_effect=fail):
            self.assertFalse(self.collector.drain(foreground=True))
        self.assertEqual(self.collector.pending_gap,'websocket_disconnected')
        self.drain()
        with self.assertRaisesRegex(ValueError,'incomplete'):self.source(20.,70.)

    def test_committed_reserved_batch_is_not_also_counted_as_lost(self):
        self.collector.event('sensor.a',dict(state='1'),event_ts=10.,received_ts=11.)
        original=self.collector._write
        def write(*args):
            result=original(*args)
            self.collector.gap('queue_overflow')
            return result
        with patch.object(self.collector,'_write',write):
            self.assertTrue(self.collector.drain(foreground=True))
        self.assertEqual(self.collector.stats['written'],1)
        self.assertEqual(self.collector.stats['dropped'],0)
        self.assertEqual(self.collector.stats['committed_after_epoch_change'],1)
        self.assertEqual(self.collector.by_kind['event']['dropped'],0)
        self.assertEqual(self.collector.pending_gap,'queue_overflow')

    def test_busy_after_snapshot_metadata_commit_is_an_explicit_gap(self):
        ids,states=self.seed()
        self.collector.snapshot(states,received_ts=60.,requested_ts=59.,epoch=self.collector.epoch)
        self.collector.barrier(80.)
        original=self.collector._journal_rows
        def native(rows):
            if rows:raise sqlite3.OperationalError('database is locked')
            return original(rows)
        with patch.object(self.collector,'_journal_rows',native):
            self.assertFalse(self.collector.drain(foreground=True))
        self.assertEqual(self.collector.pending_gap,'write_failure')
        self.assertEqual(self.collector.stats['errors'],1)
        self.drain()
        self.source(20.,40.)
        with self.assertRaisesRegex(ValueError,'incomplete'):self.source(20.,80.)

    def test_metadata_only_turn_does_not_wait_for_unrelated_store_owner(self):
        held=threading.Event();release=threading.Event()
        def hold():
            with self.store.lock:held.set();release.wait(3.)
        worker=threading.Thread(target=hold);worker.start()
        self.addCleanup(worker.join,3.);self.addCleanup(release.set)
        self.assertTrue(held.wait(1.))
        self.collector._submit('notice','metadata turn')
        self.assertTrue(self.collector.drain(foreground=True))
        self.assertFalse(self.collector.pending)

    def test_batched_receipts_keep_original_payloads_and_one_insert(self):
        from storage_budget import BudgetConnection
        queries=[];original=BudgetConnection.execute
        def execute(c,sql,*args,**kw):
            if sql.startswith('INSERT OR IGNORE INTO universal_collection_decisions'):queries.append(sql)
            return original(c,sql,*args,**kw)
        for i in range(20):
            intent=SimpleNamespace(intent_id=f'batch-{i}',agent_id='a',created_at=20.+i,
                target_entity='light.a',target_property='power',desired_value=float(i%2),
                policy_version=12,model_revision='original',decision_source='policy')
            self.assertTrue(self.collector.decision(intent,dict(id='a'),dict(state='off'),required=()))
        with patch.object(BudgetConnection,'execute',execute):
            self.assertTrue(self.collector.drain(foreground=True))
        self.assertEqual(len(queries),1)
        with self.store.conn() as c:
            rows=c.execute('SELECT at,body FROM universal_collection_decisions ORDER BY at').fetchall()
        self.assertEqual([r[0] for r in rows],[20.+i for i in range(20)])
        self.assertEqual([json.loads(r[1])['intent']['desired_value'] for r in rows],[float(i%2) for i in range(20)])

    def test_threaded_foreground_writer_preserves_qualified_native_v2_v3_samples(self):
        from tools.universal_e2e_fixture import OriginalEvidenceScenario
        # Synthetic explicit declarations and original pre-predict preparation
        # come from the existing scenario. Only its durability barrier changes:
        # the producer waits for the actual collector thread, never writes SQL.
        # This is a paced correctness test, not saturated-home training yield.
        with OriginalEvidenceScenario(self.store) as scenario:
            collector=scenario.collector
            collector.idle_ready=lambda:False
            collector.start();self.assertTrue(collector.ready.wait(1.))
            def wait_for_writer():
                deadline=time.perf_counter()+5.
                while True:
                    with collector.cv:
                        pending=bool(collector.pending) or collector.pending_gap is not None
                    if not pending:return
                    if time.perf_counter()>deadline:raise AssertionError(collector.diagnostics())
                    time.sleep(.005)
            scenario.drain=wait_for_writer
            try:
                scenario.collect_events(8)
                source=scenario.source()
                with self.store.conn() as c:
                    c.execute('BEGIN');data=source.dataset(c)
                self.assertEqual(len(data.samples),8,data.rejected)
                self.assertFalse(data.rejected)
                self.assertEqual(collector.stats['dropped'],0)
                self.assertGreater(collector.stats['foreground_batches'],0)
                scenario.ha.assert_not_called()
            finally:self.assertTrue(collector.stop(5.))

    def test_retention_tracking_survives_restart_with_collector_disabled(self):
        self.seed()
        reopened=Store(self.store.path)
        journal=FeatureJournal(reopened,clock=lambda:200000.,retention_hours=0)
        journal.prune()
        with self.assertRaisesRegex(ValueError,'retention'):self.source()

    def test_enabled_websocket_registry_refresh_keeps_monotonic_ids(self):
        import engine as module
        from test_release_117_ha_websocket_id_order import FakeEngine,FakeWebSocket
        fake=FakeEngine();ws=FakeWebSocket();fake.universal_training_collector=self.collector
        fake.registry_worker=SimpleNamespace(submit=lambda callback,*args:callback(*args))
        ws.loop_messages.insert(-1,dict(id=5,type='event',event={'data':{}}))
        stream=module.HAEventStream(fake);fake.stream=stream
        counter=iter(range(0,1000,3))
        with patch.object(module,'ws_connect',return_value=ws),patch.object(module.time,'monotonic',side_effect=lambda:next(counter)):
            stream.run()
        ids=[row['id'] for row in ws.sent if 'id' in row]
        self.assertEqual(ids,sorted(set(ids)))
        self.assertEqual(len([r for r in ws.sent if str(r.get('type','')).startswith('config/')]),6)
        self.assertTrue(fake.confirmed_when_event,fake.ws_error)
        self.assertGreaterEqual(self.collector.epoch,1)
        self.drain()
        with self.assertRaisesRegex(ValueError,'incomplete'):self.source()

    def test_enabled_websocket_disconnect_reaches_collector(self):
        import engine as module
        from test_release_117_ha_websocket_id_order import FakeEngine,FakeWebSocket
        fake=FakeEngine();ws=FakeWebSocket();fake.universal_training_collector=self.collector
        fake.registry_worker=SimpleNamespace(submit=lambda callback,*args:callback(*args))
        ws.loop_messages.pop()
        stream=module.HAEventStream(fake);fake.stream=stream;original=ws.recv
        def recv(timeout=None):
            if timeout is not None and not ws.loop_messages:
                stream.stop_event.set();raise OSError('synthetic disconnect')
            return original(timeout)
        with patch.object(module,'ws_connect',return_value=ws),patch.object(ws,'recv',side_effect=recv):
            stream.run()
        self.assertFalse(self.collector.subscription_known)
        self.assertEqual(self.collector.pending_gap,'websocket_disconnected')
        self.assertFalse(fake.ws_connected)

    def test_malformed_websocket_frame_does_not_advance_coverage(self):
        import engine as module
        from test_release_117_ha_websocket_id_order import FakeEngine,FakeWebSocket
        fake=FakeEngine();ws=FakeWebSocket();fake.universal_training_collector=self.collector
        fake.registry_worker=SimpleNamespace(submit=lambda callback,*args:callback(*args))
        ws.loop_messages[-1]['event']['data'].pop('entity_id')
        stream=module.HAEventStream(fake);fake.stream=stream
        with patch.object(module,'ws_connect',return_value=ws):stream.run()
        self.assertEqual(self.collector.pending_gap,'invalid_state_event')

    def test_shadow_prediction_is_not_dispatch_ack_or_physical_execution(self):
        from provenance import ProvenanceJournal
        from universal_history_collector import native_decision_evidence
        journal=ProvenanceJournal(self.store,clock=lambda:30.)
        journal.record_decision(decision_id='shadow',created_time=30.,agent_id='a',model_desired=1.,chosen_action=1.)
        with self.store.conn() as c:result=native_decision_evidence(c,'shadow')
        self.assertFalse(result['dispatch_recorded']);self.assertFalse(result['state_ack_recorded'])
        self.assertIsNone(result['physical_execution_confirmed']);self.assertIsNone(result['ack_state_event'])

    def test_retention_housekeeping_is_bounded_and_removes_expired_metadata(self):
        self.seed();self.collector.housekeep(now=20*86400.)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM universal_collection_segments').fetchone()[0],0)
