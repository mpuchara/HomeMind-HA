"""Storage-paused diagnostics must leave Live runnable and break future pairing."""
import threading
import unittest
from unittest.mock import Mock, patch

import support
import test_context_tournament_shadow as fixture
from episode_evaluator import EpisodeEvaluator
from episode_evaluator_runtime import install_tournament


class TournamentStorageTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ContextTournamentShadowTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.engine._inference_tls = threading.local()
        self.f.engine._inference_tls.state_revision = 1
        self.limit = self.f.store.storage_budget.db_limit

    def pause(self):
        b = self.f.store.storage_budget
        with self.f.store.conn() as c:
            b.observe_pages(c)
        b.db_limit = b.snapshot()['database_used_bytes']
        self.assertEqual(b.sample()['reason'], 'database_budget')

    def recover(self):
        b = self.f.store.storage_budget
        b.db_limit = self.limit
        self.assertEqual(b.sample()['level'], 'normal')

    def test_every_live_pass_returns_but_no_optional_observer_runs_during_pressure(self):
        self.pause()
        f = self.f
        with patch.object(f.service, 'observe_shadow', side_effect=AssertionError('expensive fit')) as observe:
            with patch.object(f.store.storage_budget, 'sample', side_effect=AssertionError('hot-path disk sample')):
                for _ in range(32):
                    self.assertEqual(f._process('on'), 'production-result')
        observe.assert_not_called()
        self.assertEqual(f.engine.runtime[f.agent['id']]['last_prediction'], 0.)
        stats = f.service.shadow_persistence_snapshot()['storage_admission']
        self.assertEqual(stats['skipped'], 32)
        self.assertEqual(stats['reason'], 'database_budget')

    def test_first_recovered_observation_cannot_score_pre_gap_prediction(self):
        f = self.f
        f._process('off')
        self.pause()
        f._process('on')
        self.assertEqual(f.service._shadow_runtime[f.agent['id']]['pending'], {})
        self.recover()
        f._process('on')
        model = f.service._load_shadow_model(f.agent['id'], f.challenger, 2)
        self.assertEqual(model['samples'], 0)
        f._process('off')
        self.assertEqual(model['samples'], 1)
        self.assertEqual(f.service.shadow_persistence_snapshot()['storage_admission']['recoveries'], 1)

    def test_handlers_run_only_at_pause_and_resume_and_old_predictions_stay_last_known(self):
        f = self.f
        f._process('off')
        predictions = dict(f.service._shadow_runtime[f.agent['id']]['predictions'])
        callback = Mock()
        f.service.register_observation_gap_handler('probe', callback)
        self.pause()
        f._process('on'); f._process('off')
        callback.assert_called_once_with(f.agent['id'], 'pause')
        self.assertEqual(f.service._shadow_runtime[f.agent['id']]['predictions'], predictions)
        self.recover(); f._process('on')
        self.assertEqual(callback.call_args.args, (f.agent['id'], 'resume'))

    def test_direct_explicit_observer_contract_is_preserved(self):
        self.pause()
        f = self.f
        del f.engine._inference_tls.state_revision
        with patch.object(f.service, 'observe_shadow', wraps=f.service.observe_shadow) as observe:
            self.assertEqual(f._process('off'), 'production-result')
        self.assertEqual(observe.call_count, 1)

    def test_invalidation_error_never_raises_through_live_and_resume_retries(self):
        f = self.f
        callback = Mock(side_effect=ValueError('bad reset'))
        f.service.register_observation_gap_handler('probe', callback)
        self.pause()
        with patch.object(f.service, 'report_runtime_error') as report:
            self.assertEqual(f._process('on'), 'production-result')
            report.assert_called_once()
        self.recover()
        with patch.object(f.service, 'report_runtime_error'):
            self.assertEqual(f._process('on'), 'production-result')
        self.assertEqual(callback.call_count, 2)
        callback.side_effect = None
        self.assertEqual(f._process('on'), 'production-result')
        self.assertEqual(callback.call_count, 3)

    def test_paused_agent_tracking_is_bounded(self):
        self.pause()
        for index in range(300):
            self.assertTrue(self.f.service.storage_observation_gate(dict(self.f.agent, id=str(index))))
        self.assertEqual(self.f.service.shadow_persistence_snapshot()['storage_admission']['paused_agents'], 256)

    def test_episode_evaluator_does_not_complete_an_episode_across_storage_gap(self):
        f = self.f
        f.engine.active_prediction = 1.
        evaluator = EpisodeEvaluator(f.store)
        install_tournament(f.service, evaluator)
        f._process('off')
        self.pause(); f._process('on'); self.recover()
        f._process('on')
        with f.store.conn() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM episode_evaluator_episodes').fetchone()[0], 0)

    def test_all_storage_pause_reasons_gate_optional_work(self):
        f = self.f
        for reason in ('database_budget', 'wal_budget', 'disk_reserve', 'storage_sample_failed'):
            with patch('context_tournament.storage_status', return_value=dict(level='paused', reason=reason)):
                with patch.object(f.service, 'observe_shadow', side_effect=AssertionError('observer ran')):
                    self.assertEqual(f._process('on'), 'production-result')


class CandidatePoolGapTests(unittest.TestCase):
    def setUp(self):
        from contextlib import ExitStack
        from pathlib import Path
        import tempfile
        from storage import Store
        from tools.benchmark_shadow_context import Fixture, STAMP, training_contract
        import context_tournament_promotion as promotion
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(promotion, '_choose_schema_after_promotion', promotion._choose_schema_after_promotion))
        self.stack.enter_context(patch.object(promotion, '_migrate_schema', promotion._migrate_schema))
        self.stack.enter_context(training_contract())
        self.stack.enter_context(patch('time.time', return_value=STAMP))
        self.f = Fixture(Store(Path(folder) / 'pool.db'))
        self.f.observe()
        self.budget = self.f.service.store.storage_budget
        self.limit = self.budget.db_limit

    def gap(self):
        with self.f.service.store.conn() as c:
            self.budget.observe_pages(c)
        self.budget.db_limit = self.budget.snapshot()['database_used_bytes']
        self.budget.sample()
        self.assertTrue(self.f.service.storage_observation_gate(self.f.agent))

    def test_pool_gap_clears_lag_baselines_and_preserves_models_and_accepted_samples(self):
        f = self.f
        before = {r['entity_id']: r for r in f.service.observed_pool(f.agent['id'])}
        models = {k: v.get('candidate_policy') for k, v in f.service._shadow_models.items()}
        self.assertTrue(any(row['last_value'] is not None for row in before.values()))
        self.gap()
        after = {r['entity_id']: r for r in f.service.observed_pool(f.agent['id'])}
        self.assertEqual(set(before), set(after))
        for eid, row in after.items():
            self.assertIsNone(row['last_value'])
            self.assertIsNone(row['last_change_ts'])
            self.assertEqual(row['screening_samples'], before[eid]['screening_samples'])
        for key, source in models.items():
            self.assertIs(f.service._shadow_models[key].get('candidate_policy'), source)

    def test_recovered_pool_transition_does_not_train_from_the_old_context(self):
        f = self.f
        self.gap()
        self.budget.db_limit = self.limit; self.budget.sample()
        f.states[f.agent['target_entity']]['state'] = 'on'
        self.assertFalse(f.service.storage_observation_gate(f.agent))
        f.observe()
        for challenger in f.challengers:
            model = f.service._load_shadow_model(f.agent['id'], challenger, 2)
            self.assertEqual(model.get('candidate_training_samples'), 0)
        self.assertTrue(all(row['screening_samples'] == 0 for row in f.service.observed_pool(f.agent['id'])))


if __name__ == '__main__':
    unittest.main()
