"""Real WAL/read-mark and durable-prefix regressions, not mocked SQLite errors."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import support
import test_training_generation_0157 as selective_fixture
from training_generation import Preparation
from training_snapshot import capture, validate
from storage_budget import StoragePressure
from wal_checkpoint import WALCheckpointWorker


class TrainingWALTests(unittest.TestCase):
    def setUp(self):
        self.fixture=selective_fixture.SelectivePreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store=self.fixture.store

    def test_01_real_wal_reader_starvation_control_and_released_batches(self):
        store=self.store
        store.start_wal_keeper()
        self.addCleanup(store.stop_wal_keeper)
        worker=WALCheckpointWorker(store)
        worker.quiesce_seconds=0
        worker.RECLAIM_THRESHOLD_BYTES=1
        worker.RECLAIM_INTERVAL_SECONDS=0
        worker.RECLAIM_PRESSURE_INTERVAL_SECONDS=0
        reports={}
        for variant in ('long_reader','bounded_copy'):
            samples=[]
            with closing(sqlite3.connect(store.path,isolation_level=None)) as writer, \
                 closing(sqlite3.connect(store.path,isolation_level=None)) as maintenance, \
                 closing(sqlite3.connect(store.path,isolation_level=None)) as reader:
                writer.execute('PRAGMA wal_autocheckpoint=0')
                maintenance.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                start=time.monotonic()
                if variant=='long_reader':
                    reader.execute('BEGIN')
                    reader.execute('SELECT * FROM agents').fetchall()
                def step():
                    if len(samples)>=64:
                        return
                    before=time.perf_counter()
                    writer.execute("INSERT INTO events(created_at,level,kind,message) VALUES('WAL fixture','info','ingress',?)",('x'*16384,))
                    write_ms=(time.perf_counter()-before)*1000
                    result=worker.checkpoint(maintenance)
                    path=Path(store.path+'-wal')
                    size=path.stat().st_size if path.exists() else 0
                    samples.append(dict(wal_bytes=size,write_ms=write_ms,**result))
                    worker.reclaim(maintenance,result)
                if variant=='long_reader':
                    for _ in range(64): step()
                else:
                    original=Preparation.checkpoint
                    def checkpoint(preparation):
                        original(preparation)
                        if not preparation.bootstrap and preparation.state.get('bootstrap_complete'):
                            self.assertFalse(preparation.src.in_transaction)
                            step()
                    with patch.object(Preparation,'checkpoint',checkpoint):
                        report=self.fixture.prepare()
                    self.assertEqual(report['status'],'READY')
                    while len(samples)<64: step()
                self.assertEqual(len(samples),64)
                reader.rollback()
                final=worker.checkpoint(maintenance)
                worker.reclaim(maintenance,final)
                reports[variant]=dict(samples=samples,wal_peak_bytes=max(x['wal_bytes'] for x in samples),
                    wal_after_bytes=Path(store.path+'-wal').stat().st_size,
                    held_snapshot_seconds=time.monotonic()-start if variant=='long_reader' else report['snapshot_lifecycle']['max_explicit_transaction_seconds'],
                    snapshot_lifecycle=report['snapshot_lifecycle'] if variant=='bounded_copy' else None)
        self.assertGreater(reports['long_reader']['samples'][-1]['remaining_frames'],0)
        self.assertTrue(all(x['remaining_frames']==0 for x in reports['bounded_copy']['samples']))
        self.assertLess(reports['bounded_copy']['wal_peak_bytes'],reports['long_reader']['wal_peak_bytes']/4)
        self.assertEqual(reports['bounded_copy']['wal_after_bytes'],0)
        self.wal_report=reports
        self.__class__.last_wal_report=reports

    def test_02_actual_soft_wal_pressure_releases_and_resumes_same_copy(self):
        store=self.store
        store.start_wal_keeper()
        self.addCleanup(store.stop_wal_keeper)
        # Test-only small budget makes a real threshold crossing inexpensive.
        # Production option minima and defaults are unchanged.
        store.storage_budget.wal_limit=256*1024
        stopped=[]
        def progress(**fields):
            if fields.get('preparation',{}).get('table_name')=='entity_history' and not stopped:
                stopped.append(True)
                with closing(sqlite3.connect(store.path)) as writer:
                    writer.execute('PRAGMA wal_autocheckpoint=0')
                    for _ in range(12):
                        writer.execute("INSERT INTO events(created_at,level,kind,message) VALUES('fixture','info','ingress',?)",('y'*16384,))
                    writer.commit()
        with patch('storage_files.worker_storage_budget',return_value=store.storage_budget):
            with self.assertRaises(StoragePressure) as raised:
                self.fixture.prepare(progress=progress)
            self.assertEqual(raised.exception.reason,'wal_budget')
            # No connection left by failed preparation can pin the read mark.
            with closing(sqlite3.connect(store.path,timeout=0)) as maintenance:
                busy,_,_=maintenance.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                self.assertEqual(busy,0)
            self.assertEqual(store.storage_budget.sample()['level'],'normal')
            report=self.fixture.prepare()
        self.assertTrue(report['resumed'])
        self.assertEqual(report['status'],'READY')

    def test_03_live_appends_do_not_leak_but_prefix_changes_fail_closed(self):
        inserted=[]
        def progress(**fields):
            if not inserted and fields.get('preparation',{}).get('table_name')=='entity_history':
                inserted.append(True)
                self.store.archive_batch([('switch.target',76500.,'on',{},None,'ha_history_full')])
        report=self.fixture.prepare(progress=progress)
        with sqlite3.connect(self.fixture.fixture.destination) as conn:
            self.assertIsNone(conn.execute('SELECT id FROM entity_history WHERE ts=76500').fetchone())
        boundary=report['snapshot_lifecycle']['boundary']
        with self.store.conn() as conn:
            conn.execute("UPDATE entity_history SET state='changed' WHERE id=1")
        with self.store.conn() as conn, self.assertRaisesRegex(RuntimeError,'prefix changed'):
            validate(conn,boundary)

    def test_04_resume_rejects_mutated_prefix_after_worker_exit(self):
        stop=[]
        def progress(**fields):
            if fields.get('preparation',{}).get('table_name')=='entity_history': stop.append(True)
        with self.assertRaises(InterruptedError):
            self.fixture.prepare(progress=progress,cancelled=lambda:bool(stop))
        with self.store.conn() as conn:
            conn.execute('DELETE FROM entity_history WHERE id=1')
        with self.assertRaisesRegex(RuntimeError,'prefix changed'):
            self.fixture.prepare()
        self.assertFalse(self.fixture.fixture.destination.exists())

    def test_05_storage_admission_distinguishes_isolated_work_and_writes(self):
        budget=self.store.storage_budget
        budget.sample()
        with budget.lock:
            budget.stats.update(level='paused',reason='database_budget')
        self.assertEqual(budget.check_operation('source_read',refresh=False),'normal')
        self.assertEqual(budget.check_operation('generation_write',refresh=False),'normal')
        with budget.lock: budget.stats.update(reason='wal_budget')
        self.assertEqual(budget.check_operation('source_read',refresh=False),'paused')

    def test_06_supervisor_retries_same_descriptor_after_real_wal_maintenance(self):
        import storage
        import training_process as process
        store=self.store
        store.start_wal_keeper()
        self.addCleanup(store.stop_wal_keeper)
        store.storage_budget.wal_limit=256*1024
        worker=WALCheckpointWorker(store)
        worker.POLL_SECONDS=.02
        worker.quiesce_seconds=0
        store._wal_checkpoint_worker=worker
        self.addCleanup(worker.stop)
        history=SimpleNamespace(stop_event=__import__('threading').Event(),job_cancel_event=None,
                                training_process_status={})
        job=dict(agent_id=self.fixture.agent['id'],job_id='wal-resume',generation_rebuild=True)
        calls=[]
        def attempt(history,start,end,descriptor,path):
            calls.append((descriptor['job_id'],start,end,descriptor['generation_rebuild']))
            def progress(**fields):
                if len(calls)==1 and fields.get('preparation',{}).get('table_name')=='entity_history':
                    with closing(sqlite3.connect(store.path)) as writer:
                        writer.execute('PRAGMA wal_autocheckpoint=0')
                        writer.execute("INSERT INTO events(created_at,level,kind,message) VALUES('fixture','info','ingress',?)",('z'*200000,))
                        writer.commit()
            try:
                result=self.fixture.prepare(progress=progress)
            except StoragePressure:
                worker.start()
                raise
            self.assertTrue(result['resumed'])
            return 42
        with patch.object(storage,'STORE',store), patch.object(process,'_job_dir',return_value=self.fixture.fixture.root), \
             patch('storage_files.worker_storage_budget',return_value=store.storage_budget), \
             patch.object(process,'_run_isolated_training_attempt',side_effect=attempt):
            self.assertEqual(process._supervise_generation(history,900,77000,job,self.fixture.fixture.root/'wal.job.json'),42)
            self.assertFalse(process._resume_path(job['agent_id']).exists())
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[0],calls[1])
        self.assertGreater(worker.snapshot()['recovery_requests'],0)

    def test_07_durable_wait_descriptor_is_recovered_without_rebuild(self):
        import storage
        import training_process as process
        store=self.store
        agent=self.fixture.agent
        job=dict(agent_id=agent['id'],job_id='persisted',start_ts=900,end_ts=77000,
                 app_version=process.APP_VERSION,agent_fingerprint=process.agent_config_fingerprint(agent))
        with patch.object(storage,'STORE',store),patch.object(process,'_job_dir',return_value=self.fixture.fixture.root), \
             patch('storage_files.worker_storage_budget',return_value=store.storage_budget):
            marker=process._resume_path(agent['id'])
            process._atomic_json(marker,dict(source=str(Path(store.path).resolve()),job=job,
                job_path=str(self.fixture.fixture.root/'persisted.job.json'),state='waiting_wal',attempts=2))
            self.assertEqual(list(process.pending_training_agents(store)),[agent['id']])
            with patch.object(process,'_supervise_generation',return_value=42) as supervise:
                self.assertTrue(process.resume_deferred_training(SimpleNamespace(),agent['id']))
                self.assertEqual(supervise.call_args.args[3]['job_id'],'persisted')
                self.assertEqual(supervise.call_args.args[-1]['attempts'],2)

    def test_08_killed_waiting_supervisor_recovers_durable_job(self):
        import storage
        import training_process as process
        store=self.store
        store.start_wal_keeper()
        self.addCleanup(store.stop_wal_keeper)
        store.save_model(self.fixture.agent['id'],dict(marker='approved-before-kill'))
        previous=store.get_model(self.fixture.agent['id'])
        with closing(sqlite3.connect(store.path)) as writer:
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute("INSERT INTO events(created_at,level,kind,message) VALUES('fixture','info','ingress',?)",('k'*200000,))
            writer.commit()
        root=self.fixture.fixture.root
        code='''
import sys,threading
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
import storage,training_process as process
from training_generation import prepare_generation
store=storage.Store(sys.argv[2]); storage.STORE=store
store.storage_budget.wal_limit=256*1024
root=Path(sys.argv[3]); process._job_dir=lambda:root
agent=store.get_agent_config(sys.argv[4])
job=dict(agent_id=agent['id'],job_id='kill-wait',start_ts=900,end_ts=77000,
 app_version=process.APP_VERSION,agent_fingerprint=process.agent_config_fingerprint(agent))
history=SimpleNamespace(stop_event=threading.Event(),job_cancel_event=None,training_process_status={})
process._run_isolated_training_attempt=lambda *args:prepare_generation(store.path,root/'killed.generation.db',
 ['switch.target','binary_sensor.motion'],900,77000,agent_id=agent['id'])
process._supervise_generation(history,900,77000,job,root/'kill-wait.job.json')
'''
        with (root/'killed-supervisor.log').open('w') as log:
            child=subprocess.Popen([sys.executable,'-c',code,str(Path(process.__file__).parent),
                store.path,str(root),self.fixture.agent['id']],stdout=log,stderr=subprocess.STDOUT)
            try:
                marker=root/('resume-'+__import__('hashlib').sha256(self.fixture.agent['id'].encode()).hexdigest()[:24]+'.json')
                deadline=time.monotonic()+15
                while time.monotonic()<deadline and child.poll() is None:
                    try:
                        if json.loads(marker.read_text())['state']=='waiting_wal': break
                    except (OSError,ValueError,KeyError): pass
                    time.sleep(.01)
                self.assertTrue(marker.exists(),(root/'killed-supervisor.log').read_text())
                self.assertEqual(json.loads(marker.read_text())['state'],'waiting_wal')
            finally:
                if child.poll() is None: child.kill()
                child.wait(timeout=15)
        with closing(sqlite3.connect(store.path,timeout=0)) as maintenance:
            self.assertEqual(maintenance.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0],0)
        history=SimpleNamespace(stop_event=__import__('threading').Event(),job_cancel_event=None,training_process_status={})
        def resumed_attempt(*args):
            return 42 if self.fixture.prepare()['status']=='READY' else 0
        with patch.object(storage,'STORE',store),patch.object(process,'_job_dir',return_value=root), \
             patch.object(process,'_run_isolated_training_attempt',side_effect=resumed_attempt):
            self.assertEqual(list(process.pending_training_agents(store)),[self.fixture.agent['id']])
            self.assertTrue(process.resume_deferred_training(history,self.fixture.agent['id']))
        self.assertEqual(store.get_model(self.fixture.agent['id']),previous)


if __name__=='__main__':
    unittest.main()
