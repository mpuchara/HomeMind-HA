import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from support import *
from fast_runtime import (
    FAST_ACTION_INTERVAL_SECONDS,
    FAST_ACK_TIMEOUT_SECONDS,
    FAST_SETTLING_SECONDS,
    FAST_OFF_CONFIRMATION_SECONDS,
    fast_light_presence_evidence,
    fast_light_on_assist_action,
    is_fast_target,
    normalize_fast_payload,
    migrate_existing_fast_agents,
    install,
    stabilize_fast_light_power_decision,
)


class FakeStore:
    def __init__(self, agents=None):
        self.agents = {a['id']: dict(a) for a in (agents or [])}
        self.meta = {}
        self.events = []
        self.created = []

    def list_agent_configs(self):
        return [dict(a) for a in self.agents.values()]

    def get_agent_config(self, agent_id):
        a = self.agents.get(agent_id)
        return dict(a) if a else None

    def update_agent(self, agent_id, payload):
        self.agents[agent_id].update(payload)
        return dict(self.agents[agent_id])

    def create_agent(self, payload):
        self.created.append(dict(payload))
        return dict(payload)

    def meta_set(self, key, value):
        self.meta[key] = str(value)

    def event(self, *args):
        self.events.append(args)


class FastRuntimeTests(unittest.TestCase):
    def fast_agent(self, **changes):
        base = dict(id='lamp', target_entity='light.kitchen', target_property='power',
                    action_interval=60.0, settling_seconds=0.0, ack_timeout=0.0)
        base.update(changes)
        return base

    def test_fast_target_includes_dimmable_lights(self):
        self.assertTrue(is_fast_target(self.fast_agent(target_property='brightness_pct')))
        self.assertTrue(is_fast_target(self.fast_agent(target_entity='switch.plug')))
        self.assertFalse(is_fast_target(self.fast_agent(target_entity='climate.room', target_property='temperature')))

    def test_payload_caps_legacy_waits(self):
        out = normalize_fast_payload(self.fast_agent(), include_defaults=True)
        self.assertEqual(out['action_interval'], FAST_ACTION_INTERVAL_SECONDS)
        self.assertEqual(out['settling_seconds'], FAST_SETTLING_SECONDS)
        self.assertEqual(out['ack_timeout'], FAST_ACK_TIMEOUT_SECONDS)

    def test_migration_updates_fast_timing_without_clearing_explicit_manual_hold(self):
        store = FakeStore([self.fast_agent(auto_created=True)])
        store.meta['manual_hold:lamp'] = '999999.0'
        store.meta['manual_hold_source:lamp'] = 'explicit_user_v8'
        engine = SimpleNamespace(runtime={'lamp': {'manual_override_until': 999999.0}})
        core = SimpleNamespace(STORE=store, ENGINE=engine)
        changed = migrate_existing_fast_agents(core)
        self.assertEqual(len(changed), 1)
        self.assertEqual(store.agents['lamp']['action_interval'], FAST_ACTION_INTERVAL_SECONDS)
        self.assertEqual(store.agents['lamp']['settling_seconds'], FAST_SETTLING_SECONDS)
        self.assertEqual(store.meta['manual_hold:lamp'], '999999.0')
        self.assertEqual(store.meta['manual_hold_source:lamp'], 'explicit_user_v8')
        self.assertEqual(engine.runtime['lamp']['manual_override_until'], 999999.0)

    def test_fast_light_on_assist_requires_two_independent_positive_sources(self):
        a = self.fast_agent()
        arms = [
            {"index": 0, "value": 0.0, "mean": 0.62, "ucb": 0.66},
            {"index": 1, "value": 1.0, "mean": 0.58, "ucb": 0.70},
        ]
        forecast = {
            "known": True,
            "occupancy_now": 0.75,
            "occupancy_in_1s": 0.82,
            "evidence_sources": [
                {"entity_id": "binary_sensor.presence", "role": "occupancy_binary",
                 "available": True, "communication_reliability": 1.0,
                 "evidence_freshness": 1.0, "contribution": 0.9},
                {"entity_id": "binary_sensor.motion", "role": "pir",
                 "available": True, "communication_reliability": 1.0,
                 "evidence_freshness": 1.0, "contribution": 0.7},
            ],
        }
        evidence = fast_light_presence_evidence(forecast)
        self.assertEqual(len(evidence), 2)
        self.assertEqual(
            fast_light_on_assist_action(
                a, 0.0, 0.0, "historical_policy_bootstrap", arms, forecast
            ),
            1,
        )

        one_source = {**forecast, "evidence_sources": forecast["evidence_sources"][:1]}
        self.assertIsNone(
            fast_light_on_assist_action(
                a, 0.0, 0.0, "historical_policy_bootstrap", arms, one_source
            )
        )

    def test_fast_light_on_assist_respects_policy_plausibility_and_explicit_sources(self):
        a = self.fast_agent()
        forecast = {
            "known": True,
            "occupancy_now": 0.8,
            "occupancy_in_1s": 0.8,
            "evidence_sources": [
                {"entity_id": "binary_sensor.presence", "role": "occupancy_binary",
                 "available": True, "communication_reliability": 1.0,
                 "evidence_freshness": 1.0, "contribution": 0.9},
                {"entity_id": "binary_sensor.motion", "role": "pir",
                 "available": True, "communication_reliability": 1.0,
                 "evidence_freshness": 1.0, "contribution": 0.7},
            ],
        }
        contradicted = [
            {"index": 0, "value": 0.0, "mean": 0.80, "ucb": 0.82},
            {"index": 1, "value": 1.0, "mean": 0.30, "ucb": 0.55},
        ]
        self.assertIsNone(
            fast_light_on_assist_action(
                a, 0.0, 0.0, "historical_policy_bootstrap", contradicted, forecast
            )
        )
        plausible = [
            {"index": 0, "value": 0.0, "mean": 0.60, "ucb": 0.64},
            {"index": 1, "value": 1.0, "mean": 0.55, "ucb": 0.65},
        ]
        for source in ("preference_model", "experiment", "scoped_instruction:one_time"):
            self.assertIsNone(
                fast_light_on_assist_action(a, 0.0, 0.0, source, plausible, forecast)
            )

    def test_statistical_off_requires_continuous_confirmation_but_on_is_immediate(self):
        a = self.fast_agent()
        rt = {}
        value, held = stabilize_fast_light_power_decision(
            a, rt, 1.0, 0.0, "historical_policy_bootstrap", 100.0
        )
        self.assertTrue(held)
        self.assertEqual(value, 1.0)
        self.assertTrue(rt["fast_off_confirmation_active"])

        value, held = stabilize_fast_light_power_decision(
            a, rt, 1.0, 0.0, "historical_policy_bootstrap",
            100.0 + FAST_OFF_CONFIRMATION_SECONDS - .01,
        )
        self.assertTrue(held)
        self.assertEqual(value, 1.0)

        value, held = stabilize_fast_light_power_decision(
            a, rt, 1.0, 0.0, "historical_policy_bootstrap",
            100.0 + FAST_OFF_CONFIRMATION_SECONDS,
        )
        self.assertFalse(held)
        self.assertEqual(value, 0.0)

        # Any renewed ON prediction cancels the pending OFF run immediately.
        rt["fast_off_candidate_since"] = 200.0
        value, held = stabilize_fast_light_power_decision(
            a, rt, 1.0, 1.0, "historical_policy_bootstrap", 200.1
        )
        self.assertFalse(held)
        self.assertEqual(value, 1.0)
        self.assertNotIn("fast_off_candidate_since", rt)

    def test_explicit_and_non_light_off_bypass_confirmation(self):
        a = self.fast_agent()
        for source in ("scoped_instruction:one_time", "preference_model", "experiment"):
            rt = {}
            value, held = stabilize_fast_light_power_decision(
                a, rt, 1.0, 0.0, source, 100.0
            )
            self.assertFalse(held, source)
            self.assertEqual(value, 0.0, source)

        switch = self.fast_agent(target_entity="switch.plug")
        value, held = stabilize_fast_light_power_decision(
            switch, {}, 1.0, 0.0, "historical_policy_bootstrap", 100.0
        )
        self.assertFalse(held)
        self.assertEqual(value, 0.0)

    def test_physical_manual_off_is_never_held_by_confirmation(self):
        a = self.fast_agent()
        rt = {"fast_off_candidate_since": 90.0}
        value, held = stabilize_fast_light_power_decision(
            a, rt, 0.0, 0.0, "historical_policy_bootstrap", 100.0
        )
        self.assertFalse(held)
        self.assertEqual(value, 0.0)
        self.assertNotIn("fast_off_candidate_since", rt)

    def test_install_preserves_manual_priority_for_fast_light(self):
        store = FakeStore([self.fast_agent(action_interval=.25, settling_seconds=.1, ack_timeout=2)])
        wake = Mock()
        engine = SimpleNamespace(runtime={}, wake_event=wake)

        def old_hold(agent, rt, timestamp):
            rt['manual_override_until'] = timestamp + 300
            store.meta_set('manual_hold:' + agent['id'], str(rt['manual_override_until']))
            store.meta_set('manual_hold_source:' + agent['id'], 'explicit_user_v8')
            wake.set()

        engine.set_manual_hold = old_hold
        core = SimpleNamespace(
            STORE=store,
            ENGINE=engine,
            HISTORY=object(),
            default_action_interval=lambda entity, prop: 1.0,
            runtime_available=lambda: True,
        )
        import history as history_module
        old_default = history_module.default_action_interval
        try:
            install(core)
            rt = {}
            engine.set_manual_hold(store.agents['lamp'], rt, 100.0)
            self.assertEqual(rt['manual_override_until'], 400.0)
            self.assertEqual(rt['manual_feedback_ts'], 100.0)
            self.assertEqual(store.meta['manual_hold:lamp'], '400.0')
            self.assertEqual(store.meta['manual_hold_source:lamp'], 'explicit_user_v8')
            self.assertEqual(core.default_action_interval('light.kitchen', 'power'), FAST_ACTION_INTERVAL_SECONDS)
            wake.set.assert_called()
        finally:
            history_module.default_action_interval = old_default


if __name__ == '__main__':
    unittest.main()
