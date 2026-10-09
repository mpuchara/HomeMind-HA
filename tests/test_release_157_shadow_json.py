"""Shadow persistence data equivalence, body reuse and current metadata boundaries."""
import copy
import json
import math
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import support
from context_tournament import ContextTournament
from model_encoding_cache import ModelEncodingCache
from policy_backend import model_checksum, verify_model_checksum
from runtime_debug_log import RuntimeDebugLogService
import shadow_model_json as codec
from storage import Store


class ShadowJSON157Tests(unittest.TestCase):
    def setUp(self):
        self.cache = ModelEncodingCache(min_bytes=0)
        for module in ['policy_backend', 'shadow_model_json']:
            patched = patch(module + '.MODEL_ENCODING_CACHE', self.cache)
            patched.start(); self.addCleanup(patched.stop)
        self.policy = dict(model_revision='epoch', dims=128,
                           schema={'entities': ['sensor.radar']},
                           heads={'1': {'a': [[1., 2.]], 'b': [[.2, .3]],
                                         'rows': [{'x': {'1': .5}, 'weight': .8}]}})
        self.policy['model_checksum'] = model_checksum(self.policy)
        self.model = dict(version=1, action_count=2, candidate_policy=self.policy, samples=3, counts={'0': [1, 2]},
                          candidate_data_version={'champion_revision': 'epoch'})

    def assert_same_data(self, raw):
        actual = json.loads(codec.shadow_model_json(raw))
        expected = json.loads(json.dumps(raw, separators=(',', ':'), sort_keys=True))
        self.assertEqual(actual, expected)
        return actual

    def test_reuses_checksum_body_entry_without_duplicate_policy_cache(self):
        before = self.cache.snapshot()
        restored = self.assert_same_data(self.model)
        self.assertEqual(self.cache.snapshot()['entries'], before['entries'])
        self.assertEqual(self.cache.snapshot()['hits'], before['hits'] + 1)
        self.assertTrue(verify_model_checksum(restored['candidate_policy']))

    def test_changing_outer_counts_reuses_body_and_preserves_every_count(self):
        before = self.cache.snapshot()
        for count in range(20):
            self.model['samples'] = count
            self.model['counts']['0'][0] = count
            self.assert_same_data(self.model)
        self.assertEqual(self.cache.snapshot()['entries'], before['entries'])
        self.assertEqual(self.cache.snapshot()['hits'], before['hits'] + 20)

    def test_ignored_bookkeeping_is_restored_fresh_on_every_hit(self):
        entries = None
        for n in range(4):
            self.policy['_history_watermark'] = n
            self.policy['_benchmark_counts'] = {'rows': [n, n + 1]}
            restored = self.assert_same_data(self.model)
            self.assertEqual(restored['candidate_policy']['_history_watermark'], n)
            self.assertTrue(verify_model_checksum(restored['candidate_policy']))
            if entries is None:
                entries = self.cache.snapshot()['entries']
            self.assertEqual(self.cache.snapshot()['entries'], entries)

    def test_changed_expected_checksum_is_not_restored_from_cache(self):
        self.assert_same_data(self.model)
        self.policy['model_checksum'] = 'changed-checksum'
        restored = self.assert_same_data(self.model)
        self.assertEqual(restored['candidate_policy']['model_checksum'], 'changed-checksum')
        self.assertFalse(verify_model_checksum(restored['candidate_policy']))

    def test_weight_mutation_is_persisted_and_old_checksum_remains_invalid(self):
        self.assert_same_data(self.model)
        self.policy['heads']['1']['b'][0][0] = .999
        restored = self.assert_same_data(self.model)
        self.assertEqual(restored['candidate_policy']['heads']['1']['b'][0][0], .999)
        self.assertFalse(verify_model_checksum(restored['candidate_policy']))

    def test_nested_underscore_and_checksum_fields_are_not_excluded(self):
        self.policy['schema']['_nested'] = {'model_checksum': [1, 2]}
        self.assert_same_data(self.model)
        self.policy['schema']['_nested']['model_checksum'].append(3)
        restored = self.assert_same_data(self.model)
        self.assertEqual(restored['candidate_policy']['schema']['_nested']['model_checksum'], [1, 2, 3])

    def test_legacy_without_candidate_or_checksum_uses_valid_json(self):
        self.assert_same_data({'version': 1, 'counts': {'1': [2, 3]}})
        del self.policy['model_checksum']
        self.assert_same_data(self.model)

    def test_empty_body_and_only_extra_fields_do_not_leave_trailing_comma(self):
        for policy in [{}, {'model_checksum': None}, {'_meta': [1], 'model_checksum': 'x'}]:
            self.assert_same_data({'candidate_policy': policy})

    def test_unicode_escape_and_signed_zero_preserve_json_data(self):
        self.policy['schema']['entities'] = ['łazienka\n\"\\\u0000', '\ud800']
        self.policy['zero'] = -0.
        encoded = codec.shadow_model_json(self.model)
        self.assertIn('-0.0', encoded)
        self.assert_same_data(self.model)

    def test_random_matrices_remain_checksum_valid_after_roundtrip(self):
        rng = random.Random(157)
        for _ in range(20):
            self.policy['heads']['1']['a'] = [[rng.uniform(-1e8, 1e8) for _ in range(32)] for _ in range(8)]
            self.policy['model_checksum'] = model_checksum(self.policy)
            restored = self.assert_same_data(self.model)
            self.assertTrue(verify_model_checksum(restored['candidate_policy']))

    def test_numeric_outer_keys_take_exact_legacy_path(self):
        model = {1: 'one', 2: {'two': [1, 2]}}
        self.assertEqual(codec.shadow_model_json(model), json.dumps(model, separators=(',', ':'), sort_keys=True))

    def test_nan_and_infinity_take_legacy_serialization_without_checksum_bypass(self):
        for value in [math.nan, math.inf, -math.inf]:
            self.policy['bad'] = value
            self.assertEqual(codec.shadow_model_json(self.model), json.dumps(self.model, separators=(',', ':'), sort_keys=True))
            with self.assertRaises(ValueError):
                verify_model_checksum(self.policy)

    def test_custom_objects_keep_legacy_type_error_even_after_checksum_encoder(self):
        self.policy['custom'] = object()
        # Checksum's historical default=str must never authorize Shadow JSON's encoding.
        model_checksum(self.policy)
        with self.assertRaises(TypeError):
            codec.shadow_model_json(self.model)

    def test_cycles_keep_legacy_failure_instead_of_truncating_content(self):
        self.policy['cycle'] = self.policy
        with self.assertRaises(ValueError):
            codec.shadow_model_json(self.model)

    def test_shadow_encoding_does_not_poison_strict_checksum_cache(self):
        before = self.policy['model_checksum']
        for _ in range(3): self.assert_same_data(self.model)
        self.assertEqual(model_checksum(self.policy), before)
        self.assertTrue(verify_model_checksum(self.policy))

    def test_shared_budget_stays_bounded_with_many_shadow_bodies(self):
        cache = ModelEncodingCache(max_bytes=800, max_entries=3, min_bytes=0)
        with patch('shadow_model_json.MODEL_ENCODING_CACHE', cache):
            for i in range(20):
                self.policy['model_revision'] = str(i)
                self.assert_same_data(self.model)
        self.assertLessEqual(cache.snapshot()['bytes'], 800)
        self.assertLessEqual(cache.snapshot()['entries'], 3)

    def test_real_store_flush_preserves_frozen_snapshot_and_restart_read(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'test.db')
            service = ContextTournament(store, SimpleNamespace())
            expected = copy.deepcopy(self.model)
            service._save_shadow_model('agent', 'sensor.challenger', self.model)
            self.model['samples'] = 100
            self.policy['heads']['1']['b'][0][0] = .999
            hits = self.cache.snapshot()['hits']
            self.assertEqual(service._flush_shadow_models(), 1)
            self.assertEqual(self.cache.snapshot()['hits'], hits + 1)
            with store.conn() as c:
                persisted = json.loads(c.execute('SELECT model_json FROM context_tournament_shadow').fetchone()[0])
            self.assertEqual(persisted, expected)
            self.assertTrue(verify_model_checksum(persisted['candidate_policy']))
            restarted = ContextTournament(store, SimpleNamespace())
            self.assertEqual(restarted._load_shadow_model('agent', 'sensor.challenger', 2), expected)
            self.assertEqual(service.shadow_persistence_snapshot()['pending'], 0)

    def test_legacy_string_snapshots_remain_literal_and_failed_batch_is_restored(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'test.db')
            service = ContextTournament(store, SimpleNamespace())
            literal = '{"samples":1,"version":1}'
            service._shadow_dirty[('agent', 'sensor.legacy')] = literal
            service._save_shadow_model('agent', 'sensor.new', self.model)
            with patch.object(store, 'conn', side_effect=RuntimeError('disk failed')):
                with self.assertRaises(RuntimeError): service._flush_shadow_models()
            self.assertEqual(service.shadow_persistence_snapshot()['pending'], 2)
            self.assertEqual(service._flush_shadow_models(), 2)
            with store.conn() as c:
                row = c.execute("SELECT model_json FROM context_tournament_shadow WHERE challenger_entity='sensor.legacy'").fetchone()[0]
            self.assertEqual(row, literal)

    def test_runtime_debug_exports_ram_shadow_encoding_stats(self):
        before = codec.snapshot()
        self.assert_same_data(self.model)
        payload = RuntimeDebugLogService(SimpleNamespace(STORE=None, ENGINE=None), None).export_payload()
        self.assertEqual(payload['shadow_json_encoding_cache']['calls'], before['calls'] + 1)
        self.assertEqual(payload['shadow_json_encoding_cache']['hits'], before['hits'] + 1)
        json.dumps(payload)
