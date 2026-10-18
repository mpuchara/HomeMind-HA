"""Resource-only reads retain identity, manual holds and fresh SQL semantics."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import support
from storage import Store
from device_agents import DeviceAgentService
from test_device_agents import _Engine, _agent


class ResourceReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'resource.db')
        self.engine = _Engine({
            'climate.a': {'device_id': 'a', 'area_id': 'room'},
            'climate.b': {'device_id': 'b', 'area_id': 'room'},
            'cover.c': {'device_id': 'c', 'area_id': 'room'},
            'light.d': {'device_id': 'd'}}, {}, {})
        self.service = DeviceAgentService(self.engine, self.store)
        self.agents = [_agent(self.store, eid, 'temperature' if eid.startswith('climate') else 'power')
                       for eid in self.engine.context.entities]

    def mapping(self, connection, entity, logical, group='', zone=''):
        connection.execute('''INSERT OR REPLACE INTO device_explicit_mappings
            (entity_id,logical_device_id,property_name,autonomy_enabled,resource_group,zone_id,updated_ts)
            VALUES(?,?,?,1,?,?,1)''', (entity, logical, 'power', group, zone))

    def test_bulk_resource_keys_match_full_descriptors_and_conflicts_without_capability_scan(self):
        with self.store.conn() as c:
            self.mapping(c, 'cover.c', 'custom-cover', 'feed', 'other')
            self.mapping(c, 'light.d', 'custom-light', 'feed')
        expected = {str(a['id']): self.service.descriptor(a)['resource_keys'] for a in self.agents}
        with patch.object(self.service, '_entities_for_identity', side_effect=AssertionError('capability scan')):
            self.assertEqual(self.service._resource_keys_for_agents(self.agents), expected)
            for a in self.agents:
                self.assertEqual(self.service.resource_keys_for_agent(a), expected[str(a['id'])])
                for b in self.agents:
                    self.assertEqual(self.service.conflicts(a, b),
                                     a['id'] != b['id'] and bool(set(expected[a['id']]) & set(expected[b['id']])))

    def test_external_mapping_changes_are_immediate_and_transaction_rollback_is_visible(self):
        a, b = self.agents[:2]
        self.assertTrue(self.service.conflicts(a, b))
        with closing(sqlite3.connect(self.store.path)) as c, c:
            self.mapping(c, 'climate.b', 'separate', zone='different')
        self.assertFalse(self.service.conflicts(a, b))
        with closing(sqlite3.connect(self.store.path)) as c:
            self.mapping(c, 'climate.b', 'a', zone='room')
            keys = self.service._resource_keys_for_agents([a, b])
            self.assertFalse(set(keys[a['id']]) & set(keys[b['id']]))
            c.rollback()
            self.assertFalse(self.service.conflicts(a, b))
        with patch.object(self.store, 'conn', side_effect=sqlite3.OperationalError('unavailable')):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.shared_manual_hold_until(a, {})

    def test_only_conflicting_peer_holds_persist_and_survive_mapping_change(self):
        a, b, cover, light = self.agents
        now = time.time()
        runtimes = {a['id']: {'manual_override_until': now + 900},
                    b['id']: {'manual_override_until': now + 100},
                    cover['id']: {'manual_override_until': now + 800}}
        self.store.meta_set('manual_hold_source:' + b['id'], 'explicit_user_v8')
        self.store.meta_set('manual_hold:' + b['id'], str(now + 200))
        with patch.object(self.service, 'descriptor', side_effect=AssertionError('capability scan')):
            self.assertEqual(self.service.shared_manual_hold_until(a, runtimes), now + 200)
            self.assertEqual(self.service.shared_manual_hold_until(light, runtimes), 0)
        with closing(sqlite3.connect(self.store.path)) as c, c:
            self.mapping(c, 'climate.b', 'separate', zone='different')
        self.assertEqual(self.service.shared_manual_hold_until(a, {}), now + 200)
        self.assertEqual(self.service._persist_runtime_manual_holds(a, runtimes), 0)


if __name__ == '__main__':
    unittest.main()
