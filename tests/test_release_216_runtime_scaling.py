"""Indexed startup, diagnostic-only scans and exact bounded immutable value reuse."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support
import history
from archive_overview import initial, refresh
from detached_mapping_snapshots import DetachedMappingSnapshots
from observation_contract import observation_value, _uncached_observation_value, _cached_observation_value
from storage import Store


class StartupScalingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'archive.db')
        with self.store.conn() as c:
            c.executemany('INSERT INTO entity_history(entity_id,ts,state,attributes_json,source,mutation_revision) '
                          'VALUES(?,?,?,?,?,1)', [(f'sensor.{i%7}', float(i), str(i), '{}', 'test' if i%2 else 'live')
                                                 for i in range(3000)])

    def test_exact_training_bounds_use_two_index_seeks(self):
        with self.store.conn() as c:
            plan = ' '.join(str(tuple(r)) for r in c.execute('EXPLAIN QUERY PLAN SELECT '
                '(SELECT MIN(ts) FROM entity_history), (SELECT MAX(ts) FROM entity_history)'))
        self.assertIn('idx_entity_history_ts', plan)
        self.assertEqual(self.store.archive_bounds(), dict(min_ts=0., max_ts=2999.))
        with patch.object(history, 'STORE', self.store), patch.object(self.store, 'archive_stats', side_effect=AssertionError('global scan')):
            manager = history.HistoryManager(SimpleNamespace())
            self.assertTrue(manager.archive_cache['stale'])
            low, high = manager._training_bounds()
            self.assertGreaterEqual(low, 0.)
            self.assertGreaterEqual(high, low)

    def test_restart_migration_does_not_scan_archive_without_legacy_cursor(self):
        statements = []
        real = self.store.conn
        from contextlib import contextmanager
        @contextmanager
        def traced():
            with real() as c:
                c.set_trace_callback(statements.append)
                yield c
        with patch.object(self.store, 'conn', traced):
            self.store._init()
        self.assertFalse(any('SELECT MIN(ts)' in s for s in statements))

    def test_missing_legacy_cursor_is_seeded_and_completed_cursor_is_preserved(self):
        agent = self.store.create_agent(support.agent(id='legacy'))
        with self.store.conn() as c:
            c.execute("UPDATE agents SET training_state='qualified',training_cursor_ts=NULL,"
                      'training_window_start_ts=NULL,training_window_end_ts=NULL WHERE id=?', (agent['id'],))
        self.store._init()
        row = self.store.get_agent_config(agent['id'])
        self.assertEqual(row['training_cursor_ts'], 2999.)
        with self.store.conn() as c:
            c.execute('UPDATE agents SET training_cursor_ts=42 WHERE id=?', (agent['id'],))
        self.store._init()
        self.assertEqual(self.store.get_agent_config(agent['id'])['training_cursor_ts'],42.)

    def test_paged_overview_matches_fresh_sql_without_writer_lock_and_supports_wal_reclaim(self):
        # Every page ends before the pause. A concurrent writer/RESTART can make
        # progress even while diagnostics scan a large archive.
        expected = self.store.archive_stats()
        class Stop:
            def is_set(s): return False
            def wait(s, pause):
                from contextlib import closing
                with closing(sqlite3.connect(self.store.path, timeout=.2)) as c:
                    c.execute("INSERT OR REPLACE INTO app_meta VALUES('scan_writer','ok')")
                    c.commit()
                    self.assertEqual(c.execute('PRAGMA wal_checkpoint(RESTART)').fetchone()[0],0)
                return False
        result = refresh(self.store, Stop(), lambda value: None, page_rows=128, pause=0)
        for field in ('n','min_ts','max_ts','entities','by_source'):
            self.assertEqual(result[field], expected[field])
        self.assertTrue(result['stale'])
        self.assertEqual(initial(self.store)['n'],3000)

    def test_cancelled_overview_never_publishes_partial_totals(self):
        stop = threading.Event(); stop.set()
        published = []
        self.assertIsNone(refresh(self.store,stop,published.append))
        self.assertEqual(published,[])

    def test_constructor_does_not_start_background_scan(self):
        with patch.object(history,'STORE',self.store), patch('archive_overview.refresh',side_effect=AssertionError('scan')):
            manager=history.HistoryManager(SimpleNamespace())
        self.assertFalse(hasattr(manager,'_archive_overview_thread'))


class DetachedRecordTests(unittest.TestCase):
    def test_unchanged_temporal_cache_does_not_materialize_every_deque_again(self):
        from collections import deque
        from context import TemporalHistory
        from temporal_observation_snapshots import TemporalObservationSnapshots
        class CountingDeque(deque):
            reads=0
            def __iter__(self):
                self.reads+=1
                return super().__iter__()
        source=CountingDeque([(1.,support.state('sensor.a','1'))])
        temporal=TemporalHistory(); temporal.samples={'sensor.a':source}
        cache=TemporalObservationSnapshots()
        first,_=cache.capture(temporal,2.)
        reads=source.reads
        second,_=cache.capture(temporal,2.)
        self.assertEqual(source.reads,reads)
        self.assertIs(first['sensor.a'],second['sensor.a'])

    def test_reuse_and_deep_in_place_correction_keep_older_frame(self):
        cache=DetachedMappingSnapshots()
        state=support.state('sensor.a','20',nested={'calibration':[1,2]})
        first,charge=cache.capture({'sensor.a':state})
        second,_=cache.capture({'sensor.a':state})
        self.assertIs(first['sensor.a'],second['sensor.a'])
        state['attributes']['nested']['calibration'][0]=99
        third,_=cache.capture({'sensor.a':state})
        self.assertEqual(first['sensor.a']['attributes']['nested']['calibration'][0],1)
        self.assertEqual(third['sensor.a']['attributes']['nested']['calibration'][0],99)
        self.assertIsNot(first['sensor.a'],third['sensor.a'])
        self.assertGreater(charge,0)

    def test_replacement_and_byte_entry_limits(self):
        cache=DetachedMappingSnapshots(max_bytes=1000,max_entries=2)
        for i in range(10):
            cache.capture({str(i):support.state(f'sensor.{i}', '1')})
        self.assertLessEqual(cache.snapshot()['bytes'],1000)
        self.assertLessEqual(cache.snapshot()['entries'],2)
        cache.max_bytes=0
        cache.capture({'big':support.state('sensor.big','x'*2048)})
        self.assertNotIn('big',cache.entries)


class ValueReuseTests(unittest.TestCase):
    def setUp(self):
        _cached_observation_value.cache_clear()

    def test_every_unit_role_validity_and_category_matches_reference(self):
        for eid in ('sensor.room','sensor.radar_still_energy','sensor.radar_moving_distance','sensor.radar_light','binary_sensor.presence'):
            for unit in ('','%','m','cm','mm','lx','°C','°F','K','hPa','kPa','W','kW','ppm','μg/m³'):
                for text in ('0','42','-2.5','nan','inf','unknown','on','off','Custom Value'):
                    for radar in (False,True):
                        state=support.state(eid,text,unit_of_measurement=unit,friendly_name='Still target distance')
                        self.assertEqual(observation_value(state,radar),_uncached_observation_value(state,radar))

    def test_cached_return_cannot_be_poisoned_and_metadata_changes_invalidate(self):
        state=support.state('sensor.temp','42',unit_of_measurement='°C')
        reference=observation_value(state)
        observation_value(state)['value']=99
        self.assertEqual(observation_value(state),reference)
        state['attributes']['unit_of_measurement']='°F'
        self.assertNotEqual(observation_value(state),reference)
        self.assertGreater(_cached_observation_value.cache_info().hits,0)

    def test_oversize_and_non_string_values_bypass_bounded_cache(self):
        for text in ('a'*257,42):
            state=support.state('sensor.a','1'); state['state']=text
            self.assertEqual(observation_value(state),_uncached_observation_value(state))
        self.assertEqual(_cached_observation_value.cache_info().currsize,0)

    def test_cache_entry_limit(self):
        for i in range(8300):observation_value(support.state('sensor.a',str(i)))
        self.assertEqual(_cached_observation_value.cache_info().currsize,8192)


if __name__=='__main__':unittest.main()
