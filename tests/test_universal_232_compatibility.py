"""Integration gates for the U01-U04c port onto the pinned 0.14.232 runtime."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import support
import engine as engine_module
from storage import Store
from universal_action_runtime import UniversalActionObserver
from test_device_agents import _Engine, _agent, _light_state
from device_agents import DeviceAgentService
from test_universal_action_runtime import Perception


class Production232CompatibilityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name) / 'compat.db')

    def test_enabled_observer_keeps_rest_archive_off_poll_thread(self):
        with patch.object(engine_module, 'STORE', self.store):
            engine = engine_module.Engine()
        for name in ('control_workers', 'poll_worker', 'housekeeping_worker'):
            pool = getattr(engine, name)
            self.addCleanup(lambda pool=pool: pool.shutdown(wait=True, cancel_futures=True))
        engine.universal_observer = Mock()
        current = support.state('binary_sensor.motion', 'on')
        with patch.object(engine_module.HA, 'states', return_value=[current]), \
                patch.object(engine, 'flush_archive', side_effect=AssertionError('poll cannot persist')), \
                patch.object(engine, '_queue_archive_state'):
            result = engine.refresh_states()
        snapshot = engine.universal_observer.submit_snapshot.call_args.args[0]
        self.assertEqual(snapshot, result)
        self.assertIsNot(snapshot, result)
        self.assertEqual(engine.state_map['binary_sensor.motion'], current)

    def test_current_deviceagent_mask_is_used_without_dispatch(self):
        state = _light_state('light.kitchen')
        runtime = _Engine(
            {'light.kitchen': {'entity_id': 'light.kitchen', 'device_id': 'lamp', 'area_id': 'kitchen'}},
            {'lamp': {'id': 'lamp', 'area_id': 'kitchen'}}, {'light.kitchen': state},
        )
        service = DeviceAgentService(runtime, self.store)
        agent = _agent(self.store, 'light.kitchen', 'power')
        runtime.executor = SimpleNamespace(device_agents=service, submit=Mock())
        observer = UniversalActionObserver(runtime, Perception())
        result = observer._evaluate_one(agent, state)
        self.assertEqual(result['evaluated'], 2)
        self.assertFalse(result['control_eligible'])
        self.assertFalse(result['trained'])
        service.claim_control_resources(agent)
        sibling = _agent(self.store, 'light.kitchen', 'brightness_pct', maximum=100)
        result = observer._evaluate_one(sibling, state)
        self.assertTrue(result['blocked'])
        self.assertEqual(result['evaluated'], 0)
        runtime.executor.submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
