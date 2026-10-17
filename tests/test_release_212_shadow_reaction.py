"""Candidate causal parity, actual event wake scheduling and exact statistics."""
from contextlib import nullcontext
import copy
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import support
import agent_candidate_shadow_runtime as runtime
import test_candidate_shadow_runtime as fixtures
from agent_candidates import AgentCandidateManager
from tools.benchmark_candidate_shadow_reaction import one
from test_release_210_screening_performance import reference_score, sample_rows
from context_tournament_policy_candidate import semantic_predictive_score

ROOT = Path(__file__).resolve().parents[1]


class ShadowReactionTests(unittest.TestCase):
    def test_four_real_policies_share_only_same_event_descriptors_with_exact_results(self):
        with patch.object(runtime, 'shared_entity_observations', lambda *args: nullcontext()):
            reference = one(ROOT, passes=3)
        current = one(ROOT, passes=3)
        self.assertEqual(reference['features_labels_metadata_predictions_sha256'],
                         current['features_labels_metadata_predictions_sha256'])
        self.assertEqual(reference['feature_descriptor_computations'],
                         4 * current['feature_descriptor_computations'])
        self.assertEqual(current['ha_calls'], 0)

    def test_actual_candidate_worker_observes_without_waiting_for_three_second_idle_poll(self):
        result = one(ROOT, worker=True)
        self.assertGreaterEqual(result['event_to_candidate_ms'], 450)
        self.assertLess(result['event_to_candidate_ms'], 1800)
        self.assertEqual(result['desired'], 1)
        self.assertEqual(result['ha_calls'], 0)

    def test_passive_deadline_uses_ram_and_respects_fresh_realtime_traffic(self):
        f = fixtures.CandidateShadowRuntimeTests(methodName='runTest')
        f.setUp()
        try:
            f._g1()
            with patch.object(runtime.time, 'monotonic', return_value=100.):
                f.manager.drain_candidate_shadow_events(force=True)
                f.engine.last_event_monotonic = 100.
                f.engine.on_state_changed({'entity_id': 'binary_sensor.presence',
                    'new_state': f._states(presence='off')['binary_sensor.presence']})
            with patch.object(f.store, 'conn', side_effect=AssertionError('deadline queried SQLite')), \
                 patch.object(runtime.time, 'monotonic', return_value=100.4):
                self.assertAlmostEqual(f.manager.candidate_shadow_wait_seconds(3), .1)
                self.assertEqual(f.manager.drain_candidate_shadow_events(), 0)
                f.engine.last_event_monotonic = 100.4
                self.assertAlmostEqual(f.manager.candidate_shadow_wait_seconds(3), .5)
            with patch.object(runtime.time, 'monotonic', return_value=101.):
                self.assertEqual(f.manager.drain_candidate_shadow_events(), 1)
                self.assertEqual(f.manager.candidate_shadow_wait_seconds(3), 3)
                self.assertEqual(f.manager.drain_candidate_shadow_events(), 0)
            f.executor.service.assert_not_called()
        finally:
            f.tearDown()

    def test_worker_keeps_wake_created_during_maintenance(self):
        # A bounded drain or producer posts the next job during maintenance.
        # It must run immediately even with a very long idle cadence.
        f = fixtures.CandidateShadowRuntimeTests(methodName='runTest')
        f.setUp()
        done = threading.Event()
        turns = []
        try:
            f.manager.poll_seconds = 30
            f.manager._all_rows = lambda: []
            def maintenance():
                turns.append(1)
                if len(turns) == 1:
                    f.manager.wake_event.set()
                else:
                    done.set()
                    f.manager.stop()
            f.manager._maintenance = maintenance
            worker = threading.Thread(target=f.manager._worker_loop, daemon=True)
            worker.start()
            try:
                self.assertTrue(done.wait(1.8), 'worker lost the next-job wake')
            finally:
                f.manager.stop()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(turns), 2)
        finally:
            f.tearDown()

    def test_dense_shared_label_moments_are_bit_exact_across_new_labels(self):
        rows = sample_rows()
        original = copy.deepcopy(rows)
        for label in (0., 1., -17., 1e100):
            rows[-1]['label'] = label
            self.assertEqual(semantic_predictive_score(rows), reference_score(rows))
        self.assertEqual(rows[:-1], original[:-1])

    def test_sparse_columns_and_missing_labels_keep_pairwise_statistics(self):
        rows = sample_rows()
        for i, row in enumerate(rows):
            row['value'] = None if i % 7 == 0 else row['value']
            row['label'] = 'bad' if i % 11 == 0 else row['label']
            if i % 3:
                row['interactions'].pop('primary_1')
        self.assertEqual(semantic_predictive_score(rows), reference_score(rows))


if __name__ == '__main__':
    unittest.main()
