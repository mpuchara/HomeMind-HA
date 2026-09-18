import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from support import *
from fast_runtime import (
    FAST_ACTION_INTERVAL_SECONDS,
    FAST_ACK_TIMEOUT_SECONDS,
    FAST_SETTLING_SECONDS,
    is_fast_target,
    normalize_fast_payload,
    migrate_existing_fast_agents,
    install,
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
