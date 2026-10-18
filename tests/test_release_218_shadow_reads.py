import sqlite3
import math
import random
import sys
import tempfile
import threading
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


class ShadowReads218Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name)/'reads.db')

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


class ProtectionSegments218Tests(unittest.TestCase):
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
