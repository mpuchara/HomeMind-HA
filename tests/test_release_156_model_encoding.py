"""Legacy SHA parity and integrity under mutable, concurrent and unusual payloads."""
import copy
import hashlib
import json
import math
import random
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import support
from model_encoding_cache import ModelEncodingCache
from policy_backend import _canonical, model_checksum, verify_model_checksum
from runtime_debug_log import RuntimeDebugLogService


def legacy(raw):
    clean = {key: value for key, value in raw.items()
             if key != 'model_checksum' and not str(key).startswith('_')}
    return hashlib.sha256(_canonical(clean).encode('utf-8')).hexdigest()


class ModelEncoding156Tests(unittest.TestCase):
    def setUp(self):
        self.cache = ModelEncodingCache(min_bytes=0)
        patched = patch('policy_backend.MODEL_ENCODING_CACHE', self.cache)
        patched.start()
        self.addCleanup(patched.stop)
        self.raw = dict(model_revision='same', dims=128, schema={'entities': ['sensor.stationary']},
                        heads={'1': {'a': [[1., 2.]], 'b': [[.3, .4]],
                                     'classifier': {'rows': [{'x': {'1': .75}, 'weight': .25}]}}})
        self.raw['model_checksum'] = legacy(self.raw)

    def test_warm_verification_keeps_legacy_digest_and_recomputes_full_sha(self):
        self.assertTrue(verify_model_checksum(self.raw))
        canonical = _canonical({k: v for k, v in self.raw.items() if k != 'model_checksum'}).encode()
        with patch('policy_backend.hashlib.sha256', wraps=hashlib.sha256) as sha:
            for _ in range(3):
                self.assertTrue(verify_model_checksum(self.raw))
            self.raw['model_checksum'] = '0' * 64
            self.assertFalse(verify_model_checksum(self.raw))
            self.assertEqual(sum(c.args == (canonical,) for c in sha.call_args_list), 4)
        self.assertEqual(self.cache.snapshot()['hits'], 4)

    def test_deep_weight_mutation_with_same_revision_and_checksum_is_detected(self):
        self.assertTrue(verify_model_checksum(self.raw))
        self.raw['heads']['1']['b'][0][1] += .0001
        self.assertFalse(verify_model_checksum(self.raw))

    def test_classifier_training_row_mutation_is_detected(self):
        self.assertTrue(verify_model_checksum(self.raw))
        self.raw['heads']['1']['classifier']['rows'][0]['x']['1'] = .5
        self.assertFalse(verify_model_checksum(self.raw))

    def test_schema_mutation_is_detected_without_a_revision_change(self):
        self.assertTrue(verify_model_checksum(self.raw))
        self.raw['schema']['entities'].append('sensor.distance')
        self.assertFalse(verify_model_checksum(self.raw))

    def test_top_level_bookkeeping_is_ignored_but_nested_underscore_is_checked(self):
        before = model_checksum(self.raw)
        self.raw['_history_watermark'] = {'mutable': []}
        self.assertEqual(model_checksum(self.raw), before)
        self.raw['schema']['_nested'] = 'checked'
        self.assertNotEqual(model_checksum(self.raw), before)

    def test_mutation_reversal_restores_original_exact_digest(self):
        before = model_checksum(self.raw)
        self.raw['schema']['entities'].append('temporary')
        self.assertNotEqual(model_checksum(self.raw), before)
        self.raw['schema']['entities'].pop()
        self.assertEqual(model_checksum(self.raw), before)

    def test_float_signed_zero_and_bool_int_are_not_equal_witnesses(self):
        for a, b in [(0., -0.), (True, 1), (1, 1.)]:
            self.assertEqual(a, b)
            first = model_checksum({'value': a})
            second = model_checksum({'value': b})
            self.assertNotEqual(first, second)
            self.assertEqual(second, legacy({'value': b}))

    def test_random_finite_matrices_preserve_sha_after_json_disk_roundtrip(self):
        rng = random.Random(156)
        for _ in range(30):
            raw = {'weights': [[rng.uniform(-1e8, 1e8) for _ in range(32)] for _ in range(8)],
                   'text': 'łazienka\n\"\\\u0000', 'large_int': 2**200, 'optional': None}
            expected = legacy(raw)
            self.assertEqual(model_checksum(raw), expected)
            disk = json.loads(json.dumps(raw))
            self.assertEqual(model_checksum(disk), expected)

    def test_reordering_and_shared_references_only_cause_safe_cache_misses(self):
        values = [1., 2., 3.]
        raw = {'a': values, 'b': values}
        before = model_checksum(raw)
        self.assertEqual(model_checksum({'b': list(values), 'a': list(values)}), before)

    def test_nan_inf_and_cycles_are_never_published_into_cache(self):
        for value in [math.nan, math.inf, -math.inf]:
            with self.assertRaises(ValueError):
                model_checksum({'x': value})
        raw = {}; raw['cycle'] = raw
        with self.assertRaises(ValueError):
            model_checksum(raw)
        self.assertEqual(self.cache.snapshot()['entries'], 0)

    def test_unknown_objects_bypass_pickle_and_follow_legacy_default_str(self):
        class Custom:
            value = 'first'
            def __reduce__(self):
                raise AssertionError('custom object was pickled')
            def __str__(self):
                return self.value
        obj = Custom()
        before = model_checksum({'x': obj})
        self.assertEqual(before, legacy({'x': obj}))
        obj.value = 'second'
        self.assertNotEqual(model_checksum({'x': obj}), before)
        self.assertEqual(self.cache.snapshot()['bypasses'], 2)

    def test_scalar_subclasses_and_container_subclasses_use_legacy_encoder(self):
        class Scalar(float): pass
        class Container(dict): pass
        for value in [Scalar(.125), Container(x=[1., 2.])]:
            raw = {'x': value}
            self.assertEqual(model_checksum(raw), legacy(raw))
        self.assertEqual(self.cache.snapshot()['entries'], 0)

    def test_encoder_uses_detached_witness_and_cannot_poison_cache_on_mutation(self):
        raw = {'x': [1., 2.]}
        def mutate_then_encode(frozen):
            raw['x'][0] = 99.
            return _canonical(frozen)
        encoded = self.cache.encode(raw, mutate_then_encode)
        self.assertEqual(encoded, b'{"x":[1.0,2.0]}')
        self.assertEqual(self.cache.encode(raw, _canonical), b'{"x":[99.0,2.0]}')
        raw['x'][0] = 1.
        self.assertEqual(self.cache.encode(raw, _canonical), encoded)

    def test_fingerprint_collision_cannot_reuse_other_content(self):
        fake = SimpleNamespace(digest=lambda: b'forced-collision')
        with patch('model_encoding_cache.hashlib.sha256', return_value=fake):
            a = self.cache.encode({'x': [1.]}, _canonical)
            b = self.cache.encode({'x': [2.]}, _canonical)
        self.assertNotEqual(a, b)
        self.assertEqual(b, b'{"x":[2.0]}')

    def test_entry_and_byte_limits_include_both_witness_and_encoding(self):
        cache = ModelEncodingCache(max_bytes=160, max_entries=2, min_bytes=0)
        for i in range(20):
            raw = {'x': [i, i + 1, i + 2]}
            self.assertEqual(cache.encode(raw, _canonical), _canonical(raw).encode())
            status = cache.snapshot()
            self.assertLessEqual(status['bytes'], 160)
            self.assertLessEqual(status['entries'], 2)
        oversized = {'x': ['large' * 200]}
        self.assertEqual(cache.encode(oversized, _canonical), _canonical(oversized).encode())
        self.assertTrue(all(isinstance(witness, bytes) and isinstance(encoded, bytes) for witness, encoded in cache.entries.values()))

    def test_zero_budget_and_small_payload_bypass_preserve_digest(self):
        for cache in [ModelEncodingCache(max_bytes=0, min_bytes=0), ModelEncodingCache()]:
            encoded = cache.encode({'x': 1}, _canonical)
            self.assertEqual(encoded, b'{"x":1}')
            self.assertEqual(cache.snapshot()['entries'], 0)

    def test_concurrent_encoders_keep_bounded_bytes_and_exact_results(self):
        cache = ModelEncodingCache(max_bytes=2000, max_entries=4, min_bytes=0)
        errors = []
        def exercise(n):
            try:
                for i in range(20):
                    raw = {'revision': n, 'values': [i / 3., n / 7.]}
                    self.assertEqual(cache.encode(raw, _canonical), _canonical(raw).encode())
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=exercise, args=(i,)) for i in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertLessEqual(cache.snapshot()['bytes'], 2000)
        self.assertLessEqual(cache.snapshot()['entries'], 4)

    def test_slow_encoder_does_not_hold_ram_cache_lock(self):
        entered, release, snap_done = threading.Event(), threading.Event(), threading.Event()
        def slow(raw):
            entered.set()
            release.wait(3)
            return _canonical(raw)
        worker = threading.Thread(target=lambda: self.cache.encode({'x': [1.]}, slow))
        worker.start()
        self.assertTrue(entered.wait(1))
        snapshotter = threading.Thread(target=lambda: (self.cache.snapshot(), snap_done.set()))
        snapshotter.start()
        try:
            self.assertTrue(snap_done.wait(1))
        finally:
            release.set()
            worker.join(3)
            snapshotter.join(3)

    def test_runtime_export_reports_ram_statistics(self):
        with patch('model_encoding_cache.MODEL_ENCODING_CACHE', self.cache):
            model_checksum(self.raw)
            model_checksum(self.raw)
            payload = RuntimeDebugLogService(SimpleNamespace(STORE=None, ENGINE=None), None).export_payload()
        self.assertEqual(payload['model_encoding_cache']['hits'], 1)
        json.dumps(payload)
