import tempfile
import threading
import time
import unittest
from pathlib import Path

from support import ROOT  # noqa: F401 - installs adaptive_ai/src on sys.path
from device_agents import DeviceAgentService, PERCEPTION_OWNER, CONTRACT_VERSION
from storage import Store


class _Context:
    def __init__(self, entities=None, devices=None):
        self.entities = dict(entities or {})
        self.devices = dict(devices or {})

    def resolved_registry(self):
        out = {}
        for eid, reg in self.entities.items():
            row = dict(reg)
            if not row.get('area_id') and row.get('device_id') in self.devices:
                row['area_id'] = self.devices[row['device_id']].get('area_id')
            out[eid] = row
        return out


class _Engine:
    def __init__(self, entities=None, devices=None, states=None):
        self.context = _Context(entities, devices)
        self.entity_registry = self.context.resolved_registry()
        self.state_map = dict(states or {})
        self.runtime = {}
        self.lock = threading.RLock()


def _light_state(entity_id, *, on=True, brightness=128):
    return {
        'entity_id': entity_id,
        'state': 'on' if on else 'off',
        'attributes': {
            'friendly_name': 'Same friendly name',
            'brightness': brightness,
            'supported_color_modes': ['brightness'],
        },
    }


def _switch_state(entity_id, on=True):
    return {'entity_id': entity_id, 'state': 'on' if on else 'off', 'attributes': {'friendly_name': 'Switch'}}


def _climate_state(entity_id):
    return {
        'entity_id': entity_id,
        'state': 'heat',
        'attributes': {'temperature': 21.0, 'current_temperature': 20.0, 'min_temp': 5, 'max_temp': 35},
    }


def _agent(store, entity, prop, *, auto=False, minimum=None, maximum=None, action_interval=.25):
    if minimum is None:
        minimum = 0 if prop != 'temperature' else 5
    if maximum is None:
        maximum = 1 if prop == 'power' else 100 if prop != 'temperature' else 35
    return store.create_agent({
        'name': f'{entity}:{prop}',
        'target_entity': entity,
        'target_property': prop,
        'min_value': minimum,
        'max_value': maximum,
        'confidence_threshold': .78,
        'deadband': .5 if prop == 'power' else 3,
        'action_interval': action_interval,
        'exploration_step': 1 if prop == 'power' else 5,
        'auto_created': auto,
        'input_entities': ['*'],
    })


class DeviceAgentIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='device-agent-')
        self.store = Store(Path(self.tmp.name) / 'test.db')

    def tearDown(self):
        self.tmp.cleanup()

    def test_power_and_brightness_agents_share_one_physical_lamp(self):
        states = {'light.kitchen': _light_state('light.kitchen')}
        entities = {'light.kitchen': {'entity_id': 'light.kitchen', 'device_id': 'dev-lamp', 'area_id': 'kitchen'}}
        devices = {'dev-lamp': {'id': 'dev-lamp', 'area_id': 'kitchen'}}
        service = DeviceAgentService(_Engine(entities, devices, states), self.store)
        power = _agent(self.store, 'light.kitchen', 'power')
        brightness = _agent(self.store, 'light.kitchen', 'brightness_pct', maximum=100)

        pdesc = service.descriptor(power)
        bdesc = service.descriptor(brightness)
        self.assertEqual(pdesc['logical_device_id'], 'ha-device:dev-lamp')
        self.assertEqual(pdesc['logical_device_id'], bdesc['logical_device_id'])
        self.assertTrue(service.conflicts(power, brightness))
        self.assertFalse(pdesc['compound_action']['separate_power_and_brightness_owners_allowed'])

        plan = service.dispatch_plan(brightness, states['light.kitchen'], 42)
        self.assertEqual(plan['service'], 'turn_on')
        self.assertEqual(plan['data']['brightness_pct'], 42)
        self.assertEqual(plan['semantics'], 'compound_power_brightness')
        self.assertFalse(plan['double_dispatch_required'])
        off = service.dispatch_plan(brightness, states['light.kitchen'], 0)
        self.assertEqual(off['service'], 'turn_off')
        self.assertNotIn('brightness_pct', off['data'])

    def test_two_entities_of_one_registry_device_share_resource(self):
        states = {
            'light.fixture': _light_state('light.fixture'),
            'switch.fixture_relay': _switch_state('switch.fixture_relay'),
        }
        entities = {
            'light.fixture': {'entity_id': 'light.fixture', 'device_id': 'dev-1'},
            'switch.fixture_relay': {'entity_id': 'switch.fixture_relay', 'device_id': 'dev-1'},
        }
        service = DeviceAgentService(_Engine(entities, {'dev-1': {'id': 'dev-1'}}, states), self.store)
        light = _agent(self.store, 'light.fixture', 'power')
        relay = _agent(self.store, 'switch.fixture_relay', 'power')
        self.assertEqual(service.primary_resource_for_entity('light.fixture'), service.primary_resource_for_entity('switch.fixture_relay'))
        self.assertTrue(service.conflicts(light, relay))

    def test_explicit_mapping_groups_entities_without_name_guessing(self):
        states = {
            'light.a': _light_state('light.a'),
            'light.b': _light_state('light.b'),
            'light.c': _light_state('light.c'),
        }
        engine = _Engine({}, {}, states)
        service = DeviceAgentService(engine, self.store)
        service.set_explicit_mapping('light.a', 'room-fixture')
        service.set_explicit_mapping('light.b', 'room-fixture')
        a = _agent(self.store, 'light.a', 'power')
        b = _agent(self.store, 'light.b', 'power')
        c = _agent(self.store, 'light.c', 'power')
        self.assertTrue(service.conflicts(a, b))
        self.assertFalse(service.conflicts(a, c))
        self.assertNotEqual(service.primary_resource_for_entity('light.a'), service.primary_resource_for_entity('light.c'))

    def test_identity_migration_keeps_agent_id_and_persists_property_mapping(self):
        states = {'light.kitchen': _light_state('light.kitchen')}
        entities = {'light.kitchen': {'entity_id': 'light.kitchen', 'device_id': 'dev-identity'}}
        service = DeviceAgentService(_Engine(entities, {'dev-identity': {'id': 'dev-identity'}}, states), self.store)
        original = _agent(self.store, 'light.kitchen', 'brightness_pct', maximum=100)
        original_id = original['id']
        service.migrate_agent_identity(original)
        migrated = self.store.get_agent_config(original_id)
        self.assertEqual(migrated['id'], original_id)
        self.assertEqual(migrated['logical_device_id'], 'ha-device:dev-identity')
        self.assertEqual(migrated['device_property'], 'brightness_pct')
        self.assertEqual(int(migrated['device_contract_version']), CONTRACT_VERSION)

    def test_explicit_logical_property_mapping_does_not_rewrite_physical_target(self):
        states = {'number.fixture_level': {
            'entity_id': 'number.fixture_level', 'state': '40',
            'attributes': {'min': 0, 'max': 100, 'step': 1},
        }}
        service = DeviceAgentService(_Engine({}, {}, states), self.store)
        original = _agent(self.store, 'number.fixture_level', 'value', maximum=100)
        service.set_explicit_mapping(
            'number.fixture_level', 'fixture-logic', property_name='brightness_pct', autonomy_enabled=True
        )
        migrated = self.store.get_agent_config(original['id'])
        self.assertEqual(migrated['logical_device_id'], 'fixture-logic')
        self.assertEqual(migrated['device_property'], 'brightness_pct')
        self.assertEqual(migrated['target_property'], 'value')
        desc = service.descriptor(migrated)
        self.assertEqual(desc['device_property'], 'brightness_pct')
        self.assertEqual(desc['physical_target_property'], 'value')

    def test_exact_entity_fallback_never_merges_equal_friendly_names(self):
        states = {'light.a': _light_state('light.a'), 'light.b': _light_state('light.b')}
        service = DeviceAgentService(_Engine({}, {}, states), self.store)
        self.assertNotEqual(service.primary_resource_for_entity('light.a'), service.primary_resource_for_entity('light.b'))


class SharedResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='device-resource-')
        self.store = Store(Path(self.tmp.name) / 'test.db')
        self.states = {'light.kitchen': _light_state('light.kitchen')}
        self.entities = {'light.kitchen': {'entity_id': 'light.kitchen', 'device_id': 'dev-lamp', 'area_id': 'kitchen'}}
        self.engine = _Engine(self.entities, {'dev-lamp': {'id': 'dev-lamp', 'area_id': 'kitchen'}}, self.states)
        self.service = DeviceAgentService(self.engine, self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_manual_takeover_on_one_agent_blocks_sibling_property_independent_of_reward(self):
        power = _agent(self.store, 'light.kitchen', 'power')
        brightness = _agent(self.store, 'light.kitchen', 'brightness_pct', maximum=100)
        hold_until = time.time() + 600
        self.store.meta_set('manual_hold_source:' + power['id'], 'explicit_user_v8')
        self.store.meta_set('manual_hold:' + power['id'], str(hold_until))
        mask = self.service.legal_action_mask(
            brightness, self.states['light.kitchen'], [40], runtime_by_agent={}, now=time.time()
        )
        self.assertFalse(mask['actions'][0]['legal'])
        self.assertIn('manual', mask['actions'][0]['reason'])
        self.assertEqual(mask['fallback'], 'abstain')

    def test_restart_during_resource_lease_preserves_exclusion(self):
        first = _agent(self.store, 'light.kitchen', 'power', action_interval=.25)
        second = _agent(self.store, 'light.kitchen', 'brightness_pct', maximum=100, action_interval=.25)
        reservation = self.service.reserve_dispatch(first, 'intent-a', now=100.0, ttl=1.0)
        self.assertIsNotNone(reservation)

        restarted = DeviceAgentService(self.engine, self.store)
        self.assertIsNone(restarted.reserve_dispatch(second, 'intent-b', now=100.5, ttl=1.0))
        self.assertIsNotNone(restarted.reserve_dispatch(second, 'intent-b', now=101.1, ttl=1.0))

    def test_completed_dispatch_clears_lease_but_keeps_cross_agent_dwell(self):
        first = _agent(self.store, 'light.kitchen', 'power', action_interval=2)
        second = _agent(self.store, 'light.kitchen', 'brightness_pct', maximum=100, action_interval=2)
        reservation = self.service.reserve_dispatch(first, 'intent-a', now=100.0, ttl=5.0)
        self.service.finish_dispatch(reservation, success=True, action={'value': 1}, now=100.1)
        self.assertEqual(self.service.active_lease(first, now=100.2), [])
        own = self.service.legal_action_mask(first, self.states['light.kitchen'], [1], now=100.2)
        sibling = self.service.legal_action_mask(second, self.states['light.kitchen'], [40], now=100.2)
        self.assertTrue(own['actions'][0]['legal'])  # Executor owns same-agent cooldown.
        self.assertFalse(sibling['actions'][0]['legal'])
        self.assertIn('dwell', sibling['actions'][0]['reason'])

    def test_concurrent_sibling_reservations_have_one_winner_and_no_deadlock(self):
        power = _agent(self.store, 'light.kitchen', 'power', action_interval=.25)
        brightness = _agent(self.store, 'light.kitchen', 'brightness_pct', maximum=100, action_interval=.25)
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def run(agent, intent):
            try:
                barrier.wait(timeout=2)
                results.append(self.service.reserve_dispatch(agent, intent, now=200.0, ttl=1.0))
            except Exception as exc:  # pragma: no cover - assertion below exposes it
                errors.append(exc)

        threads = [
            threading.Thread(target=run, args=(power, 'intent-power')),
            threading.Thread(target=run, args=(brightness, 'intent-brightness')),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        self.assertTrue(all(not thread.is_alive() for thread in threads), 'resource arbitration deadlocked')
        self.assertEqual(errors, [])
        self.assertEqual(sum(item is not None for item in results), 1)

    def test_two_radar_consumers_share_one_perception_owned_configuration_lease(self):
        sensor_states = {'number.radar_threshold': {'entity_id': 'number.radar_threshold', 'state': '45', 'attributes': {'min': 0, 'max': 100, 'step': 1}}}
        entities = {'number.radar_threshold': {
            'entity_id': 'number.radar_threshold', 'device_id': 'radar-1', 'entity_category': 'config', 'area_id': 'kitchen'
        }}
        engine = _Engine(entities, {'radar-1': {'id': 'radar-1', 'area_id': 'kitchen'}}, sensor_states)
        service = DeviceAgentService(engine, self.store)
        first = service.acquire_perception_lease('number.radar_threshold', 'presence-model', now=10, ttl_seconds=300,
                                                 config_snapshot={'threshold': 45})
        second = service.acquire_perception_lease('number.radar_threshold', 'lighting-consumer', now=11, ttl_seconds=300)
        self.assertEqual(first['owner_service'], PERCEPTION_OWNER)
        self.assertEqual(second['owner_service'], PERCEPTION_OWNER)
        self.assertEqual(set(second['consumers']), {'presence-model', 'lighting-consumer'})
        with self.assertRaises(ValueError):
            service.acquire_perception_lease('number.radar_threshold', 'rogue', owner_service='lighting-agent')


class CapabilityAndDynamicsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='device-capability-')
        self.store = Store(Path(self.tmp.name) / 'test.db')

    def tearDown(self):
        self.tmp.cleanup()

    def test_auto_created_generic_switch_needs_explicit_description_but_manual_agent_is_compatible(self):
        states = {'switch.pump': _switch_state('switch.pump')}
        entities = {'switch.pump': {'entity_id': 'switch.pump', 'device_id': 'pump-1'}}
        service = DeviceAgentService(_Engine(entities, {'pump-1': {'id': 'pump-1'}}, states), self.store)
        auto = _agent(self.store, 'switch.pump', 'power', auto=True)
        manual = _agent(self.store, 'switch.pump', 'power', auto=False)
        self.assertFalse(service.control_eligibility(auto)['allowed'])
        self.assertTrue(service.control_eligibility(manual)['allowed'])
        service.set_explicit_mapping('switch.pump', 'pump-1-described', autonomy_enabled=True)
        self.assertTrue(service.control_eligibility(auto)['allowed'])

    def test_configuration_entity_is_perception_owned_even_when_manually_created(self):
        states = {'number.radar_threshold': {'entity_id': 'number.radar_threshold', 'state': '40', 'attributes': {'min': 0, 'max': 100, 'step': 1}}}
        entities = {'number.radar_threshold': {'entity_id': 'number.radar_threshold', 'device_id': 'radar-1', 'entity_category': 'config'}}
        service = DeviceAgentService(_Engine(entities, {'radar-1': {'id': 'radar-1'}}, states), self.store)
        agent = _agent(self.store, 'number.radar_threshold', 'value', auto=False, maximum=100)
        desc = service.descriptor(agent)
        self.assertEqual(desc['configuration_owner'], PERCEPTION_OWNER)
        self.assertFalse(service.control_eligibility(agent)['allowed'])

    def test_hvac_and_cover_expose_process_model_contract_not_lighting_bandit_claim(self):
        states = {'climate.room': _climate_state('climate.room')}
        entities = {'climate.room': {'entity_id': 'climate.room', 'device_id': 'hvac-1', 'area_id': 'room'}}
        service = DeviceAgentService(_Engine(entities, {'hvac-1': {'id': 'hvac-1', 'area_id': 'room'}}, states), self.store)
        auto = _agent(self.store, 'climate.room', 'temperature', auto=True, minimum=5, maximum=35, action_interval=60)
        manual = _agent(self.store, 'climate.room', 'temperature', auto=False, minimum=5, maximum=35, action_interval=60)
        backend = service.descriptor(auto)['backend']
        self.assertEqual(backend['kind'], 'process_model_required')
        self.assertFalse(backend['process_model']['lighting_bandit_is_sufficient'])
        self.assertFalse(backend['process_model']['implementation_ready'])
        self.assertFalse(service.control_eligibility(auto)['allowed'])
        self.assertTrue(service.control_eligibility(manual)['allowed'])

    def test_two_thermal_devices_in_one_zone_share_conflict_resource(self):
        states = {'climate.a': _climate_state('climate.a'), 'climate.b': _climate_state('climate.b')}
        entities = {
            'climate.a': {'entity_id': 'climate.a', 'device_id': 'hvac-a', 'area_id': 'room'},
            'climate.b': {'entity_id': 'climate.b', 'device_id': 'hvac-b', 'area_id': 'room'},
        }
        devices = {'hvac-a': {'id': 'hvac-a', 'area_id': 'room'}, 'hvac-b': {'id': 'hvac-b', 'area_id': 'room'}}
        service = DeviceAgentService(_Engine(entities, devices, states), self.store)
        a = _agent(self.store, 'climate.a', 'temperature', minimum=5, maximum=35, action_interval=60)
        b = _agent(self.store, 'climate.b', 'temperature', minimum=5, maximum=35, action_interval=60)
        self.assertTrue(service.conflicts(a, b))
        self.assertIn('zone:room:thermal', service.descriptor(a)['resource_keys'])


class ExecutorIntegrationContractTests(unittest.TestCase):
    def test_executor_is_still_the_only_dispatch_boundary_and_uses_device_arbiter(self):
        executor = (ROOT / 'adaptive_ai' / 'src' / 'executor.py').read_text(encoding='utf-8')
        device = (ROOT / 'adaptive_ai' / 'src' / 'device_agents.py').read_text(encoding='utf-8')
        self.assertIn('DeviceAgentService', executor)
        self.assertIn('reserve_dispatch(', executor)
        self.assertIn('finish_dispatch(', executor)
        self.assertIn('primary_resource_for_entity', executor)
        self.assertNotIn('HA.service(', device)
        self.assertNotIn('from ha import', device)


if __name__ == '__main__':
    unittest.main()
