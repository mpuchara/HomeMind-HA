"""Measure shared belief lock waiting separately from work while it is owned."""
from contextlib import contextmanager
import time
from telemetry import TELEMETRY


@contextmanager
def timed_lock(lock, name):
    queued=time.perf_counter()
    lock.acquire()
    acquired=time.perf_counter()
    try:
        yield
    finally:
        finished=time.perf_counter()
        lock.release()
        # Never acquire the diagnostic lock while holding the measured lock.
        TELEMETRY.observe(name+'_lock_wait',(acquired-queued)*1000)
        TELEMETRY.observe(name+'_locked_work',(finished-acquired)*1000)
