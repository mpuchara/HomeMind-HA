"""Thread-local, bounded Recorder timing counters; never record request contents."""
from contextlib import contextmanager
import threading
import time

_LOCAL = threading.local()


def add(name, value):
    metrics = getattr(_LOCAL, 'metrics', None)
    if metrics is not None:
        metrics[name] = metrics.get(name, 0.)+value


@contextmanager
def scope(metrics):
    previous = getattr(_LOCAL, 'metrics', None)
    _LOCAL.metrics = metrics
    try:
        yield
    finally:
        _LOCAL.metrics = previous


@contextmanager
def timed(name):
    start = time.perf_counter()
    try:
        yield
    finally:
        add(name, time.perf_counter()-start)
