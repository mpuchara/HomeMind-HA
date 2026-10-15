"""Fresh paired evidence without repeated UI scans blocking realtime Python work."""
import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import support
import episode_evaluator as module
from episode_evaluator import EpisodeEvaluator
from episode_comparison_cache import ComparisonCache, revision
from storage import Store


class LegacyQueryEvaluator(EpisodeEvaluator):
    def _paired_policy_rows(self, connection, agent_id, parent_key, candidate_key):
        p, c = self._policy_rows(agent_id,parent_key), self._policy_rows(agent_id,candidate_key)
        ids = sorted(set(p)&set(c), key=lambda eid:(p[eid]['start_ts'],eid))
        return p,c,ids


class EpisodeComparison203Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name)/'episode.db')
        self.e = EpisodeEvaluator(self.store)
        self.ref = LegacyQueryEvaluator(self.store)
        self.ref._comparison_cache = ComparisonCache(max_entries=0)

    def add(self, eid='one', aid='a', policies=('p','c'), need=True):
        return self.e.evaluate_episode(episode_id=eid,agent_id=aid,start_ts=0.,end_ts=60.,
            observations=[{'ts':0.,'light_need':need,'presence':True,'power':False},
                          {'ts':60.,'light_need':need,'presence':True,'power':True}],
            policies=[dict(policy_key=k,role='candidate',executed=False,initial_power=False,
                           decisions=[{'ts':i+1.,'power':True}]) for i,k in enumerate(policies)])

    def check(self,aid='a',p='p',c='c'):
        actual = self.e.compare_policies(aid,p,c)
        self.assertEqual(actual,self.ref.compare_policies(aid,p,c))
        return actual

    def sql(self,text,args=()):
        with self.store.conn() as c:c.execute(text,args)

    def test_exact_join_matches_legacy_for_overlap_missing_same_policy_and_proxy(self):
        self.add('one');self.add('unpaired',policies=('p',));self.add('other',aid='b');self.add('proxy',need=None)
        self.assertEqual(self.check()['episode_ids'],['one','proxy'])
        self.check(p='p',c='p');self.check(aid='b');self.check(c='missing')

    def test_warm_hit_rereads_sql_revision_without_loading_rows_and_is_owned(self):
        self.add()
        with patch.object(self.e,'_paired_policy_rows',wraps=self.e._paired_policy_rows) as read, \
             patch.object(module,'revision',wraps=module.revision) as rev:
            first=self.e.compare_policies('a','p','c');expected=json.loads(json.dumps(first))
            first['episode_ids'].clear();first['candidate']['metric_sums'].clear()
            self.assertEqual(self.e.compare_policies('a','p','c'),expected)
            self.assertEqual(read.call_count,1);self.assertEqual(rev.call_count,3)
        self.assertEqual(self.e.comparison_cache_snapshot()['hits'],1)

    def test_new_episode_and_missing_counterpart_invalidate_after_commit(self):
        self.add();self.check();self.add('two',policies=('p',));self.check()
        with self.store.conn() as c:
            c.execute("INSERT INTO episode_evaluator_policy_results SELECT 'two',policy_key,role,executed,counterfactual,metrics_json,evidence_json FROM episode_evaluator_policy_results WHERE episode_id='one' AND policy_key='c'")
        self.assertEqual(self.check()['matched_episodes'],2)

    def test_external_result_update_delete_and_key_change_are_fresh(self):
        self.add();self.check()
        external=sqlite3.connect(self.store.path)
        self.addCleanup(external.close)
        for statement in ["UPDATE episode_evaluator_policy_results SET metrics_json='{}' WHERE policy_key='c'",
                          "UPDATE episode_evaluator_policy_results SET policy_key='new' WHERE policy_key='c'",
                          "DELETE FROM episode_evaluator_policy_results WHERE policy_key='new'"]:
            external.execute(statement);external.commit();self.check();self.check(c='new')

    def test_episode_label_agent_and_deletion_invalidate_both_owners(self):
        self.add();self.check();self.check(aid='b')
        self.sql("UPDATE episode_evaluator_episodes SET labels_json='{}'");self.check()
        self.sql("UPDATE episode_evaluator_episodes SET agent_id='b'");self.check();self.check(aid='b')
        self.sql('DELETE FROM episode_evaluator_policy_results');self.check(aid='b')
        self.sql('DELETE FROM episode_evaluator_episodes');self.check(aid='b')

    def test_policy_row_move_invalidates_old_and_new_episode_agents(self):
        self.add();self.add('two',aid='b',policies=('p',));self.check();self.check(aid='b')
        self.sql("UPDATE episode_evaluator_policy_results SET episode_id='two' WHERE episode_id='one' AND policy_key='c'")
        self.check();self.check(aid='b')

    def test_rollback_does_not_invalidate_a_committed_result(self):
        self.add();expected=self.check()
        with self.assertRaises(RuntimeError):
            with self.store.conn() as c:
                c.execute("UPDATE episode_evaluator_policy_results SET metrics_json='{}'")
                raise RuntimeError('rollback')
        with patch.object(self.e,'_paired_policy_rows',side_effect=AssertionError('cache should remain valid')):
            self.assertEqual(self.e.compare_policies('a','p','c'),expected)

    def test_commit_during_miss_never_publishes_a_mixed_revision_cache(self):
        self.add();original=self.e._paired_policy_rows
        def concurrent(*args):
            result=original(*args)
            self.sql("UPDATE episode_evaluator_policy_results SET metrics_json='{}' WHERE policy_key='c'")
            return result
        with patch.object(self.e,'_paired_policy_rows',side_effect=concurrent):
            self.e.compare_policies('a','p','c')
        self.assertEqual(self.e.comparison_cache_snapshot()['entries'],0)
        self.check()

    def test_uncommitted_explicit_transaction_neither_reads_nor_seeds_cache(self):
        self.add();expected=self.check()
        connection=sqlite3.connect(self.store.path);connection.row_factory=sqlite3.Row
        self.addCleanup(connection.close)
        class TransactionStore:
            lock=threading.RLock()
            @contextmanager
            def conn(self):yield connection
        self.e.store=TransactionStore()
        connection.execute('BEGIN')
        connection.execute("UPDATE episode_evaluator_policy_results SET metrics_json='{}'")
        dirty=self.e.compare_policies('a','p','c');self.assertNotEqual(dirty,expected)
        connection.rollback()
        self.assertEqual(self.e.compare_policies('a','p','c'),expected)

    def test_legacy_rows_before_revision_migration_are_preserved_and_invalidated(self):
        self.add()
        with self.store.conn() as c:
            triggers=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'comparison_rev_%'")]
            for trigger in triggers:c.execute('DROP TRIGGER '+trigger)
            c.execute('DROP TABLE episode_comparison_revisions')
        self.e=EpisodeEvaluator(self.store)
        self.check();self.sql("UPDATE episode_evaluator_episodes SET labels_json='{}'");self.check()
        with self.store.conn() as c:self.assertGreater(revision(c,'a'),0)

    def test_malformed_audit_json_still_blocks_comparison(self):
        self.add()
        self.sql("UPDATE episode_evaluator_policy_results SET evidence_json='not-json'")
        self.sql("UPDATE episode_evaluator_episodes SET observability_json='not-json'")
        with self.assertRaises(json.JSONDecodeError):self.e.compare_policies('a','p','c')
        # The full audit reader preserves the same failure boundary.
        with self.assertRaises(json.JSONDecodeError):self.e._policy_rows('a','p')

    def test_empty_legacy_audit_json_keeps_the_empty_object_fallback(self):
        self.add()
        self.sql("UPDATE episode_evaluator_policy_results SET evidence_json=''")
        self.sql("UPDATE episode_evaluator_episodes SET observability_json=''")
        self.check()

    def test_sqlite_without_json_extension_retains_full_reader_and_parity(self):
        self.add();self.e._comparison_json_sql=False;self.check()
        self.sql("UPDATE episode_evaluator_policy_results SET evidence_json='not-json'")
        with self.assertRaises(json.JSONDecodeError):self.e.compare_policies('a','p','c')


class BoundedCache203Tests(unittest.TestCase):
    def test_entry_count_bytes_eviction_tokens_and_oversize(self):
        c=ComparisonCache(max_entries=2,max_bytes=100,max_entry_bytes=70)
        c.put(('a',),1,{'x':1});c.put(('b',),1,{'x':2})
        self.assertEqual(c.get(('a',),1),{'x':1})
        c.put(('c',),1,{'x':3});self.assertIsNone(c.get(('b',),1))
        self.assertIsNone(c.get(('a',),2))
        c.put(('large',),1,{'x':'x'*100});s=c.snapshot()
        self.assertLessEqual(s['bytes'],100);self.assertLessEqual(s['entries'],2)
        self.assertEqual(s['evictions'],1);self.assertEqual(s['bypasses'],1)

    def test_configured_memory_budget_is_bounded_and_can_disable_cache(self):
        for value, expected in [(0,0),(64,64),(999,256),(-1,0),(None,64)]:
            with patch.dict(module.OPTIONS,{'runtime_episode_comparison_cache_mb':value}):
                e=EpisodeEvaluator()
            self.assertEqual(e.comparison_cache_snapshot()['max_bytes'],expected*1024*1024)

    def test_no_store_keeps_empty_comparison_contract(self):
        result=EpisodeEvaluator().compare_policies('a','p','c')
        self.assertEqual(result['matched_episodes'],0)
        self.assertEqual(result['evidence_mode'],'insufficient')
        self.assertIsNone(result['candidate']['episode_success_rate'])


if __name__=='__main__':unittest.main()
