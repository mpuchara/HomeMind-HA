"""One bounded private process for optional JSON encoding and classifier fitting.

Only explicit observer/worker scopes use IPC. Control and ordinary policy calls
keep their local implementation. Errors break an optional observation; they never
fall back to expensive in-process work while realtime agents are waiting.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import pickle
import subprocess
import sys
import threading
import time

from runtime_compute_child import read_frame, write_frame, MAX_FRAME

_local = threading.local()


@contextmanager
def background_compute_scope(enabled=True):
    previous = getattr(_local, 'enabled', False)
    _local.enabled = bool(enabled)
    try:
        yield
    finally:
        _local.enabled = previous


def enabled():
    return bool(getattr(_local, 'enabled', False))


class BackgroundCompute:
    def __init__(self):
        self.lock = threading.RLock()
        self.process = None
        self.sequence = 0
        self.deadline = 0.
        self.stats = dict(starts=0, requests=0, errors=0, max_payload_bytes=0,
                          last_ms=None, ram_limit_mb=None, timeouts=0)

    def _start(self):
        from settings import OPTIONS
        from runtime_sqlite_cache import memory_headroom
        limit = max(256, min(1024, int(OPTIONS.get('runtime_compute_ram_mb', 256))))
        total, available = memory_headroom()
        if available is not None and available < limit + 128:
            raise MemoryError('insufficient headroom for isolated optional compute')
        env = dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1',
                   MKL_NUM_THREADS='1', NUMEXPR_NUM_THREADS='1')
        self.process = subprocess.Popen(
            [sys.executable, '-u', str(Path(__file__).with_name('runtime_compute_child.py')), str(limit)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        self.stats['starts'] += 1
        self.stats['ram_limit_mb'] = limit
        process = self.process
        def supervise():
            while process.poll() is None:
                deadline = self.deadline
                if self.process is process and deadline and time.monotonic() > deadline:
                    self.stats['timeouts'] += 1
                    process.terminate()
                    return
                time.sleep(.25)
        threading.Thread(target=supervise, name='adaptive-ai-compute-watchdog', daemon=True).start()

    def call(self, operation, value, options=None):
        # Serialize requests from the two optional observer threads. No live
        # policy path acquires this lock or waits on a child response.
        with self.lock:
            started = time.perf_counter()
            try:
                if self.process is None or self.process.poll() is not None:
                    self._close_unlocked()
                    self._start()
                self.deadline = time.monotonic()+30.
                self.sequence += 1
                request = (self.sequence, operation, value, options or {})
                size = write_frame(self.process.stdin, request)
                self.stats['max_payload_bytes'] = max(self.stats['max_payload_bytes'], size)
                response = read_frame(self.process.stdout)
                if response is None:
                    raise RuntimeError('optional compute worker exited')
                sequence, success, result = pickle.loads(response)
                if sequence != self.sequence:
                    raise RuntimeError('optional compute response mismatch')
                if not success:
                    kind, detail = result
                    if kind == 'TypeError':
                        raise TypeError(detail)
                    if kind == 'ValueError':
                        raise ValueError(detail)
                    raise RuntimeError(kind + ': ' + detail)
                self.stats['requests'] += 1
                return result
            except Exception:
                self.stats['errors'] += 1
                self._close_unlocked()
                raise
            finally:
                self.deadline = 0.
                self.stats['last_ms'] = (time.perf_counter()-started)*1000

    def _close_unlocked(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()

    def close(self):
        with self.lock:
            self._close_unlocked()

    def snapshot(self):
        # Diagnostics must not queue behind a long encode/fit.
        process = self.process
        return dict(self.stats, alive=process is not None and process.poll() is None,
                    pid=process.pid if process is not None else None,
                    semantics='pure optional compute, private pipe, no HA or SQLite')


COMPUTE = BackgroundCompute()


def dumps(value, **options):
    if enabled() and len(pickle.dumps(value, protocol=5)) >= 16384:
        return COMPUTE.call('json', value, options)
    return json.dumps(value, **options)


def roundtrip(value):
    if enabled():
        return COMPUTE.call('roundtrip', value)
    return json.loads(json.dumps(value))
