"""Receipt correlation and SQL reductions must retain durable semantics."""
from concurrent.futures import Future
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import subprocess
import sys
import json
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import support
import agent_candidate_user_promotion as promotion
import agent_explore as explore
import engine as engine_module
from runtime_event_latency import EventReceipts
from storage import Store
from runtime_lock_timing import timed_lock


class PromotionReadTests(unittest.TestCase):
    def manager(self, stable=False):
        manager=SimpleNamespace(core=SimpleNamespace(_final_explicit_http_only=True),
            _comparison_summary=Mock(return_value={}), before_live_process=Mock(return_value='before'),
            after_live_process=Mock(return_value='after'), promote=Mock(),
            candidate_hot_active=lambda aid: True,
            _candidate_stable_blocked_shadow_installed=stable)
        promotion.install(manager)
        return manager

    def test_stable_adapter_skips_both_reads_and_keeps_callback_inputs(self):
        m=self.manager(stable=True);a={'id':'root'};states={}
        with patch.object(promotion,'_active_observation_row',side_effect=AssertionError('redundant SQL')):
            self.assertEqual(m.before_live_process(a,states),'before')
            self.assertEqual(m.after_live_process(a,states),'after')
        self.assertTrue(m._candidate_user_promotion_installed)

    def test_without_stable_adapter_blocked_gate_is_still_entered(self):
        m=self.manager();calls=[]
        @contextmanager
        def gate(*args,**kwargs):
            calls.append(kwargs['purpose']);yield
        row=dict(state='offline_blocked',offline_gate_json='{"passed":false}')
        with patch.object(promotion,'_active_observation_row',return_value=row) as query, \
             patch.object(promotion,'_temporary_offline_gate_pass',gate):
            m.before_live_process({'id':'root'},{});m.after_live_process({'id':'root'},{})
        self.assertEqual(query.call_count,2)
        self.assertEqual(calls,['observation','observation'])


class ExploreReadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'read.db')

    def test_missing_table_then_external_create_insert_update_and_drop_are_fresh(self):
        self.assertIsNone(explore._active_free_for_live(self.store,'root'))
        explore.ensure_explore_tables(self.store)
        with closing(sqlite3.connect(self.store.path)) as c, c:
            c.execute('''INSERT INTO agent_explore_sessions
                (session_id,root_agent_id,parent_generation_id,child_generation_id,live_owner_agent_id,
                 mode,status,created_ts,updated_ts) VALUES('s','root','p','c','root','free','collecting',1,1)''')
        self.assertEqual(explore._active_free_for_live(self.store,'root')['session_id'],'s')
        with closing(sqlite3.connect(self.store.path)) as c, c:
            c.execute("UPDATE agent_explore_sessions SET status='done'")
        self.assertIsNone(explore._active_free_for_live(self.store,'root'))
        with closing(sqlite3.connect(self.store.path)) as c, c:
            c.execute('DROP TABLE agent_explore_sessions')
        self.assertIsNone(explore._active_free_for_live(self.store,'root'))

    def test_one_statement_and_no_schema_probe_or_negative_cache(self):
        explore.ensure_explore_tables(self.store);sql=[]
        with self.store.connection_session() as c:
            c.set_trace_callback(sql.append)
            self.assertIsNone(explore._active_free_for_live(self.store,'root'))
            c.set_trace_callback(None)
        reads=[statement for statement in sql if statement.lstrip().upper().startswith('SELECT')]
        self.assertEqual(len(reads),1,reads)
        self.assertNotIn('sqlite_master',reads[0])

    def test_errors_other_than_absent_optional_table_are_not_hidden(self):
        with patch.object(self.store,'conn',side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaisesRegex(sqlite3.OperationalError,'locked'):
                explore._active_free_for_live(self.store,'root')

    def test_rolled_back_session_change_does_not_leave_cached_absence(self):
        explore.ensure_explore_tables(self.store)
        with self.store.connection_session() as c:
            c.execute('BEGIN')
            c.execute('''INSERT INTO agent_explore_sessions
                (session_id,root_agent_id,parent_generation_id,child_generation_id,live_owner_agent_id,
                 mode,status,created_ts,updated_ts) VALUES('s','root','p','c','root','free','collecting',1,1)''')
            c.rollback()
        self.assertIsNone(explore._active_free_for_live(self.store,'root'))


class ExploreInactiveTests(unittest.TestCase):
    def test_no_active_trial_neither_reads_session_nor_consumes_explore_budget(self):
        from test_agent_explore import AgentExploreTests
        fixture=AgentExploreTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        fixture.manager.workflow_explore(fixture.root['id'],dict(mode='free',config=dict(
            focus='presence',intensity=.2,interval=300,daily_budget=1,observation_seconds=10,max_step=1)))
        before=fixture.manager.workflow_explore_status(fixture.root['id'])['session']
        fixture.engine.experiments._get(fixture.root['id'])['active']=None
        with patch.object(explore,'_active_free_for_live',side_effect=AssertionError('inactive SQL')):
            fixture.engine.experiments._finish(fixture.root['id'],None,'policy changed')
        after=fixture.manager.workflow_explore_status(fixture.root['id'])['session']
        self.assertEqual(before,after)
        fixture.executor.service.assert_not_called()


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        from test_release_118_event_decision_hotpath import EventDecisionHotpath118Tests
        self.fixture=EventDecisionHotpath118Tests();self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.engine=self.fixture.engine

    def prepare(self):
        agent=self.fixture._qualified_shadow_agent()
        e=self.engine
        e.state_map={agent['target_entity']:support.state(agent['target_entity'],'off'),
                     'binary_sensor.motion':support.state('binary_sensor.motion','off')}
        e.context.configure(e.state_map);e._refresh_agent_index(force=True)
        return agent

    def test_ingress_receipt_does_not_leak_to_direct_event_without_timestamp(self):
        data=dict(entity_id='binary_sensor.motion',new_state=support.state('binary_sensor.motion','on'),
                  _hm_ws_received_perf=1.)
        with patch.object(engine_module.time,'perf_counter',return_value=2.), \
             patch.object(engine_module.TELEMETRY,'observe') as observe:
            self.engine.on_state_changed(data)
        self.assertEqual(self.engine.entity_ws_received_perf['binary_sensor.motion'],1.)
        observe.assert_any_call('ws_receive_to_engine',1000.)
        data.pop('_hm_ws_received_perf')
        data['new_state']=support.state('binary_sensor.motion','off')
        self.engine.on_state_changed(data)
        self.assertNotIn('binary_sensor.motion',self.engine.entity_ws_received_perf)

    def test_invalid_and_future_receipts_are_rejected(self):
        for stamp in (None,False,True,0.,-1.,float('nan'),float('inf'),3.,'1'):
            with patch.object(engine_module.time,'perf_counter',return_value=2.):
                self.engine.on_state_changed(dict(entity_id='binary_sensor.motion',
                    new_state=support.state('binary_sensor.motion','on'),_hm_ws_received_perf=stamp))
            self.assertNotIn('binary_sensor.motion',self.engine.entity_ws_received_perf)

    def test_optional_receipt_map_is_bounded_without_changing_legacy_clocks(self):
        e=self.engine;e.entity_ws_received_perf={f'sensor.old_{i}':1. for i in range(8192)}
        with patch.object(engine_module.time,'perf_counter',return_value=2.):
            e.on_state_changed(dict(entity_id='binary_sensor.motion',new_state=support.state(
                'binary_sensor.motion','on'),_hm_ws_received_perf=1.))
        self.assertEqual(len(e.entity_ws_received_perf),8192)
        self.assertNotIn('sensor.old_0',e.entity_ws_received_perf)
        self.assertEqual(e.entity_event_received_perf['binary_sensor.motion'],2.)

    def test_scheduler_correlates_clocks_to_dependencies_and_excludes_timers(self):
        self.prepare();e=self.engine
        e.entity_event_received_perf={'binary_sensor.motion':2.,'sensor.unrelated':3.}
        e.entity_ws_received_perf={'binary_sensor.motion':1.,'sensor.unrelated':.5}
        with patch.object(e.control_workers,'submit',return_value=Future()) as submit:
            e.process(e.state_map,{'binary_sensor.motion','sensor.unrelated'})
            receipts=submit.call_args.args[-1]
            self.assertEqual(dict(receipts),{'binary_sensor.motion':2.})
            self.assertEqual(receipts.websocket,{'binary_sensor.motion':1.})
            e.in_flight.clear()
            e.process(e.state_map,{'binary_sensor.motion'},event_driven=False)
            self.assertEqual(submit.call_args.args[-1],{})
            self.assertEqual(submit.call_args.args[-1].websocket,{})

    def test_worker_binding_is_local_and_cleared_even_after_agent_error(self):
        a=self.prepare();e=self.engine;seen=[]
        def fail(*args):
            seen.append(dict(e._inference_tls.websocket_received_perf))
            raise ValueError('optional fixture error')
        with patch.object(e,'process_agent',side_effect=fail):
            e.process_target([a],{'binary_sensor.motion'},event_received_perf=EventReceipts(
                {'binary_sensor.motion':2.},websocket={'binary_sensor.motion':1.}))
        self.assertEqual(seen,[{'binary_sensor.motion':1.}])
        self.assertFalse(hasattr(e._inference_tls,'websocket_received_perf'))

    def test_websocket_owns_timestamp_before_parse_and_never_trusts_payload_clock(self):
        from test_release_116_realtime_correct_runtime import FakeEngine, FakeWebSocket
        fake=FakeEngine()
        ws=FakeWebSocket([dict(id=2,type='event',event=dict(data=dict(
            entity_id='sensor.a',new_state={'state':'on'},_hm_ws_received_perf=-1000)))])
        stream=engine_module.HAEventStream(fake);fake.stream=stream;ws.stream=stream
        with patch.object(engine_module,'ws_connect',return_value=ws), \
             patch.object(engine_module.time,'perf_counter',return_value=7.):
            stream.run()
        self.assertEqual(fake.events[0]['_hm_ws_received_perf'],7.)

    def test_export_contains_existing_detailed_hot_path_snapshot(self):
        from runtime_debug_log import RuntimeDebugLogService
        self.engine.inference_hot_path_metrics.observe('fixture_stage',12.)
        service=RuntimeDebugLogService(SimpleNamespace(ENGINE=self.engine,STORE=self.fixture.store),SimpleNamespace())
        exported=service.export_payload()
        self.assertEqual(exported['inference_hot_path'],self.engine.inference_hot_path_metrics.snapshot())
        self.assertEqual(exported['notes']['receipt_clock_contract'],2)


class LockTimingTests(unittest.TestCase):
    def test_wait_is_separate_and_error_releases_lock_before_reporting(self):
        lock=threading.Lock();lock.acquire();waiting=threading.Event();observations=[]
        def observe(name,value):
            self.assertTrue(lock.acquire(blocking=False))
            lock.release();observations.append((name,value))
        def work():
            waiting.set()
            try:
                with timed_lock(lock,'fixture'):
                    raise ValueError('test release')
            except ValueError:pass
        with patch('runtime_lock_timing.TELEMETRY.observe',side_effect=observe):
            thread=threading.Thread(target=work);thread.start()
            self.assertTrue(waiting.wait(2));time.sleep(.03);lock.release();thread.join(3)
        self.assertFalse(thread.is_alive())
        values=dict(observations)
        self.assertGreater(values['fixture_lock_wait'],10.)
        self.assertLess(values['fixture_locked_work'],values['fixture_lock_wait'])


class FullRuntimeBenchmarkTests(unittest.TestCase):
    def test_real_candidate_ui_cadence_cache_expiry_and_inflight_coalescing(self):
        root=Path(__file__).resolve().parents[1]
        result=subprocess.run(['node',str(root/'tools/verify_candidate_live_ui.js')],
            cwd=root,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        row=json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(row['visible_cadence_ms'],100)
        self.assertTrue(row['shared_inflight'])
        self.assertTrue(row['first_card_fresh'])
        self.assertTrue(row['no_cards_no_requests'])
        self.assertTrue(row['hidden_no_requests'])

    def test_shipped_composition_real_root_candidate_and_debug_ui_work_together(self):
        root=Path(__file__).resolve().parents[1]
        result=subprocess.run([sys.executable,str(root/'tools/benchmark_full_runtime.py'),
            '--seconds','1','--warmup-seconds','1','--active','3','--configured','20',
            '--candidates','--ui','--trace'],cwd=root,capture_output=True,text=True,timeout=100)
        self.assertEqual(result.returncode,0,result.stdout[-4000:]+result.stderr[-4000:])
        row=json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(row['production_root']);self.assertFalse(row['prediction_override'])
        self.assertEqual(row['entities'],778);self.assertEqual(len(row['root_decisions']),3)
        self.assertEqual(row['pipeline']['registered_layers'],7)
        self.assertGreater(row['candidate_diagnostics']['processed'],0)
        self.assertEqual(row['candidate_diagnostics']['execution'],'existing_candidate_worker')
        self.assertEqual(row['candidate_diagnostics']['errors'],0)
        self.assertGreater(row['measured_window']['ws_receive_to_decision']['count'],0)
        self.assertEqual(row['service_calls'],0);self.assertGreater(row['ui_stats']['requests'],0)
        self.assertEqual(row['ui_stats']['errors'],0)
        sqlite_status = row['sqlite_at_input_stop']
        self.assertGreater(sqlite_status['storage_budget']['wal_max_bytes'],0)
        self.assertEqual(sqlite_status['wal_checkpoint']['mode'],'PASSIVE')
        self.assertEqual(sqlite_status['wal_checkpoint']['errors'],0)
        self.assertGreaterEqual(row['transport_pending_at_input_stop'],0)
        self.assertGreater(row['measured_window']['benchmark_emit_to_ws_receive']['count'],0)
        self.assertTrue(all(sample['transport_pending_messages']>=0 for sample in row['queue_series']))
        self.assertTrue(all(sample['wal_allocated_bytes']>=0 and sample['wal_remaining_frames']>=0
                            for sample in row['queue_series']))

    def test_control_fixture_dispatches_with_twenty_configured_agents_and_all_guards(self):
        root=Path(__file__).resolve().parents[1]
        result=subprocess.run([sys.executable,str(root/'tools/benchmark_full_runtime.py'),
            '--seconds','1','--warmup-seconds','1','--active','3','--configured','20',
            '--candidates','--mode','control','--training-fixture','legacy'],cwd=root,capture_output=True,text=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stdout[-4000:]+result.stderr[-4000:])
        row=json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(row['control_dispatch_exercised'],row['root_executor_results'])
        self.assertGreaterEqual(row['service_calls'],3)


class BulkDeviceMappingTests(unittest.TestCase):
    def setUp(self):
        from device_agents import DeviceAgentService
        from test_device_agents import _Engine
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'bulk.db')
        self.registry={f'sensor.{i}':dict(device_id='one' if i%7==0 else 'other') for i in range(778)}
        self.service=DeviceAgentService(_Engine(entities=self.registry),self.store)
        self.identity=dict(entity_id='sensor.0',device_id='one',logical_device_id='ha-device:one')

    def write(self,c,eid,logical):
        c.execute('''INSERT OR REPLACE INTO device_explicit_mappings
            (entity_id,logical_device_id,property_name,autonomy_enabled,updated_ts) VALUES(?,?,'power',0,1)''',
            (eid,logical))

    def reference(self):
        out=[]
        for eid,reg in self.registry.items():
            mapped=self.service.explicit_mapping(eid)
            if mapped and str(mapped['logical_device_id'])==self.identity['logical_device_id']:
                out.append(eid)
            elif not mapped and reg.get('device_id')==self.identity['device_id']:
                out.append(eid)
        return sorted(set(out+[self.identity['entity_id']]))

    def test_exact_legacy_parity_and_four_reads_for_778_entities(self):
        with self.store.conn() as c:
            self.write(c,'sensor.7','custom:split')
            self.write(c,'sensor.13','ha-device:one')
        expected=self.reference();sql=[]
        with self.store.connection_session() as c:
            c.set_trace_callback(sql.append)
            actual=self.service._entities_for_identity(self.identity)
            c.set_trace_callback(None)
        self.assertEqual(actual,expected)
        reads=[s for s in sql if s.startswith('SELECT entity_id,logical_device_id')]
        self.assertEqual(len(reads),4)
        self.assertNotIn('sensor.7',actual);self.assertIn('sensor.13',actual)

    def test_external_commit_and_rollback_cannot_leave_a_cached_resource_group(self):
        before=self.service._entities_for_identity(self.identity)
        with closing(sqlite3.connect(self.store.path)) as c:
            self.write(c,'sensor.7','custom:split');c.rollback()
        self.assertEqual(self.service._entities_for_identity(self.identity),before)
        with closing(sqlite3.connect(self.store.path)) as c,c:
            self.write(c,'sensor.7','custom:split')
        self.assertNotIn('sensor.7',self.service._entities_for_identity(self.identity))
        with closing(sqlite3.connect(self.store.path)) as c,c:
            c.execute("DELETE FROM device_explicit_mappings WHERE entity_id='sensor.7'")
        self.assertEqual(self.service._entities_for_identity(self.identity),before)

    def test_mapping_failure_is_not_treated_as_unmapped_permission(self):
        with patch.object(self.store,'conn',side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaises(sqlite3.OperationalError):
                self.service._entities_for_identity(self.identity)

    def test_no_registry_preserves_exact_entity_fallback(self):
        self.service.engine.context.entities={}
        self.assertEqual(self.service._entities_for_identity(self.identity),['sensor.0'])


if __name__=='__main__':unittest.main()
