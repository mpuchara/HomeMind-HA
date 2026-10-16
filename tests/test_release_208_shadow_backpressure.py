"""Saturated Candidate evidence is backpressure, never a policy failure/replay."""
import unittest
from unittest.mock import Mock, patch

import support
import test_candidate_shadow_context as fixture
import agent_candidate_shadow_runtime as runtime
from deferred_evidence import DeferredEvidence, EvidenceBufferFull
from storage_budget import StoragePressure


class ShadowBackpressureTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.CandidateShadowExactContextTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.budget = self.f.store.storage_budget
        self.original_limit = self.budget.db_limit

    def pause(self):
        # Use real page accounting and admission with a small quota; no 9 GB fixture.
        with self.f.store.conn() as c:
            self.budget.observe_pages(c)
        self.budget.db_limit = self.budget.snapshot()['database_used_bytes']
        self.assertEqual(self.budget.sample()['reason'], 'database_budget')

    def observe(self, index, *, drain=True):
        f = self.f
        ts = 20. + index * .1
        states = f.states('on' if index % 2 else 'off')
        f.engine.runtime[f.root['id']] = dict(last_prediction=float(index % 2),
                                             last_confidence=.88, last_inference_ts=ts - .01)
        f.manager.before_live_process(f.root, states)
        f.engine.runtime[f.root['id']]['last_inference_ts'] = ts
        f.engine.policy(f.root).features(states, None, at_ts=ts)
        f.manager.after_live_process(f.root, states)
        return f.manager.drain_deferred_candidate_shadow(max_roots=1) if drain else None

    def fill(self):
        self.pause()
        for index in range(128):
            self.assertEqual(self.observe(index), 1)
        snap = self.f.manager.candidate_shadow_persistence_snapshot()
        self.assertEqual(snap['pending'], 128)
        return snap

    def recover(self):
        self.budget.db_limit = self.original_limit
        self.assertEqual(self.budget.sample()['level'], 'normal')
        return self.f.manager.flush_candidate_shadow_decisions(force=True)

    def test_full_queue_skips_policy_without_error_storm_or_eviction(self):
        before = self.fill()
        f = self.f
        with patch.object(runtime, '_predict_candidate', side_effect=AssertionError('unrecordable policy work')):
            with patch.object(f.store, 'event') as event:
                for index in range(128, 328):
                    self.assertEqual(self.observe(index), 0)
            event.assert_not_called()
        after = f.manager.candidate_shadow_persistence_snapshot()
        self.assertEqual(after['pending'], 128)
        self.assertEqual(after['bytes'], before['bytes'])
        self.assertEqual(after['rejected'], 0)
        self.assertEqual(after['admission']['skipped'], 200)
        self.assertEqual(after['admission']['reason'], 'database_budget')
        self.assertEqual(f.manager.candidate_shadow_async_diagnostics()['errors'], 0)
        self.assertEqual(f.manager.candidate_shadow_async_diagnostics()['observation_gaps'], 200)
        self.assertTrue(f.manager._worker_health()['shadow_observations']['paused'])
        f.executor.service.assert_not_called()

    def test_recovery_flushes_exact_rows_without_prediction_and_only_observes_new_context(self):
        self.fill()
        self.observe(128)
        f = self.f
        with patch.object(f.engine, 'policy', side_effect=AssertionError('replayed policy')):
            self.assertEqual(self.recover(), 128)
        with f.store.conn() as c:
            rows = c.execute('SELECT ts,desired FROM candidate_generation_decisions WHERE generation_id=? ORDER BY ts',
                             (f.generation_id,)).fetchall()
        self.assertEqual(len(rows), 128)
        for index, row in enumerate(rows):
            self.assertEqual(tuple(row), (20. + index * .1, float(index % 2)))
        self.assertEqual(self.observe(129), 1)
        snap = f.manager.candidate_shadow_persistence_snapshot()
        self.assertFalse(snap['admission']['paused'])
        self.assertEqual(snap['admission']['recoveries'], 1)
        self.assertEqual(f.manager.candidate_latest_runtime(f.generation_id)['ts'], 32.9)
        with f.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM candidate_generation_pairs').fetchone()[0], 0)
        f.executor.service.assert_not_called()

    def test_passive_heartbeat_also_respects_full_evidence_queue(self):
        self.fill()
        f = self.f
        f.engine.state_revision = 600
        f.engine.state_map = f.states('on')
        with patch.object(runtime, '_predict_candidate', side_effect=AssertionError('passive policy ran')):
            with patch('agent_candidate_shadow_runtime.time.monotonic', return_value=100000.):
                self.assertEqual(f.manager.drain_candidate_shadow_events(force=True), 0)
        self.assertGreater(f.manager.candidate_shadow_persistence_snapshot()['admission']['skipped'], 0)
        self.assertEqual(f.manager._worker_health()['error_count'], 0)

    def test_byte_overflow_after_inference_is_a_persistence_gap(self):
        f = self.f
        with patch.object(DeferredEvidence, 'submit', side_effect=EvidenceBufferFull('encoded byte limit')):
            self.assertEqual(self.observe(0), 0)
        self.assertEqual(f.manager.candidate_shadow_async_diagnostics()['errors'], 0)
        self.assertEqual(f.manager.candidate_shadow_persistence_snapshot()['admission']['persistence_gaps'], 1)
        with f.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM candidate_generation_decisions').fetchone()[0], 0)

    def test_storage_pressure_during_outcome_is_not_an_inference_failure(self):
        self.observe(1)
        self.pause()
        states = self.f.states('on')
        states['light.exact']['state'] = 'on'
        with patch.object(self.f.store, 'event') as event:
            self.f.manager.before_live_process(self.f.root, states)
        event.assert_not_called()
        snap = self.f.manager.candidate_shadow_persistence_snapshot()
        self.assertGreater(snap['admission']['persistence_gaps'], 0)
        self.assertEqual(self.f.manager._worker_health()['error_count'], 0)

    def test_genuine_candidate_failure_still_raises_visible_inference_error(self):
        f = self.f
        self.observe(0, drain=False)
        f.manager.execute_candidate_shadow_job = Mock(side_effect=ValueError('broken model'))
        with patch.object(f.store, 'event') as event:
            self.assertEqual(f.manager.drain_deferred_candidate_shadow(), 0)
        self.assertEqual(event.call_args.args[2], 'candidate_shadow_deferred_error')
        self.assertEqual(f.manager.candidate_shadow_async_diagnostics()['errors'], 1)

    def test_gap_breaks_lead_continuity_and_cannot_pair_old_decision_to_later_outcome(self):
        self.fill()
        self.observe(128)
        self.recover()
        f = self.f
        states = f.states('on')
        states['light.exact']['state'] = 'on'
        f.manager.before_live_process(f.root, states)
        with f.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM candidate_generation_pairs').fetchone()[0], 0)
        original = f.manager.execute_candidate_shadow_job
        bundles = []
        def capture(job):
            result = original(job)
            bundles.append(result)
            return result
        f.manager.execute_candidate_shadow_job = capture
        self.observe(129)
        bundle = bundles[-1]
        self.assertTrue(bundle['results'])
        self.assertTrue(all(row['desired_since_ts'] == bundle['ts'] for row in bundle['results'].values()))

    def test_unrelated_buffer_error_is_not_misclassified_as_capacity(self):
        f = self.f
        with patch.object(DeferredEvidence, 'submit', side_effect=BufferError('unrelated model buffer')):
            with patch.object(f.store, 'event') as event:
                self.assertEqual(self.observe(0), 0)
        self.assertEqual(event.call_args.args[2], 'candidate_shadow_deferred_error')
        self.assertEqual(f.manager.candidate_shadow_async_diagnostics()['errors'], 1)


class EvidenceAdmissionTests(unittest.TestCase):
    def test_item_capacity_duplicate_conflict_and_exact_flush(self):
        writer = Mock()
        queue = DeferredEvidence(object(), writer, max_items=1, max_bytes=100)
        self.assertFalse(queue.full())
        queue.submit('one', {'value': 1})
        self.assertTrue(queue.full())
        self.assertFalse(queue.submit('one', {'value': 1}))
        with self.assertRaises(ValueError):
            queue.submit('one', {'value': 2})
        with self.assertRaises(EvidenceBufferFull):
            queue.submit('two', {'value': 2})
        self.assertEqual(queue.flush(force=True), 1)
        writer.assert_called_once_with({'value': 1})
        self.assertFalse(queue.full())

    def test_exact_byte_limit_and_overflow_preserve_original_payload(self):
        writer = Mock()
        queue = DeferredEvidence(object(), writer, max_items=128, max_bytes=11)
        queue.submit('one', {'value': 1})
        self.assertTrue(queue.full())
        with self.assertRaises(EvidenceBufferFull):
            queue.submit('two', {})
        self.assertEqual(queue.snapshot()['bytes'], 11)
        self.assertEqual(queue.flush(force=True), 1)
        writer.assert_called_once_with({'value': 1})


if __name__ == '__main__':
    unittest.main()
