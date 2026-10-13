"""Coalesce complete, cumulative shadow observations away from realtime SQLite I/O."""
import threading
import sys
import time

from telemetry import RUNTIME_DEBUG, TELEMETRY


class ContextRowBuffer:
    def __init__(self, store, name, statement, *, flush_seconds=30., max_bytes=16 * 1024 * 1024, max_rows=4096):
        self.store, self.name, self.statement = store, str(name), statement
        self.lock = threading.Lock()
        self.flush_lock = threading.Lock()
        self.pending = {}
        self.pending_sizes = {}
        self.flushed = self.errors = 0
        self.flush_seconds = max(0., float(flush_seconds))
        self.max_bytes, self.max_rows = max(1, int(max_bytes)), max(1, int(max_rows))
        self.bytes = self.inflight_count = self.overflow_flushes = 0
        self.first_pending_at = None

    @staticmethod
    def _row_bytes(row):
        # Conservative charge for immutable scalars/strings and dictionary/key
        # overhead. Shared JSON strings are charged for each row as well.
        return 512 + sys.getsizeof(row) + sum(sys.getsizeof(value) for value in row)

    def submit(self, rows):
        prepared = [(row, self._row_bytes(row)) for row in map(tuple, rows)]
        if any(size > self.max_bytes for row, size in prepared):
            raise ValueError("Context row exceeds RAM buffer budget")
        # Complete cumulative snapshots coalesce without discarding votes/samples.
        # Rare capacity pressure forces the existing durable flush, not a spill file.
        for row, size in prepared:
            key = (str(row[0]), str(row[1]))
            while True:
                with self.lock:
                    previous = self.pending.get(key)
                    delta = size - self.pending_sizes.get(key, 0)
                    count = len(self.pending) + self.inflight_count + int(previous is None)
                    if self.bytes + delta <= self.max_bytes and count <= self.max_rows:
                        self.pending[key] = row
                        self.pending_sizes[key] = size
                        self.bytes += delta
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
                batch, self.pending = self.pending, {}
                batch_sizes, self.pending_sizes = self.pending_sizes, {}
                queued_at, self.first_pending_at = self.first_pending_at, None
                self.inflight_count = len(batch)
                reserved_bytes = sum(batch_sizes.values())
            if not batch:
                return 0
            started = time.perf_counter()
            trace = RUNTIME_DEBUG.begin('context_rows_persist', table=self.name,
                                        rows=len(batch)) if RUNTIME_DEBUG.enabled else None
            status = 'error'
            try:
                with self.store.lock, self.store.conn() as connection:
                    connection.executemany(self.statement, list(batch.values()))
                with self.lock:
                    self.flushed += len(batch)
                    self.bytes -= reserved_bytes
                    self.inflight_count = 0
                status = 'ok'
                return len(batch)
            except Exception:
                with self.lock:
                    for key, row in batch.items():
                        if key in self.pending:
                            self.bytes -= batch_sizes[key]
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
                        overflow_flushes=self.overflow_flushes)
