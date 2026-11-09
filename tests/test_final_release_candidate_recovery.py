"""Bounded-queue recovery contracts; full Candidate/runtime acceptance is separate."""
import unittest

import support
import test_candidate_shadow_deferred as base


class DeferredRecoveryTests(unittest.TestCase):
    setUp = base.CandidateShadowDeferredTests.setUp
    job = staticmethod(base.CandidateShadowDeferredTests.job)

    def test_overload_drains_and_fresh_wave_processes_latest_causal_jobs(self):
        for index in range(40):
            self.queue.enqueue(self.job(root=f'root-{index}', revision=1, context_ts=10))
        overloaded = self.queue.diagnostics()
        self.assertEqual(overloaded['queue_depth'], 4)
        self.assertEqual(overloaded['dropped'], 36)
        while self.queue.diagnostics()['queue_depth']:
            self.queue.drain(max_roots=2)
        self.assertEqual({r['root'] for r in self.executed}, {f'root-{i}' for i in range(36,40)})
        self.assertEqual(len(self.executed), 4)
        self.executed.clear()
        for index in range(4):
            self.queue.enqueue(self.job(root=f'root-{index}', revision=2, context_ts=20))
        self.queue.drain(max_roots=4)
        self.assertEqual({r['root'] for r in self.executed}, {f'root-{i}' for i in range(4)})
        self.assertTrue(all(r['state_revision']==2 and r['job']['context_ts']==20 for r in self.executed))
        self.assertEqual(self.queue.diagnostics()['queue_depth'], 0)
        self.assertEqual(self.queue.diagnostics()['errors'], 0)

    def test_obsolete_generation_then_fresh_generation_does_not_resurrect(self):
        for index in range(4):
            self.queue.enqueue(self.job(root=f'root-{index}', generation_revision=7))
        self.store._provenance_generation_revision = 8
        self.queue.drain(max_roots=4)
        self.assertEqual(self.executed, [])
        self.assertEqual(self.queue.diagnostics()['stale_generation_dropped'], 4)
        for index in range(4):
            self.queue.enqueue(self.job(root=f'root-{index}', generation_revision=8,
                                        revision=2, context_ts=20))
        self.queue.drain(max_roots=4)
        self.assertEqual(len(self.executed), 4)
        self.assertTrue(all(r['job']['generation_revision']==8 for r in self.executed))
        self.assertEqual(self.queue.diagnostics()['queue_depth'], 0)

    def test_one_candidate_exception_is_visible_and_does_not_starve_peers(self):
        events = []
        self.store.event = lambda *args, **kwargs: events.append(args)
        original = self.manager.execute_candidate_shadow_job
        def fail_one(job):
            if job['root_agent_id']=='broken':
                raise RuntimeError('injected Candidate failure')
            return original(job)
        self.manager.execute_candidate_shadow_job = fail_one
        for root in ('broken','healthy-1','healthy-2','healthy-3'):
            self.queue.enqueue(self.job(root=root))
        self.queue.drain(max_roots=4)
        self.assertEqual({r['root'] for r in self.executed}, {'healthy-1','healthy-2','healthy-3'})
        diag = self.queue.diagnostics()
        self.assertEqual(diag['errors'], 1)
        self.assertEqual(events[0][2], 'candidate_shadow_deferred_error')
        self.assertIn('injected Candidate failure', events[0][-1]['error'])
        self.assertEqual(diag['queue_depth'], 0)
        self.manager.execute_candidate_shadow_job = original
        self.queue.enqueue(self.job(root='broken',revision=2,context_ts=20))
        self.queue.drain(max_roots=1)
        self.assertEqual(self.executed[-1]['root'], 'broken')
        self.assertEqual(self.executed[-1]['state_revision'], 2)

    def test_latest_job_for_peer_is_used_after_a_slow_preceding_root(self):
        original = self.manager.execute_candidate_shadow_job
        def replace_peer(job):
            if job['root_agent_id']=='first':
                self.queue.enqueue(self.job(root='peer',revision=3,context_ts=30))
            return original(job)
        self.manager.execute_candidate_shadow_job = replace_peer
        self.queue.enqueue(self.job(root='first',revision=1,context_ts=10))
        self.queue.enqueue(self.job(root='peer',revision=2,context_ts=20))
        self.queue.drain(max_roots=2)
        self.assertEqual([r['root'] for r in self.executed], ['first','peer'])
        self.assertEqual(self.executed[-1]['state_revision'], 3)
        self.assertEqual(self.executed[-1]['job']['context_ts'], 30)
        self.assertEqual(self.queue.diagnostics()['queue_depth'], 0)
