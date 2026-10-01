import time
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

import support
import engine as engine_module
import executor as executor_module
from engine import Engine
from intent import ActionIntent
from storage import Store

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'adaptive_ai' / 'src'


class ForbiddenLock:
    def __enter__(self):
        raise AssertionError('Shadow validation must not wait on engine.lock')

    def __exit__(self, *_args):
        return False


class EventDecisionHotpath118Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'test.db')
        self.patches = [
            patch.object(engine_module, 'STORE', self.store),
            patch.object(executor_module, 'STORE', self.store),
        ]
        for item in self.patches:
            item.start()
        self.engine = Engine()

    def tearDown(self):
        patch.stopall()
        for name in ('control_workers', 'poll_worker', 'registry_worker', 'housekeeping_worker'):
            worker = getattr(self.engine, name, None)
            if worker is not None:
                worker.shutdown(wait=False, cancel_futures=True)
        self.temp.cleanup()

    def _qualified_shadow_agent(self, **overrides):
        row = support.agent(
            id=overrides.pop('id', 'a1'),
            name=overrides.pop('name', 'A1'),
            target_entity=overrides.pop('target_entity', 'light.kitchen'),
            input_entities=overrides.pop('input_entities', ['binary_sensor.motion']),
            mode='shadow',
            **overrides,
        )
        created = self.store.create_agent(row)
        detail = {'balanced': True, 'counts': {'samples': 80, 'correct': 80, 'per_action': {
            '0': {'samples': 40, 'correct': 40}, '1': {'samples': 40, 'correct': 40}}}}
        self.store.set_training_state(created['id'], 'qualified', score=1.0, samples=80, detail=detail)
        self.store.update_agent(created['id'], {'mode': 'shadow'})
        return self.store.get_agent_config(created['id'])

    def test_inflight_retry_preserves_real_dependency_entity(self):
        agent = self._qualified_shadow_agent()
        self.engine.state_map = {
            agent['target_entity']: support.state(agent['target_entity'], 'off'),
            'binary_sensor.motion': support.state('binary_sensor.motion', 'off'),
        }
        self.engine.context.configure(self.engine.state_map)
        self.engine._refresh_agent_index(force=True)
        future = Future()
        self.engine.in_flight[agent['target_entity']] = future
        self.engine.wake_event.clear()
        self.engine.process(self.engine.state_map, {'binary_sensor.motion'})
        future.set_result(None)
        self.assertIn('binary_sensor.motion', self.engine.dirty_entities)
        self.assertNotIn(agent['target_entity'], self.engine.dirty_entities)
        self.assertTrue(self.engine.wake_event.is_set())

    def test_paused_shadow_snapshot_is_lock_free_and_observation_only(self):
        agent = self._qualified_shadow_agent()
        self.engine.state_map = {agent['target_entity']: support.state(agent['target_entity'], 'off')}
        self.engine.context.configure(self.engine.state_map)
        model = self.engine.policy(agent)
        self.store.save_model(agent['id'], model.serialize())
        self.store.set_training_state(agent['id'], 'paused', score=.70, samples=80,
                                      detail={'balanced': True}, shadow_after_completion=True)
        paused = self.store.get_agent_config(agent['id'])
        self.assertEqual(paused['mode'], 'shadow')
        self.assertEqual(paused['training_state'], 'paused')
        intent = ActionIntent.create(
            agent_id=paused['id'], target_entity=paused['target_entity'],
            target_property=paused['target_property'], desired_value=1, confidence=.9,
            support=.8, novelty=.1, prediction_horizon=3, created_at=time.time(), ttl=2,
            policy_version=model.VERSION, model_revision=model.model_revision,
            context_revision=self.engine.context.home.revision, target_revision=0,
            policy_head=min(model.heads), reason='test', context_dependencies=(),
        )
        service = patch.object(executor_module.HA, 'service', return_value=[]).start()
        self.engine.lock = ForbiddenLock()
        result = self.engine.executor.submit(intent, agent_snapshot=paused)
        self.assertEqual(result['status'], 'SHADOW')
        service.assert_not_called()

    def test_timer_scheduler_marks_synthetic_target_as_non_event(self):
        source = (SRC / 'engine.py').read_text(encoding='utf-8')
        self.assertIn('self.process(state_map, due_targets, event_driven=False)', source)
        self.assertIn('if event_driven else {}', source)

    def test_event_metric_is_decision_ready_and_executor_is_separate(self):
        source = (SRC / 'engine.py').read_text(encoding='utf-8')
        self.assertIn("TELEMETRY.observe('event_to_decision'", source)
        self.assertIn('metric_semantics="ha_ws_receive_to_decision_ready"', source)
        self.assertIn("TELEMETRY.observe('decision_to_executor'", source)
        self.assertIn('"executor_result"', source)

    def test_scheduler_fallback_never_uses_global_timestamp_for_timer_pass(self):
        source = (SRC / 'engine.py').read_text(encoding='utf-8')
        self.assertIn('if changed_entities and not pass_has_event_timestamps else None', source)
        self.assertIn('self.pending_target_changes', source)

    def test_overview_event_latency_is_recent_only_with_sample_count(self):
        source = (SRC / 'static' / 'p0.js').read_text(encoding='utf-8')
        self.assertIn('metrics?.event_to_decision', source)
        self.assertIn('recentCount=Number(latency.recent_count||0)', source)
        self.assertIn('waiting · 0 samples', source)
        self.assertNotIn('latency.p95_ms', source)

    def test_debug_contract_describes_new_latency_boundaries(self):
        source = (SRC / 'runtime_debug_log.py').read_text(encoding='utf-8')
        self.assertIn('CONTRACT_VERSION = 4', source)
        self.assertIn('"event_to_decision_semantics"', source)
        self.assertIn('"decision_to_executor_metric": True', source)
        self.assertIn('"resubmit_preserves_trigger_entities": True', source)


if __name__ == '__main__':
    unittest.main()
