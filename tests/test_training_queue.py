import threading
import time
import unittest

from support import *
from telemetry import HEAVY_JOBS
from training_queue import TrainingQueue


class FakeStore:
    def __init__(self):
        self.agents = {
            'a': agent(id='a', name='A', mode='shadow', training_state='paused'),
            'b': agent(id='b', name='B', mode='shadow', training_state='paused'),
        }
        self.events = []
        self.meta = {}

    def get_agent(self, agent_id):
        row = self.agents.get(agent_id)
        return dict(row) if row else None

    def set_training_state(self, agent_id, state, score=None, samples=0, source=None, detail=None):
        self.agents[agent_id]['training_state'] = state
        self.agents[agent_id]['benchmark_score'] = score
        self.agents[agent_id]['benchmark_samples'] = samples
        self.agents[agent_id]['benchmark_source'] = source
        self.agents[agent_id]['benchmark_detail'] = detail or {}

    def event(self, agent_id, level, code, message, detail=None):
        self.events.append((agent_id, level, code, message, detail))

    def meta_set(self, key, value):
        self.meta[str(key)] = str(value)


class FakeExecutor:
    def __init__(self):
        self.released = []

    def release_control(self, agent, reason='training'):
        self.released.append((agent['id'], reason))


class FakeRLTeaching:
    def __init__(self, trace):
        self.trace = trace
        self.selection_needed = True
        self.aborted = []

    def needs_context_selection(self, agent_id):
        return self.selection_needed

    def prepare_context_selection(self, agent):
        self.trace.append(('prepare_context', agent['id']))
        self.selection_needed = False
        return {'selected': ['binary_sensor.test']}

    def mark_training(self, agent_id):
        self.trace.append(('mark_training', agent_id))

    def finalize_retrain(self, agent_id):
        self.trace.append(('finalize', agent_id))
        return {'labels_applied': 1}

    def abort_retrain(self, agent_id, reason, state='failed'):
        self.aborted.append((agent_id, state, str(reason)))
        self.trace.append(('abort', agent_id, state))


class FakeEngine:
    def __init__(self, trace):
        self.executor = FakeExecutor()
        self.rl_teaching = FakeRLTeaching(trace)


class FakeHistory:
    def __init__(self, store, trace):
        self.store = store
        self.trace = trace
        self.agent_jobs = set()
        self.agent_jobs_lock = threading.RLock()
        self.blocked = False
        self.started = []
        self.fetch_calls = 0
        self.status_updates = []

    def _fetch_history_resilient(self, *args, **kwargs):
        self.fetch_calls += 1
        self.trace.append(('discovery_fetch', self.fetch_calls))
        return 1

    def _manual_lightweight_cycle(self, current, controllable, end_ts):
        self.trace.append(('discovery_cycle', tuple(controllable)))
        return self._fetch_history_resilient(controllable, 0, end_ts)

    def set_status(self, *args, **kwargs):
        self.status_updates.append((args, kwargs))

    def _start(self, agent_id, rebuild):
        with self.agent_jobs_lock:
            if self.blocked or self.agent_jobs:
                return False
            self.agent_jobs.add(agent_id)
        self.store.agents[agent_id]['training_state'] = 'training'
        self.started.append((agent_id, rebuild))
        self.trace.append(('history_start', agent_id, rebuild))
        return True

    def request_agent_resume(self, agent_id):
        return self._start(agent_id, False)

    def request_agent_rebuild(self, agent_id):
        return self._start(agent_id, True)

    def complete(self, agent_id, state='qualified'):
        with self.agent_jobs_lock:
            self.agent_jobs.discard(agent_id)
        self.store.agents[agent_id]['training_state'] = state


class TrainingQueueTests(unittest.TestCase):
    def setUp(self):
        # A failed prior test must never leave the process-wide heavy slot occupied.
        for owner in ('bootstrap-test', 'discovery', 'home_bootstrap'):
            HEAVY_JOBS.release(owner)
        self.trace = []
        self.store = FakeStore()
        self.history = FakeHistory(self.store, self.trace)
        self.engine = FakeEngine(self.trace)
        self.queue = TrainingQueue(self.history, self.store, self.engine, poll_seconds=.02)
        self.queue.start()

    def tearDown(self):
        for owner in ('bootstrap-test', 'discovery', 'home_bootstrap'):
            HEAVY_JOBS.release(owner)
        self.queue.stop()
        self.queue.join(timeout=1)

    def wait_for(self, predicate, timeout=1.5):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            time.sleep(.01)
        return False

    def test_jobs_run_fifo_in_single_heavy_slot(self):
        self.queue.enqueue('a', rebuild=False)
        self.queue.enqueue('b', rebuild=True)
        self.assertTrue(self.wait_for(lambda: self.history.started == [('a', False)]))
        b = self.queue.status_for('b')
        self.assertEqual(b['state'], 'queued')
        self.assertEqual(b['position'], 1)
        self.assertEqual(b['ahead'], 1)
        self.history.complete('a')
        self.assertTrue(self.wait_for(lambda: self.history.started == [('a', False), ('b', True)]))
        self.assertEqual(self.queue.status_for('b')['state'], 'active')

    def test_busy_slot_queues_instead_of_rejecting(self):
        self.history.blocked = True
        result = self.queue.enqueue('a', rebuild=False)
        self.assertEqual(result['state'], 'queued')
        time.sleep(.08)
        self.assertEqual(self.history.started, [])
        self.history.blocked = False
        self.assertTrue(self.wait_for(lambda: self.history.started == [('a', False)]))

    def test_fresh_train_preempts_background_discovery_and_rebuilds(self):
        self.assertTrue(HEAVY_JOBS.acquire('discovery'))
        result = self.queue.enqueue('a', rebuild=True, reason='training')
        self.assertEqual(result['state'], 'queued')
        self.assertEqual(result['blocked_by'], 'discovery')
        # New Recorder requests from the background discovery are cooperatively skipped.
        self.assertEqual(self.history._fetch_history_resilient(['light.a'], 0, 1), 0)
        self.assertEqual(self.history.fetch_calls, 0)
        self.assertTrue(any(e[2] == 'discovery_yielded_to_training' for e in self.store.events))
        HEAVY_JOBS.release('discovery')
        self.assertTrue(self.wait_for(lambda: self.history.started == [('a', True)]))
        self.assertEqual(self.queue.status_for('a')['state'], 'active')
        self.history.complete('a')
        self.assertTrue(self.wait_for(lambda: self.queue.status_for('a') is None))
        self.assertEqual(self.store.meta.get('manual_discovery_refresh'), '')
        self.assertTrue(any(e[2] == 'discovery_rescheduled_after_training' for e in self.store.events))

    def test_explicit_home_bootstrap_is_not_preempted_by_train(self):
        self.assertTrue(HEAVY_JOBS.acquire('home_bootstrap'))
        self.queue.enqueue('a', rebuild=True)
        self.assertEqual(self.history._fetch_history_resilient(['light.a'], 0, 1), 1)
        self.assertEqual(self.history.fetch_calls, 1)
        time.sleep(.05)
        self.assertEqual(self.history.started, [])
        HEAVY_JOBS.release('home_bootstrap')
        self.assertTrue(self.wait_for(lambda: self.history.started == [('a', True)]))

    def test_duplicate_request_is_deduplicated_and_can_upgrade_to_rebuild(self):
        self.history.blocked = True
        first = self.queue.enqueue('a', rebuild=False)
        second = self.queue.enqueue('a', rebuild=True)
        self.assertEqual(first['position'], 1)
        self.assertEqual(second['position'], 1)
        snapshot = self.queue.snapshot()
        self.assertEqual(snapshot['queued_count'], 1)
        self.assertTrue(snapshot['queued'][0]['rebuild'])

    def test_control_is_released_and_waiting_state_blocks_reacquire(self):
        self.history.blocked = True
        self.store.agents['a']['mode'] = 'control'
        self.store.agents['a']['training_state'] = 'qualified'
        self.queue.enqueue('a', rebuild=True, reason='full_rebuild')
        self.assertEqual(self.engine.executor.released, [('a', 'full_rebuild')])
        self.assertEqual(self.store.agents['a']['training_state'], 'waiting')

    def test_pending_job_can_be_cancelled(self):
        self.history.blocked = True
        self.queue.enqueue('a')
        self.assertTrue(self.queue.cancel('a'))
        self.assertIsNone(self.queue.status_for('a'))
        self.assertEqual(self.queue.snapshot()['queued_count'], 0)

    def test_teach_rl_prepares_context_before_rebuild_and_finalizes_after(self):
        self.queue.enqueue('a', rebuild=True, reason='teach_rl')
        self.assertTrue(self.wait_for(lambda: self.history.started == [('a', True)]))
        self.assertIn(('prepare_context', 'a'), self.trace)
        self.assertIn(('mark_training', 'a'), self.trace)
        self.assertLess(self.trace.index(('prepare_context', 'a')),
                        self.trace.index(('history_start', 'a', True)))
        self.assertLess(self.trace.index(('history_start', 'a', True)),
                        self.trace.index(('mark_training', 'a')))
        self.history.complete('a')
        self.assertTrue(self.wait_for(lambda: ('finalize', 'a') in self.trace))

    def test_teach_rl_context_preflight_waits_for_shared_heavy_gate(self):
        self.assertTrue(HEAVY_JOBS.acquire('bootstrap-test'))
        self.queue.enqueue('a', rebuild=True, reason='teach_rl')
        time.sleep(.08)
        self.assertNotIn(('prepare_context', 'a'), self.trace)
        self.assertEqual(self.history.started, [])
        HEAVY_JOBS.release('bootstrap-test')
        self.assertTrue(self.wait_for(lambda: self.history.started == [('a', True)]))
        self.assertIn(('prepare_context', 'a'), self.trace)

    def test_cancelled_teach_rl_job_aborts_teach_preparation(self):
        self.history.blocked = True
        self.queue.enqueue('a', rebuild=True, reason='teach_rl')
        self.assertTrue(self.queue.cancel('a'))
        self.assertEqual(self.engine.rl_teaching.aborted[0][:2], ('a', 'cancelled'))


if __name__ == '__main__':
    unittest.main()
