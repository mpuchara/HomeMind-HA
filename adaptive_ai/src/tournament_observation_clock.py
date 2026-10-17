"""Event time for deferred Tournament work; deadlines keep the real clock."""
from contextlib import contextmanager
import threading
import time as _real

_local = threading.local()


class _Clock:
    def time(self):
        value = getattr(_local, 'timestamp', None)
        return _real.time() if value is None else value

    def __getattr__(self, name):
        return getattr(_real, name)


time = _Clock()


@contextmanager
def observation_clock(timestamp):
    previous = getattr(_local, 'timestamp', None)
    _local.timestamp = float(timestamp)
    try:
        yield
    finally:
        _local.timestamp = previous
