"""Bounded, read-only Linux write accounting for runtime diagnostics.

One producer (the existing WAL maintenance worker); snapshots only read RAM.
Linux /proc/self/io includes reaped children, not necessarily active children.
Counters are not NAND writes, nor a measurement of the full add-on/device.
"""
from collections import deque
from pathlib import Path
import threading
import time


def read_proc_io():
    values = {}
    for line in Path("/proc/self/io").read_text(encoding="ascii").splitlines():
        key, _, raw = line.partition(":")
        if key in ("write_bytes", "cancelled_write_bytes"):
            values[key] = int(raw.strip())
    if "write_bytes" not in values or any(value < 0 for value in values.values()):
        raise ValueError("invalid process I/O counters")
    return values["write_bytes"], values.get("cancelled_write_bytes")


class StorageWriteHealth:
    SAMPLE_SECONDS = 30.
    MAX_SAMPLES = 121
    MIN_RATE_SECONDS = 60.
    STALE_SECONDS = 90.

    def __init__(self, *, clock=time.monotonic, reader=read_proc_io):
        self._clock = clock
        self._reader = reader
        self._lock = threading.Lock()
        self._samples = deque(maxlen=self.MAX_SAMPLES)
        self._next = float("-inf")
        self._baseline = None
        self._last = None
        self._error = None
        self._resets = 0

    def sample(self):
        """Never propagate diagnostic read failure into checkpoint maintenance."""
        now = self._clock()
        with self._lock:
            if now < self._next:
                return False
            self._next = now + self.SAMPLE_SECONDS
        try:
            written, cancelled = self._reader()
            if type(written) is not int or written < 0:
                raise ValueError("invalid write counter")
            if cancelled is not None and (type(cancelled) is not int or cancelled < 0):
                raise ValueError("invalid cancelled counter")
        except Exception as exc:
            with self._lock:
                self._error = type(exc).__name__
                self._samples.clear()
                self._baseline = None
                self._last = None
            return False
        with self._lock:
            if self._last is not None and written < self._last[1]:
                self._resets += 1
                self._samples.clear()
                self._baseline = None
            if self._baseline is None:
                self._baseline = written
            self._last = (now, written, cancelled)
            self._samples.append((now, written))
            self._error = None
        return True

    def _rate(self, seconds):
        if not self._samples:
            return None, 0.
        end, written = self._samples[-1]
        start, first = next((item for item in self._samples
                             if item[0] >= end-seconds), self._samples[-1])
        span = end-start
        if span < self.MIN_RATE_SECONDS:
            return None, span
        return round((written-first)*3600./span, 2), span

    def snapshot(self):
        now = self._clock()
        with self._lock:
            recent, recent_span = self._rate(300.)
            hour, hour_span = self._rate(3600.)
            last = self._last
            age = max(0., now-last[0]) if last else None
            stale = age is None or age > self.STALE_SECONDS
            return {
                "scope": "linux_proc_self_io_including_reaped_children",
                "available": last is not None,
                "stale": stale,
                "sample_age_seconds": age,
                "sample_interval_seconds": self.SAMPLE_SECONDS,
                "sample_count": len(self._samples),
                "recent_window_seconds": recent_span,
                "hour_window_seconds": hour_span,
                "recent_write_bytes_per_hour": None if stale else recent,
                "hour_write_bytes_per_hour": None if stale else hour,
                "observed_write_bytes": last[1]-self._baseline if last else None,
                "process_write_bytes": last[1] if last else None,
                "process_cancelled_write_bytes": last[2] if last else None,
                "counter_resets": self._resets,
                "error": self._error,
            }
