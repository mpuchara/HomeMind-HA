import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
import test_candidate_shadow_runtime as shadow_tests
import agent_candidate_shadow_runtime as shadow_runtime
from episode_evaluator_runtime import install_candidate


class ShadowFastReadTests(unittest.TestCase):
    def test_busy_root_keeps_fifo_admission_and_newest_payload(self):
        import test_candidate_shadow_deferred as deferred_tests
        helper=deferred_tests.CandidateShadowDeferredTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        helper.queue.enqueue(helper.job(root='busy'))
        helper.queue.enqueue(helper.job(root='peer'))
        for revision in range(2,102):
            helper.queue.enqueue(helper.job(root='busy',revision=revision,context_ts=float(revision)))
        self.assertEqual(helper.queue.diagnostics()['pending_roots'],['busy','peer'])
        helper.queue.drain(max_roots=2)
        self.assertEqual([row['root'] for row in helper.executed],['busy','peer'])
        self.assertEqual(helper.executed[0]['state_revision'],101)

    def test_unchanged_predictions_skip_edge_sql_but_transition_reads_and_scores(self):
        helper=shadow_tests.CandidateShadowRuntimeTests()
        helper.setUp();self.addCleanup(helper.tearDown)
        status,generation=helper._g1(prediction=1.)
        helper._run_shadow()
        with patch.object(shadow_runtime,'_active_comparison_edge',side_effect=AssertionError('unexpected lifecycle SQL')):
            helper._run_shadow()
        raw=helper.store.get_model(status['candidate_id'])
        raw['prediction']=0.;raw['model_revision']='changed'
        helper.store.save_model(status['candidate_id'],raw)
        helper.engine.models.pop(status['candidate_id'],None)
        with patch.object(shadow_runtime,'_active_comparison_edge',wraps=shadow_runtime._active_comparison_edge) as read:
            helper._run_shadow()
        self.assertEqual(read.call_count,1)
        with helper.store.conn() as c:
            import json
            row=c.execute('SELECT comparison_json FROM agent_candidates WHERE candidate_id=?',(status['candidate_id'],)).fetchone()
        self.assertEqual(json.loads(row[0])['candidate_false_early'],1)
        helper.executor.service.assert_not_called()

    def test_pending_proxy_keeps_fresh_edge_reads_and_existing_deadline(self):
        bundle=dict(current=0.,ts=10.,event_id='e',results={
            'p':dict(desired=1.),'c':dict(desired=1.)})
        manager=SimpleNamespace(before_live_process=lambda *args:None,
            after_live_process=lambda *args:bundle,_comparison_summary=lambda *args:{})
        evaluator=SimpleNamespace(evaluate_episode=Mock())
        install_candidate(manager,evaluator)
        cfg={'id':'root','target_property':'power'}
        edge={'parent_generation_id':'p','child_generation_id':'c'}
        with patch('episode_evaluator_runtime._candidate_edge',return_value=edge) as read:
            manager.after_live_process(cfg,{})
            self.assertEqual(read.call_count,1)
            bundle['ts']=17.99;manager.after_live_process(cfg,{})
            self.assertEqual(read.call_count,2)
            evaluator.evaluate_episode.assert_not_called()
            bundle['ts']=18.;manager.after_live_process(cfg,{})
            self.assertEqual(read.call_count,3)
            self.assertEqual(evaluator.evaluate_episode.call_count,1)
            bundle['ts']=19.
            bundle['results']={'p':dict(desired=0.),'c':dict(desired=0.)}
            manager.after_live_process(cfg,{})
            self.assertEqual(read.call_count,4)
            self.assertEqual(evaluator.evaluate_episode.call_count,2)
            bundle['ts']=20.;manager.after_live_process(cfg,{})
            self.assertEqual(read.call_count,4)


if __name__=='__main__':unittest.main()
