"""Narrow busy-wait policy for batches which already retain data on failure."""
from contextlib import nullcontext
import sqlite3


def is_sqlite_busy(exc):
    """Recognize retryable SQLite contention without swallowing other failures."""
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
