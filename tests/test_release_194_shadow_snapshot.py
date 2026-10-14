"""Owned immutable source fragments retain frozen metadata and durable ordering."""
import copy
import gc
import json
import pickle
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import support
from storage import Store
from policy import MultiHorizonPolicy
from policy_backend import model_checksum, verify_model_checksum
from context_tournament import ContextTournament
from shadow_model_json import shadow_model_json
from shadow_model_snapshot import freeze_shadow_model, snapshot_json, ShadowModelSnapshot
import sealed_policy_snapshot as sealed


class ShadowSnapshot194Tests(unittest.TestCase):
    def setUp(self):
        self.budget = sealed.SnapshotBudget()
        p = patch.object(sealed, '_BUDGET', self.budget)
        p.start()
        self.addCleanup(p.stop)

    def source(self, **extra):
        raw = {'model_revision': 'same', 'heads': {'1': {'b': [[.2, .3]]}},
               'schema': {'entities': ['sensor.łazienka']}, '_history_watermark': {'ts': 12.}, **extra}
        raw['model_checksum'] = model_checksum(raw)
        return sealed.seal_policy(raw)

    def model(self, source):
        return {'version': 1, 'action_count': 2, 'samples': 1,
                'counts': {'0:4': [0, 1]}, 'candidate_policy': source}

    def test_snapshot_detaches_metadata_and_owns_exact_old_source(self):
        source = self.source()
        model = self.model(source)
        expected = copy.deepcopy(model)
        frozen = freeze_shadow_model(model)
        self.assertIs(type(frozen), ShadowModelSnapshot)
        self.assertIs(frozen.source, source)
        model['counts']['0:4'][1] = 99
        model['candidate_policy'] = self.source(new_source=True)
        self.assertEqual(json.loads(snapshot_json(frozen)), expected)
        self.assertEqual(snapshot_json(frozen), shadow_model_json(expected))

    def test_fast_snapshot_and_json_do_not_visit_large_source_tree(self):
        source = self.source(rows=[[i / 17.] * 32 for i in range(128)])
        model = self.model(source)
        with patch.object(sealed, '_thaw', side_effect=AssertionError('source traversal')):
            with patch('shadow_model_snapshot.shadow_model_json', side_effect=AssertionError('source encoding')):
                raw = freeze_shadow_model(model)
                actual = json.loads(snapshot_json(raw))
        self.assertLess(len(raw.metadata_bytes), 1024)
        self.assertEqual(actual['candidate_policy']['heads']['1']['b'][0], [.2, .3])
        self.assertTrue(verify_model_checksum(actual['candidate_policy']))

    def test_metadata_serialization_failure_keeps_previous_pending_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            service = ContextTournament(Store(Path(folder) / 'test.db'), SimpleNamespace())
            model = self.model(self.source())
            service._save_shadow_model('a', 'sensor.x', model)
            previous = service._shadow_dirty[('a', 'sensor.x')]
            with patch('shadow_model_snapshot.pickle.dumps', side_effect=ValueError('snapshot failed')):
                with self.assertRaises(ValueError): service._save_shadow_model('a', 'sensor.x', model)
            self.assertIs(service._shadow_dirty[('a', 'sensor.x')], previous)

    def test_failed_commit_retains_newer_snapshot_and_restart_exact_model(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'test.db')
            service = ContextTournament(store, SimpleNamespace())
            model = self.model(self.source())
            service._save_shadow_model('a', 'sensor.x', model)
            @contextmanager
            def failed():
                model['samples'] = 2
                model['counts']['0:4'][1] = 2
                service._save_shadow_model('a', 'sensor.x', model)
                raise RuntimeError('disk failed')
                yield
            with patch.object(store, 'conn', failed):
                with self.assertRaises(RuntimeError): service._flush_shadow_models()
            self.assertEqual(json.loads(snapshot_json(service._shadow_dirty[('a', 'sensor.x')]))['samples'], 2)
            self.assertEqual(service._flush_shadow_models(), 1)
            restarted = ContextTournament(store, SimpleNamespace())
            loaded = restarted._load_shadow_model('a', 'sensor.x', 2)
            self.assertEqual(loaded, copy.deepcopy(model))
            self.assertTrue(verify_model_checksum(loaded['candidate_policy']))
            self.assertEqual(service.shadow_persistence_snapshot()['shared_source_snapshots'], 2)

    def test_optional_fragment_budget_does_not_remove_canonical_fast_path(self):
        raw = {'x': 1, '_large_bookkeeping': 'x' * 1800}
        raw['model_checksum'] = model_checksum(raw)
        budget = sealed.SnapshotBudget(max_bytes=1000, max_policy_bytes=500)
        with patch.object(sealed, '_BUDGET', budget):
            source = sealed.seal_policy(raw)
            self.assertIs(type(source), sealed.SealedPolicy)
            self.assertIsNone(source._persistence_json)
            self.assertTrue(source.verify())
            self.assertEqual(budget.snapshot()['persistence_fallbacks'], 1)
            self.assertEqual(budget.snapshot()['fallbacks'], 0)
            frozen = freeze_shadow_model(self.model(source))
            self.assertIs(type(frozen), bytes)
            self.assertEqual(json.loads(snapshot_json(frozen))['candidate_policy'], raw)

    def test_fragment_charge_stays_owned_until_last_snapshot_is_discarded(self):
        source = self.source()
        snap = freeze_shadow_model(self.model(source))
        stats = self.budget.snapshot()
        self.assertEqual(stats['persistence_json_bytes'], sys.getsizeof(source._persistence_json))
        self.assertEqual(stats['bytes'], stats['canonical_bytes'] + stats['persistence_json_bytes'])
        del source
        gc.collect()
        self.assertEqual(self.budget.snapshot()['entries'], 1)
        del snap
        gc.collect()
        self.assertEqual((self.budget.snapshot()['bytes'], self.budget.snapshot()['entries']), (0, 0))

    def test_empty_source_unicode_and_ignored_fields_keep_legacy_json_data(self):
        for body in ({}, {'label': 'łazienka', 'value': -0.}):
            raw = dict(body, _ignored={'valid': True})
            raw['model_checksum'] = model_checksum(raw)
            source = sealed.seal_policy(raw)
            model = self.model(source)
            self.assertEqual(snapshot_json(freeze_shadow_model(model)), shadow_model_json(copy.deepcopy(model)))
            self.assertEqual(json.loads(snapshot_json(freeze_shadow_model(model)))['candidate_policy'], raw)

    def test_real_policy_predictions_survive_persistence_and_remain_mutable(self):
        agent = support.agent()
        parent = MultiHorizonPolicy(agent, {}, {}, set())
        for i in range(8): parent.update(1, i % 2, {0: 1., 5: i / 8.}, 1.)
        raw = parent.serialize()
        source = sealed.seal_policy(raw)
        restored = json.loads(snapshot_json(freeze_shadow_model(self.model(source))))['candidate_policy']
        clone = MultiHorizonPolicy(agent, {}, {}, set(), model=restored)
        for x in ({0: 1., 5: 0.}, {0: 1., 5: .5}, {0: 1., 5: 1.}):
            self.assertEqual(parent.predict(x), clone.predict(x))
        clone.update(1, 1, {0: 1., 5: .6}, 1.)
        self.assertEqual(copy.deepcopy(source), raw)
        self.assertTrue(source.verify())


if __name__ == '__main__': unittest.main()
