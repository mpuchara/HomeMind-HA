"""Bounded immutable evidence retained across transient SQLite write failures.

Existing owners drain the queue; no new thread and no policy replay. Explicit
flushes bypass the background timeout/backoff and expose durability failures.
"""
from collections import OrderedDict
from contextlib import nullcontext
import json
import threading

from sqlite_background import background_sqlite, is_sqlite_busy
from storage_budget import StoragePressure, StorageRetry


class EvidenceBufferFull(BufferError):
    """No room for a new observation; retained evidence is still intact."""


class DeferredEvidence:
    def __init__(self, store, writer, *, max_items=128, max_bytes=4 * 1024 * 1024):
        self.store, self.writer = store, writer
        self.max_items, self.max_bytes = max_items, max_bytes
        self.lock = threading.RLock()
        self.flush_lock = threading.RLock()
        self.pending = OrderedDict()
        self.bytes = 0
        self.retry = StorageRetry()
        self.flushed = self.errors = self.rejected = 0

    def submit(self, key, payload):
        raw = json.dumps(payload, separators=(',', ':'), sort_keys=True).encode('utf-8')
        with self.lock:
            previous = self.pending.get(key)
            if previous is not None:
                if previous != raw:
                    raise ValueError('Evidence key already queued with a different payload')
                return False
            if len(self.pending) >= self.max_items or self.bytes + len(raw) > self.max_bytes:
                self.rejected += 1
                raise EvidenceBufferFull('Deferred evidence RAM budget exhausted; observation remains a gap')
            self.pending[key] = raw
            self.bytes += len(raw)
        return True

    def full(self):
        """RAM-only admission hint; submit still checks the exact encoded size."""
        with self.lock:
            return len(self.pending) >= self.max_items or self.bytes >= self.max_bytes

    def flush(self, *, force=False, limit=4):
        with self.flush_lock:
            if not force and self.retry.delay() > 0:
                return 0
            completed = 0
            while force or completed < limit:
                with self.lock:
                    if not self.pending:
                        break
                    key, raw = next(iter(self.pending.items()))
                try:
                    if force:
                        explicit = getattr(self.store, 'explicit_sqlite', None)
                        with explicit() if callable(explicit) else nullcontext():
                            self.writer(json.loads(raw))
                    else:
                        with background_sqlite(self.store):
                            self.writer(json.loads(raw))
                except Exception as exc:
                    self.retry.defer(exc)
                    if not force and (is_sqlite_busy(exc) or isinstance(exc, StoragePressure)):
                        return completed
                    with self.lock:
                        self.errors += 1
                    raise
                with self.lock:
                    self.pending.pop(key)
                    self.bytes -= len(raw)
                    self.flushed += 1
                self.retry.recovered()
                completed += 1
            return completed

    def snapshot(self):
        with self.lock:
            return dict(pending=len(self.pending), bytes=self.bytes,
                        max_items=self.max_items, max_bytes=self.max_bytes,
                        flushed=self.flushed, errors=self.errors, rejected=self.rejected,
                        storage_retry=self.retry.snapshot())
