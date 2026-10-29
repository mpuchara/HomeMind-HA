"""Opt-in, bounded host benchmark instrumentation; never imported by the runtime.

Inclusive wall/thread/process clocks are not additive across nested or overlapping
spans. SQL execution includes SQLite busy waits; it does not isolate them from VM
work. No SQL text, parameters, entity IDs or household states are retained.
"""
from collections import defaultdict, deque
from contextlib import contextmanager
from functools import wraps
import threading
import time
import sys
import os
from pathlib import Path


def thread_cpu_snapshot():
    """Linux scheduler accounting, including auxiliary threads; no span sums."""
    try:
        hz = os.sysconf('SC_CLK_TCK')
        names = {thread.native_id: thread.name for thread in threading.enumerate()}
        result = {}
        for path in Path('/proc/self/task').glob('*/stat'):
            try:
                fields = path.read_text().rsplit(')', 1)[1].split()
                tid = int(path.parent.name)
                result[tid] = (names.get(tid, 'native'), (int(fields[11])+int(fields[12]))*1000/hz)
            except (OSError, ValueError, IndexError):
                continue
        return result
    except (OSError, ValueError, AttributeError):
        return {}


def thread_cpu_delta(before, after):
    return dict(resolution_ms=1000/os.sysconf('SC_CLK_TCK') if after else None,
                exited_threads_omitted=len(set(before)-set(after)),
                rows=[dict(thread=name, cpu_ms=value-before.get(tid, ('', 0))[1])
                      for tid, (name, value) in after.items()])


class Probe:
    def __init__(self, capacity=4096):
        self.capacity = capacity
        self.lock = threading.Lock()
        self.rows = defaultdict(lambda: deque(maxlen=capacity))
        self.counts = defaultdict(int)

    @staticmethod
    def clock():
        return time.perf_counter_ns(), time.thread_time_ns(), time.process_time_ns()

    def record(self, name, start):
        end = self.clock()
        row = tuple((b-a)/1e6 for a, b in zip(start, end))
        self.append(name, row)
        return row

    def append(self, name, row):
        with self.lock:
            self.rows[name].append(row)
            self.counts[name] += 1

    @contextmanager
    def span(self, name):
        start = self.clock()
        try:
            yield
        finally:
            self.record(name, start)

    def wrap(self, stack, owner, method, name):
        from unittest.mock import patch
        original = getattr(owner, method)
        @wraps(original)
        def wrapped(*args, **kwargs):
            with self.span(name):
                return original(*args, **kwargs)
        stack.enter_context(patch.object(owner, method, wrapped))

    def reset(self):
        with self.lock:
            self.rows.clear()
            self.counts.clear()

    def snapshot(self):
        with self.lock:
            return dict(capacity_per_operation=self.capacity,
                        clocks=['wall_ms', 'thread_cpu_ms', 'process_cpu_ms'],
                        inclusive=True, process_cpu_additive=False,
                        operations={name: dict(count=self.counts[name], samples=list(rows),
                                    dropped=self.counts[name]-len(rows))
                                    for name, rows in self.rows.items()})

    def install(self, stack, engine, store):
        from unittest.mock import patch
        from policy import MultiHorizonPolicy
        from storage_budget import BudgetCursor
        from runtime_sqlite_cache import CachedBudgetConnection
        stack.enter_context(patch.object(engine, 'lock', TimedLock(engine.lock, self, 'engine_lock')))
        stack.enter_context(patch.object(store, 'lock', TimedLock(store.lock, self, 'store_lock')))
        for owner, method, name in (
            (engine, 'on_state_changed', 'engine_ingress'),
            (engine.context, 'observe', 'context_update'),
            (engine, 'process', 'scheduler_pass'),
            (engine, 'process_agent', 'composed_agent'),
            (MultiHorizonPolicy, 'features', 'features'),
            (MultiHorizonPolicy, 'predict', 'model_inference'),
            (CachedBudgetConnection, 'storage_statement', 'sqlite_session_classification'),
        ):
            self.wrap(stack, owner, method, name)
        sql_execute = BudgetCursor.execute
        def sql_timed(cursor, sql, parameters=()):
            frame = sys._getframe(2)
            caller = frame.f_globals.get('__name__', '')+'.'+frame.f_code.co_name
            with self.span('sqlite_execute.'+caller):
                return sql_execute(cursor, sql, parameters)
        stack.enter_context(patch.object(BudgetCursor, 'execute', sql_timed))
        service = getattr(engine, 'context_tournament', None)
        if service:
            queue = service._deferred_observations
            for method in ('_capture', '_execute', 'run_synchronous'):
                self.wrap(stack, queue, method, 'tournament'+method)
        # Submission-to-entry, separate from event-to-target admission/deferral.
        submit = engine.control_workers.submit
        def timed_submit(fn, *args, **kwargs):
            queued = time.perf_counter_ns()
            def entered():
                delay = (time.perf_counter_ns()-queued)/1e6
                with self.lock:
                    self.rows['target_executor_queue'].append((delay, None, None))
                    self.counts['target_executor_queue'] += 1
                return fn(*args, **kwargs)
            return submit(entered)
        stack.enter_context(patch.object(engine.control_workers, 'submit', timed_submit))


class TimedLock:
    """Delegate RLock semantics, including nonblocking acquire and reentrancy."""
    def __init__(self, lock, probe, name):
        self.lock, self.probe, self.name = lock, probe, name
        self.local = threading.local()

    def acquire(self, *args, **kwargs):
        start = self.probe.clock()
        acquired = self.lock.acquire(*args, **kwargs)
        wait = self.probe.record(self.name+'_wait', start)
        frame = sys._getframe(2)
        caller = frame.f_globals.get('__name__', '')+'.'+frame.f_code.co_name
        if self.name == 'engine_lock':
            # The scheduler snapshot is one precise Engine.process with-block.
            # Code locations distinguish it from retry/admission bookkeeping.
            caller += ':'+str(frame.f_lineno)
        self.probe.append(self.name+'_wait.'+caller, wait)
        if acquired:
            if not hasattr(self.local, 'starts'):
                self.local.starts = []
            self.local.starts.append((self.probe.clock(), caller))
        return acquired

    def release(self):
        start, caller = self.local.starts.pop()
        self.lock.release()
        held = self.probe.record(self.name+'_held', start)
        self.probe.append(self.name+'_held.'+caller, held)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()

    def __getattr__(self, name):
        return getattr(self.lock, name)
