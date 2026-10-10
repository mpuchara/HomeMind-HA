"""Promotion snapshots must retain proof without overriding metrics' periodic writes."""
import copy
import json
import unittest
from unittest.mock import patch, Mock

import support
import context_tournament_promotion as promotion
from telemetry import Telemetry
import test_context_tournament_metrics as metrics
import test_context_tournament_promotion as integration


class PromotionSnapshots170Tests(unittest.TestCase):
    def case(self):
        case = metrics.ContextTournamentMetricIntegrationTests('test_runtime_row_contains_incremental_value_contract')
        case.setUp()
        self.addCleanup(case.tearDown)
        promotion.install_promotion(case.service)
        return case

    def observe(self, case, now, value='off', unavailable=False, origin='external'):
        states = copy.deepcopy(case.states)
        states[case.target]['state'] = value
        if unavailable:
            states[case.challenger]['state'] = 'unavailable'
        case.engine.runtime[case.agent['id']]['last_change_origin'] = origin
        with patch('context_tournament.time.time', return_value=now), patch(
                'context_tournament_metrics.time.time', return_value=now):
            return case.service.observe_shadow(case.agent, states, {case.target, case.challenger})

    def model(self, case):
        return case.service._load_shadow_model(case.agent['id'], case.challenger, 2)

    def disk(self, case):
        case.service._flush_shadow_models()
        with case.store.conn() as c:
            return json.loads(c.execute('SELECT model_json FROM context_tournament_shadow '
                                       'WHERE agent_id=? AND challenger_entity=?',
                                       (case.agent['id'], case.challenger)).fetchone()[0])

    def test_quiet_edges_keep_all_live_availability_but_do_not_queue_large_snapshots(self):
        case = self.case()
        self.observe(case, 1000)
        self.disk(case)
        queued = case.service.shadow_persistence_snapshot()['queued']
        for i in range(1, 60):
            self.observe(case, 1000 + i, unavailable=i % 2 == 0)
        self.assertEqual(case.service.shadow_persistence_snapshot()['queued'], queued)
        model = self.model(case)
        self.assertEqual(model['observation_opportunities'], 60)
        self.assertEqual(model['available_observations'], 31)
        self.assertEqual(model['samples'], 0)
        self.observe(case, 1060)
        saved = self.disk(case)
        self.assertEqual(saved['observation_opportunities'], 61)
        self.assertEqual(saved['available_observations'], 32)

    def test_each_short_on_and_off_label_persists_after_promotion_absorbs_it(self):
        case = self.case()
        for now, value, samples in [(1000, 'off', 0), (1001, 'on', 1), (1002, 'off', 2)]:
            result = self.observe(case, now, value)
            saved = self.disk(case)
            self.assertEqual(saved['samples'], samples)
            self.assertEqual(saved['promotion_seen_samples'], samples)
            self.assertEqual(saved['promotion_window_samples'], samples)
            self.assertEqual(result['scored'], int(samples > 0))
        self.assertEqual(saved['promotion_window_class_totals'], [1, 1])

    def test_own_command_is_not_new_promotion_proof(self):
        case = self.case()
        self.observe(case, 1000)
        self.disk(case)
        queued = case.service.shadow_persistence_snapshot()['queued']
        result = self.observe(case, 1001, 'on', origin='own_command')
        self.assertEqual(result['scored'], 0)
        self.assertEqual(self.model(case)['promotion_seen_samples'], 0)
        self.assertEqual(case.service.shadow_persistence_snapshot()['queued'], queued)

    def test_empty_elapsed_window_is_saved_and_breaks_win_streak(self):
        case = self.case()
        self.observe(case, 1000)
        model = self.model(case)
        model['promotion_consecutive_wins'] = 2
        self.observe(case, model['promotion_window_end_ts'])
        saved = self.disk(case)
        self.assertEqual(saved['promotion_completed_windows'], 1)
        self.assertEqual(saved['promotion_consecutive_wins'], 0)
        self.assertEqual(len(saved['promotion_window_history']), 1)
        self.assertFalse(saved['promotion_window_history'][0]['win'])

    def test_initialization_and_changed_window_configuration_are_saved(self):
        case = self.case()
        self.observe(case, 1000)
        self.assertEqual(self.disk(case)['promotion_epoch_version'], promotion.PROMOTION_EPOCH_VERSION)
        with patch.dict(promotion.OPTIONS, {'context_tournament_evaluation_hours': 2}):
            self.observe(case, 1001)
            self.assertEqual(self.disk(case)['promotion_window_hours'], 2)

    def test_continuous_error_evidence_is_saved_without_reinterpreting_classes(self):
        case = integration.PromotionIntegrationTests('test_ready_challenger_is_promoted_without_exceeding_fast_limit')
        case.setUp()
        self.addCleanup(case.tearDown)
        case.agent.update(min_value=0, max_value=100, deadband=50, target_property='brightness')
        model = case.service._model
        with patch.dict(promotion.OPTIONS, {'context_tournament_enabled': False}):
            model.pop('promotion_epoch_version')
            case.service.observe_shadow(case.agent, {}, set())
            model.update(samples=41, last_scored_ts=1000, active_abs_error_sum=20, shadow_abs_error_sum=5)
            save = Mock(wraps=case.service._save_shadow_model)
            case.service._save_shadow_model = save
            case.service.observe_shadow(case.agent, {}, set())
            self.assertEqual(save.call_count, 1)
            self.assertEqual(model['promotion_seen_samples'], 41)
            self.assertEqual(model['promotion_window_active_abs_error_sum'], 20)
            self.assertEqual(model['promotion_window_shadow_abs_error_sum'], 5)

    def test_promotion_is_still_checked_when_cooldown_expires_without_a_new_label(self):
        case = integration.PromotionIntegrationTests('test_ready_challenger_is_promoted_without_exceeding_fast_limit')
        case.setUp()
        self.addCleanup(case.tearDown)
        # Enough completed windows, but the previous promotion blocks it for 10s.
        now = case.service._model['promotion_window_start_ts']
        with case.store.conn() as c:
            c.execute('INSERT INTO context_tournament_promotions(agent_id,last_promotion_ts,updated_ts) '
                      'VALUES(?,?,?)', (case.agent['id'], now - 24 * 3600 + 10, now))
        with patch('context_tournament_promotion.time.time', return_value=now):
            case.service.observe_shadow(case.agent, {}, set())
        self.assertNotIn(case.challenger, case.policy.schema.entities)
        with patch('context_tournament_promotion.time.time', return_value=now + 11):
            case.service.observe_shadow(case.agent, {}, set())
        self.assertIn(case.challenger, case.policy.schema.entities)
        self.assertIsNotNone(case.service.promotion_status(case.agent)['last_promotion_ts'])

    def test_count_only_telemetry_does_not_allocate_fake_latency_samples(self):
        telemetry = Telemetry()
        telemetry.inc('context_promotion_snapshot_skipped')
        telemetry.inc('context_promotion_snapshot_skipped')
        result = telemetry.snapshot()
        self.assertEqual(result['counts']['context_promotion_snapshot_skipped'], 2)
        self.assertNotIn('context_promotion_snapshot_skipped', result['metrics'])

    def test_paired_benchmark_checks_every_live_model_and_final_durable_json(self):
        from tools.benchmark_promotion_snapshots import run
        result = run(passes=8, label_every=4, width=8, rows=8)
        self.assertTrue(result['every_live_result_and_model_parity'])
        self.assertTrue(result['final_durable_json_parity'])
        for pair in result['series']:
            self.assertLess(pair['current']['snapshots'], pair['previous']['snapshots'])


if __name__ == '__main__':
    unittest.main()
