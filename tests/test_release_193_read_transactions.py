"""Read-only SQL scopes avoid page probes; all writers retain fresh admission."""
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import support
from storage import Store
from storage_budget import StoragePressure
import context_tournament_policy_candidate as candidates
import context_tournament_promotion as promotion
import test_context_tournament_observed_pool_runtime as pool_fixture


class ReadTransactions193Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / 'read.db')
        self.budget = self.store.storage_budget

    def test_reused_selects_do_not_probe_pages_and_see_external_commits(self):
        with self.store.connection_session() as db:
            with patch.object(self.budget, 'observe_pages', wraps=self.budget.observe_pages) as pages:
                for i in range(20):
                    with self.store.conn() as read:
                        self.assertIs(read, db)
                        self.assertEqual(read.execute('SELECT count(*) FROM app_meta').fetchone()[0], i)
                    with closing(sqlite3.connect(self.store.path)) as other, other:
                        other.execute('INSERT INTO app_meta VALUES(?,?)', (str(i), 'value'))
                self.assertEqual(pages.call_count, 0)
                with self.store.conn() as read:
                    self.assertEqual(read.execute('SELECT count(*) FROM app_meta').fetchone()[0], 20)
                self.assertFalse(db.in_transaction)

    def test_writer_samples_pages_before_admission_and_after_commit(self):
        with self.store.connection_session() as db:
            with patch.object(self.budget, 'observe_pages', wraps=self.budget.observe_pages) as pages:
                for i in range(2):
                    with self.store.conn() as c:
                        c.execute('INSERT INTO app_meta VALUES(?,?)', (str(i), 'value'))
                    self.assertEqual(pages.call_count, 2 * (i + 1))
                with self.store.conn() as c:
                    c.execute('SELECT count(*) FROM app_meta').fetchone()
                self.assertEqual(pages.call_count, 4)
                self.assertFalse(db.storage_pages_dirty)

    def test_ddl_and_commented_mutating_pragma_refresh_pages(self):
        with self.store.connection_session():
            with patch.object(self.budget, 'observe_pages', wraps=self.budget.observe_pages) as pages:
                with self.store.conn() as c:
                    c.execute('CREATE TABLE new_table(value TEXT)')
                self.assertEqual(pages.call_count, 2)
                with self.store.conn() as c:
                    c.execute('/* retention */ PRAGMA incremental_vacuum(2)')
                self.assertEqual(pages.call_count, 4)

    def test_cursor_bulk_and_explicit_rollback_do_not_leave_stale_page_flag(self):
        with self.store.connection_session() as db:
            with patch.object(self.budget, 'observe_pages', wraps=self.budget.observe_pages) as pages:
                with self.assertRaisesRegex(RuntimeError, 'cancelled'), self.store.conn() as c:
                    c.cursor().executemany('INSERT INTO app_meta VALUES(?,?)', [('a', 'x' * 8000), ('b', 'y')])
                    raise RuntimeError('cancelled')
                self.assertEqual(pages.call_count, 2)
                self.assertFalse(db.storage_pages_dirty)
                with self.store.conn() as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM app_meta').fetchone()[0], 0)

    def test_pressure_before_commit_still_rolls_back_after_read_scopes(self):
        with self.store.connection_session(), patch('storage_budget.time.monotonic', return_value=100.):
            with self.store.conn() as c:
                c.execute('SELECT count(*) FROM app_meta').fetchone()
            with self.assertRaises(StoragePressure), self.store.conn() as c:
                c.execute("INSERT INTO app_meta VALUES('guarded','value')")
                with patch('storage_budget.shutil.disk_usage', return_value=SimpleNamespace(free=self.budget.hard_reserve - 1)):
                    c.commit()
            with self.store.conn() as c:
                self.assertEqual(c.execute('SELECT count(*) FROM app_meta').fetchone()[0], 0)

    def test_first_writer_observes_pages_grown_by_another_connection(self):
        with self.store.connection_session():
            with closing(sqlite3.connect(self.store.path)) as other, other:
                other.execute("INSERT INTO app_meta VALUES('external',?)", ('z' * 200000,))
            captured = []
            original = self.budget.check
            def check(*args, **kwargs):
                captured.append(self.budget.snapshot()['database_used_bytes'])
                return original(*args, **kwargs)
            with patch.object(self.budget, 'check', check), self.store.conn() as c:
                expected = (c.execute('PRAGMA page_count').fetchone()[0] - c.execute('PRAGMA freelist_count').fetchone()[0]) * c.execute('PRAGMA page_size').fetchone()[0]
                c.execute("INSERT INTO app_meta VALUES('own','value')")
            self.assertEqual(captured[0], expected)

    def test_complete_observation_includes_promotion_reads_in_single_census(self):
        self.addCleanup(setattr, promotion, '_choose_schema_after_promotion', promotion._choose_schema_after_promotion)
        self.addCleanup(setattr, promotion, '_migrate_schema', promotion._migrate_schema)
        agent = self.store.create_agent(support.agent())
        engine = SimpleNamespace(models={}, state_map={}, entity_registry={}, context=None, temporal_history=None)
        service = pool_fixture.ObservedPoolRuntimeTests.FakeService(self.store, engine, {'schema_revision': 1})
        service._eligible_entities = lambda *a: set()
        service.observe_shadow = lambda *a, **kw: service.state(agent['id'])
        status = []
        def promotion_status(a):
            status.append(service.state(a['id']))
            service._state['schema_revision'] += 1
            return {'last_promotion_ts': None}
        service.promotion_status = promotion_status
        candidates.install_policy_candidates(service)
        with self.store.connection_session() as db:
            trace = []
            db.set_trace_callback(trace.append)
            result = service.observe_shadow(agent)
            db.set_trace_callback(None)
        self.assertEqual(sum('SELECT COUNT(*)' in sql for sql in trace), 1)
        self.assertEqual(result['schema_revision'], 2)
        self.assertEqual(status[-1]['schema_revision'], 2)
        self.assertEqual(service.state(agent['id'])['schema_revision'], 3)
        self.assertEqual(service.observation_pool_cache_snapshot()['reads'], 1)


if __name__ == '__main__':
    unittest.main()
