"""Read-set oracle before considering any reduction of the full RAM snapshot."""
from copy import deepcopy
from unittest.mock import patch
import time
import unittest

import support
import test_universal_predecision as base
import universal_predecision
from device_agents import DeviceAgentService


class SnapshotScopeTests(unittest.TestCase):
    setUp = base.PredecisionTests.setUp
    prepare = base.PredecisionTests.prepare
    bind = base.PredecisionTests.bind
    drain = base.PredecisionTests.drain
    def scope_fixture(self):
        entities = self.engine.context.entities
        entities.update({
            'light.sibling': {'device_id': 'light-device'},
            'light.excluded': {'device_id': 'light-device'},
            'light.remapped': {'device_id': 'other-device'},
        })
        for eid in ('light.sibling', 'light.excluded', 'light.remapped', 'light.unrelated'):
            self.engine.state_map[eid] = support.state(eid, 'on', brightness=128)
        with self.store.conn() as conn:
            conn.executemany(
                'INSERT INTO device_explicit_mappings(entity_id,logical_device_id,updated_ts) VALUES(?,?,?)',
                [('light.excluded', 'another-logical-device', time.time()),
                 ('light.remapped', 'ha-device:light-device', time.time())])
        return {'light.kitchen', 'light.sibling', 'light.remapped'}

    def test_original_descriptor_reads_siblings_and_explicit_remaps(self):
        expected = self.scope_fixture()
        import device_agents
        reads = []
        original = device_agents.target_options_for_state
        def traced(state):
            reads.append(state['entity_id'])
            return original(state)
        with patch.object(device_agents, 'target_options_for_state', traced):
            self.prepare()
        evidence = self.evidence.prepared[self.agent['id']]
        self.assertEqual(set(reads), expected)
        self.assertEqual(set(evidence['descriptor']['entities']), expected)
        self.assertEqual(set(evidence['related_states']), expected)
        self.assertEqual(evidence['legal_mask'], self.service.legal_action_mask(
            self.agent, self.state, [0., 1.], now=evidence['created_ts']))

    def test_full_snapshot_keeps_unrelated_and_excluded_nested_records_detached(self):
        self.scope_fixture()
        captured = []
        original = universal_predecision.bounded_copy
        def traced(value, **kwargs):
            result = original(value, **kwargs)
            if 'states' in value:
                captured.append(result[0])
            return result
        with patch.object(universal_predecision, 'bounded_copy', traced):
            self.prepare()
        before = deepcopy(captured[0])
        self.engine.state_map['light.unrelated']['attributes']['brightness'] = 1
        self.engine.state_map['light.excluded']['attributes']['brightness'] = 2
        self.engine.context.entities['light.sibling']['device_id'] = 'changed'
        self.assertEqual(captured[0], before)
        self.assertIn('light.unrelated', before['states'])
        self.assertIn('light.excluded', before['states'])

    def test_related_state_change_during_preparation_cannot_bind(self):
        expected = self.scope_fixture()
        original = universal_predecision.bounded_copy
        def raced(value, **kwargs):
            result = original(value, **kwargs)
            if 'states' in value:
                self.engine.state_map['light.remapped']['attributes']['brightness'] = 1
            return result
        with patch.object(universal_predecision, 'bounded_copy', raced):
            self.prepare()
        evidence = self.evidence.prepared[self.agent['id']]
        self.assertEqual(set(evidence['related_states']), expected)
        self.assertEqual(evidence['related_states']['light.remapped']['attributes']['brightness'], 128)
        self.assertIsNone(self.evidence.begin(self.agent, self.state, [0., 1.]))

    def test_registry_revision_change_during_preparation_cannot_bind(self):
        self.scope_fixture()
        original = DeviceAgentService.descriptor
        def raced(service, agent):
            result = original(service, agent)
            self.engine.context.registry_revision += 1
            return result
        with patch.object(DeviceAgentService, 'descriptor', raced):
            self.prepare()
        self.assertIsNone(self.evidence.begin(self.agent, self.state, [0., 1.]))

    def test_mapping_change_after_native_snapshot_rejected_at_persistence(self):
        self.scope_fixture()
        original = DeviceAgentService.descriptor
        changed = False
        def raced(service, agent):
            nonlocal changed
            result = original(service, agent)
            if not changed:
                changed = True
                with self.store.conn() as conn:
                    conn.execute("UPDATE device_explicit_mappings SET logical_device_id='new-device' "
                                 "WHERE entity_id='light.remapped'")
            return result
        with patch.object(DeviceAgentService, 'descriptor', raced):
            self.prepare()
        intent, evidence = self.bind()
        with self.store.conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(ValueError, 'revision changed'):
                universal_predecision.verify_for_write(conn, evidence, vars(intent), self.agent, self.state)
