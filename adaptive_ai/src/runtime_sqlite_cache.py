"""Bounded idle inference connections; SQLite still owns data freshness.

Only explicitly started realtime inference sessions participate. Connections are
exclusively leased, and every cursor is closed before a lease becomes idle so
the cache cannot retain a WAL reader snapshot between decisions.
"""
from pathlib import Path
import re
import sqlite3
import threading
import time
import weakref
from functools import lru_cache

from storage_budget import BudgetConnection


def session_sql_taints(sql):
    """Cache syntax only, never data, admission, results or connection state.

    Bound retained source bytes as well as entry count. Unusual objects and
    large/generated statements keep the original conservative classification.
    """
    if type(sql) is str and len(sql) <= 2048:
        return _cached_session_sql_taints(sql)
    return _session_sql_taints(sql)


@lru_cache(maxsize=512)
def _cached_session_sql_taints(sql):
    return _session_sql_taints(sql)


def _session_sql_taints(sql):
    clean = re.sub(r'\A\s*(?:(?:--[^\n]*\n|/\*.*?\*/)\s*)*', '', str(sql), flags=re.S).strip()
    pragma = re.match(r'PRAGMA\s+(?:\w+\.)?(\w+)\s*(.*)', clean, re.I | re.S)
    return bool(re.match(r'(ATTACH|DETACH|VACUUM|SCRIPT)\b', clean, re.I)
                or re.search(r'\bTEMP(?:ORARY)?\b', clean, re.I)
                or (pragma and pragma[2].strip(' ;') and pragma[1].lower()
                    not in ('busy_timeout', 'wal_autocheckpoint')))


def memory_headroom():
    """Respect host and ancestor cgroup limits, including container headroom."""
    total, available = None, None
    try:
        values = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, _, value = line.partition(':')
            if key in ('MemTotal', 'MemAvailable'):
                values[key] = int(value.split()[0]) / 1024
        total, available = values.get('MemTotal'), values.get('MemAvailable')
        groups = Path('/proc/self/cgroup').read_text().splitlines()
        for line in groups:
            _, controllers, relative = line.split(':', 2)
            if controllers == '':
                root, limit_name, used_name = Path('/sys/fs/cgroup'), 'memory.max', 'memory.current'
            elif 'memory' in controllers.split(','):
                root, limit_name, used_name = Path('/sys/fs/cgroup/memory'), 'memory.limit_in_bytes', 'memory.usage_in_bytes'
            else:
                continue
            # Namespace-mounted containers expose their own limits at root even
            # when /proc/self/cgroup describes a path outside that mount.
            directory = root / relative.lstrip('/')
            if not directory.is_dir():
                directory = root
            while directory == root or root in directory.parents:
                try:
                    limit = int((directory / limit_name).read_text()) / (1024 ** 2)
                    used = int((directory / used_name).read_text()) / (1024 ** 2)
                    total = min(total, limit) if total is not None else limit
                    free = max(0, limit - used)
                    available = min(available, free) if available is not None else free
                except (OSError, ValueError):
                    pass  # 'max' is unbounded, ancestor limits still apply.
                if directory == root:
                    break
                directory = directory.parent
    except (OSError, ValueError):
        pass
    return total, available


class CachedBudgetConnection(BudgetConnection):
    """Keep abandoned SELECT cursors and session-local SQL out of idle leases."""
    cache_tracking = False
    cache_tainted = False

    def cursor(self, factory=None):
        cursor = super().cursor(factory)
        if self.cache_tracking:
            self.cache_cursors.add(cursor)
            if factory is not None:
                self.cache_tainted = True
        return cursor

    def storage_statement(self, sql, parameters=()):
        if self.cache_tracking and session_sql_taints(sql):
            self.cache_tainted = True
        return super().storage_statement(sql, parameters)

    def executescript(self, sql):
        if self.cache_tracking:
            self.cache_tainted = True
        return super().executescript(sql)

    def set_authorizer(self, callback):
        if self.cache_tracking:
            self.cache_tainted = True
        return super().set_authorizer(callback)

    def create_function(self, *args, **kwargs):
        self.cache_tainted = self.cache_tracking or self.cache_tainted
        return super().create_function(*args, **kwargs)

    def create_aggregate(self, *args, **kwargs):
        self.cache_tainted = self.cache_tracking or self.cache_tainted
        return super().create_aggregate(*args, **kwargs)

    def create_collation(self, *args, **kwargs):
        self.cache_tainted = self.cache_tracking or self.cache_tainted
        return super().create_collation(*args, **kwargs)

    def create_window_function(self, *args, **kwargs):
        self.cache_tainted = self.cache_tracking or self.cache_tainted
        return super().create_window_function(*args, **kwargs)

    def set_progress_handler(self, *args, **kwargs):
        self.cache_tainted = self.cache_tracking or self.cache_tainted
        return super().set_progress_handler(*args, **kwargs)

    def prepare_cache_tracking(self):
        self.cache_cursors = weakref.WeakSet()
        self.cache_tracking = True

    def finish_lease(self):
        for cursor in list(self.cache_cursors):
            cursor.close()
        self.cache_cursors.clear()
        self.rollback()
        self.set_trace_callback(None)
        return (not self.cache_tainted and self.row_factory is sqlite3.Row
                and self.isolation_level == '')


class RuntimeSQLiteCache:
    MAX_CONNECTIONS = 4
    MAX_CONNECTION_MB = 128

    def __init__(self, configured_mb=256, *, memory=memory_headroom, clock=time.monotonic):
        self.configured_mb = max(0, min(512, int(configured_mb)))
        self.memory, self.clock = memory, clock
        self.lock = threading.RLock()
        self.idle = []
        self.leased = {}
        self.closed = False
        self.effective_mb = 0
        self.last_sample = float('-inf')
        self.hits = self.misses = self.bypasses = self.discards = 0
        self._refresh()

    def _refresh(self):
        if self.clock() - self.last_sample < 5:
            return
        total, available = self.memory()
        self.last_sample = self.clock()
        self.effective_mb = (int(max(0, min(self.configured_mb, total / 16,
                                 available / 4, available - 512)))
                             if total is not None and available is not None else 0)
        # Reclaim idle caches first. Active leases are retired on return when
        # host/container headroom shrinks; never interrupt an active transaction.
        while self.idle and self._charged() > self.effective_mb:
            connection, _ = self.idle.pop()
            connection.close()
            self.discards += 1

    def _charged(self):
        return sum(size for _, size in self.idle) + sum(self.leased.values())

    def acquire(self, factory):
        with self.lock:
            if self.closed:
                return None
            self._refresh()
            if self.idle:
                connection, size = self.idle.pop()
                self.leased[connection] = size
                self.hits += 1
                return connection
            slots = self.MAX_CONNECTIONS - len(self.leased)
            size = min(self.MAX_CONNECTION_MB, (self.effective_mb - self._charged()) // max(1, slots))
            if slots <= 0 or size < 16:
                self.bypasses += 1
                return None
            reservation = object()
            self.leased[reservation] = size
        # Cold SQLite setup may encounter slow filesystem I/O. Reserve the RAM
        # target first, then open outside the pool lock so warm peers can run.
        try:
            connection = factory(size)
        except BaseException:
            with self.lock:
                del self.leased[reservation]
            raise
        with self.lock:
            self.leased[connection] = self.leased.pop(reservation)
            self.misses += 1
            return connection

    def trim(self):
        with self.lock:
            if not self.closed:
                self._refresh()

    def release(self, connection, *, healthy):
        try:
            healthy = connection.finish_lease() and healthy
        except sqlite3.Error:
            healthy = False
        with self.lock:
            self._refresh()
            size = self.leased.pop(connection)
            if healthy and not self.closed and self._charged() + size <= self.effective_mb:
                self.idle.append((connection, size))
            else:
                connection.close()
                self.discards += 1

    def stop(self):
        with self.lock:
            self.closed = True
            for connection, _ in self.idle:
                connection.close()
            self.idle.clear()

    def snapshot(self):
        with self.lock:
            return dict(active=not self.closed, configured_mb=self.configured_mb,
                        effective_mb=self.effective_mb, cache_target_mb=self._charged(),
                        idle=len(self.idle), leased=len(self.leased), hits=self.hits,
                        misses=self.misses, bypasses=self.bypasses, discards=self.discards,
                        max_connections=self.MAX_CONNECTIONS, mmap_mb=0)
