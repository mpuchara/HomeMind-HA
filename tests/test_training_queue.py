import threading
import time
import unittest

from support import *
from training_queue import TrainingQueue


class FakeStore:
    def __init__(self):
        self.agents = {
            'a': agent(id='a', name='A', mode='shadow', training_state='paused'),
            'b': agent(id='b', name='B', mode='shadow', training_state='paused'),
        }
        self.events = []

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


class FakeExecutor:
    def __init__(self):
        self.released = []

    def release_control(self, agent, reason='training'):
        self.released.append((agent['id'], reason))


class FakeEngine:
    def __init__(self):
        self.executor = FakeExecutor()


class FakeHistory:
    def __init__(self, store):
        self.store = store
        self.agent_jobs = set()
        self.agent_jobs_lock = threading.RLock()
        self.blocked = False
        self.started = []

    def _start(self, agent_id, rebuild):
        with self.agent_jobs_lock:
            if self.blocked or self.agent_jobs:
                return False
            self.agent_jobs.add(agent_id)
        self.store.agents[agent_id]['training_state'] = 'training'
        self.started.append((agent_id, rebuild))
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
        self.store = FakeStore()
        self.history = FakeHistory(self.store)
        self.engine = FakeEngine()
        self.queue = TrainingQueue(self.history, self.store, self.engine, poll_seconds=.02)
        self.queue.start()

    def tearDown(self):
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


if __name__ == '__main__':
    unittest.main()
