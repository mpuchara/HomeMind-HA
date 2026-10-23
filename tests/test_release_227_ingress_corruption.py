"""Corrupt audit storage must not keep slowing HA/Shadow state delivery."""
import hashlib
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import support
import test_executor as executor_fixture
from provenance import ProvenanceJournal
from provenance_runtime import install
from runtime_debug_log import RuntimeDebugLogService
from sqlite_background import DeferredBatchError, is_sqlite_busy, is_sqlite_corruption
from storage import Store
from tools.check_database_health import inspect_copy


def corrupt_error():
    exc=sqlite3.DatabaseError('database disk image is malformed')
    exc.sqlite_errorcode=sqlite3.SQLITE_CORRUPT
    exc.sqlite_errorname='SQLITE_CORRUPT'
    return exc


class CorruptEventAuditTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.store=Store(Path(temp.name)/'audit.db')
        self.journal=ProvenanceJournal(self.store, clock=lambda:1000.)

    def record(self, index):
        return self.journal.record_event('sensor.pir',support.state('sensor.pir',str(index)),
            event_time=float(index),received_time=1000.+index,event_id='e'+str(index),origin='own_command')

    def test_corrupt_batch_is_retained_and_new_events_use_bounded_ram_without_sql(self):
        for i in range(3):self.record(i)
        with patch('provenance.insert_bounded_rows',side_effect=corrupt_error()):
            with self.assertRaises(sqlite3.DatabaseError):self.journal.flush_events_batch()
        self.assertEqual(self.journal.pending_event_count(),3)
        self.journal._event_cache_limit=32
        with patch.object(self.store,'conn',side_effect=AssertionError('No repeated SQL after corruption')):
            for i in range(3,1003):
                event_id,inserted=self.record(i)
                self.assertFalse(inserted)
                self.assertFalse(self.journal.event_processed(event_id))
                self.journal.mark_event_processed(event_id)
                self.assertTrue(self.journal.event_processed(event_id))
                self.assertEqual(self.journal.event(event_id)['origin'],'own_command')
            for _ in range(3):
                with self.assertRaises(sqlite3.DatabaseError) as failure:self.journal.flush_events_batch()
                self.assertEqual(failure.exception.sqlite_errorcode,sqlite3.SQLITE_CORRUPT)
            health=self.journal.event_audit_health()
        self.assertEqual(health['skipped_events'],1000)
        self.assertEqual(health['retained_events'],3)
        self.assertEqual(health['gap_start_ts'],1003.)
        self.assertEqual(health['gap_end_ts'],2002.)
        self.assertEqual(len(self.journal._event_cache),32)
        self.assertEqual(self.journal._pending_event_overflow_sync,0)
        self.assertEqual(health['fault']['reason'],'database_corrupt')
        self.assertEqual(self.record(1002),('e1002',False))
        self.assertEqual(self.journal.event_audit_health()['skipped_events'],1000)

    def test_corruption_detected_during_overflow_does_not_grow_failed_queue(self):
        self.journal._pending_event_limit=2
        self.record(0);self.record(1)
        with patch('provenance.insert_bounded_rows',side_effect=corrupt_error()):
            self.assertEqual(self.record(2),('e2',False))
        self.assertEqual(self.journal.pending_event_count(),2)
        self.assertEqual(self.journal.event('e2')['audit_gap_reason'],'database_corrupt')
        self.assertEqual(self.journal.event_audit_health()['skipped_events'],1)

    def test_busy_failure_remains_retryable_and_does_not_latch_corruption(self):
        self.record(0)
        with patch('provenance.insert_bounded_rows',side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaises(sqlite3.OperationalError):self.journal.flush_events_batch()
        self.assertFalse(self.journal.event_audit_health()['paused'])
        self.assertEqual(self.journal.flush_events_batch(),1)

    def test_poll_cache_only_miss_avoids_database_and_explicit_read_keeps_durable_lookup(self):
        self.record(0);self.journal.flush_events_batch()
        self.journal._event_cache.clear();self.journal._history_event_cache.clear()
        with patch.object(self.store,'conn',side_effect=AssertionError('Poll must not read audit history')):
            self.assertIsNone(self.journal.history_provenance('sensor.pir',0.,cache_only=True)['event_id'])
        self.assertEqual(self.journal.history_provenance('sensor.pir',0.)['origin'],'own_command')

    def test_optional_restart_cache_corruption_is_visible_without_weakening_command_cache(self):
        with patch.object(ProvenanceJournal,'_load_recent_event_cache',side_effect=corrupt_error()), \
             patch.object(ProvenanceJournal,'_load_active_command_cache') as commands:
            journal=ProvenanceJournal(self.store)
        self.assertTrue(journal.event_audit_health()['paused']);commands.assert_called_once()
        with patch.object(ProvenanceJournal,'_load_active_command_cache',side_effect=corrupt_error()):
            with self.assertRaises(sqlite3.DatabaseError):ProvenanceJournal(self.store)


class IngressIntegration227Tests(unittest.TestCase):
    def setUp(self):
        self.fixture=executor_fixture.ExecutorTests();self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        install(SimpleNamespace(STORE=self.fixture.store,ENGINE=self.fixture.e))
        # Deterministic explicit writer turns; production lifecycle is covered elsewhere.
        self.fixture.e.stop_event.set();self.fixture.e.provenance_writer_event.set()
        self.fixture.e.provenance_writer_thread.join(timeout=2.)

    def test_ha_event_and_poll_deliver_current_with_corrupt_audit_and_store_lock_held(self):
        f=self.fixture;journal=f.e.provenance
        journal._pending_event_limit=1
        journal.record_event('sensor.old',support.state('sensor.old','1'),event_time=1.,event_id='old')
        with patch('provenance.insert_bounded_rows',side_effect=corrupt_error()):
            with self.assertRaises(sqlite3.DatabaseError):journal.flush_events_batch()
        completed=threading.Event();errors=[]
        def deliver():
            try:
                with patch.object(f.store,'conn',side_effect=AssertionError('Ingress must not touch SQLite')):
                    f.e.on_state_changed({'entity_id':'light.kitchen','new_state':support.state('light.kitchen','on')})
                    f.e._queue_archive_state(support.state('sensor.poll','7'),force=True)
            except Exception as exc:errors.append(exc)
            finally:completed.set()
        with f.store.lock:
            worker=threading.Thread(target=deliver,daemon=True);worker.start()
            self.assertTrue(completed.wait(.5),'HA delivery waited for the Store writer')
        worker.join(timeout=1.)
        self.assertEqual(errors,[])
        self.assertEqual(f.e.state_map['light.kitchen']['state'],'on')
        self.assertTrue(any(row[0]=='sensor.poll' for row in f.e.pending_archive))
        f.service.assert_not_called()

    def test_combined_error_keeps_native_code_and_debug_status_reads_health_from_ram(self):
        f=self.fixture;journal=f.e.provenance
        journal.record_event('sensor.old',support.state('sensor.old','1'),event_time=1.,event_id='old')
        with patch('provenance.insert_bounded_rows',side_effect=corrupt_error()):
            with self.assertRaises(DeferredBatchError) as result:f.store._flush_all_provenance()
        self.assertTrue(is_sqlite_corruption(result.exception));self.assertFalse(is_sqlite_busy(result.exception))
        service=RuntimeDebugLogService(SimpleNamespace(STORE=f.store,ENGINE=f.e),SimpleNamespace())
        with patch.object(f.store,'conn',side_effect=AssertionError('Status must be RAM-only')):
            self.assertTrue(service.status()['event_audit_health']['paused'])

    def test_background_turn_skips_latched_corrupt_audit_but_explicit_flush_still_fails(self):
        f=self.fixture;journal=f.e.provenance
        journal.record_event('sensor.old',support.state('sensor.old','1'),event_time=1.,event_id='old')
        with patch('provenance.insert_bounded_rows',side_effect=corrupt_error()):
            with self.assertRaises(DeferredBatchError):f.store._flush_all_provenance()
        with patch.object(journal,'flush_events_batch',side_effect=AssertionError('retried corrupt INSERT')):
            self.assertEqual(f.store._flush_all_provenance(max_event_batches=4)['events'],0)
        self.assertEqual(journal.pending_event_count(),1)
        with self.assertRaises(DeferredBatchError):f.store._flush_all_provenance()


class HealthCheck227Tests(unittest.TestCase):
    def test_actual_delivery_regression_harness_finishes_before_writer_is_released(self):
        result=subprocess.run([__import__('sys').executable,'tools/verify_provenance_ingress.py'],
            cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=8.)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_offline_checker_reads_healthy_and_notadb_copies_without_modifying_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            healthy=Path(tmp)/'healthy.db';bad=Path(tmp)/'bad.db'
            c=sqlite3.connect(healthy);c.execute('CREATE TABLE sample(n)');c.commit();c.close()
            bad.write_bytes(b'not a database'*200)
            for path,expected in ((healthy,True),(bad,False)):
                before=hashlib.sha256(path.read_bytes()).digest()
                self.assertEqual(inspect_copy(path)['ok'],expected)
                self.assertEqual(inspect_copy(path,full=True)['ok'],expected)
                self.assertEqual(hashlib.sha256(path.read_bytes()).digest(),before)
                self.assertFalse(Path(str(path)+'-wal').exists())
            error=sqlite3.DatabaseError('native extended corruption')
            error.sqlite_errorcode=sqlite3.SQLITE_CORRUPT | (3<<8)
            self.assertTrue(is_sqlite_corruption(error))
            self.assertFalse(is_sqlite_corruption(sqlite3.OperationalError('database is locked')))

    @unittest.skipUnless(shutil.which('node'),'Node required')
    def test_diagnostics_renders_corruption_from_fast_status_without_claiming_repair(self):
        script="""
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const node={innerHTML:''};const c={window:{},document:{getElementById:id=>id==='runtimeDebugPanel'?node:null},console};
c.window=c;vm.runInNewContext(fs.readFileSync('adaptive_ai/src/static/runtime_debug_ui.js','utf8'),c);
c.renderRuntimeDebugDiagnostics({runtime_debug:{event_audit_health:{paused:true,retained_events:8192,skipped_events:33}}});
assert.ok(node.innerHTML.includes('SQLite reports database corruption'));
assert.ok(node.innerHTML.includes('8192 accepted events'));assert.ok(node.innerHTML.includes('33 new audit events'));
assert.ok(node.innerHTML.includes('will not repair it'));
"""
        result=subprocess.run(['node','-e',script],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=5.)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
