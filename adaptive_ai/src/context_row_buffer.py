"""Coalesce complete, cumulative shadow observations away from realtime SQLite I/O."""
import threading
import sys
import time
from itertools import islice
from sqlite_background import admit_background_write

from telemetry import RUNTIME_DEBUG, TELEMETRY


class ContextRowBuffer:
    def __init__(self, store, name, statement, *, flush_seconds=30., max_bytes=16 * 1024 * 1024, max_rows=4096, shared_columns=(), batch_rows=64):
        self.store, self.name, self.statement = store, str(name), statement
        self.lock = threading.Lock()
        self.flush_lock = threading.Lock()
        self.pending = {}
        self.pending_sizes = {}
        self.shared_columns = tuple(dict.fromkeys(shared_columns))
        self._shared_refs = {}
        self.flushed = self.errors = 0
        self.flush_seconds = max(0., float(flush_seconds))
        self.max_bytes, self.max_rows = max(1, int(max_bytes)), max(1, int(max_rows))
        self.bytes = self.inflight_count = self.overflow_flushes = 0
        self.first_pending_at = None
        self.batch_rows = max(1, min(256, int(batch_rows)))
        self.commits = 0
        self.max_store_hold_ms = 0.

    @staticmethod
    def _row_bytes(row):
        # Conservative charge for immutable scalars/strings and dictionary/key
        # overhead. Shared JSON strings are charged for each row as well.
        return 512 + sys.getsizeof(row) + sum(sys.getsizeof(value) for value in row)

    def _row_charge(self, row):
        base = self._row_bytes(row)
        shared = {}
        if not self.shared_columns:
            return base, shared
        owner = (str(row[0]), str(row[1]))
        for column in self.shared_columns:
            if column >= len(row):
                continue
            value = row[column]
            if type(value) is str and sys.getsizeof(value) >= 1024:
                size = sys.getsizeof(value)
                # Share only the same row/column across pending/in-flight
                # versions. Other sensor rows stay conservatively charged even
                # when somebody passes the same string to multiple SQL bindings.
                token = owner + (column, id(value))
                shared[token] = (value, size + 512)
                base -= size
        return base, shared

    @staticmethod
    def _reference_updates(incoming, previous):
        changes = {token: (value, size, 1) for token, (value, size) in incoming.items()}
        for token, (value, size) in previous.items():
            delta = changes[token][2] - 1 if token in changes else -1
            changes[token] = (value, size, delta)
        return changes

    def _reference_delta(self, changes):
        delta = 0
        for token, (value, size, change) in changes.items():
            before = self._shared_refs[token][2] if token in self._shared_refs else 0
            after = before + change
            if after < 0:
                raise RuntimeError('Context payload reference underflow')
            if before == 0 and after > 0:
                delta += size
            elif before > 0 and after == 0:
                delta -= size
        return delta

    def _apply_reference_updates(self, changes):
        for token, (value, size, change) in changes.items():
            if not change:
                continue
            before = self._shared_refs[token][2] if token in self._shared_refs else 0
            after = before + change
            if after:
                # Strong reference prevents identity reuse while any row owns it.
                self._shared_refs[token] = (value, size, after)
            else:
                del self._shared_refs[token]

    def _release_row(self, row, base):
        _, shared = self._row_charge(row)
        changes = self._reference_updates({}, shared)
        self.bytes += -base + self._reference_delta(changes)
        self._apply_reference_updates(changes)

    def submit(self, rows):
        prepared = [(row, *self._row_charge(row)) for row in map(tuple, rows)]
        if any(size + sum(part[1] for part in shared.values()) > self.max_bytes
               for row, size, shared in prepared):
            raise ValueError("Context row exceeds RAM buffer budget")
        # Complete cumulative snapshots coalesce without discarding votes/samples.
        # Rare capacity pressure forces the existing durable flush, not a spill file.
        for row, size, shared in prepared:
            key = (str(row[0]), str(row[1]))
            while True:
                with self.lock:
                    previous = self.pending.get(key)
                    old_shared = self._row_charge(previous)[1] if previous is not None else {}
                    changes = self._reference_updates(shared, old_shared)
                    delta = size - self.pending_sizes.get(key, 0) + self._reference_delta(changes)
                    count = len(self.pending) + self.inflight_count + int(previous is None)
                    if self.bytes + delta <= self.max_bytes and count <= self.max_rows:
                        self.pending[key] = row
                        self.pending_sizes[key] = size
                        self.bytes += delta
                        self._apply_reference_updates(changes)
                        if self.first_pending_at is None:
                            self.first_pending_at = time.monotonic()
                        break
                    self.overflow_flushes += 1
                self.flush()

    def flush_if_due(self, now=None):
        now = time.monotonic() if now is None else float(now)
        with self.lock:
            due = bool(self.pending and (
                now - self.first_pending_at >= self.flush_seconds
                or self.bytes >= self.max_bytes * .75
                or len(self.pending) >= self.max_rows * .75))
        return self.flush() if due else 0

    def flush(self):
        # Serialize drains through commit: an older batch cannot overwrite a newer one.
        with self.flush_lock:
            with self.lock:
                if self.pending:
                    admit_background_write(self.store)
                batch, self.pending = self.pending, {}
                batch_sizes, self.pending_sizes = self.pending_sizes, {}
                queued_at, self.first_pending_at = self.first_pending_at, None
                self.inflight_count = len(batch)
            if not batch:
                return 0
            started = time.perf_counter()
            trace = RUNTIME_DEBUG.begin('context_rows_persist', table=self.name,
                                        rows=len(batch)) if RUNTIME_DEBUG.enabled else None
            status = 'error'
            committed = 0
            try:
                while batch:
                    keys = tuple(islice(batch, self.batch_rows))
                    queued = time.perf_counter()
                    with self.store.lock:
                        acquired = time.perf_counter()
                        with self.store.conn() as connection, connection.storage_batch():
                            connection.executemany(self.statement, [batch[key] for key in keys])
                    finished = time.perf_counter()
                    with self.lock:
                        self.flushed += len(keys)
                        self.commits += 1
                        self.max_store_hold_ms = max(self.max_store_hold_ms, (finished-acquired)*1000)
                        for key in keys:
                            self._release_row(batch.pop(key), batch_sizes.pop(key))
                        self.inflight_count -= len(keys)
                    committed += len(keys)
                    TELEMETRY.observe('context_rows_store_lock_wait', (acquired-queued)*1000)
                    TELEMETRY.observe('context_rows_store_hold', (finished-acquired)*1000)
                    if batch:
                        time.sleep(0)  # Other owners can acquire Store before the next commit.
                status = 'ok'
                return committed
            except Exception:
                with self.lock:
                    for key, row in batch.items():
                        if key in self.pending:
                            self._release_row(row, batch_sizes[key])
                        else:
                            self.pending[key] = row
                            self.pending_sizes[key] = batch_sizes[key]
                    self.inflight_count = 0
                    if batch:
                        self.first_pending_at = min(queued_at, self.first_pending_at) if self.first_pending_at is not None else queued_at
                    self.errors += 1
                raise
            finally:
                TELEMETRY.observe('context_rows_persist', (time.perf_counter() - started) * 1000)
                RUNTIME_DEBUG.end(trace, status=status)

    def snapshot(self):
        with self.lock:
            return dict(pending=len(self.pending), flushed=self.flushed, errors=self.errors,
                        bytes=self.bytes, max_bytes=self.max_bytes, max_rows=self.max_rows,
                        inflight=self.inflight_count, flush_seconds=self.flush_seconds,
                        overflow_flushes=self.overflow_flushes,
                        batch_rows=self.batch_rows, commits=self.commits,
                        max_store_hold_ms=self.max_store_hold_ms,
                        shared_payload_columns=self.shared_columns,
                        shared_payloads=len(self._shared_refs),
                        shared_payload_references=sum(part[2] for part in self._shared_refs.values()),
                        shared_payload_bytes=sum(part[1] for part in self._shared_refs.values()),
                        duplicate_payload_charge_avoided_bytes=sum((part[1] - 512) * (part[2] - 1)
                                                                   for part in self._shared_refs.values()))
