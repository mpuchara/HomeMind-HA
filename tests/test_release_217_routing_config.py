import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_release_034_agent_hotpath as hotpath
import engine as engine_module


class RoutingConfigTests(unittest.TestCase):
    def test_queue_receipt_never_queries_candidate_episode_edge(self):
        from episode_evaluator_runtime import install_candidate
        receipt = {'deferred': True, 'queue_depth': 1}
        manager = SimpleNamespace(before_live_process=lambda *args: None,
            after_live_process=lambda *args: receipt,
            _comparison_summary=lambda *args: {})
        install_candidate(manager, SimpleNamespace())
        with patch('episode_evaluator_runtime._candidate_edge', side_effect=AssertionError('unexpected SQL')):
            self.assertIs(manager.after_live_process({'id': 'live', 'target_property': 'power'}, {}), receipt)
        observed = {'results': {'parent': {'desired': 1}, 'child': {'desired': 0}}}
        receipt.update(observed)
        with patch('episode_evaluator_runtime._candidate_edge', return_value=None) as read:
            manager.after_live_process({'id': 'live', 'target_property': 'power'}, {})
        read.assert_called_once_with(manager, 'live')

    def test_context_rebuild_reuses_config_but_not_dependencies_or_expiry(self):
        helper = hotpath.AgentHotPathTests()
        runtime = helper.make_engine()
        self.addCleanup(helper.doCleanups)
        configured = helper.configured_agent()
        store = hotpath.CountingStore([configured])
        dependencies = {'binary_sensor.old'}
        runtime.event_dependencies = lambda *args: set(dependencies)
        runtime.refresh_automation_context = lambda: None
        runtime.agent_index_ttl_seconds = 10.
        with patch.object(engine_module, 'STORE', store), patch.object(engine_module.time, 'monotonic') as clock:
            clock.return_value = 100.
            runtime._refresh_agent_index()
            self.assertEqual(store.list_calls, 1)
            dependencies.clear()
            dependencies.add('binary_sensor.new')
            runtime.context.registry_revision += 1
            clock.return_value = 109.
            runtime._refresh_agent_index()
            self.assertEqual(store.list_calls, 1)
            self.assertEqual(runtime.dependency_agents, {'binary_sensor.new': {configured['id']}})
            self.assertEqual(runtime._agent_config_index_at, 100.)
            clock.return_value = 110.
            runtime._refresh_agent_index()
            self.assertEqual(store.list_calls, 2)
            store.rows[configured['id']]['mode'] = 'paused'
            store._agent_index_revision += 1
            runtime._refresh_agent_index()
            self.assertEqual(store.list_calls, 3)
            self.assertEqual(runtime.agent_configs, {})
            runtime._refresh_agent_index(force=True)
            self.assertEqual(store.list_calls, 4)
            runtime.agent_index_at = 0.
            runtime._refresh_agent_index()
            self.assertEqual(store.list_calls, 5)


if __name__ == '__main__':
    unittest.main()
