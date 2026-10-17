"""RAM delivery freshness, wait bounds, Root instrumentation and worker leases."""
import threading
import time
import unittest
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import support
from live_card_updates import LiveCardUpdates
from agent_live_card_refresh import live_agent_payload, live_request_payload
import agent_candidate_shadow_context as shadow_context
import test_candidate_shadow_runtime as candidate_fixture
from runtime_sqlite_cache import RuntimeSQLiteCache


def core_fixture():
    a = support.agent(id='a', target_entity='light.a')
    engine = SimpleNamespace(lock=threading.RLock(), all_agent_configs={'a': a},
        state_map={'light.a': support.state('light.a', 'on')}, entity_revisions={'light.a': 2},
        runtime={'a': {'last_prediction': 1., 'last_confidence': .1,
            '_live_decision': dict(last_prediction=0., last_confidence=.9,
                last_inference_ts=10., decision_ready_ts=11., decision_target_revision=1,
                fast_off_confirmation_active=True, fast_off_confirmation_required=6.,
                fast_off_confirmation_elapsed=2.)}},
        _refresh_agent_index=lambda: None, live_card_updates=LiveCardUpdates())
    core = SimpleNamespace(ENGINE=engine)
    core.live_agent_payload = lambda include_configs=False: live_agent_payload(core, include_configs=include_configs)
    return core


class LiveDeliveryTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_production_frontend_receives_revision_and_explains_pending_off(self):
        result = subprocess.run(['node','tools/verify_live_delivery_ui.js'],
                                cwd=support.ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    def test_current_is_physical_and_desired_is_one_completed_record(self):
        core = core_fixture()
        row = live_agent_payload(core)['agents'][0]
        self.assertEqual(row['current_value'], 1.)
        self.assertEqual(row['last_prediction'], 0.)
        self.assertEqual(row['last_confidence'], .9)
        self.assertEqual(row['decision_ready_ts'], 11.)
        self.assertTrue(row['current_newer_than_decision'])
        self.assertTrue(row['fast_off_confirmation_active'])
        self.assertEqual(row['fast_off_confirmation_required'], 6.)

    def test_wait_releases_engine_lock_and_wakes_for_new_completed_decision(self):
        core = core_fixture()
        updates = core.ENGINE.live_card_updates
        after = updates.token()
        result = []
        worker = threading.Thread(target=lambda: result.append(live_request_payload(core,
            {'after': [after], 'wait_ms': ['1500']})))
        worker.start()
        try:
            deadline = time.monotonic() + 1
            while not updates.snapshot()['waiters'] and time.monotonic() < deadline:
                time.sleep(.001)
            self.assertEqual(updates.snapshot()['waiters'], 1)
            self.assertTrue(core.ENGINE.lock.acquire(timeout=.1))
            core.ENGINE.runtime['a']['_live_decision'] = dict(last_prediction=1., last_confidence=.8,
                decision_ready_ts=12., decision_target_revision=2)
            core.ENGINE.lock.release()
            started = time.perf_counter()
            updates.publish()
            worker.join(.5)
            self.assertFalse(worker.is_alive())
            self.assertLess(time.perf_counter()-started, .5)
            self.assertEqual(result[0]['agents'][0]['last_prediction'], 1.)
            self.assertNotEqual(result[0]['revision'], after)
        finally:
            updates.publish()
            worker.join(2)

    def test_notification_before_wait_and_restart_token_never_wait_for_timeout(self):
        updates = LiveCardUpdates()
        before = updates.token()
        updates.publish()
        started = time.perf_counter()
        self.assertTrue(updates.wait(before, 1.5))
        self.assertTrue(updates.wait(LiveCardUpdates().token(), 1.5))
        self.assertLess(time.perf_counter()-started, .1)

    def test_waiter_capacity_falls_back_without_holding_more_threads(self):
        updates = LiveCardUpdates()
        after = updates.token()
        workers = [threading.Thread(target=updates.wait, args=(after, 1.5)) for _ in range(8)]
        for worker in workers:
            worker.start()
        try:
            deadline = time.monotonic()+1
            while updates.snapshot()['waiters'] < 8 and time.monotonic() < deadline:
                time.sleep(.001)
            self.assertEqual(updates.snapshot()['waiters'], 8)
            self.assertFalse(updates.wait(after, 100))
            self.assertEqual(updates.snapshot()['overflows'], 1)
        finally:
            updates.publish()
            for worker in workers:
                worker.join(2)
        self.assertEqual(updates.snapshot()['waiters'], 0)

    def test_notification_during_snapshot_is_not_acknowledged_early(self):
        core = core_fixture()
        updates = core.ENGINE.live_card_updates
        token = updates.token()
        def racing_token():
            updates.publish()
            return token
        with patch.object(updates, 'token', racing_token):
            first = live_agent_payload(core)
        self.assertEqual(first['revision'], token)
        started = time.perf_counter()
        next_payload = live_request_payload(core, {'after': [token]})
        self.assertLess(time.perf_counter()-started, .1)
        self.assertNotEqual(next_payload['revision'], token)

    def test_malformed_wait_and_legacy_core_remain_ordinary_reads(self):
        core = core_fixture()
        after = core.ENGINE.live_card_updates.token()
        started = time.perf_counter()
        self.assertIn('agents', live_request_payload(core, {'after': [after], 'wait_ms': ['bad']}))
        self.assertLess(time.perf_counter()-started, .1)
        del core.ENGINE.live_card_updates
        self.assertNotIn('revision', live_request_payload(core, {'after': [after]}))

    def test_warm_root_policy_does_not_repeat_generation_reads(self):
        f = candidate_fixture.CandidateShadowRuntimeTests(methodName='runTest')
        f.setUp()
        try:
            f._g1()
            policy = f.engine.policy(f.root)
            f.engine.policy = lambda agent: policy
            with patch.object(shadow_context, '_generation', wraps=shadow_context._generation) as read:
                shadow_context.install(f.manager)
                for _ in range(12):
                    self.assertIs(f.engine.policy(f.root), policy)
                self.assertEqual(read.call_count, 1)
        finally:
            f.tearDown()

    def test_candidate_worker_reuses_bounded_sqlite_pages_and_sees_fresh_commits(self):
        f = candidate_fixture.CandidateShadowRuntimeTests(methodName='runTest')
        f.setUp()
        pool = RuntimeSQLiteCache(256, memory=lambda: (8192, 4096))
        f.store._runtime_sqlite_cache = pool
        observed = []
        def maintenance():
            with f.store.conn() as c:
                observed.append(c.execute("SELECT value FROM app_meta WHERE key='probe'").fetchone()[0])
            if len(observed) == 1:
                # A separate connection, not a shared transaction, updates the value.
                import sqlite3
                with sqlite3.connect(f.store.path) as other:
                    other.execute("UPDATE app_meta SET value='fresh' WHERE key='probe'")
            if len(observed) == 3:
                f.manager.stop()
            else:
                f.manager.wake_event.set()
        try:
            with f.store.conn() as c:
                c.execute("INSERT INTO app_meta(key,value) VALUES('probe','old')")
            f.manager._maintenance = maintenance
            f.manager._all_rows = lambda: []
            f.manager._worker_loop()
            self.assertEqual(observed, ['old','fresh','fresh'])
            snapshot = pool.snapshot()
            self.assertEqual(snapshot['misses'], 1)
            self.assertGreaterEqual(snapshot['hits'], 2)
            self.assertEqual(snapshot['leased'], 0)
            self.assertLessEqual(snapshot['cache_target_mb'], snapshot['effective_mb'])
        finally:
            pool.stop()
            f.tearDown()


if __name__ == '__main__':
    unittest.main()
