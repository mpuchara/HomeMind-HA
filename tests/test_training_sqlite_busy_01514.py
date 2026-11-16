"""SQLite contention retries are bounded, cancellable and never corruption retries."""
import sqlite3
import threading
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support
import training_process as process
import training_generation as generation
from storage_budget import StoragePressure


class SQLiteBusyTests(unittest.TestCase):
    def test_worker_classification_uses_native_sqlite_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'real.db'
            with sqlite3.connect(path) as a, sqlite3.connect(path,timeout=0) as b:
                a.execute('CREATE TABLE test(id INTEGER)')
                a.execute('BEGIN IMMEDIATE')
                with self.assertRaises(sqlite3.OperationalError) as failure:
                    b.execute('BEGIN IMMEDIATE')
                result=process.worker_sqlite_error(failure.exception)
                self.assertTrue(result['sqlite_busy'])
                self.assertEqual(result['sqlite_errorcode']&255,sqlite3.SQLITE_BUSY)
        self.assertFalse(process.worker_sqlite_error(sqlite3.DatabaseError('database disk image is malformed'))['sqlite_busy'])

    def test_preparation_does_not_retry_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'source.db'
            with patch.object(generation.Preparation,'run',side_effect=sqlite3.DatabaseError('database disk image is malformed')) as run:
                with self.assertRaises(sqlite3.DatabaseError):
                    generation.prepare_generation(path,Path(tmp)/'dest.db',[],0,1)
                self.assertEqual(run.call_count,1)

    def test_supervisor_retries_same_unpublished_job_without_reimport(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            marker=root/'resume.json'
            fake=SimpleNamespace(path=str(root/'source.db'),event=lambda *a,**k:None)
            history=SimpleNamespace(stop_event=threading.Event(),job_cancel_event=threading.Event(),training_process_status={})
            job=dict(agent_id='a',job_id='id',training_generation=True)
            with patch('storage.STORE',fake), patch.object(process,'_resume_path',return_value=marker), patch.object(process,'_run_isolated_training_attempt',side_effect=[process.WorkerSQLiteBusy('locked'),42]) as run:
                self.assertEqual(process._supervise_generation(history,0,100,job,root/'job.json'),42)
            self.assertEqual(run.call_count,2)
            self.assertEqual(run.call_args_list[0],run.call_args_list[1])
            self.assertFalse(marker.exists())

    def test_cancel_during_retry_does_not_start_another_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            marker=root/'resume.json'
            cancel=threading.Event()
            history=SimpleNamespace(stop_event=threading.Event(),job_cancel_event=cancel,training_process_status={})
            fake=SimpleNamespace(path=str(root/'source.db'),event=lambda *a,**k:cancel.set())
            job=dict(agent_id='a',job_id='id',training_generation=True)
            with patch('storage.STORE',fake), patch.object(process,'_resume_path',return_value=marker), patch.object(process,'_run_isolated_training_attempt',side_effect=process.WorkerSQLiteBusy('locked')) as run:
                with self.assertRaises(InterruptedError):
                    process._supervise_generation(history,0,100,job,root/'job.json')
            self.assertEqual(run.call_count,1)
            self.assertFalse(marker.exists())

if __name__=='__main__':
    unittest.main()
