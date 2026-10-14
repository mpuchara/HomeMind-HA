"""Fresh SHA with read-only RAM sources and unchanged learner/persistence data."""
import copy
import gc
import hashlib
import json
import pickle
from pathlib import Path
import unittest
from unittest.mock import patch

import support
from policy import MultiHorizonPolicy
from policy_backend import model_checksum, verify_model_checksum
import sealed_policy_snapshot as sealed
import context_tournament_policy_candidate as candidates


class SealedPolicy192Tests(unittest.TestCase):
    def setUp(self):
        self.budget = sealed.SnapshotBudget()
        p = patch.object(sealed, '_BUDGET', self.budget)
        p.start()
        self.addCleanup(p.stop)
        self.raw = {'model_revision': 'same', 'schema': {'entities': ['sensor.radar']},
                    'heads': {'1': {'b': [[.2, .3]], 'rows': [{'x': {'5': .12}, 'weight': 1.}]}},
                    '_history_watermark': {'ts': 10.}}
        self.raw['model_checksum'] = model_checksum(self.raw)

    def test_warm_verification_hashes_canonical_bytes_every_time(self):
        payload = sealed.seal_policy(self.raw)
        canonical = payload._canonical_bytes
        with patch.object(sealed.hashlib, 'sha256', wraps=hashlib.sha256) as sha:
            with patch.object(candidates, 'verify_model_checksum', side_effect=AssertionError('mutable validation path')):
                for _ in range(4):
                    self.assertTrue(candidates._verify_candidate_checksum(payload))
        self.assertEqual(sum(call.args == (canonical,) for call in sha.call_args_list), 4)
        self.assertEqual(self.budget.snapshot()['checks'], 4)
        self.assertEqual(model_checksum(payload), self.raw['model_checksum'])

    def test_source_is_detached_and_all_normal_nested_mutations_are_refused(self):
        payload = sealed.seal_policy(self.raw)
        mutations = [lambda: payload.update(model_revision='bad'), lambda: payload.pop('heads'),
                     lambda: payload.__setitem__('model_checksum', 'bad'),
                     lambda: payload['heads']['1']['b'][0].__setitem__(0, 9.),
                     lambda: payload['schema']['entities'].append('sensor.bad'),
                     lambda: payload['schema']['entities'].sort(),
                     lambda: payload['heads']['1']['rows'][0]['x'].clear(),
                     lambda: setattr(payload, '_canonical_bytes', b'bad')]
        for mutate in mutations:
            with self.assertRaises(TypeError):
                mutate()
        self.raw['heads']['1']['b'][0][0] = 9.
        self.assertFalse(verify_model_checksum(self.raw))
        self.assertTrue(payload.verify())
        self.assertEqual(payload['heads']['1']['b'][0][0], .2)

    def test_deepcopy_pickle_and_json_produce_original_plain_data(self):
        payload = sealed.seal_policy(self.raw)
        for data in (copy.deepcopy(payload), pickle.loads(pickle.dumps(payload, protocol=5)), json.loads(json.dumps(payload))):
            self.assertEqual(data, self.raw)
            self.assertIs(type(data), dict)
            self.assertIs(type(data['heads']), dict)
            self.assertIs(type(data['heads']['1']['b']), list)
            data['heads']['1']['b'][0][0] = .5
        self.assertTrue(payload.verify())

    def test_corrupt_new_source_does_not_get_admitted(self):
        self.raw['schema']['entities'].append('corruption')
        with self.assertRaises(ValueError):
            sealed.seal_policy(self.raw)
        self.assertEqual(self.budget.snapshot()['entries'], 0)

    def test_replacement_same_revision_binds_a_different_verified_source(self):
        before = sealed.seal_policy(self.raw)
        changed = copy.deepcopy(before)
        changed['heads']['1']['b'][0][0] = .8
        changed['model_checksum'] = model_checksum(changed)
        after = sealed.seal_policy(changed)
        self.assertNotEqual(candidates._candidate_payload_version(before), candidates._candidate_payload_version(after))
        self.assertTrue(before.verify())
        self.assertTrue(after.verify())
        self.assertEqual(after['heads']['1']['b'][0][0], .8)

    def test_quota_is_bounded_and_released_without_retaining_sources(self):
        budget = sealed.SnapshotBudget(max_bytes=1024, max_entries=1, max_policy_bytes=1024)
        with patch.object(sealed, '_BUDGET', budget):
            first = sealed.seal_policy(self.raw)
            self.assertIsNotNone(first)
            self.assertIsNone(sealed.seal_policy(self.raw))
            self.assertLessEqual(budget.snapshot()['bytes'], 1024)
            del first
            gc.collect()
            self.assertEqual((budget.snapshot()['entries'], budget.snapshot()['bytes']), (0, 0))
            self.assertIsNotNone(sealed.seal_policy(self.raw))

    def test_oversized_or_custom_or_unversioned_payload_uses_existing_path(self):
        with patch.object(sealed, '_BUDGET', sealed.SnapshotBudget(max_policy_bytes=1)):
            self.assertIsNone(sealed.seal_policy(self.raw))
        self.assertIsNone(sealed.seal_policy({'no_checksum': [1.]}))
        class Custom:
            def __reduce__(self):
                raise AssertionError('custom reduction')
            def __deepcopy__(self, memo):
                raise AssertionError('custom deep copy')
        self.raw['custom'] = Custom()
        self.assertIsNone(sealed.seal_policy(self.raw))

    def test_real_policy_clone_updates_without_touching_sealed_parent(self):
        agent = support.agent()
        parent = MultiHorizonPolicy(agent, {}, {}, set())
        for i in range(8):
            parent.update(1, i % 2, {0: 1., 5: i / 8.}, 1.)
        raw = parent.serialize()
        source = sealed.seal_policy(raw)
        self.assertIsNotNone(source)
        clone = MultiHorizonPolicy(agent, {}, {}, set(), model=copy.deepcopy(source))
        for x in ({0: 1., 5: 0.}, {0: 1., 5: .5}, {0: 1., 5: 1.}):
            self.assertEqual(parent.predict(x), clone.predict(x))
        clone.update(1, 1, {0: 1., 5: .6}, 1.)
        self.assertEqual(copy.deepcopy(source), raw)
        self.assertTrue(source.verify())

    def test_new_source_published_during_admission_is_not_overwritten(self):
        import sys
        import tempfile
        sys.path.insert(0, str(support.ROOT / 'tools'))
        from benchmark_shadow_context import Fixture, training_contract, STAMP
        from storage import Store
        import context_tournament_promotion as promotion
        with tempfile.TemporaryDirectory() as folder, training_contract(), patch('time.time', return_value=STAMP), \
             patch.object(promotion, '_choose_schema_after_promotion', promotion._choose_schema_after_promotion), \
             patch.object(promotion, '_migrate_schema', promotion._migrate_schema):
            fixture = Fixture(Store(Path(folder) / 'race.db'))
            fixture.observe()
            original_seal = candidates.seal_policy
            def replace_during_seal(raw):
                model = next(m for m in fixture.service._shadow_models.values() if m['candidate_policy'] is raw)
                replacement = copy.deepcopy(raw)
                replacement['published_later'] = True
                replacement['model_checksum'] = model_checksum(replacement)
                model['candidate_policy'] = replacement
                return original_seal(raw)
            with patch.object(candidates, 'seal_policy', replace_during_seal):
                fixture.observe()
            for model in fixture.service._shadow_models.values():
                self.assertTrue(model['candidate_policy']['published_later'])
                self.assertIs(type(model['candidate_policy']), dict)
                self.assertTrue(verify_model_checksum(model['candidate_policy']))
            fixture.observe()
            for model in fixture.service._shadow_models.values():
                self.assertTrue(model['candidate_policy']['published_later'])
                self.assertIs(type(model['candidate_policy']), sealed.SealedPolicy)

    def test_malformed_non_mapping_source_keeps_existing_rebuild_behavior(self):
        import sys
        import tempfile
        sys.path.insert(0, str(support.ROOT / 'tools'))
        from benchmark_shadow_context import Fixture, training_contract, STAMP
        from storage import Store
        import context_tournament_promotion as promotion
        with tempfile.TemporaryDirectory() as folder, training_contract(), patch('time.time', return_value=STAMP), \
             patch.object(promotion, '_choose_schema_after_promotion', promotion._choose_schema_after_promotion), \
             patch.object(promotion, '_migrate_schema', promotion._migrate_schema):
            fixture = Fixture(Store(Path(folder) / 'invalid.db'))
            fixture.observe()
            for model in fixture.service._shadow_models.values():
                model['candidate_policy'] = 'invalid source'
            fixture.observe()
            for model in fixture.service._shadow_models.values():
                self.assertIsInstance(model['candidate_policy'], dict)
                self.assertTrue(verify_model_checksum(model['candidate_policy']))


if __name__ == '__main__':
    unittest.main()
