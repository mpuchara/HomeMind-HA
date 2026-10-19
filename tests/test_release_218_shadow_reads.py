import sqlite3
import math
import random
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
import agent_candidate_shadow_runtime as shadow
import agent_workflow_actions as workflow
from agent_explore import _session_for_child
from context_row_buffer import ContextRowBuffer
from storage import Store
from teaching import Teaching
from observation_contract import _maximum_protection_segments
from storage_budget import BudgetConnection, StoragePressure


class ShadowReads218Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name)/'reads.db')

    def test_benchmark_waits_for_runtime_producer_before_temporary_database_cleanup(self):
        import importlib.util
        path = Path(__file__).resolve().parents[1]/'tools/benchmark_full_runtime.py'
        spec = importlib.util.spec_from_file_location('full_runtime_cleanup218', path)
        benchmark = importlib.util.module_from_spec(spec);spec.loader.exec_module(benchmark)
        stopped = threading.Event()
        producer = threading.Thread(target=lambda: (time.sleep(.02), stopped.set()))
        producer.start()
        benchmark.join_runtime_producers(SimpleNamespace(ENGINE=producer))
        self.assertTrue(stopped.is_set())
        self.assertFalse(producer.is_alive())
        alive = threading.Event();release = threading.Event()
        producer = threading.Thread(target=lambda: (alive.set(), release.wait()))
        producer.start();self.assertTrue(alive.wait(1))
        try:
            with self.assertRaisesRegex(AssertionError, 'ENGINE did not stop'):
                benchmark.join_runtime_producers(SimpleNamespace(ENGINE=producer), timeout=.01)
        finally:
            release.set();producer.join(1)

    def preference_manager(self, complete=True):
        from agent_candidate_preference_metrics import install
        observed = dict(comparison_metric='observed_ui_snapshot', promotion_gates={},
                        promotion_vetoes=[], promotable=True) if complete else dict(promotable=True)
        fresh = dict(comparison_metric='fresh_status', promotion_gates={}, promotion_vetoes=['fresh veto'], promotable=False)
        result = dict(parent_agent_id='root', candidate_id='child', comparison=observed, state='comparing')
        summary = Mock(side_effect=lambda *args: dict(fresh))
        store = SimpleNamespace(get_agent_config=Mock(return_value=dict(
            target_entity='climate.test',target_property='temperature')))
        manager = SimpleNamespace(core=SimpleNamespace(_final_explicit_http_only=True), store=store,
            _comparison_summary=summary, _candidate_row=Mock(return_value=dict(parent_agent_id='root',candidate_id='child')),
            status=lambda _: dict(result), list_status=lambda: [dict(result)])
        return install(manager), summary

    def test_candidate_list_reuses_its_already_computed_card_summary_without_lifecycle_reads(self):
        manager, compute = self.preference_manager()
        row = manager.list_status()[0]
        self.assertEqual(row['comparison']['comparison_metric'], 'observed_ui_snapshot')
        compute.assert_not_called()
        manager._candidate_row.assert_not_called()
        manager.store.get_agent_config.assert_not_called()

    def test_single_candidate_status_still_recomputes_fresh_vetoes(self):
        manager, compute = self.preference_manager()
        row = manager.status('root')
        self.assertFalse(row['promotable'])
        self.assertEqual(row['comparison']['comparison_metric'], 'fresh_status')
        compute.assert_called_once()
        manager._candidate_row.assert_called_once_with('root')

    def test_incomplete_custom_list_summary_keeps_the_original_fresh_path(self):
        manager, compute = self.preference_manager(complete=False)
        row = manager.list_status()[0]
        self.assertFalse(row['promotable'])
        compute.assert_called_once()

    def test_decision_history_bulk_failure_is_atomic_and_restores_every_observed_row(self):
        teaching = Teaching(self.store)
        base = int(time.time())-1000
        for index in range(600):teaching.record('a',index%3,index%2,base+index)
        retained = list(teaching.buffer)
        self.assertEqual(len(retained), 600)
        with self.store.connection_session() as c:
            c.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,999)
            c.execute(f"CREATE TRIGGER fail_history BEFORE INSERT ON decision_history WHEN NEW.ts={base+333} "
                      "BEGIN SELECT RAISE(ABORT,'history batch'); END")
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'history batch'):
                teaching.flush()
            self.assertEqual(list(teaching.buffer),retained)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM decision_history').fetchone()[0],0)
            c.execute('DROP TRIGGER fail_history')
            self.assertEqual(teaching.flush(),600)
            self.assertEqual([tuple(row) for row in c.execute('SELECT * FROM decision_history ORDER BY ts')],retained)

    def overlay(self, teaching):
        engine = SimpleNamespace(teaching=teaching, models={'child': object()}, temporal_history=None)
        manager = SimpleNamespace(engine=engine, store=self.store)
        original = self.enterContext(patch.object(shadow, '_predict_candidate', return_value={'desired': 1.}))
        self.enterContext(patch.object(shadow, '_workflow_teaching_overlay_installed', False, create=True))
        workflow._install_candidate_teaching_overlay(manager)
        return lambda: shadow._predict_candidate(manager, {'agent_id':'child','generation_id':'g','root_agent_id':'r'}, {}, 20.)

    def test_empty_standard_label_cache_avoids_agent_sql_but_invalidation_restores_fresh_read(self):
        teaching = Teaching(self.store)
        teaching.labels('child')  # Standard matcher's own authoritative empty cache.
        observe = self.overlay(teaching)
        with patch.object(self.store, 'get_agent_config', side_effect=AssertionError('redundant read')):
            self.assertEqual(observe()['desired'], 1.)
        with self.store.conn() as c:
            c.execute("INSERT INTO teaching_labels(agent_id,created_ts,sample_ts,desired,fingerprint,signature_json,source) "
                      "VALUES('child',1,1,0,'different','{}','test')")
        with teaching.lock:
            teaching.cache.pop('child')  # Same invalidation used by teach/undo.
        with patch.object(self.store, 'get_agent_config', return_value={'id':'child'}) as get:
            self.assertEqual(observe()['desired'], 1.)
            get.assert_called_once_with('child')

    def test_custom_matcher_is_not_bypassed_by_an_empty_label_hint(self):
        teaching = SimpleNamespace(labels=Mock(return_value=[]), match=Mock(return_value={'id':4,'desired':.25}))
        observe = self.overlay(teaching)
        with patch.object(self.store, 'get_agent_config', return_value={'id':'child'}) as get:
            result = observe()
        self.assertEqual(result['desired'], .25)
        self.assertEqual(result['teaching_id'], 4)
        get.assert_called_once_with('child')
        teaching.labels.assert_not_called()

    def test_session_select_is_fresh_without_a_table_probe_and_surfaces_schema_errors(self):
        with self.store.conn() as c:
            c.execute('CREATE TABLE agent_explore_sessions(child_generation_id TEXT,status TEXT)')
            c.execute("INSERT INTO agent_explore_sessions VALUES('g','collecting')")
        statements = []
        original = self.store.conn
        @contextmanager
        def traced():
            with original() as c:
                c.set_trace_callback(statements.append)
                yield c
        with patch.object(self.store, 'conn', traced):
            self.assertEqual(_session_for_child(self.store,'g')['status'], 'collecting')
        self.assertFalse(any('sqlite_master' in s.lower() for s in statements))
        self.assertEqual(sum('SELECT * FROM agent_explore_sessions' in s for s in statements), 1)
        with self.store.conn() as c:
            c.execute("UPDATE agent_explore_sessions SET status='quality_blocked'")
        self.assertEqual(_session_for_child(self.store,'g')['status'], 'quality_blocked')
        with self.store.conn() as c:
            c.execute('DROP TABLE agent_explore_sessions')
        self.assertIsNone(_session_for_child(self.store,'g'))
        with self.store.conn() as c:
            c.execute('CREATE TABLE agent_explore_sessions(wrong_column TEXT)')
        with self.assertRaises(sqlite3.OperationalError):
            _session_for_child(self.store,'g')

    def test_context_transactions_obey_complete_payload_bytes_and_yield_between_chunks(self):
        with self.store.conn() as c:
            c.execute('CREATE TABLE payload(a TEXT,e TEXT,p TEXT,PRIMARY KEY(a,e))')
        buf = ContextRowBuffer(self.store,'payload','INSERT OR REPLACE INTO payload VALUES(?,?,?)',
                               batch_rows=64,batch_bytes=64*1024,shared_columns=(2,))
        shared = 'x' * 40000
        buf.submit([('a',str(i),shared) for i in range(4)])
        visible = []
        def yield_to_reader(_seconds):
            def read():
                with self.store.lock, self.store.conn() as c:
                    visible.append(c.execute('SELECT COUNT(*) FROM payload').fetchone()[0])
            thread = threading.Thread(target=read)
            thread.start();thread.join(3)
            self.assertFalse(thread.is_alive())
        with patch('context_row_buffer.time.sleep', side_effect=yield_to_reader):
            self.assertEqual(buf.flush(), 4)
        self.assertEqual(visible, [1,2,3])
        self.assertEqual(buf.snapshot()['commits'], 4)
        self.assertEqual(buf.snapshot()['bytes'], 0)

    def test_non_plain_values_expression_keeps_executemany_semantics(self):
        with self.store.conn() as c:
            c.execute('CREATE TABLE expressions(a TEXT,e TEXT,n INTEGER)')
        buf = ContextRowBuffer(self.store,'expressions','INSERT INTO expressions VALUES(?,?,length(?))')
        buf.submit([('a','b','hello'),('a','c','x')])
        self.assertEqual(buf.flush(), 2)
        with self.store.conn() as c:
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM expressions ORDER BY e')],
                             [('a','b',5),('a','c',1)])

    def journal_engine(self):
        import test_release_196_deferred_contention as contention
        helper = contention.DeferredContention196Tests()
        helper.store = self.store
        self.addCleanup(helper.doCleanups)
        return helper.journal_fixture()

    def test_large_narrow_backlog_uses_bounded_larger_turn_and_explicit_drain_finishes(self):
        engine = self.journal_engine()
        for i in range(600):
            engine.executor.submit(SimpleNamespace(agent_id='a',intent_id='narrow'+str(i),created_at=100.))
        result = engine.flush_feature_journal(max_batches=1)
        self.assertEqual(result['windows'],512)
        self.assertEqual(engine.feature_window_deferred_snapshot()['windows']['pending'],89)
        self.assertEqual(engine.flush_feature_journal()['windows'],89)
        self.assertEqual(engine.feature_window_deferred_snapshot()['windows']['pending'],0)

    def test_wide_schemas_cannot_multiply_the_background_transaction_by_its_row_limit(self):
        engine = self.journal_engine()
        engine.models['a'].schema.entities = ['sensor.'+str(i) for i in range(1000)]
        for i in range(600):
            engine.executor.submit(SimpleNamespace(agent_id='a',intent_id='wide'+str(i),created_at=100.))
        # Observe the detached transaction boundary, without making 600000 test links.
        with patch.object(engine.feature_journal,'open_windows_batch',side_effect=lambda rows:len(rows)) as write:
            result = engine.flush_feature_journal(max_batches=1)
        self.assertEqual(result['windows'],5)
        self.assertEqual(sum(len(r['entities']) for r in write.call_args.args[0]),4001)
        self.assertEqual(engine.feature_window_deferred_snapshot()['windows']['pending'],596)


class ReadonlyProtection218Tests(unittest.TestCase):
    query = 'SELECT * FROM (WITH RECURSIVE r(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM r WHERE n<3000) SELECT SUM(n) FROM r)'

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name)/'readonly.db')

    def test_pure_read_has_no_guard_callbacks_but_next_write_still_runs_them(self):
        calls = []
        original = BudgetConnection.storage_progress
        def progress(connection):
            calls.append(connection.storage_writing)
            return original(connection)
        with patch.object(BudgetConnection, 'storage_progress', progress), self.store.conn() as c:
            c.execute('CREATE TABLE scratch(n INTEGER)')
            calls.clear()
            self.assertEqual(c.storage_readonly_one(self.query)[0], 4501500)
            self.assertEqual(calls, [])
            self.assertEqual(c.execute('PRAGMA query_only').fetchone()[0], 0)
            c.execute('INSERT INTO scratch '+self.query)
            self.assertTrue(calls)
            self.assertTrue(all(value>0 for value in calls))

    def test_query_only_blocks_direct_and_sql_function_writes_without_leaking_mode(self):
        with self.store.conn() as c:
            c.execute('CREATE TABLE scratch(n INTEGER)')
            prepared = 'INSERT INTO scratch VALUES(?)'
            c.execute(prepared, (8,))
            with self.assertRaises(sqlite3.OperationalError):
                c.storage_readonly_one(prepared, (9,))
            c.execute('DELETE FROM scratch')
            with self.assertRaises(sqlite3.OperationalError):
                c.storage_readonly_one('INSERT INTO scratch VALUES(1)')
            failures = []
            def attempt_write():
                try:
                    c.execute('INSERT INTO scratch VALUES(2)')
                except sqlite3.OperationalError as exc:
                    failures.append(str(exc))
                return 7
            c.create_function('attempt_write', 0, attempt_write)
            self.assertEqual(c.storage_readonly_one('SELECT attempt_write()')[0], 7)
            self.assertEqual(len(failures), 1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM scratch').fetchone()[0], 0)
            self.assertEqual(c.execute('PRAGMA query_only').fetchone()[0], 0)
            c.execute('INSERT INTO scratch VALUES(3)')

    def test_failed_read_restores_guard_and_preexisting_readonly_mode_is_preserved(self):
        with self.store.conn() as c:
            original = c.storage_managed_progress
            with self.assertRaises(sqlite3.OperationalError):
                c.storage_readonly_one('SELECT no_such_column')
            self.assertEqual(c.storage_managed_progress, original)
            self.assertEqual(c.execute('PRAGMA query_only').fetchone()[0], 0)
            c.execute('PRAGMA query_only=ON')
            self.assertEqual(c.storage_readonly_one('SELECT 1')[0], 1)
            self.assertEqual(c.execute('PRAGMA query_only').fetchone()[0], 1)
            c.execute('PRAGMA query_only=OFF')

    def test_external_progress_handler_keeps_running_during_and_after_read(self):
        calls = []
        with self.store.conn() as c:
            c.set_progress_handler(lambda: calls.append(True) or 0, 10)
            self.assertEqual(c.storage_readonly_one(self.query)[0], 4501500)
            before = len(calls)
            self.assertGreater(before, 0)
            c.execute(self.query).fetchone()
            self.assertGreater(len(calls), before)
            self.assertIsNone(c.storage_managed_progress)

    def test_next_write_is_still_interrupted_and_rolled_back_by_vm_pressure(self):
        with self.store.conn() as c:
            c.execute('CREATE TABLE scratch(n INTEGER)')
            c.storage_readonly_one(self.query)
            original = self.store.storage_budget.check
            def check(parameters=(), *, refresh=True):
                if not refresh and c.storage_writing:
                    raise StoragePressure('VM guard retained after read')
                return original(parameters, refresh=refresh)
            with patch.object(self.store.storage_budget, 'check', check):
                with self.assertRaisesRegex(StoragePressure, 'VM guard retained'):
                    c.execute('INSERT INTO scratch '+self.query)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM scratch').fetchone()[0], 0)

    def test_external_cancellation_is_not_bypassed(self):
        with self.store.conn() as c:
            calls = [0]
            def cancel():
                calls[0] += 1
                return int(calls[0]>=50)
            c.set_progress_handler(cancel, 100)
            try:
                with self.assertRaises(sqlite3.OperationalError) as error:
                    c.storage_readonly_one(self.query)
                self.assertEqual(error.exception.sqlite_errorcode, sqlite3.SQLITE_INTERRUPT)
                self.assertGreaterEqual(calls[0], 50)
            finally:
                c.set_progress_handler(None, 0)
            self.assertEqual(c.execute('PRAGMA query_only').fetchone()[0], 0)


class ProtectionSegments218Tests(unittest.TestCase):
    def test_native_prune_matches_legacy_ids_links_ties_unicode_and_multiple_bounded_turns(self):
        from observation_contract import FeatureJournal
        temp = tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        windows = [dict(window_id=f'{owner},"ąć:{i}',agent_id=str(owner),entities=['sensor.a','sensor.b'],
                        anchor_time=float(i%4)) for owner in range(70) for i in range(10)]
        windows += [dict(window_id='large:'+str(i),agent_id='large',entities=['sensor.a'],anchor_time=float(i%5))
                    for i in range(700)]
        snapshots = []
        for name,native in [('native',True),('legacy',False)]:
            store = Store(Path(temp.name)/(name+'.db'))
            journal = FeatureJournal(store,clock=lambda:100.,max_windows_per_agent=5)
            journal._window_prune_json = native
            with store.connection_session() as c:
                c.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,999)
                journal.open_windows_batch(windows)
                snapshots.append(([tuple(r) for r in c.execute('SELECT * FROM feature_windows ORDER BY window_id')],
                                  [tuple(r) for r in c.execute('SELECT * FROM feature_window_entities ORDER BY window_id,entity_id')]))
        self.assertEqual(snapshots[0],snapshots[1])
        self.assertEqual(len(snapshots[0][0]),71*5)

    def test_missing_json_uses_legacy_prune_but_lock_error_rolls_back_the_complete_batch(self):
        from observation_contract import FeatureJournal
        temp = tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        store = Store(Path(temp.name)/'fallback.db')
        journal = FeatureJournal(store,clock=lambda:100.,max_windows_per_agent=2)
        rows = [dict(window_id=str(i),agent_id='a',entities=['sensor.a'],anchor_time=float(i)) for i in range(4)]
        with patch.object(BudgetConnection,'storage_readonly_one',side_effect=sqlite3.OperationalError('no such function: json_group_array')):
            journal.open_windows_batch(rows)
        self.assertFalse(journal._window_prune_json)
        with store.conn() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM feature_windows').fetchone()[0],2)
        journal._window_prune_json = True
        with patch.object(BudgetConnection,'storage_readonly_one',side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaisesRegex(sqlite3.OperationalError,'database is locked'):
                journal.open_windows_batch([dict(window_id='new',agent_id='a',entities=['sensor.b'],anchor_time=10.)])
        with store.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM feature_windows WHERE window_id='new'").fetchone()[0],0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM feature_window_entities WHERE window_id='new'").fetchone()[0],0)

    def test_bulk_sample_protection_keeps_native_float_precision_across_bind_groups(self):
        from observation_contract import FeatureJournal
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = Store(Path(temp.name)/'protection.db')
        stamp = [0.]
        journal = FeatureJournal(store, clock=lambda: stamp[0], window_retention_days=0.)
        windows = []
        for i, expiry in enumerate((math.nextafter(100., math.inf), 1./3., 1e200, 1e-200)):
            stamp[0] = expiry
            selected = ['sensor.a'] if i % 2 else ['sensor.a', 'sensor.b']
            lo, hi = float(i*10), float(i*10+12)
            journal.open_window(str(i), 'a', selected, lo, 'test', before=0., after=hi-lo)
            windows.append((selected, lo, hi, expiry))
        stamp[0] = 0.
        records = [dict(event_key=str(i), entity_id=('sensor.a','sensor.b','sensor.missing')[i%3],
                        event_time=float(i%61), received_time=100.,
                        state=support.state('sensor.sample', i), source='test') for i in range(600)]
        with store.connection_session() as c:
            c.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
            journal.record_batch(records)
        with store.conn() as c:
            actual = dict(c.execute('SELECT event_key,protected_until FROM feature_observation_events'))
        self.assertEqual(len(actual), len(records))
        for row in records:
            values = [expiry for selected,lo,hi,expiry in windows
                      if row['entity_id'] in selected and lo<=row['event_time']<=hi]
            self.assertEqual(actual[row['event_key']], max(values) if values else 0., row)

    def test_older_sqlite_fallback_retains_complete_sql_parity(self):
        from test_release_217_writer_turns import JournalAdmissionTests
        helper = JournalAdmissionTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        with patch('observation_contract.sqlite3.sqlite_version_info', (3,32,0)):
            helper.test_grouped_window_protection_matches_legacy_pointwise_max()

    def test_envelope_preserves_closed_endpoints_singletons_gaps_and_every_adjacent_float(self):
        rng = random.Random(218)
        intervals = [(0.,1.,2.),(1.,2.,4.),(4.,4.,9.),(-2.,-1.,3.),
                     (5.,4.,99.),(sys.float_info.max,sys.float_info.max,7.)]
        intervals += [(rng.randint(-100,100)/7, rng.randint(-100,100)/7,
                       rng.randint(-5,50)/3) for _ in range(180)]
        segments = _maximum_protection_segments(intervals)
        self.assertIsNotNone(segments)
        points = {rng.uniform(-20,20) for _ in range(500)}
        for lo,hi,_ in intervals:
            points.update((lo,hi,math.nextafter(lo,-math.inf),math.nextafter(lo,math.inf),
                           math.nextafter(hi,-math.inf),math.nextafter(hi,math.inf)))
        for point in points:
            expected = max([0.]+[v for lo,hi,v in intervals if lo<=point<=hi])
            actual = max([0.]+[v for lo,hi,v in segments if lo<=point<hi])
            self.assertEqual(expected,actual,point)
            self.assertLessEqual(sum(lo<=point<hi for lo,hi,_ in segments),1)
        gap = _maximum_protection_segments([(0.,1.,5.),(3.,4.,8.)])
        self.assertFalse(any(lo<=2.<hi for lo,hi,_ in gap))

    def test_nonfinite_ranges_require_the_unchanged_legacy_sql_path(self):
        for interval in [(0.,math.inf,1.),(-math.inf,0.,1.),(0.,1.,math.inf),(0.,1.,math.nan)]:
            self.assertIsNone(_maximum_protection_segments([interval]))


if __name__ == '__main__':
    unittest.main()
