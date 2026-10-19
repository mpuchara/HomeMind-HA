"""Bounded immutable evidence retained across transient SQLite write failures.

Existing owners drain the queue; no new thread and no policy replay. Explicit
flushes bypass the background timeout/backoff and expose durability failures.
"""
from collections import OrderedDict
from contextlib import nullcontext
from itertools import islice
import json
import threading

from sqlite_background import background_sqlite, is_sqlite_busy
from storage_budget import StoragePressure, StorageRetry


class EvidenceBufferFull(BufferError):
    """No room for a new observation; retained evidence is still intact."""


class DeferredEvidence:
    def __init__(self, store, writer, *, max_items=128, max_bytes=4 * 1024 * 1024, batch_writer=None):
        self.store, self.writer = store, writer
        self.batch_writer = batch_writer
        self.batch_items = 32
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

    def retained_payloads(self):
        """Detached read of accepted evidence, including a transaction in flight."""
        with self.lock:
            rows = tuple(self.pending.values())
        return [json.loads(raw) for raw in rows]

    def has_pending_prefix(self, prefix):
        with self.lock:
            return any(str(key).startswith(prefix) for key in self.pending)

    def acknowledge(self, key, payload):
        """Release one immutable payload only after its transaction committed."""
        raw = json.dumps(payload, separators=(',', ':'), sort_keys=True).encode('utf-8')
        with self.lock:
            current = self.pending.get(key)
            if current is None:
                return False
            if current != raw:
                raise ValueError('Committed evidence differs from the retained payload')
            self.pending.pop(key)
            self.bytes -= len(raw)
            self.flushed += 1
        return True

    def flush(self, *, force=False, limit=4):
        with self.flush_lock:
            if not force and self.retry.delay() > 0:
                return 0
            completed = 0
            while force or completed < limit:
                with self.lock:
                    if not self.pending:
                        break
                    count = len(self.pending) if force else max(1, limit - completed)
                    # Keep decode/transaction memory bounded even at an explicit
                    # durability barrier. All detached entries remain charged
                    # and immutable until their whole transaction commits.
                    count = min(1024, self.batch_items, count) if self.batch_writer else 1
                    batch = list(islice(self.pending.items(), count))
                try:
                    def write():
                        payloads = [json.loads(raw) for _, raw in batch]
                        if self.batch_writer:
                            self.batch_writer(payloads)
                        else:
                            self.writer(payloads[0])
                    if force:
                        explicit = getattr(self.store, 'explicit_sqlite', None)
                        with explicit() if callable(explicit) else nullcontext():
                            write()
                    else:
                        with background_sqlite(self.store):
                            write()
                except Exception as exc:
                    self.retry.defer(exc)
                    if not force and (is_sqlite_busy(exc) or isinstance(exc, StoragePressure)):
                        return completed
                    with self.lock:
                        self.errors += 1
                    raise
                with self.lock:
                    for key, raw in batch:
                        # A typed batch writer may acknowledge earlier committed
                        # groups before attempting a later transaction.
                        if self.pending.get(key) == raw:
                            self.pending.pop(key)
                            self.bytes -= len(raw)
                            self.flushed += 1
                self.retry.recovered()
                completed += len(batch)
            return completed

    def snapshot(self):
        with self.lock:
            return dict(pending=len(self.pending), bytes=self.bytes,
                        max_items=self.max_items, max_bytes=self.max_bytes,
                        flushed=self.flushed, errors=self.errors, rejected=self.rejected,
                        storage_retry=self.retry.snapshot())
