import json
import math
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from support import *
import storage
from policy_backend import PolicyBackend
from policy_full_ridge import FullRidgeLinUCBBackend
from policy_backend_benchmark import (
    _FullRidgeBenchmarkBackend, _learn_episode, _metrics, run_benchmark,
    semantic_feature_indices, split_future, trial_records_to_episodes,
)
from policy_backend_shadow import PolicyBackendShadowService
from trial_knowledge import ensure_trial_tables


class FullRidgeBackendTests(unittest.TestCase):
    def test_backend_implements_contract_and_versioned_roundtrip(self):
        backend = FullRidgeLinUCBBackend(actions=[0, 1], horizons=[1], feature_indices=[0, 1, 2])
        self.assertIsInstance(backend, PolicyBackend)
        for _ in range(5):
            backend.update(1, 1, {0: 1, 1: 1, 2: 1}, 1.0)
        raw = backend.serialize()
        self.assertEqual(raw['backend'], 'full_ridge_linucb')
        self.assertEqual(raw['version'], 1)
        restored = FullRidgeLinUCBBackend.deserialize(raw)
        self.assertEqual(restored.serialize()['feature_indices'], [0, 1, 2])
        self.assertEqual(restored.predict({0: 1, 1: 1, 2: 1})[0]['index'], 1)

    def test_correlated_and_duplicate_features_remain_numerically_stable(self):
        backend = FullRidgeLinUCBBackend(actions=[0, 1], horizons=[1], feature_indices=[0, 1, 2, 3], ridge=.5)
        for i in range(80):
            x = float((i % 7) - 3) / 3.0
            features = {0: 1.0, 1: x, 2: x, 3: x + 1e-8}
            backend.update(1, int(x > 0), features, 1.0)
        chosen, confidence, arms, *_ = backend.predict({0: 1, 1: .5, 2: .5, 3: .50000001})
        self.assertIn(chosen['index'], (0, 1))
        self.assertTrue(math.isfinite(confidence))
        self.assertTrue(all(math.isfinite(a['mean']) and math.isfinite(a['uncertainty']) for a in arms))

    def test_pair_only_interaction_can_be_learned_when_interaction_slot_exists(self):
        backend = FullRidgeLinUCBBackend(actions=[0, 1], horizons=[1], feature_indices=[0, 1, 2, 3])
        rows = [(-1, -1, 1), (-1, 1, 0), (1, -1, 0), (1, 1, 1)] * 12
        for a, b, target in rows:
            features = {0: 1, 1: a, 2: b, 3: a * b}
            backend.update(1, target, features, 1.0)
        for a, b, target in [(-1, -1, 1), (-1, 1, 0), (1, -1, 0), (1, 1, 1)]:
            chosen = backend.predict({0: 1, 1: a, 2: b, 3: a * b})[0]['index']
            self.assertEqual(chosen, target)

    def test_missing_features_are_zero_not_unknown_rewards(self):
        backend = FullRidgeLinUCBBackend(actions=[0, 1], horizons=[1], feature_indices=[0, 1, 2])
        backend.update(1, 1, {0: 1, 1: 1}, 1.0)
        chosen = backend.predict({0: 1})[0]
        self.assertTrue(math.isfinite(chosen['mean']))


class BenchmarkContractTests(unittest.TestCase):
    def episode(self, i, *, kind='demonstration', target=None, executed=None, reward=None,
                source='manual', prop=(.5, .5), drift=False):
        base = time.time() - 10000
        x = 1.0 if (i % 4 in (2, 3)) else -1.0
        if drift and i >= 30:
            x = -x
        target = int(x > 0) if target is None else int(target)
        return {
            'id': f'e{i}', 'timestamp': base + i,
            'features': {0: 1.0, 1: x, 2: x, 3: x * x},
            'allowed_actions': [0, 1], 'kind': kind,
            'demonstration_action': target if kind == 'demonstration' else None,
            'demonstration_source': source if kind == 'demonstration' else None,
            'executed_action': executed if kind == 'bandit' else None,
            'reward': reward if kind == 'bandit' else None,
            'action_propensities': {0: prop[0], 1: prop[1]} if kind == 'bandit' else {},
        }

    def test_feature_selection_and_hyperparameters_do_not_see_future_test(self):
        episodes = [self.episode(i) for i in range(40)]
        train, validation, test = split_future(episodes)
        self.assertLess(train[-1]['timestamp'], validation[0]['timestamp'])
        self.assertLess(validation[-1]['timestamp'], test[0]['timestamp'])
        selected = semantic_feature_indices(train, max_features=3)
        changed_future = [dict(x) for x in episodes]
        for row in changed_future[-8:]:
            row['features'] = {0: 1, 99: 1000}
        selected_again = semantic_feature_indices(split_future(changed_future)[0], max_features=3)
        self.assertEqual(selected, selected_again)

    def test_bandit_learning_updates_only_executed_action(self):
        backend = _FullRidgeBenchmarkBackend(actions=[0, 1], feature_indices=[0, 1])
        row = self.episode(1, kind='bandit', executed=1, reward=-1.0)
        _learn_episode(backend, row)
        head = backend.backend.heads[1]
        self.assertEqual(head.counts[0], 0)
        self.assertGreater(head.counts[1], 0)
        self.assertLess(head.reward_sums[1], 0)

    def test_automation_replay_is_not_scored_as_demonstration_quality(self):
        backend = _FullRidgeBenchmarkBackend(actions=[0, 1], feature_indices=[0, 1, 2, 3])
        rows = [self.episode(i, source='automation') for i in range(6)]
        self.assertEqual(_metrics(backend, rows)['demonstration_samples'], 0)

    def test_off_policy_report_refuses_action_without_propensity_coverage(self):
        backend = _FullRidgeBenchmarkBackend(actions=[0, 1], feature_indices=[0, 1, 2, 3])
        for _ in range(10):
            backend.update(1, {0: 1, 1: 1}, 1.0)
        row = self.episode(20, kind='bandit', executed=0, reward=0.5, prop=(1.0, 0.0))
        row['features'] = {0: 1, 1: 1}
        metrics = _metrics(backend, [row])
        self.assertFalse(metrics['bandit_supported'])
        self.assertIsNone(metrics['bandit_ips_reward'])
        self.assertEqual(metrics['bandit_unsupported_rows'], 1)

    def test_benchmark_reports_cost_calibration_and_keeps_diagonal_default(self):
        episodes = [self.episode(i, drift=True) for i in range(36)]
        for i in range(0, 36, 4):
            target = episodes[i]['demonstration_action']
            episodes[i] = self.episode(i, kind='bandit', executed=target, reward=1.0, prop=(.5, .5), drift=True)
        result = run_benchmark(episodes, [0, 1], max_features=4)
        self.assertEqual(result['default_backend'], 'diagonal_linucb')
        self.assertFalse(result['automatic_backend_switch'])
        self.assertFalse(result['nonlinear_backend_added'])
        self.assertIn(result['candidate_status'], ('keep_diagonal_default', 'shadow_candidate_supported'))
        self.assertIn('mean_inference_us', result['baseline']['future_test'])
        self.assertIn('reward_calibration_mae', result['candidate']['future_test'])
        self.assertGreater(result['candidate']['serialized_bytes'], 0)
        self.assertTrue(result['small_correction_curve']['candidate'])


class TrialAndShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = storage.Store(Path(self.temp.name) / 'backend.db')
        ensure_trial_tables(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_trial_records_preserve_action_set_propensities_and_only_executed_reward(self):
        now = time.time()
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO experiment_trial_records
                   (trial_id,record_version,owner_agent_id,hypothesis_json,context_json,model_versions_json,
                    action_set_json,assigned_action_json,propensity,baseline_json,dispatch_json,ack_json,
                    outcome_sources_json,episode_result_json,termination_reason,reward,status,created_ts,updated_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ('t1', 1, 'a1', '{}', json.dumps({'policy_features': {'0': 1, '1': .4}, 'horizon': 1}), '{}',
                 json.dumps([{'index': 0, 'value': 0, 'propensity': .25}, {'index': 1, 'value': 1, 'propensity': .75}]),
                 json.dumps({'index': 1, 'value': 1}), .75, '{}', '{}', '{}', '{}', '{}', 'confirmed', .6,
                 'labelled', now, now),
            )
        rows = trial_records_to_episodes(self.store, 'a1')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['executed_action'], 1)
        self.assertEqual(rows[0]['reward'], .6)
        self.assertEqual(rows[0]['action_propensities'], {0: .25, 1: .75})
        self.assertNotIn('counterfactual_rewards', rows[0])

    def test_shadow_is_flagged_non_controlling_and_reward_updates_logged_action_only(self):
        service = PolicyBackendShadowService(self.store, enabled=True)
        agent = {'id': 'a1', 'target_entity': 'light.kitchen'}
        policy = SimpleNamespace(actions=[0.0, 1.0], horizons=[1])
        result = service.observe_decision(agent, policy, {0: 1, 1: .8}, {0: ['bias'], 1: ['sensor:value']}, [0, 1])
        self.assertEqual(result['evaluation'], 'shadow_only_no_dispatch')
        pending = {'policy_head': 1, 'action_index': 0, 'features': {0: 1, 1: .8}}
        service.observe_reward(agent, pending, -1.0, 'test')
        head = service.backends['a1'].heads[1]
        self.assertGreater(head.counts[0], 0)
        self.assertEqual(head.counts[1], 0)
        diag = service.diagnostics('a1')
        self.assertFalse(diag['dispatch_capability'])
        self.assertEqual(diag['mode'], 'shadow')


if __name__ == '__main__':
    unittest.main()
