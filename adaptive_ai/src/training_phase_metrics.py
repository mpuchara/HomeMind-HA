"""RAM-only timings for synchronous classifier and Shadow training phases."""
from contextlib import contextmanager
import time

from telemetry import RUNTIME_DEBUG, TELEMETRY


@contextmanager
def measure_training_phase(name, **fields):
    started = time.perf_counter()
    token = RUNTIME_DEBUG.begin(name, **fields) if RUNTIME_DEBUG.enabled else None
    status = 'error'
    try:
        yield fields
        status = 'ok'
    finally:
        TELEMETRY.observe(name, (time.perf_counter() - started) * 1000)
        RUNTIME_DEBUG.end(token, status=status, **fields)
