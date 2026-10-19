"""Bounded startup diagnostics; nested component durations are inclusive."""
from contextlib import contextmanager, nullcontext
import threading
import time


class StartupComponents:
    def __init__(self, *, clock=time.perf_counter, cpu_clock=time.thread_time, limit=128):
        self.clock, self.cpu_clock = clock, cpu_clock
        self.limit = max(1, min(256, int(limit)))
        self.lock = threading.RLock()
        self.steps, self.active = {}, {}
        self.serial = 0

    @contextmanager
    def measure(self, name):
        name = str(name)
        start, cpu = self.clock(), self.cpu_clock()
        with self.lock:
            self.serial += 1
            token = self.serial
            retained = name in self.steps or len(self.steps) < self.limit
            if retained:
                self.steps.setdefault(name, dict(calls=0, seconds=0., cpu_seconds=0., status='running'))
            if retained and len(self.active) < self.limit:
                self.active[token] = (name, start)
        status = 'ok'
        try:
            yield
        except BaseException:
            status = 'error'
            raise
        finally:
            elapsed, used_cpu = max(0., self.clock()-start), max(0., self.cpu_clock()-cpu)
            with self.lock:
                self.active.pop(token, None)
                if retained:
                    row = self.steps[name]
                    row.update(calls=row['calls']+1, seconds=row['seconds']+elapsed,
                               cpu_seconds=row['cpu_seconds']+used_cpu, status=status)

    def snapshot(self):
        now = self.clock()
        with self.lock:
            return dict(semantics='inclusive nested durations; do not sum components',
                        steps={name: dict(row) for name, row in self.steps.items()},
                        active=[dict(name=name, seconds=max(0., now-start))
                                for name, start in self.active.values()])


def startup_scope(core, name):
    owner = getattr(core, 'STARTUP_COMPONENTS', None)
    return owner.measure(name) if owner is not None else nullcontext()


def startup_call(core, name, operation, /, *args, **kwargs):
    with startup_scope(core, name):
        return operation(*args, **kwargs)
