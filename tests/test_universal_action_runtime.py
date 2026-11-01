"""U04c observer-only action projections over real DeviceAgent mask-shaped data."""
from types import SimpleNamespace
from unittest import mock
import threading
import time
import unittest

import numpy as np

import settings
from universal_action_runtime import UniversalActionObserver, install, _sample_proposals


class Perception:
    def __init__(self):
        self.available = True
        self.calls = 0

    def snapshot_areas(self, areas):
        self.calls += 1
        if not self.available:
            return None
        return {
            "home": np.zeros(32, dtype=np.float32),
            "rooms": {str(area): np.zeros(32, dtype=np.float32) for area in areas},
            "timestamp": time.time(),
        }


class FakeDeviceAgent:
    def __init__(self, *, deny=False):
        self.deny = deny
        self.mask_calls = 0
        self.descriptor_calls = 0

    def descriptor(self, agent):
        self.descriptor_calls += 1
        target = agent["target_entity"]
        prop = agent["target_property"]
        return {
            "logical_device_id": "ha-device:1",
            "device_property": prop,
            "physical_target_property": prop,
            "area_id": "kitchen",
            "resource_keys": ["device:ha-device:1"],
            "autonomy_allowed": True,
            "properties": [{"entity_id": target, "property": prop}],
        }

    def legal_action_mask(self, agent, state, values, *, now=None):
        self.mask_calls += 1
        rows = []
        for value in values:
            rows.append({
                "requested": value,
                "value": value,
                "legal": not self.deny,
                "reason": "manual hold" if self.deny else None,
            })
        return {
            "resource_keys": ["device:ha-device:1"],
            "actions": rows,
            "manual_priority": True,
            "fallback": "abstain" if self.deny else "policy_choice_within_mask",
        }


def make_engine(*, devices=1, deny=False):
    service = FakeDeviceAgent(deny=deny)
    agents = {}
    states = {}
    routing = {"binary_sensor.motion": set()}
    for i in range(devices):
        agent_id = f"agent-{i}"
        eid = f"light.lamp_{i}"
        agents[agent_id] = {
            "id": agent_id, "target_entity": eid,
            "target_property": "power", "min_value": 0.0, "max_value": 1.0,
        }
        states[eid] = {"entity_id": eid, "state": "off", "attributes": {}}
        routing["binary_sensor.motion"].add(agent_id)
    return SimpleNamespace(
        lock=threading.RLock(), dependency_agents=routing,
        agent_configs=agents, state_map=states,
        executor=SimpleNamespace(device_agents=service),
        universal_observer=None,
        universal_action_observer=None,
    ), service


def eventually(condition, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("action observer did not reach expected state")


class DynamicActionObserverTests(unittest.TestCase):
    def test_default_disabled_uses_no_worker(self):
        engine, _ = make_engine()
        with mock.patch.dict(settings.OPTIONS, {
            "universal_observer_enabled": False,
            "universal_action_observer_enabled": False,
        }):
            self.assertIsNone(install(engine))
            self.assertIsNone(engine.universal_action_observer)

    def test_enabled_requires_running_perception_option(self):
        engine, _ = make_engine()
        engine.universal_observer = Perception()
        with mock.patch.dict(settings.OPTIONS, {
            "universal_observer_enabled": False,
            "universal_action_observer_enabled": True,
        }):
            self.assertIsNone(install(engine))

    def test_dynamic_number_of_targets_uses_shared_scorer(self):
        for devices in (1, 5, 25):
            with self.subTest(devices=devices):
                engine, service = make_engine(devices=devices)
                perception = Perception()
                observer = UniversalActionObserver(
                    engine, perception, max_agents=32, min_interval=0,
                )
                observer.start()
                try:
                    self.assertTrue(observer.submit_changed(("binary_sensor.motion",)))
                    eventually(lambda: observer.diagnostics()["stats"]["agents_evaluated"] == devices)
                    result = observer.diagnostics()
                    self.assertEqual(result["stats"]["actions_evaluated"], devices*2)
                    self.assertEqual(len(result["last_examples"]), min(devices, 32))
                    self.assertEqual(result["physical_actions"], 0)
                    self.assertFalse(result["control_eligible"])
                    self.assertFalse(result["trained"])
                    self.assertEqual(service.mask_calls, devices)
                finally:
                    observer.stop()
                self.assertFalse(observer.is_alive())

    def test_denied_mask_produces_no_actions_or_projections(self):
        engine, service = make_engine(deny=True)
        observer = UniversalActionObserver(engine, Perception(), min_interval=0)
        observer.start()
        try:
            observer.submit_changed(("binary_sensor.motion",))
            eventually(lambda: observer.diagnostics()["stats"]["blocked"] > 0)
            result = observer.diagnostics()
            self.assertEqual(result["stats"]["actions_evaluated"], 0)
            self.assertEqual(result["last_examples"], [])
            self.assertEqual(service.mask_calls, 1)
        finally:
            observer.stop()

    def test_stale_neural_state_abstains(self):
        engine, _ = make_engine()
        perception = Perception()
        perception.available = False
        observer = UniversalActionObserver(engine, perception, min_interval=0)
        observer.start()
        try:
            observer.submit_changed(("binary_sensor.motion",))
            eventually(lambda: observer.diagnostics()["stats"]["no_neural_context"] >= 1)
            self.assertEqual(observer.diagnostics()["stats"]["actions_evaluated"], 0)
        finally:
            observer.stop()

    def test_unrelated_sensor_does_not_trigger_all_targets(self):
        engine, service = make_engine(devices=20)
        observer = UniversalActionObserver(engine, Perception(), min_interval=0)
        observer.start()
        try:
            observer.submit_changed(("binary_sensor.unrelated",))
            eventually(lambda: observer.diagnostics()["stats"]["not_routed"] > 0)
            self.assertEqual(service.mask_calls, 0)
        finally:
            observer.stop()

    def test_bounded_fanout_and_debounce(self):
        engine, _ = make_engine(devices=50)
        observer = UniversalActionObserver(engine, Perception(), max_agents=3, min_interval=600)
        observer.start()
        try:
            observer.submit_changed(("binary_sensor.motion",))
            eventually(lambda: observer.diagnostics()["stats"]["agents_evaluated"] == 3)
            first = observer.diagnostics()
            self.assertGreater(first["stats"]["not_routed"], 0)
            observer.submit_changed(("binary_sensor.motion",))
            eventually(lambda: observer.diagnostics()["stats"]["skipped_cooldown"] >= 3)
            self.assertEqual(observer.diagnostics()["stats"]["agents_evaluated"], 3)
        finally:
            observer.stop()

    def test_mailbox_overflow_is_bounded_and_nondispatching(self):
        engine, _ = make_engine()
        observer = UniversalActionObserver(engine, Perception(), max_pending=2)
        self.assertTrue(observer.submit_changed(("sensor.a", "sensor.b", "sensor.c")))
        self.assertEqual(observer.diagnostics()["queue_pending"], 2)
        self.assertEqual(observer.diagnostics()["stats"]["pending_overflow"], 1)
        observer.stop()

    def test_sampling_preserves_endpoints_and_filters_invalid(self):
        self.assertEqual(_sample_proposals([0,1,2,3,4,5,6,7,8,9], 4), [0,3,6,9])
        self.assertEqual(_sample_proposals([0,0,float("nan"),1], 8), [0,1])
        self.assertEqual(_sample_proposals([], 8), [])


if __name__ == "__main__":
    unittest.main()
