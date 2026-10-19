import json
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import support
from detached_mapping_snapshots import DetachedMappingSnapshots
from provenance import ProvenanceJournal
from startup_components import StartupComponents, startup_call
from storage import Store
from storage_budget import StoragePressure


class Startup219Tests(unittest.TestCase):
    def test_nested_live_progress_is_inclusive_owned_and_survives_failure(self):
        tick = [10.]
        owner = StartupComponents(clock=lambda: tick[0], cpu_clock=lambda: tick[0]/2)
        core = SimpleNamespace(STARTUP_COMPONENTS=owner)
        def outer():
            tick[0] = 12.
            with owner.measure('inner'):
                active = owner.snapshot()['active']
                self.assertEqual([v['name'] for v in active], ['outer', 'inner'])
                tick[0] = 14.
                raise ValueError('installer failed')
        with self.assertRaisesRegex(ValueError, 'installer failed'):
            startup_call(core, 'outer', outer)
        result = owner.snapshot()
        self.assertEqual(result['active'], [])
        self.assertEqual(result['steps']['outer'], dict(calls=1, seconds=4., cpu_seconds=2., status='error'))
        self.assertEqual(result['steps']['inner']['seconds'], 2.)
        result['steps']['outer']['seconds'] = -1
        self.assertEqual(owner.snapshot()['steps']['outer']['seconds'], 4.)

    def test_bounded_diagnostics_never_suppress_installation(self):
        owner = StartupComponents(limit=2)
        seen = []
        for i in range(7):
            startup_call(SimpleNamespace(STARTUP_COMPONENTS=owner), str(i), seen.append, i)
        self.assertEqual(seen, list(range(7)))
        self.assertEqual(len(owner.snapshot()['steps']), 2)
        self.assertEqual(startup_call(SimpleNamespace(), 'legacy', lambda a, b: a+b, 2, b=3), 5)

    def test_main_exposes_steps_before_readiness(self):
        runtime = runpy.run_path(str(support.ROOT/'adaptive_ai/src/main.py'))
        with runtime['STARTUP_COMPONENTS'].measure('waiting-installer'):
            current = runtime['startup_snapshot']()
            self.assertFalse(current['ready'])
            self.assertEqual(current['components']['active'][0]['name'], 'waiting-installer')


class ReceiptCache219Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name)/'audit.db')
        self.journal = ProvenanceJournal(self.store, clock=lambda:100.)

    def seed(self):
        with self.store.conn() as c:
            c.executemany('INSERT INTO provenance_events '
                '(event_id,contract_version,event_time,received_time,entity_id,source,origin,state_json,processed_time) '
                'VALUES(?,1,?,?,?,?,?,?,?)',
                [(str(i),float(20000-i),float(i//3),f'sensor.{i%5}','test','own_command' if i%2 else 'user',
                  json.dumps(dict(state=str(i))),float(i)+.5) for i in range(8250)])

    def test_index_preserves_exact_receipt_order_and_cache_bound_with_ties(self):
        self.seed()
        with self.store.conn() as c:
            c.execute('DROP INDEX idx_provenance_events_received_219')
            expected = [dict(r) for r in c.execute('SELECT * FROM provenance_events ORDER BY received_time DESC LIMIT 8192')]
        fresh = ProvenanceJournal(self.store, clock=lambda:100.)
        self.assertEqual(list(fresh._event_cache), [r['event_id'] for r in reversed(expected)])
        self.assertEqual(dict(fresh._event_cache), {r['event_id']:r for r in expected})
        with self.store.conn() as c:
            plan = ' '.join(str(tuple(r)) for r in c.execute('EXPLAIN QUERY PLAN '
                    'SELECT * FROM provenance_events ORDER BY received_time DESC LIMIT 8192'))
        self.assertIn('idx_provenance_events_received_219', plan)
        self.assertNotIn('TEMP B-TREE', plan)
        self.assertEqual(fresh.startup_diagnostics['recent_event_cache_rows'],8192)
        self.assertIsNone(fresh.startup_diagnostics['recent_event_index_error'])

    def test_processed_restart_event_still_suppressed_without_sql_or_writes(self):
        self.seed()
        fresh = ProvenanceJournal(self.store, clock=lambda:100.)
        fresh.store.conn = Mock(side_effect=AssertionError('warm duplicate must remain RAM-only'))
        eid, created = fresh.record_event('sensor.4', support.state('sensor.4','8249'),
                                        event_time=11751., event_id='8249')
        self.assertEqual(eid,'8249')
        self.assertFalse(created)
        self.assertTrue(fresh.event_processed(eid))
        self.assertEqual(fresh.pending_event_count(),0)

    def test_denied_optional_index_keeps_original_query_and_does_not_bypass_budget(self):
        self.seed()
        with self.store.conn() as c:c.execute('DROP INDEX idx_provenance_events_received_219')
        self.journal.startup_diagnostics.clear()
        def deny(*args, **kwargs):
            raise StoragePressure('test DDL budget')
        with patch.object(self.store.storage_budget,'check',side_effect=deny):
            self.journal._ensure_recent_event_index()
        self.assertIn('test DDL budget',self.journal.startup_diagnostics['recent_event_index_error'])
        self.journal._load_recent_event_cache()
        self.assertEqual(len(self.journal._event_cache),8192)
        with self.store.conn() as c:
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='idx_provenance_events_received_219'").fetchone())

    def test_pressure_after_ddl_rolls_back_index_and_preserves_audit_rows(self):
        self.seed()
        with self.store.conn() as c:c.execute('DROP INDEX idx_provenance_events_received_219')
        original=self.store.storage_budget.check
        with self.store.connection_session() as connection:
            seen=[]
            connection.set_trace_callback(seen.append)
            def fail_after_ddl(*args,**kwargs):
                if any(sql.startswith('CREATE INDEX IF NOT EXISTS idx_provenance_events_received_219') for sql in seen):
                    raise StoragePressure('test post-DDL admission')
                return original(*args,**kwargs)
            with patch.object(self.store.storage_budget,'check',side_effect=fail_after_ddl):
                self.journal._ensure_recent_event_index()
            connection.set_trace_callback(None)
            self.assertFalse(connection.in_transaction)
        self.assertIn('post-DDL',self.journal.startup_diagnostics['recent_event_index_error'])
        with self.store.conn() as c:
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='idx_provenance_events_received_219'").fetchone())
            self.assertEqual(c.execute('SELECT COUNT(*) FROM provenance_events').fetchone()[0],8250)


class Registry219Tests(unittest.TestCase):
    def test_complete_hit_and_replacement_still_detect_nested_in_place_changes(self):
        cache=DetachedMappingSnapshots(reuse_complete=True)
        source={'a':dict(attributes=dict(labels=['x']), device_id='one')}
        first,charge=cache.capture(source)
        again,charged=cache.capture(source)
        self.assertEqual(again,first)
        self.assertIsNot(again,first)
        self.assertIs(again['a'],first['a'])
        self.assertEqual(charge,charged)
        self.assertEqual(cache.snapshot()['complete_hits'],1)
        again.clear()
        self.assertEqual(cache.capture(source)[0],first)
        source['a']['attributes']['labels'].append('y')
        current,_=cache.capture(source)
        self.assertEqual(current['a']['attributes']['labels'],['x','y'])
        self.assertEqual(first['a']['attributes']['labels'],['x'])
        replaced={'a':dict(attributes=dict(labels=['x','y']),device_id='one')}
        self.assertEqual(cache.capture(replaced)[0]['a'],current['a'])
        self.assertIsNot(cache.capture(replaced)[0]['a'],current['a'])
        cache.capture({'number':{'value':1}})
        typed,_=cache.capture({'number':{'value':True}})
        self.assertIs(typed['number']['value'],True)

    def test_membership_and_budget_eviction_never_hide_changes_or_retain_extra_records(self):
        cache=DetachedMappingSnapshots(max_entries=2,max_bytes=1800,reuse_complete=True)
        source={'a':{'value':1},'b':{'value':2}}
        cache.capture(source)
        self.assertLessEqual(cache.snapshot()['bytes'],1800)
        del source['a'];source['c']={'value':3}
        current,_=cache.capture(source)
        self.assertEqual(current,source)
        source['d']={'payload':'x'*2000}
        result,_=cache.capture(source)
        self.assertEqual(result,source)
        self.assertIsNone(cache.complete)
        self.assertLessEqual(cache.snapshot()['bytes'],1800)
        self.assertLessEqual(cache.snapshot()['entries'],2)
        cache.max_bytes=0
        cache.capture({'a':{'value':4}})
        self.assertEqual(cache.snapshot()['bytes'],0)
        self.assertIsNone(cache.complete)
        fresh=DetachedMappingSnapshots(reuse_complete=True)
        fresh.capture({'a':{'value':1}})
        fresh.max_bytes=0
        self.assertEqual(fresh.capture({'a':{'value':1}})[0],{'a':{'value':1}})
        self.assertEqual(fresh.snapshot()['bytes'],0)

    def test_fast_validation_is_opt_in_and_unknown_mappings_keep_original_path(self):
        cache=DetachedMappingSnapshots()
        for _ in range(3):cache.capture({'a':{'value':1}})
        self.assertEqual(cache.snapshot()['complete_hits'],0)
        cache=DetachedMappingSnapshots(reuse_complete=True)
        class Mapping(dict):pass
        for _ in range(3):cache.capture(Mapping(a={'value':1}))
        self.assertIsNone(cache.complete)


if __name__=='__main__':unittest.main()
