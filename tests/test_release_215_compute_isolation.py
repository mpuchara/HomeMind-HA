"""Exact compute parity, live-lock independence and bounded RAM history reuse."""
import copy
import json
import os
from pathlib import Path
import pickle
import sys
import threading
import time
import unittest
from unittest.mock import patch

import support
from context import TemporalHistory
from observation_contract import register_live_sample
from runtime_background_compute import COMPUTE, background_compute_scope, dumps, roundtrip
from temporal_observation_snapshots import TemporalObservationSnapshots
from settings import parse_ts, _parse_aware_ts

sys.path.insert(0, str(support.ROOT/'tools'))


class ComputeIsolationTests(unittest.TestCase):
    def tearDown(self):
        COMPUTE.close()

    def test_real_classifier_coefficients_and_predictions_match_local_fit(self):
        from benchmark_classifier_fit import fixture, compare
        left = fixture(dims=128, varying=76, rows=64)
        right = copy.deepcopy(left)
        left.fit()
        with background_compute_scope():
            right.fit()
        self.assertLessEqual(compare(left, right), 1e-10)
        self.assertNotEqual(COMPUTE.snapshot()['pid'], os.getpid())

    def test_json_roundtrip_checksum_and_nan_errors_keep_original_semantics(self):
        from policy_backend import serialize_backend_model, verify_model_checksum
        value = dict(schema={'version':12, 'entities':['sensor.żółć']}, dims=128,
                     negative=-0., rows=[[.1*i for i in range(128)] for _ in range(96)])
        reference = serialize_backend_model(value, policy_backend='diagonal_linucb', backend_version=10)
        with background_compute_scope():
            self.assertEqual(dumps(value, sort_keys=True, ensure_ascii=False),
                             json.dumps(value, sort_keys=True, ensure_ascii=False))
            self.assertEqual(roundtrip(value), json.loads(json.dumps(value)))
            result = serialize_backend_model(value, policy_backend='diagonal_linucb', backend_version=10)
            self.assertEqual(reference, result)
            self.assertTrue(verify_model_checksum(result))
            with self.assertRaises(ValueError):
                COMPUTE.call('json', {'invalid':float('nan')}, {'allow_nan':False})

    def test_foreground_small_json_never_enters_child_and_worker_restarts(self):
        COMPUTE.close()
        self.assertEqual(dumps({'a':1}), '{"a": 1}')
        self.assertFalse(COMPUTE.snapshot()['alive'])
        with background_compute_scope():
            self.assertEqual(COMPUTE.call('roundtrip', {'x':[1,2]}), {'x':[1,2]})
            first = COMPUTE.snapshot()['pid']
            COMPUTE.process.kill(); COMPUTE.process.wait(3)
            self.assertEqual(COMPUTE.call('roundtrip', {'x':[3]}), {'x':[3]})
            self.assertNotEqual(first, COMPUTE.snapshot()['pid'])

    def test_child_imports_no_application_store_or_ha_and_uses_bounded_frames(self):
        from runtime_compute_child import MAX_FRAME, read_frame
        import io, struct
        with self.assertRaises(ValueError):
            read_frame(io.BytesIO(struct.pack('!I', MAX_FRAME+1)))
        source = (support.ROOT/'adaptive_ai/src/runtime_compute_child.py').read_text()
        self.assertNotIn('import storage', source)
        self.assertNotIn('import ha', source)
        with background_compute_scope():
            self.assertEqual(COMPUTE.call('roundtrip', [1]), [1])
        self.assertLessEqual(COMPUTE.snapshot()['max_payload_bytes'], MAX_FRAME)

    def test_policy_json_work_releases_prediction_lock(self):
        import benchmark_shadow_context as fixture
        import tempfile
        from storage import Store
        from runtime_background_compute import roundtrip as original
        entered, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as directory, fixture.training_contract():
            f = fixture.Fixture(Store(Path(directory)/'lock.db'))
            def held(value):
                entered.set()
                self.assertTrue(release.wait(3))
                return original(value)
            result, errors = [], []
            def run():
                try:
                    result.append(f.live.serialize())
                except Exception as exc:
                    errors.append(exc)
            with patch('runtime_background_compute.roundtrip', new=held):
                thread = threading.Thread(target=run)
                thread.start()
                try:
                    self.assertTrue(entered.wait(1))
                    acquired = f.live.lock.acquire(timeout=.1)
                    self.assertTrue(acquired, 'JSON cannot retain a live prediction lock')
                    if acquired:
                        f.live.lock.release()
                finally:
                    release.set(); thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors, errors)
            self.assertEqual(result[0]['model_revision'], f.live.model_revision)


class TemporalSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.history = TemporalHistory()
        for eid in ('sensor.a','sensor.b'):
            for ts in range(96):
                self.history.add(eid, ts, support.state(eid, str(ts)))
        self.cache = TemporalObservationSnapshots()

    def test_only_changed_entity_is_copied_and_pending_history_survives_rollover(self):
        first, charge = self.cache.capture(self.history, 95.)
        self.history.add('sensor.a', 96., support.state('sensor.a','new'))
        second, _ = self.cache.capture(self.history, 96.)
        self.assertIs(first['sensor.b'], second['sensor.b'])
        self.assertIsNot(first['sensor.a'], second['sensor.a'])
        self.assertEqual(first['sensor.a'][-1][1]['state'], '95')
        self.assertEqual(self.cache.snapshot()['hits'], 1)
        self.assertEqual(self.cache.snapshot()['misses'], 3)
        self.assertGreater(charge, 0)

    def test_late_journal_metadata_invalidates_snapshot_without_history_append(self):
        first, _ = self.cache.capture(self.history, 95.)
        self.assertTrue(register_live_sample(self.history,'sensor.a',{},95.,96.,'ha'))
        second, _ = self.cache.capture(self.history,96.)
        self.assertIsNot(first['sensor.a'], second['sensor.a'])
        self.assertNotIn('__hm_received_time', first['sensor.a'][-1][1]['attributes'])
        self.assertEqual(second['sensor.a'][-1][1]['attributes']['__hm_received_time'],96.)

    def test_event_time_cutoff_and_memory_eviction_preserve_detached_rows(self):
        self.cache.max_bytes = 1
        first, _ = self.cache.capture(self.history, 25.)
        self.assertEqual(first['sensor.a'][-1][0],25.)
        self.history.samples['sensor.a'][0][1]['attributes']['changed'] = True
        self.assertNotIn('changed',first['sensor.a'][0][1]['attributes'])
        self.assertEqual(self.cache.snapshot()['bytes'], 0)
        self.assertEqual(self.cache.snapshot()['entities'], 0)

    def test_queue_copies_history_outside_engine_lock_and_excludes_it_from_pickle(self):
        import test_context_tournament_shadow as fixture
        f = fixture.ContextTournamentShadowTests(); f.setUp()
        try:
            f.engine.models['agent-1'] = f.policy
            f.engine.temporal_history = self.history
            queue = f.service._deferred_observations
            original = queue.temporal_snapshots.capture
            def capture(*args):
                self.assertFalse(f.engine.lock._is_owned(), 'copy cannot hold global Engine lock')
                return original(*args)
            with patch.object(queue.temporal_snapshots, 'capture', new=capture):
                job = queue._capture(f.agent, f.states, set(), None)
            self.assertNotIn('samples', pickle.loads(job['blob']))
            self.assertIn('sensor.a', job['history'])
            self.assertGreater(job['charge'], len(job['blob'])*8)
        finally:
            f.tearDown()


class TimestampReuseTests(unittest.TestCase):
    def test_timezone_aware_cache_is_bounded_and_naive_timestamps_remain_local(self):
        from datetime import datetime
        _parse_aware_ts.cache_clear()
        for value in ('2026-10-17T12:00:00Z','2026-10-17T12:00:00+02:00',
                      '2026-10-17T12:00:00-05:30','2026-10-17T12:00:00'):
            expected = datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
            self.assertEqual(parse_ts(value), expected)
            self.assertEqual(parse_ts(value), expected)
        self.assertEqual(_parse_aware_ts.cache_info().hits, 3)
        self.assertEqual(_parse_aware_ts.cache_info().maxsize,32768)
        self.assertIsNone(parse_ts('not a date'))


if __name__ == '__main__':
    unittest.main()
