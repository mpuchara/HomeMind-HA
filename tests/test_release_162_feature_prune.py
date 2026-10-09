import json
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
import threading
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import support
from observation_contract import FeatureJournal
from storage import Store
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from benchmark_feature_prune import previous_prune, seed_events


class FeaturePrune162Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.serial = 0

    def fixture(self, rows, *, limit=5, retention=24., per_entity=100, windows=()):
        self.serial += 1
        store = Store(Path(self.temp.name) / f'{self.serial}.db')
        journal = FeatureJournal(store, clock=lambda:1000., global_event_limit=limit,
                                 retention_hours=retention, max_events_per_entity=per_entity)
        with store.conn() as c:
            seed_events(c, rows)
            for key, until in windows:
                c.execute('''INSERT INTO feature_windows
                    VALUES(?,4,'agent',950,900,999,'decision',950,?)''', (key,until))
                c.execute('INSERT INTO feature_window_entities VALUES(?,?)', (key,'sensor.a'))
        return store, journal

    def rows(self, n=12, protected=None):
        return [(f'key-{n-i:04d}', 'sensor.a' if i%2 else 'sensor.b', 900.+i,
                 900.+i, str(i%2), json.dumps({'sentinel': [i, .25]}),
                 float(protected(i)) if protected else 0.) for i in range(n)]

    def snapshot(self, store):
        with store.conn() as c:
            return {name: [tuple(r) for r in c.execute(f'SELECT * FROM {name} ORDER BY 1,2')]
                    for name in ('feature_observation_events','feature_windows','feature_window_entities')}

    def assert_parity(self, rows, **kwargs):
        entity = kwargs.pop('entity', None)
        old_store, old = self.fixture(rows, **kwargs)
        new_store, new = self.fixture(rows, **kwargs)
        with old_store.conn() as c:
            c.execute('DROP INDEX idx_feature_obs_prune_cover')
            c.execute('DROP INDEX idx_feature_windows_expiry')
        self.assertTrue(previous_prune(old, entity))
        self.assertTrue(new.prune(entity))
        self.assertEqual(self.snapshot(new_store), self.snapshot(old_store))
        return new_store

    def test_randomized_retention_windows_caps_and_ties_match_original_rows(self):
        rng = random.Random(162)
        for case in range(20):
            rows = [(f'key-{rng.randrange(10**8):08d}-{i}', f'sensor.{i%3}', float(850+i),
                     float(rng.randrange(850,1001)), str(i%2), json.dumps({'case':case,'i':i}),
                     float(rng.choice([0,999,1000,1001,2000]))) for i in range(60)]
            with self.subTest(case=case):
                self.assert_parity(rows, limit=rng.randrange(0,50), per_entity=rng.randrange(1,20),
                                   retention=rng.choice([.01,.1,24.]), entity='sensor.1',
                                   windows=[('expired',999.),('boundary',1000.),('future',2000.)])

    def test_unprotected_priority_and_exact_protection_boundary(self):
        store = self.assert_parity(self.rows(protected=lambda i:2000. if i<8 else 1000.), limit=8)
        with store.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM feature_observation_events WHERE protected_until>1000').fetchone()[0],8)

    def test_all_protected_rows_still_obey_hard_global_cap(self):
        store = self.assert_parity(self.rows(protected=lambda i:2000.), limit=5)
        with store.conn() as c:
            rows = c.execute('SELECT received_time FROM feature_observation_events ORDER BY received_time').fetchall()
        self.assertEqual([r[0] for r in rows], [907.,908.,909.,910.,911.])

    def test_equal_receive_times_preserve_original_insertion_order_ties(self):
        rows = [(key,eid,ts,950.,value,attrs,until) for key,eid,ts,received,value,attrs,until
                in self.rows(40,protected=lambda i:[999.,1000.,2000.][i%3])]
        self.assert_parity(rows,limit=17)

    def test_under_cap_preserves_every_event_and_only_expires_old_windows(self):
        store = self.assert_parity(self.rows(8),limit=20,
                                  windows=[('expired',999.),('boundary',1000.),('future',2000.)])
        snapshot = self.snapshot(store)
        self.assertEqual(len(snapshot['feature_observation_events']),8)
        self.assertEqual([r[0] for r in snapshot['feature_windows']],['boundary','future'])

    def test_per_entity_limit_does_not_delete_protected_evidence(self):
        store = self.assert_parity(self.rows(20,protected=lambda i:2000. if i%3==0 else 0.),
                                  limit=30,per_entity=2,entity='sensor.a')
        with store.conn() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM feature_observation_events WHERE entity_id=? AND protected_until>1000',
                                       ('sensor.a',)).fetchone()[0],3)

    def test_covering_plan_and_idempotent_migration_preserve_model_and_events(self):
        store,journal = self.fixture(self.rows(8))
        store.save_model('agent',{'sentinel':{'weights':[1.,2.,3.]}})
        before,model = self.snapshot(store),store.get_model('agent')
        for _ in range(2):
            FeatureJournal(store)
        self.assertEqual(self.snapshot(store),before)
        self.assertEqual(store.get_model('agent'),model)
        with store.conn() as c:
            plan = [r[3] for r in c.execute('''EXPLAIN QUERY PLAN SELECT event_key
                FROM feature_observation_events INDEXED BY idx_feature_obs_prune_cover
                WHERE protected_until<=? ORDER BY received_time,rowid LIMIT ?''',(1000,2))]
            window_plan = [r[3] for r in c.execute('EXPLAIN QUERY PLAN SELECT window_id FROM feature_windows WHERE protected_until<?',(1000,))]
        self.assertTrue(any('COVERING INDEX idx_feature_obs_prune_cover' in r for r in plan),plan)
        self.assertNotIn('USE TEMP B-TREE FOR ORDER BY',plan)
        self.assertTrue(any('idx_feature_windows_expiry' in r for r in window_plan),window_plan)

    def test_global_delete_failure_rolls_back_expired_windows_and_all_events(self):
        store,journal = self.fixture(self.rows(),windows=[('expired',999.)])
        before = self.snapshot(store)
        original = store.conn
        event_deletes = 0
        def authorize(action,table,*args):
            nonlocal event_deletes
            if action==sqlite3.SQLITE_DELETE and table=='feature_observation_events':
                event_deletes+=1
                if event_deletes==2:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        @contextmanager
        def guarded():
            with original() as c:
                c.set_authorizer(authorize)
                yield c
        with patch.object(store,'conn',guarded), self.assertRaises(sqlite3.DatabaseError):
            journal.prune()
        self.assertEqual(self.snapshot(store),before)


class PoolCount162Tests(unittest.TestCase):
    def fixture(self, buffered):
        import context_tournament_policy_candidate as pool
        import context_tournament_promotion as promotion
        from context_row_buffer import ContextRowBuffer
        from test_context_tournament_observed_pool_runtime import ObservedPoolRuntimeTests
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        old_chooser,old_migrate=promotion._choose_schema_after_promotion,promotion._migrate_schema
        self.addCleanup(setattr,promotion,'_choose_schema_after_promotion',old_chooser)
        self.addCleanup(setattr,promotion,'_migrate_schema',old_migrate)
        store=Store(Path(temp.name)/'pool.db')
        a=support.agent()
        entities={f'sensor.context_{i}' for i in range(4)}
        states={eid:support.state(eid,i) for i,eid in enumerate(sorted(entities))}
        states[a['target_entity']]=support.state(a['target_entity'],'off')
        engine=SimpleNamespace(models={},state_map=states,runtime={},entity_registry={},context_relevance={},
                               state_revision=0,lock=threading.RLock())
        service=ObservedPoolRuntimeTests.FakeService(store,engine,dict(active_features=[],challenger_features=[],
                                                                   feature_scores={},schema_revision=1))
        service._eligible_entities=lambda *args:entities
        buffers=[]
        if buffered:
            def register(name,statement):
                result=ContextRowBuffer(store,name,statement)
                buffers.append(result)
                return result
            service.register_context_rows=register
            service.background_persistence_available=lambda:True
        pool.install_policy_candidates(service)
        with store.conn() as c:
            c.executemany('INSERT INTO context_tournament_observed_pool(agent_id,entity_id,updated_ts) VALUES(?,?,0)',
                          [(a['id'],eid) for eid in ['sensor.context_0','sensor.context_2','sensor.durable']])
        service.observe_shadow(a,states,entities)
        return store,service,buffers,a

    def assert_one_read(self,store,service,a,expected):
        statements=[]
        original=store.conn
        @contextmanager
        def traced():
            with original() as c:
                c.set_trace_callback(statements.append)
                yield c
        with patch.object(store,'conn',traced):
            self.assertEqual(service.state(a['id'])['observed_pool_count'],expected)
        self.assertEqual(sum(s.startswith('SELECT') for s in statements),1)

    def test_background_count_deduplicates_durable_pending_and_refreshes_database(self):
        store,service,buffers,a=self.fixture(True)
        self.assert_one_read(store,service,a,5)
        buffers[0].flush()
        self.assert_one_read(store,service,a,5)
        with store.conn() as c:
            c.execute("DELETE FROM context_tournament_observed_pool WHERE entity_id='sensor.durable'")
        self.assert_one_read(store,service,a,4)

    def test_synchronous_count_keeps_one_fresh_durable_read(self):
        store,service,buffers,a=self.fixture(False)
        self.assert_one_read(store,service,a,5)
        with store.conn() as c:
            c.execute("DELETE FROM context_tournament_observed_pool WHERE entity_id='sensor.durable'")
        self.assert_one_read(store,service,a,4)


if __name__=='__main__':
    unittest.main()
