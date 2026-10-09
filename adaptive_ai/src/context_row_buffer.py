"""Coalesce complete, cumulative shadow observations away from realtime SQLite I/O."""
import threading
import time

from telemetry import RUNTIME_DEBUG, TELEMETRY


class ContextRowBuffer:
    def __init__(self, store, name, statement):
        self.store, self.name, self.statement = store, str(name), statement
        self.lock = threading.Lock()
        self.flush_lock = threading.Lock()
        self.pending = {}
        self.flushed = self.errors = 0

    def submit(self, rows):
        # All fields must already be immutable scalars/JSON strings. Replacement is
        # safe only for complete cumulative snapshots, never individual sample deltas.
        with self.lock:
            for row in rows:
                self.pending[(str(row[0]), str(row[1]))] = tuple(row)

    def flush(self):
        # Serialize drains through commit: an older batch cannot overwrite a newer one.
        with self.flush_lock:
            with self.lock:
                batch, self.pending = self.pending, {}
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
                status = 'ok'
                return len(batch)
            except Exception:
                with self.lock:
                    for key, row in batch.items():
                        self.pending.setdefault(key, row)
                    self.errors += 1
                raise
            finally:
                TELEMETRY.observe('context_rows_persist', (time.perf_counter() - started) * 1000)
                RUNTIME_DEBUG.end(trace, status=status)

    def snapshot(self):
        with self.lock:
            return dict(pending=len(self.pending), flushed=self.flushed, errors=self.errors)
