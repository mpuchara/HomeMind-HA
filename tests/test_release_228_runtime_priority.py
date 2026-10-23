"""Slow diagnostics and Candidate outcome writes cannot precede Live inference."""
import json
import sqlite3
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

import support
import test_candidate_shadow_context as fixture
from context_tournament import context_row_ram_budget
from correct_learning_debug import _raw_window


class OutcomePriorityTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.CandidateShadowExactContextTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.manager.request_candidate_shadow_persistence = Mock()
        self.observe()
        self.f.manager.flush_candidate_shadow_decisions(force=True)

    def observe(self):
        f = self.f
        states = f.states('on')
        f.engine.runtime[f.root['id']] = dict(last_prediction=1., last_confidence=.88, last_inference_ts=10.)
        f.manager.before_live_process(f.root, states)
        f.engine.runtime[f.root['id']]['last_inference_ts'] = 20.
        f.engine.policy(f.root).features(states, None, at_ts=20.25)
        f.manager.after_live_process(f.root, states)
        self.assertEqual(f.manager.drain_deferred_candidate_shadow(max_roots=1), 1)

    def outcome(self):
        states = self.f.states('on')
        states['light.exact'].update(state='on', last_updated='2026-10-23T10:00:00+00:00',
                                     context={'user_id': 'physical-user'})
        self.f.manager.before_live_process(self.f.root, states)
        return states

    def summary(self):
        with self.f.store.conn() as c:
            row = c.execute('SELECT summary_json FROM candidate_generation_comparisons').fetchone()
        return json.loads(row[0]) if row else {}

    def test_actual_before_hook_returns_while_store_mutex_and_sqlite_writer_are_held(self):
        f = self.f
        blocker = sqlite3.connect(f.store.path)
        self.addCleanup(blocker.close)
        blocker.execute('BEGIN IMMEDIATE')
        completed, errors = threading.Event(), []
        def call():
            try:
                with patch.object(f.store, 'conn', side_effect=AssertionError('SQL before Live')):
                    self.outcome()
            except BaseException as exc:
                errors.append(exc)
            finally:
                completed.set()
        with f.store.lock:
            thread = threading.Thread(target=call, daemon=True)
            thread.start()
            self.assertTrue(completed.wait(1.), 'before-Live hook blocked on persistence')
        thread.join(2.)
        blocker.rollback()
        self.assertEqual(errors, [])
        q = f.manager.candidate_shadow_transition_writes
        self.assertEqual(q.snapshot()['pending'], 1)
        payload = q.retained_payloads()[0]
        self.assertEqual(payload['values'][6], 1.)
        self.assertEqual(payload['values'][14], 'manual_user_target_change')
        f.manager.flush_candidate_shadow_decisions(force=True)
        self.assertEqual(q.snapshot()['pending'], 0)
        self.assertEqual(self.summary()['samples'], 1)
        f.executor.service.assert_not_called()

    def test_counter_failure_rolls_back_pair_then_retries_exact_payload(self):
        self.outcome()
        f = self.f
        retained = f.manager.candidate_shadow_transition_writes.retained_payloads()
        with f.store.conn() as c:
            c.execute("CREATE TRIGGER fail_transition BEFORE UPDATE ON agent_candidates "
                      "BEGIN SELECT RAISE(ABORT,'counter rollback'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'counter rollback'):
            f.manager.flush_candidate_shadow_decisions(force=True)
        with f.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM candidate_generation_pairs').fetchone()[0], 0)
            c.execute('DROP TRIGGER fail_transition')
        self.assertEqual(f.manager.candidate_shadow_transition_writes.retained_payloads(), retained)
        f.manager.flush_candidate_shadow_decisions(force=True)
        self.assertEqual(self.summary()['samples'], 1)

    def test_episode_retry_does_not_double_count_and_receives_exact_committed_pair(self):
        self.outcome()
        f = self.f
        callback = Mock(side_effect=RuntimeError('episode retry'))
        f.manager.candidate_pair_persisted = callback
        with self.assertRaisesRegex(RuntimeError, 'episode retry'):
            f.manager.flush_candidate_shadow_decisions(force=True)
        self.assertEqual(self.summary()['samples'], 1)
        callback.side_effect = None
        f.manager.flush_candidate_shadow_decisions(force=True)
        self.assertEqual(self.summary()['samples'], 1)
        self.assertEqual(callback.call_args.args[0][6], 1.)

    def test_replaced_generation_rejects_old_outcome(self):
        self.outcome()
        f = self.f
        with f.store.conn() as c:
            c.execute('UPDATE agent_candidate_generations SET created_ts=created_ts+1 WHERE generation_id=?',
                      (f.generation_id,))
        f.manager.flush_candidate_shadow_decisions(force=True)
        with f.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM candidate_generation_pairs').fetchone()[0], 0)
        self.assertEqual(f.manager.candidate_shadow_persistence_snapshot()['comparison_evidence_invalidations'], 1)

    def test_capacity_is_a_gap_and_never_falls_back_to_foreground_sql(self):
        q = self.f.manager.candidate_shadow_transition_writes
        q.max_bytes = 1
        with patch.object(self.f.store, 'conn', side_effect=AssertionError('SQL on full queue')):
            self.outcome()
        self.assertEqual(q.snapshot()['pending'], 0)
        self.assertGreater(self.f.manager.candidate_shadow_persistence_snapshot()['admission']['persistence_gaps'], 0)


class DiagnosticReadTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.CandidateShadowExactContextTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def test_readonly_job_reuses_connection_without_writer_mutex_or_journal_setup(self):
        store = self.f.store
        with store.diagnostic_read_session() as c:
            statements = []
            c.set_trace_callback(statements.append)
            self.assertIsNotNone(store.get_agent_config(self.f.root['id']))
            self.assertIsNotNone(store.get_model(self.f.root['id']))
            self.assertFalse(c.in_transaction)
            self.assertFalse(any('journal_size_limit' in q.lower() for q in statements))
            with self.assertRaises(sqlite3.OperationalError):
                with store.conn() as borrowed:
                    borrowed.execute('UPDATE agents SET enabled=0')
        self.assertIsNone(getattr(store._connection_session, 'connection', None))
        self.assertTrue(store.get_agent_config(self.f.root['id'])['enabled'])

    def test_raw_window_limits_each_entity_and_preserves_global_time_order(self):
        store = self.f.store
        with store.conn() as c:
            for i in range(20):
                c.execute('INSERT INTO entity_history(entity_id,ts,state,attributes_json,source) VALUES(?,?,?,?,?)',
                          ('sensor.a' if i % 2 else 'sensor.b', 1700000000. + i, str(i), '{}', 'fixture'))
        with store.diagnostic_read_session():
            result = _raw_window(store, ['sensor.a', 'sensor.b'], 1700000010., 10., 5)
        self.assertEqual([r['ts'] for r in result], [1700000000. + i for i in range(5)])
        self.assertEqual(_raw_window(store, ['sensor.a'], 10., 10., 0), [])

    def test_context_buffer_scales_only_with_known_headroom_and_preserves_custom_caps(self):
        with patch('runtime_sqlite_cache.memory_headroom', return_value=(8192, 6000)):
            self.assertEqual(context_row_ram_budget({}), 64)
            self.assertEqual(context_row_ram_budget({'context_row_ram_mb': 7}), 7)
        with patch('runtime_sqlite_cache.memory_headroom', return_value=(1024, 300)):
            self.assertLessEqual(context_row_ram_budget({}), 2)
        with patch('runtime_sqlite_cache.memory_headroom', return_value=(None, None)):
            self.assertEqual(context_row_ram_budget({}), 16)

    def test_correct_label_read_deduplicates_legacy_ids_without_migration(self):
        from correct_data_foundation import _install_teaching
        source = [dict(id=1, fingerprint='same', sample_ts=1700000000., desired=1.,
                       created_ts=1., supervision_event_id=None),
                  dict(id=2, fingerprint='same', sample_ts=1700000000., desired=1.,
                       created_ts=2., supervision_event_id=None)]
        service = SimpleNamespace(labels=Mock(return_value=source), _insert_label=Mock())
        core = SimpleNamespace(STORE=self.f.store, ENGINE=SimpleNamespace(rl_teaching=service))
        with patch('correct_data_foundation._ensure_schema'):
            _install_teaching(core)
        with patch.object(core.STORE, 'conn', side_effect=AssertionError('label read migrated IDs')):
            result = service.labels('root')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['id'], 2)
        self.assertTrue(result[0]['supervision_event_id'])
        self.assertIsNone(source[0]['supervision_event_id'])
