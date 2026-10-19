import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support
from confidence_contract import ProbabilityCalibrationJournal, _save_report_cache
from storage import Store
from storage_budget import StoragePressure


TABLE='confidence_probability_report_cache'
SQL='''INSERT INTO confidence_probability_report_cache
       (metric_id,model_key,scope_id,contract_version,revision,report_json,scanned_rows,updated_ts)
       VALUES(?,?,?,?,?,?,?,?)'''


class ConfidenceCache219Tests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store=Store(Path(temp.name)/'reports.db')
        self.journal=ProbabilityCalibrationJournal(self.store)

    def params(self, metric='test'):
        return (metric,'m','s',2,0,'{"fresh":true}',0,100.)

    def count(self):
        with self.store.conn() as connection:
            return connection.execute('SELECT COUNT(*) FROM confidence_probability_report_cache').fetchone()[0]

    def test_fresh_report_returns_while_another_thread_still_owns_store(self):
        held,ready,release=threading.Event(),threading.Event(),threading.Event()
        def writer():
            with self.store.lock:
                held.set();ready.set();release.wait(3);held.clear()
        thread=threading.Thread(target=writer);thread.start()
        self.assertTrue(ready.wait(3))
        try:
            report=self.journal.report('comfort','model','room')
            self.assertTrue(held.is_set(),'report waited for optional cache persistence')
            self.assertEqual(report['episodes'],0)
            self.assertFalse(report['sufficient_evidence'])
            self.assertEqual(self.count(),0)
            self.assertEqual(self.store.confidence_cache_write_snapshot()['lock_skips'],1)
        finally:
            release.set();thread.join(3)

    def test_external_sqlite_writer_is_skipped_and_connection_timeout_is_restored(self):
        with self.store.connection_session() as connection:
            before=connection.execute('PRAGMA busy_timeout').fetchone()[0]
            external=sqlite3.connect(self.store.path,isolation_level=None)
            try:
                external.execute('BEGIN IMMEDIATE')
                self.assertFalse(self.store.try_confidence_cache_write(TABLE,SQL,self.params()))
                self.assertFalse(connection.in_transaction)
                self.assertEqual(connection.execute('PRAGMA busy_timeout').fetchone()[0],before)
            finally:
                external.rollback();external.close()
            self.assertTrue(self.store.try_confidence_cache_write(TABLE,SQL,self.params()))
            self.assertEqual(connection.execute('PRAGMA busy_timeout').fetchone()[0],before)
            self.assertFalse(connection.in_transaction)
        self.assertEqual(self.count(),1)
        self.assertEqual(self.store.confidence_cache_write_snapshot()['busy_skips'],1)

    def test_nested_transaction_is_not_committed_or_reused_for_cache_writes(self):
        with self.store.connection_session() as connection:
            connection.execute(SQL,self.params('outer'))
            self.assertTrue(connection.in_transaction)
            self.assertFalse(self.store.try_confidence_cache_write(TABLE,SQL,self.params('inner')))
            self.assertTrue(connection.in_transaction)
            with sqlite3.connect(self.store.path) as external:
                self.assertEqual(external.execute('SELECT COUNT(*) FROM confidence_probability_report_cache').fetchone()[0],0)
            connection.rollback()
        self.assertEqual(self.count(),0)
        self.assertEqual(self.store.confidence_cache_write_snapshot()['transaction_skips'],1)

    def test_post_statement_pressure_rolls_back_cache_and_preserves_timeout(self):
        original=self.store.storage_budget.check
        with self.store.connection_session() as connection:
            before=connection.execute('PRAGMA busy_timeout').fetchone()[0]
            seen=[];connection.set_trace_callback(seen.append)
            def pressure(*args,**kwargs):
                if any(sql.startswith('INSERT INTO confidence_probability_report_cache') for sql in seen):
                    raise StoragePressure('test cache admission after INSERT')
                return original(*args,**kwargs)
            try:
                with patch.object(self.store.storage_budget,'check',side_effect=pressure):
                    self.assertFalse(self.store.try_confidence_cache_write(TABLE,SQL,self.params()))
            finally:
                connection.set_trace_callback(None)
            self.assertFalse(connection.in_transaction)
            self.assertEqual(connection.execute('PRAGMA busy_timeout').fetchone()[0],before)
        self.assertEqual(self.count(),0)
        self.assertEqual(self.store.confidence_cache_write_snapshot()['pressure_skips'],1)

    def test_storage_pressure_does_not_replace_a_fresh_report_by_an_error(self):
        original=self.store.try_confidence_cache_write
        def deny_cache_only(*args,**kwargs):
            with patch.object(self.store.storage_budget,'check',side_effect=StoragePressure('test cache quota')):
                return original(*args,**kwargs)
        with patch.object(self.store,'try_confidence_cache_write',side_effect=deny_cache_only):
            report=self.journal.report('comfort','model','room')
        self.assertEqual(report['episodes'],0)
        self.assertFalse(report['sufficient_evidence'])
        self.assertEqual(self.count(),0)
        self.assertEqual(self.store.confidence_cache_write_snapshot()['pressure_skips'],1)

    def test_wal_reclaim_defers_zero_timeout_cache_writer(self):
        self.store._wal_reclaiming.set()
        try:
            self.assertFalse(self.store.try_confidence_cache_write(TABLE,SQL,self.params()))
        finally:
            self.store._wal_reclaiming.clear()
        self.assertEqual(self.count(),0)
        self.assertEqual(self.store.confidence_cache_write_snapshot()['busy_skips'],1)
        self.assertTrue(self.store.try_confidence_cache_write(TABLE,SQL,self.params()))

    def test_only_derived_cache_tables_are_optional_and_sql_errors_propagate(self):
        for table in ('agents','confidence_evaluation_epochs','provenance_commands'):
            with self.assertRaises(ValueError):
                self.store.try_confidence_cache_write(table,f'INSERT INTO {table}(id) VALUES(?)',('x',))
        with self.assertRaises(ValueError):
            self.store.try_confidence_cache_write(TABLE,'INSERT INTO agents(id) VALUES(?)',('x',))
        with self.assertRaises(sqlite3.OperationalError):
            self.store.try_confidence_cache_write(TABLE,'INSERT INTO confidence_probability_report_cache(missing_column) VALUES(?)',('x',))
        self.assertTrue(self.store.try_confidence_cache_write(TABLE,SQL,self.params()))

    def test_standalone_providers_keep_original_cache_persistence(self):
        provider=SimpleNamespace(lock=self.store.lock,conn=self.store.conn)
        self.assertTrue(_save_report_cache(provider,TABLE,SQL,self.params()))
        self.assertEqual(self.count(),1)

    def test_successful_report_cache_reuse_keeps_independent_return_objects(self):
        first=self.journal.report('comfort','model','room')
        first['sufficient_evidence']=True
        second=self.journal.report('comfort','model','room')
        self.assertFalse(second['sufficient_evidence'])
        self.assertEqual(second['episodes'],0)
        self.assertEqual(self.store.confidence_cache_write_snapshot()['written'],1)


if __name__=='__main__':unittest.main()
