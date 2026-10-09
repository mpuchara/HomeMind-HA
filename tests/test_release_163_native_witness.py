"""Native witness parity, rejection boundaries and mandatory warm integrity."""
import copy
import hashlib
import json
import math
import pickle
import random
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
import model_encoding_cache as encoding
from model_encoding_cache import ModelEncodingCache, builtin_witness
from policy_backend import _canonical, model_checksum, verify_model_checksum
from runtime_debug_log import RuntimeDebugLogService
from tools.benchmark_native_witness import previous_witness
import test_release_151_candidate_epoch as paired


class NativeWitness163Tests(unittest.TestCase):
    def test_exact_builtin_scalars_and_containers_keep_every_witness_byte(self):
        for value in [None, True, False, 1, -2, 2**200, .125, -0., 'łazienka',
                      [], {}, (), [1, .2, None], (1, 'a'), {'a': {'b': [3.]}}]:
            with self.subTest(value=repr(value)):
                self.assertEqual(builtin_witness(value), previous_witness(value))

    def test_random_nested_trees_match_frozen_previous_witness(self):
        rng = random.Random(163)
        def tree(depth):
            if not depth:
                return rng.choice([None, True, False, rng.randint(-2**80, 2**80),
                                   rng.uniform(-1e6, 1e6), 'łazienka\\n"'])
            values = [tree(depth-1) for _ in range(rng.randint(0, 4))]
            return rng.choice([values, tuple(values), {str(i): x for i, x in enumerate(values)}])
        for _ in range(100):
            value = tree(4)
            self.assertEqual(builtin_witness(value), previous_witness(value))

    def test_scalar_dict_keys_preserve_old_acceptance_and_bytes(self):
        for key in [None, True, 3, -0., 'text']:
            raw = {key: [1., 2.]}
            self.assertEqual(builtin_witness(raw), previous_witness(raw))

    def test_empty_and_nonempty_tuple_keys_never_enter_cache(self):
        for key in [(), ('key',), ((),), (1, ('nested',))]:
            for values in [[1.], [(), ('valid-value',)]]:
                raw = {key: values}
                self.assertIsNone(builtin_witness(raw))
                cache = ModelEncodingCache(min_bytes=0)
                with self.assertRaises(TypeError):
                    cache.encode(raw, _canonical)
                self.assertEqual(cache.snapshot()['entries'], 0)

    def test_native_non_json_types_reject_roots_nested_values_and_keys(self):
        for value in [b'', b'ab', bytearray(), bytearray(b'ab'),
                      set(), {1, 2}, frozenset(), frozenset([1, 2])]:
            for raw in [value, {'x': [value]}, (value,)]:
                self.assertIsNone(builtin_witness(raw))
            try:
                hash(value)
            except TypeError:
                continue
            self.assertIsNone(builtin_witness({value: 'key'}))

    def test_scalar_and_container_subclasses_never_run_reduction(self):
        for base, value in [(str, 's'), (int, 3), (float, .3), (dict, {}),
                            (list, []), (tuple, ()), (bytes, b'a'),
                            (set, set()), (frozenset, frozenset())]:
            def reject_reduce(self, protocol):
                raise AssertionError('custom reduction ran')
            cls = type('Custom'+base.__name__, (base,), {'__reduce_ex__': reject_reduce})
            obj = cls(value)
            self.assertIsNone(builtin_witness({'x': obj}))

    def test_custom_object_uses_encoder_without_reduce_or_decode(self):
        class Custom:
            def __reduce_ex__(self, protocol):
                raise AssertionError('custom reduction ran')
            def __str__(self):
                return 'custom'
        cache = ModelEncodingCache(min_bytes=0)
        raw = {'x': Custom()}
        with patch.object(encoding.pickle, 'loads', side_effect=AssertionError('decoded unsupported object')):
            self.assertEqual(cache.encode(raw, _canonical), _canonical(raw).encode())
        self.assertEqual(cache.snapshot()['bypasses'], 1)

    def test_globals_functions_classes_and_complex_values_are_rejected(self):
        for value in [len, lambda: None, object, float, type, 1j, range(3), slice(0, 1),
                      SimpleNamespace(a=1), memoryview(b'a')]:
            self.assertIsNone(builtin_witness({'x': value}))

    def test_pickle_buffers_cannot_escape_native_type_guard(self):
        for buffer in [b'', b'a', bytearray(), bytearray(b'a')]:
            value = pickle.PickleBuffer(buffer)
            try:
                self.assertIsNone(builtin_witness(value))
                self.assertIsNone(builtin_witness({'x': value}))
            finally:
                value.release()

    def test_numpy_values_keep_existing_json_fallback(self):
        import numpy as np
        cache = ModelEncodingCache(min_bytes=0)
        for value in [np.float64(.25), np.int64(3), np.array([1., 2.])]:
            raw = {'x': value}
            self.assertIsNone(builtin_witness(raw))
            with patch.object(encoding.pickle, 'loads', side_effect=AssertionError('numpy decoded')):
                self.assertEqual(cache.encode(raw, _canonical), _canonical(raw).encode())
        self.assertEqual(cache.snapshot()['entries'], 0)

    def test_metaclass_cannot_spoof_a_builtin_or_run_its_reducer(self):
        class Meta(type):
            def __eq__(self, other):
                return True
            def __hash__(self):
                raise AssertionError('custom type hash ran')
        class Custom(metaclass=Meta):
            def __reduce_ex__(self, protocol):
                raise AssertionError('custom reduction ran')
        self.assertIsNone(builtin_witness({'x': Custom()}))

    def test_shared_references_cycles_and_unicode_keep_exact_internal_bytes(self):
        shared = ['łazienka', -0., 2**200]
        raw = {'a': shared, 'b': shared, 'tuple': (shared,)}
        self.assertEqual(builtin_witness(raw), previous_witness(raw))
        raw['cycle'] = raw
        self.assertEqual(builtin_witness(raw), previous_witness(raw))
        cache = ModelEncodingCache(min_bytes=0)
        with self.assertRaises(ValueError):
            cache.encode(raw, _canonical)
        self.assertEqual(cache.snapshot()['entries'], 0)

    def test_nan_inf_fail_without_publishing_an_encoding(self):
        cache = ModelEncodingCache(min_bytes=0)
        for value in [math.nan, math.inf, -math.inf]:
            with self.assertRaises(ValueError):
                cache.encode({'x': value}, _canonical)
        self.assertEqual(cache.snapshot()['entries'], 0)

    def test_warm_verification_still_hashes_all_canonical_bytes_and_detects_mutation(self):
        cache = ModelEncodingCache(min_bytes=0)
        raw = {'model_revision': 'fixed', 'weights': [[1., 2.]], 'schema': {'entities': ['sensor.x']}}
        canonical = _canonical(raw).encode()
        raw['model_checksum'] = hashlib.sha256(canonical).hexdigest()
        with patch('policy_backend.MODEL_ENCODING_CACHE', cache):
            self.assertTrue(verify_model_checksum(raw))
            with patch('policy_backend.hashlib.sha256', wraps=hashlib.sha256) as sha:
                for _ in range(3):
                    self.assertTrue(verify_model_checksum(raw))
                self.assertEqual(sum(call.args == (canonical,) for call in sha.call_args_list), 3)
            raw['weights'][0][1] = 3.
            self.assertFalse(verify_model_checksum(raw))
            raw['weights'][0][1] = 2.
            self.assertTrue(verify_model_checksum(raw))

    def test_cache_snapshots_match_old_encoder_across_mutations_and_aliases(self):
        caches = [ModelEncodingCache(min_bytes=0), ModelEncodingCache(min_bytes=0)]
        raw = {'x': [[1., 2.]], 'label': 'łazienka', 'tuple': ('v',)}
        for n in range(40):
            raw['x'][0][n%2] = n/7
            raw['optional'] = [True, 1, -0., None][n%4]
            outputs = []
            for cache, witness in zip(caches, [previous_witness, builtin_witness]):
                with patch.object(encoding, 'builtin_witness', witness):
                    outputs.append(cache.encode(raw, _canonical))
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(hashlib.sha256(outputs[1]).hexdigest(), hashlib.sha256(_canonical(raw).encode()).hexdigest())
        self.assertEqual(caches[0].snapshot(), caches[1].snapshot())

    def test_encoder_still_receives_detached_snapshot_after_capture(self):
        cache = ModelEncodingCache(min_bytes=0)
        raw = {'weights': [1., 2.]}
        def encode(frozen):
            raw['weights'][0] = 99.
            return _canonical(frozen)
        self.assertEqual(cache.encode(raw, encode), b'{"weights":[1.0,2.0]}')
        self.assertEqual(cache.encode(raw, _canonical), b'{"weights":[99.0,2.0]}')


class CandidatePhase163Tests(unittest.TestCase):
    def case(self):
        case = paired.CandidateEpoch151Tests('test_champion_decay_preserves_candidate_evidence_and_instance')
        self.addCleanup(case.doCleanups)
        case.setUp()
        return case

    def test_real_candidate_features_then_prediction_emit_separate_phases(self):
        case = self.case()
        with patch('training_phase_metrics.TELEMETRY') as telemetry:
            case.observe()
        names = [call.args[0] for call in telemetry.observe.call_args_list]
        self.assertEqual(names, ['context_candidate_features', 'context_candidate_predict'])

    def test_feature_failure_is_measured_and_prevents_prediction(self):
        case = self.case()
        candidate = case.created[0]
        debug = Mock(enabled=True)
        with patch.object(candidate, 'features', side_effect=RuntimeError('features failed')), patch.object(
                candidate, 'predict') as predict, patch('training_phase_metrics.RUNTIME_DEBUG', debug):
            case.observe()
        predict.assert_not_called()
        self.assertEqual(debug.end.call_args.kwargs['status'], 'error')
        self.assertEqual(case.service._model['candidate_blocked_reason'], 'candidate_prediction:RuntimeError')

    def test_prediction_failure_is_measured_without_escaping_shadow_guard(self):
        case = self.case()
        debug = Mock(enabled=True)
        with patch.object(case.created[0], 'predict', side_effect=RuntimeError('predict failed')), patch(
                'training_phase_metrics.RUNTIME_DEBUG', debug):
            case.observe()
        self.assertEqual([call.kwargs['status'] for call in debug.end.call_args_list], ['ok', 'error'])
        self.assertEqual(case.service._model['candidate_blocked_reason'], 'candidate_prediction:RuntimeError')

    def test_runtime_export_declares_both_prediction_metrics(self):
        payload = RuntimeDebugLogService(SimpleNamespace(STORE=None, ENGINE=None), None).export_payload()
        self.assertEqual(payload['notes']['context_candidate_prediction_metrics'],
                         ['context_candidate_features', 'context_candidate_predict'])
