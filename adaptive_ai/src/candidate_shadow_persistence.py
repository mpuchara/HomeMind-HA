"""Persist immutable Candidate decisions independently of Shadow computation.

Only the shipped composition starts this writer. Standalone compositions retain
their synchronous flush behavior. No prediction, replay, lifecycle or HA service
is run here. Accepted evidence stays charged in the bounded queue until commit.
"""
import threading

from settings import OPTIONS
from sqlite_background import run_connection_worker


class CandidateShadowPersistence:
    def __init__(self, manager):
        self.manager = manager
        self.queue = manager.candidate_shadow_decision_writes
        self.engine_stop_event = manager.engine.stop_event
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        with self.queue.lock:
            self.queue.max_items = 1024
            self.queue.batch_items = 1024
            self.queue.max_bytes = max(4, min(64, int(
                OPTIONS.get('runtime_candidate_evidence_mb', 16)))) * 1024 * 1024
        self.thread = threading.Thread(target=self.run,
            name='adaptive-ai-candidate-evidence-writer', daemon=True)

    def request(self):
        self.wake_event.set()

    def run(self):
        # Do not let connection-open failures kill the evidence owner. The next
        # attempt uses a fresh connection and the unchanged immutable payloads.
        run_connection_worker(self.manager.store, self.stop_event, self.run_session,
                              self.queue.retry, self.report)

    def report(self, exc):
        self.manager._record_worker_error('candidate_evidence_writer', exc)

    def run_session(self):
        # A worker-owned connection stays open between turns, without retaining
        # a transaction or WAL reader snapshot during the wait.
        while not self.stop_event.is_set() and not self.engine_stop_event.is_set():
            self.wake_event.wait(.5)
            self.wake_event.clear()
            if self.stop_event.is_set():
                break
            if self.stop_event.wait(self.queue.retry.delay()):
                break
            if self.queue.snapshot()['pending'] < 32 and self.stop_event.wait(.025):
                break
            try:
                self.queue.flush(limit=1024)
            except Exception as exc:
                if self.queue.retry.defer(exc):
                    self.report(exc)
            if self.queue.snapshot()['pending']:
                self.wake_event.set()
            # Yield the Store and Python scheduler between bounded transactions.
            self.stop_event.wait(.005)

    def close(self):
        self.stop_event.set()
        self.wake_event.set()
        self.thread.join()

    def snapshot(self):
        return dict(owner=self.thread.name, alive=self.thread.is_alive(),
                    drain_limit=1024, decision_transaction_limit=32, quality_transaction_roots=4,
                    stopped=self.stop_event.is_set() or self.engine_stop_event.is_set())


def install(manager):
    if getattr(manager, 'candidate_shadow_persistence_writer', None) is not None:
        return manager
    writer = CandidateShadowPersistence(manager)
    manager.candidate_shadow_persistence_writer = writer
    manager.request_candidate_shadow_persistence = writer.request
    writer.thread.start()
    return manager
