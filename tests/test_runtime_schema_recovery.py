"""Fresh Shadow schema checks avoid writer contention without a stale cache."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import support
from storage import Store
from storage_budget import BudgetConnection
from tiny_mlp_shadow import ensure_tables


class ShadowSchemaTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / 'schema.db')
        ensure_tables(self.store)

    def test_ready_schema_uses_read_without_global_lock_or_ddl(self):
        queries = []
        original = BudgetConnection.execute
        def execute(conn, sql, *args, **kw):
            queries.append(sql)
            return original(conn, sql, *args, **kw)
        with patch.object(self.store, 'lock') as lock, patch.object(BudgetConnection, 'execute', execute), patch.object(
                BudgetConnection, 'executescript', side_effect=AssertionError('no-op DDL')):
            ensure_tables(self.store)
        lock.__enter__.assert_not_called()
        self.assertTrue(any('FROM tiny_mlp_shadow_models LIMIT 0' in q for q in queries))

    def test_external_column_loss_is_repaired_and_model_bytes_preserved(self):
        with self.store.conn() as c:
            c.execute("INSERT INTO tiny_mlp_shadow_models VALUES('a','tiny_mlp',1,'s','m','{\"exact\":true}',NULL,NULL,NULL,NULL,'diagonal_linucb',10,20)")
            c.execute('ALTER TABLE tiny_mlp_shadow_models DROP COLUMN training_json')
        ensure_tables(self.store)
        with self.store.conn() as c:
            row = c.execute('SELECT model_json,created_ts,updated_ts,training_json FROM tiny_mlp_shadow_models').fetchone()
        self.assertEqual(tuple(row), ('{\"exact\":true}',10.,20.,None))
        ensure_tables(Store(self.store.path))

    def test_external_table_loss_and_concurrent_initialization(self):
        with self.store.conn() as c:
            c.execute('DROP TABLE tiny_mlp_shadow_models')
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: ensure_tables(self.store), range(24)))
        ensure_tables(self.store)
        with self.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM tiny_mlp_shadow_models').fetchone()[0],0)

    def test_non_schema_failures_are_not_treated_as_migrations(self):
        with patch.object(self.store,'conn',side_effect=sqlite3.OperationalError('database is locked')), patch.object(self.store,'lock') as lock:
            with self.assertRaisesRegex(sqlite3.OperationalError,'locked'):
                ensure_tables(self.store)
        lock.__enter__.assert_not_called()

    def test_retained_inference_connection_revalidates_external_ddl(self):
        self.store.start_runtime_cache()
        self.addCleanup(self.store.stop_runtime_cache)
        with self.store.inference_connection_session():ensure_tables(self.store)
        with sqlite3.connect(self.store.path) as c:
            c.execute('ALTER TABLE tiny_mlp_shadow_models DROP COLUMN training_json')
        with self.store.inference_connection_session():
            ensure_tables(self.store)
            with self.store.conn() as c:
                c.execute('SELECT training_json FROM tiny_mlp_shadow_models')

    def test_replaced_database_with_same_schema_version_is_checked_fresh(self):
        other = Store(Path(self.store.path).with_name('other.db'))
        self.store.path = other.path
        ensure_tables(self.store)
        with other.conn() as c:
            c.execute('SELECT training_json FROM tiny_mlp_shadow_models')
