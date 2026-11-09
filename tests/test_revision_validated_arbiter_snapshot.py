"""Full receipt parity with the frozen 4b989c69 fresh-snapshot oracle.

The new schema-version writer guard is independently checked below; all legacy
receipt values, including nested masks, resources, clocks and byte accounting,
must match the unmodified original implementation.
"""
from contextlib import closing
from copy import deepcopy
import importlib.util
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch

import support
import test_universal_predecision as base
from test_device_agents import _agent
from device_agents import DeviceAgentService
from agent_candidate_lineage import _ensure_root
from universal_predecision import PredecisionEvidence, verify_for_write

spec = importlib.util.spec_from_file_location('predecision_fresh_reference',
    Path(__file__).parent/'reference'/'predecision_fresh.py')
oracle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle)


class ReusableSnapshotTests(unittest.TestCase):
    setUp = base.PredecisionTests.setUp
    drain = base.PredecisionTests.drain
    prepare = base.PredecisionTests.prepare
    bind = base.PredecisionTests.bind

    def tearDown(self):
        self.evidence.close()

    def parity(self, agent=None):
        agent = agent or self.agent
        fresh = oracle.PredecisionEvidence(self.engine, self.store, self.collector)
        resource_reads = [[], []]
        arm = 0
        original_rows = DeviceAgentService._resource_rows
        def rows(service, keys):
            result = original_rows(service, keys)
            resource_reads[arm].append(deepcopy(result))
            return result
        with patch('time.time', return_value=2000000000.), patch.object(DeviceAgentService, '_resource_rows', rows):
            fresh.request(agent['id']); fresh.prepare_one()
            arm = 1
            self.evidence.request(agent['id']); self.evidence.prepare_one()
        self.assertEqual(resource_reads[0], resource_reads[1])
        actual = deepcopy(self.evidence.prepared[agent['id']])
        expected = fresh.prepared[agent['id']]
        with self.store.conn() as c:
            self.assertEqual(actual.pop('arbiter_schema_version'),
                             c.execute('PRAGMA schema_version').fetchone()[0])
        # _bytes includes the new schema guard field. Check its accounting
        # independently, then compare the entire original receipt.
        from universal_history_collector import bounded_copy
        original_bytes = actual.pop('_bytes')
        expected = dict(expected); expected.pop('_bytes')
        with_guard = dict(actual, arbiter_schema_version=self.evidence.arbiter_snapshot.identity[3])
        self.assertEqual(original_bytes, bounded_copy(with_guard)[1])
        self.assertEqual(actual, expected)
        self.assertFalse(self.evidence.arbiter_snapshot.db.in_transaction)
        return actual

    def test_unchanged_multiple_agents_normalize_only_on_rebuild(self):
        peer = _agent(self.store, 'light.kitchen', 'brightness_pct', maximum=100)
        peer = self.store.get_agent_config(peer['id'])
        _ensure_root(self.store, peer['id'], generation_number=0)
        self.engine.agent_configs[peer['id']] = peer
        self.parity()
        db = self.evidence.arbiter_snapshot.db
        # Oracle normalizes each turn; the optimized arm must not do so.
        with patch.object(self.store, '_agent_dict', side_effect=AssertionError('redundant normalization')):
            for agent in (self.agent, peer, self.agent):
                with patch('time.time', return_value=2000000000.):
                    self.evidence.request(agent['id']); self.evidence.prepare_one()
        self.assertIs(db, self.evidence.arbiter_snapshot.db)
        self.assertEqual(self.evidence.stats['arbiter_snapshot_cache_hits'], 3)
        self.assertEqual(self.evidence.stats['arbiter_snapshot_rebuilds'], 1)
        self.parity(peer)

    def test_external_writer_config_binding_and_revision_invalidation(self):
        self.parity()
        db = self.evidence.arbiter_snapshot.db
        with closing(sqlite3.connect(self.store.path)) as c, c:
            c.execute("UPDATE agents SET name='external mutation' WHERE id=?", (self.agent['id'],))
        with self.assertRaisesRegex(ValueError, 'config mismatch'):
            self.prepare()
        self.assertIsNone(self.evidence.arbiter_snapshot.db)
        self.agent = self.store.get_agent_config(self.agent['id'])
        self.engine.agent_configs[self.agent['id']] = self.agent
        self.parity()
        self.assertIsNot(db, self.evidence.arbiter_snapshot.db)

    def test_missing_and_replaced_trigger_fail_closed(self):
        self.parity()
        for body in (None, 'SELECT 1;'):
            with self.subTest(body=body):
                with self.store.conn() as c:
                    c.execute('DROP TRIGGER IF EXISTS uc_revision_agents_UPDATE')
                    if body:
                        c.execute('CREATE TRIGGER uc_revision_agents_UPDATE AFTER UPDATE ON agents BEGIN '+body+' END')
                before = self.evidence.stats['prepared']
                with self.assertRaisesRegex(ValueError, 'revision contract'):
                    self.prepare()
                self.assertEqual(before, self.evidence.stats['prepared'])
                self.assertIsNone(self.evidence.arbiter_snapshot.db)

    def test_manual_hold_rollback_and_expiry_match_original(self):
        peer = _agent(self.store, 'light.kitchen', 'brightness_pct', maximum=100)
        self.engine.runtime[peer['id']] = {'manual_override_until':2000000060.}
        held = self.parity()
        self.assertTrue(all(not action['legal'] for action in held['legal_mask']['actions']))
        db = self.evidence.arbiter_snapshot.db
        self.assertEqual(db.execute('SELECT count(*) FROM device_resource_state').fetchone()[0], 0)
        self.engine.runtime.clear()
        unheld = self.parity()
        self.assertTrue(any(action['legal'] for action in unheld['legal_mask']['actions']))
        self.assertIs(db, self.evidence.arbiter_snapshot.db)

    def test_shared_mapping_ownership_leases_and_dwell_parity(self):
        from test_final_release_snapshot_scope import SnapshotScopeTests
        SnapshotScopeTests.scope_fixture(self)
        with self.store.conn() as c:
            c.execute("UPDATE device_explicit_mappings SET resource_group='shared' WHERE entity_id='light.remapped'")
        self.parity()
        for values in (('other',0,0,''), ('',2000000030.,0,''), ('',0,1999999999.,'peer')):
            with self.subTest(values=values):
                with self.store.conn() as c:
                    c.execute('INSERT OR REPLACE INTO device_resource_state '
                              '(resource_key,control_owner_agent_id,lease_until,last_dispatch_ts,last_dispatch_agent_id,updated_ts) '
                              'VALUES(?,?,?,?,?,0)', ('device:ha-device:light-device',)+values)
                self.parity()

    def test_schema_rebuild_missing_table_and_native_materialization_failure(self):
        self.parity(); previous = self.evidence.arbiter_snapshot.db
        with self.store.conn() as c:
            c.execute('CREATE TABLE unrelated_schema_change (id INTEGER)')
        self.parity()
        self.assertIsNot(previous, self.evidence.arbiter_snapshot.db)
        self.evidence.close()
        with patch.object(self.store, '_agent_dict', side_effect=RuntimeError('decode failure')):
            with self.assertRaisesRegex(RuntimeError, 'decode failure'):
                self.prepare()
        self.assertIsNone(self.evidence.arbiter_snapshot.db)
        self.parity()
        with self.store.conn() as c:
            c.execute('DROP TABLE device_resource_state')
        with self.assertRaisesRegex(ValueError, 'missing arbiter table'):
            self.prepare()
        self.assertIsNone(self.evidence.arbiter_snapshot.db)

    def test_real_rollback_failure_disposes_connection_and_counts_failure(self):
        self.parity()
        original = self.evidence.arbiter_snapshot.db
        class FailingRollback:
            def __getattr__(self, name):
                return getattr(original, name)
            def execute(self, sql, *args):
                if sql.startswith('ROLLBACK TO'):
                    raise sqlite3.DatabaseError('injected SQL rollback failure')
                return original.execute(sql, *args)
        self.evidence.arbiter_snapshot.db = FailingRollback()
        before = self.evidence.stats['prepared']
        with self.assertRaisesRegex(sqlite3.DatabaseError, 'SQL rollback failure'):
            self.prepare()
        self.assertEqual(self.evidence.stats['arbiter_snapshot_cleanup_failures'], 1)
        self.assertEqual(self.evidence.stats['prepared'], before)
        self.assertIsNone(self.evidence.arbiter_snapshot.db)
        with self.assertRaises(sqlite3.ProgrammingError):
            original.execute('SELECT 1')
        self.parity()

    def test_fresh_ram_nested_metadata_registry_and_epoch(self):
        self.parity()
        db = self.evidence.arbiter_snapshot.db
        self.engine.state_map['light.kitchen']['attributes']['nested'] = {'value':[1,2]}
        self.engine.context.registry_revision += 1
        self.store._meta_cache['manual_hold:peer'] = '2000000100'
        self.parity()
        self.assertIs(db, self.evidence.arbiter_snapshot.db)
        self.collector.epoch += 1
        self.parity()
        self.assertIsNot(db, self.evidence.arbiter_snapshot.db)

    def test_schema_change_between_prepare_and_writer_rejects(self):
        self.prepare(); intent, evidence = self.bind()
        with self.store.conn() as c:
            c.execute('DROP TRIGGER uc_revision_agents_UPDATE')
        with self.store.conn() as c:
            c.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(ValueError, 'schema changed'):
                verify_for_write(c, evidence, vars(intent), self.agent, self.state)

    def test_exceptions_and_cancellation_dispose_then_recover(self):
        for method in ('descriptor', 'legal_action_mask', '_resource_rows'):
            for error in (RuntimeError('injected'), KeyboardInterrupt()):
                with self.subTest(method=method, error=type(error).__name__):
                    self.parity()
                    before = self.evidence.stats['prepared']
                    with patch.object(DeviceAgentService, method, side_effect=error):
                        with self.assertRaises(type(error)):
                            self.prepare()
                    self.assertIsNone(self.evidence.arbiter_snapshot.db)
                    self.assertEqual(before, self.evidence.stats['prepared'])
                    self.parity()

    def test_failed_rollback_never_publishes(self):
        self.parity(); before = self.evidence.stats['prepared']
        db = self.evidence.arbiter_snapshot.db
        db.execute('SAVEPOINT predecision_evaluation')
        # An unexpected transaction is contamination, even before evaluation.
        with self.assertRaisesRegex(ValueError, 'retained transaction'):
            self.prepare()
        self.assertEqual(before, self.evidence.stats['prepared'])
        self.assertIsNone(self.evidence.arbiter_snapshot.db)
        self.parity()
        with patch.object(self.evidence.arbiter_snapshot, 'rollback', side_effect=sqlite3.DatabaseError('rollback failed')):
            with self.assertRaises(sqlite3.DatabaseError):
                self.prepare()
        self.assertIsNone(self.evidence.arbiter_snapshot.db)
        self.parity()

    def test_memory_bound_and_restart(self):
        self.parity()
        with patch.object(self.evidence.arbiter_snapshot, 'LIMIT', 1):
            self.evidence.close()
            with self.assertRaisesRegex(ValueError, 'memory bound'):
                self.prepare()
        self.assertIsNone(self.evidence.arbiter_snapshot.db)
        self.parity()
        self.evidence.close()
        self.evidence = PredecisionEvidence(self.engine, self.store, self.collector)
        self.parity()
        self.assertEqual(self.evidence.stats['arbiter_snapshot_rebuilds'], 1)
        previous = self.evidence.arbiter_snapshot.db
        errors = []
        def worker():
            try:
                self.prepare()
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=worker); thread.start(); thread.join(10)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
        self.assertIsNot(previous, self.evidence.arbiter_snapshot.db)

    def test_stop_before_worker_start_disposes_synchronous_snapshot(self):
        self.parity()
        db = self.evidence.arbiter_snapshot.db
        self.assertTrue(self.collector.stop())
        self.assertIsNone(self.evidence.arbiter_snapshot.db)
        self.assertEqual(self.evidence.stats['arbiter_snapshot_memory_bytes'], 0)
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute('SELECT 1')


if __name__ == '__main__':
    unittest.main()
