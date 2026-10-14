"""Narrow busy-wait policy for batches which already retain data on failure."""
from contextlib import nullcontext
import sqlite3


class DeferredBatchError(RuntimeError):
    """Keep native failures when independent deferred queues are drained together."""
    def __init__(self, errors):
        self.errors = tuple(errors)
        super().__init__('; '.join(f'{name}: {type(exc).__name__}: {exc}'
                                 for name, exc in self.errors))


def is_sqlite_busy(exc):
    """Recognize retryable SQLite contention without swallowing other failures."""
    if isinstance(exc, DeferredBatchError):
        return bool(exc.errors) and all(is_sqlite_busy(error) for _, error in exc.errors)
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, 'sqlite_errorcode', None)
    if code is not None:
        return code & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    # Some adapters and tests do not attach SQLite's native result code.
    message = str(exc).lower()
    return message == 'database is locked' or message.startswith('database table is locked')


def background_sqlite(store):
    scope = getattr(store, "background_sqlite", None)
    return scope() if callable(scope) else nullcontext()


def admit_background_write(store):
    check = getattr(store, 'admit_background_write', None)
    if callable(check):
        check()
