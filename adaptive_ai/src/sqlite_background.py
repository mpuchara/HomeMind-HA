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


def background_sqlite(store, *, retryable=True):
    scope = getattr(store, "background_sqlite", None)
    if not callable(scope):
        return nullcontext()
    return scope() if retryable else scope(retryable=False)


def admit_background_write(store):
    check = getattr(store, 'admit_background_write', None)
    if callable(check):
        check()


def connection_session(store):
    """Reuse a worker-owned connection; conn() still owns each transaction."""
    scope = getattr(store, 'connection_session', None)
    return scope() if callable(scope) else nullcontext()


def run_connection_worker(store, stop_event, callback, retry, report):
    """Retain worker retry behavior when opening its reusable connection fails."""
    while not stop_event.is_set():
        if stop_event.wait(retry.delay()):
            break
        try:
            with connection_session(store):
                callback()
            return
        except Exception as exc:
            if retry.defer(exc):
                try:
                    report(exc)
                except Exception:
                    pass
    # The owner callback also performs its explicit final flush when already
    # stopped. A stop before connection admission must not skip that barrier.
    callback()
