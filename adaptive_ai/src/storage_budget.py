"""Storage admission at every SQL write, including cursors and isolated workers.

Age retention is not a byte quota. SQLite page limits bound the main database;
admission/progress checks stop WAL growth and leave disk space for Home Assistant.
"""
import re
import os
import json
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager, closing
from pathlib import Path
from sqlite_background import is_sqlite_busy

MIB = 1024 * 1024
MAX_WRITE_BYTES = 16 * MIB
SAMPLE_INTERVAL_SECONDS = 0.05
SAMPLE_PAYLOAD_BYTES = MIB


def _file_bytes(path):
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0

# Durable preferences, models and control ownership are deliberately absent.
BULK_TABLES = frozenset({
    'entity_history', 'historical_experiences', 'events', 'provenance_events',
    'provenance_history_links', 'provenance_decisions', 'provenance_experiences',
    'feature_observation_events', 'feature_windows', 'feature_window_entities',
    'decision_history', 'candidate_generation_decisions', 'candidate_generation_pairs',
    'adaptation_episode_observations', 'adaptation_environment_snapshots',
    'episode_evaluator_episodes', 'episode_evaluator_policy_results',
    'automatic_reward_experiences', 'policy_backend_shadow_events',
    'home_checkpoints', 'model_backups', 'confidence_probability_episodes',
})


class StoragePressure(sqlite3.OperationalError):
    """A storage limit was reached before publishing a new transaction."""


def _is_write(sql):
    # CTEs are conservatively checked. Read PRAGMAs configure/inspect SQLite only;
    # application data writes through execute/cursor/executescript are guarded.
    sql = re.sub(r'\A\s*(?:(?:--[^\n]*\n|/\*.*?\*/)\s*)*', '', str(sql), flags=re.S)
    return sql.split(None, 1)[0].upper() not in ('SELECT', 'EXPLAIN', 'PRAGMA', 'BEGIN', 'ROLLBACK', 'SAVEPOINT') if sql.strip() else False


def _payload_bytes(parameters):
    values = parameters.values() if isinstance(parameters, dict) else parameters
    return sum(len(v.encode('utf-8')) if isinstance(v, str) else
               len(v) if isinstance(v, (bytes, bytearray, memoryview)) else 16
               for v in values)


class StorageBudget:
    def __init__(self, path, options):
        self.path = Path(path)
        self.mode = options.get('storage_budget_mode', 'auto')
        self.auto_use_percent = max(10, min(80, int(options.get('storage_auto_use_percent', 50))))
        self.auto_pool_bytes = None
        self.db_limit = max(128, min(65536, int(options.get('storage_database_max_mb', 2048)))) * MIB
        self.wal_limit = max(32, min(4096, int(options.get('storage_wal_max_mb', 128)))) * MIB
        self.reserve = max(256, min(65536, int(options.get('storage_min_free_mb', 2048)))) * MIB
        self.hard_reserve = min(256 * MIB, self.reserve // 2)
        self.lock = threading.Lock()
        self.local = threading.local()
        self._last_sample_at = float('-inf')
        self._unchecked_payload_bytes = 0
        self.training_process = os.environ.get('ADAPTIVE_AI_TRAINING_WORKER') == '1'
        self.stats = dict(level='normal', reason=None, blocked_writes=0,
                          db_bytes=0, database_used_bytes=0, wal_bytes=0,
                          free_bytes=None, sample_error=None)
        inherited = os.environ.get('ADAPTIVE_AI_STORAGE_BUDGET')
        if os.environ.get('ADAPTIVE_AI_STORAGE_WORKER') == '1' and inherited:
            try:
                data = json.loads(inherited)
            except (ValueError, TypeError) as exc:
                raise StoragePressure('Storage protection: invalid inherited worker budget') from exc
            if (not isinstance(data, dict)
                    or data.get('path') != str(self.path.resolve())
                    or data.get('mode') not in ('auto', 'manual')
                    or any(type(data.get(key)) is not int or data[key] < 0
                           for key in ('db_limit', 'wal_limit', 'reserve', 'hard_reserve', 'auto_use_percent'))
                    or min(data['db_limit'], data['wal_limit']) < 4096
                    or data['hard_reserve'] > data['reserve']
                    or not 10 <= data['auto_use_percent'] <= 80
                    or 'auto_pool_bytes' not in data
                    or (data['mode'] == 'auto' and
                        (type(data['auto_pool_bytes']) is not int or data['auto_pool_bytes'] <= 0))):
                raise StoragePressure('Storage protection: invalid inherited worker budget')
            for key in ('mode', 'db_limit', 'wal_limit', 'reserve', 'hard_reserve',
                        'auto_use_percent', 'auto_pool_bytes'):
                setattr(self, key, data[key])
            return
        if self.mode != 'manual':
            self.mode = 'auto'
            # A failed sample cannot fall back to the old fixed limits. Retry when
            # sampling next; writes remain blocked until capacity is known.
            self.db_limit = self.wal_limit = self.reserve = self.hard_reserve = 0
            try:
                self._configure_auto()
            except OSError:
                pass

    def _configure_auto(self):
        free = shutil.disk_usage(self.path.parent).free
        db = _file_bytes(self.path)
        wal_path = Path(str(self.path) + '-wal')
        wal = _file_bytes(wal_path)
        # Count existing app files back into the available pool. As the app grows,
        # free space falls by the same amount: restarting cannot ratchet the quota
        # upwards. Other applications' data is excluded. Freeze for this process,
        # keeping the reserved space protected if another add-on later grows.
        pool = max(0, int(free) + db + wal)
        allocation = pool * self.auto_use_percent // 100
        self.wal_limit = max(4096, allocation // 20)
        self.db_limit = max(4096, allocation - self.wal_limit)
        self.reserve = pool - allocation
        self.hard_reserve = self.reserve // 10
        self.auto_pool_bytes = pool

    def worker_environment(self):
        # A child created later must not recalculate a smaller reserve when some
        # other add-on has grown. It shares the running parent's frozen admission.
        if self.sample().get('sample_error') is not None:
            raise self.pressure()
        data = dict(path=str(self.path.resolve()))
        for key in ('mode', 'db_limit', 'wal_limit', 'reserve', 'hard_reserve',
                    'auto_use_percent', 'auto_pool_bytes'):
            data[key] = getattr(self, key)
        return {'ADAPTIVE_AI_STORAGE_WORKER': '1',
                'ADAPTIVE_AI_STORAGE_BUDGET': json.dumps(data, separators=(',', ':'))}

    @contextmanager
    def maintenance(self, *, update_tables=()):
        previous = getattr(self.local, 'maintenance', False)
        previous_updates = getattr(self.local, 'update_tables', frozenset())
        self.local.maintenance = True
        self.local.update_tables = frozenset(update_tables)
        try:
            yield
        finally:
            self.local.maintenance = previous
            self.local.update_tables = previous_updates

    def sample(self):
        try:
            if self.mode == 'auto' and self.auto_pool_bytes is None:
                self._configure_auto()
            db = _file_bytes(self.path)
            wal_path = Path(str(self.path) + '-wal')
            wal = _file_bytes(wal_path)
            free = shutil.disk_usage(self.path.parent).free
            hard = free < self.hard_reserve or wal >= self.wal_limit
            with self.lock:
                used = self.stats['database_used_bytes']
            soft = free < self.reserve or used >= self.db_limit * .90 or wal >= self.wal_limit * .5
            reason = ('disk_reserve' if free < self.reserve else
                      'wal_budget' if wal >= self.wal_limit * .5 else
                      'database_budget' if used >= self.db_limit * .90 else None)
            data = dict(db_bytes=db, wal_bytes=wal, free_bytes=free,
                        level='blocked' if hard else 'paused' if soft else 'normal',
                        reason=reason, sample_error=None)
        except OSError as exc:
            # Unknown free space must never permit uncontrolled writes.
            data = dict(level='blocked', reason='storage_sample_failed',
                        sample_error=f'{type(exc).__name__}: {exc}')
        with self.lock:
            self.stats.update(data)
            self._last_sample_at = time.monotonic()
            self._unchecked_payload_bytes = 0
            self.stats['disk_samples'] = self.stats.get('disk_samples', 0) + 1
            return dict(self.stats)

    def snapshot(self):
        with self.lock:
            return {**self.stats, 'database_max_bytes': self.db_limit,
                    'budget_mode': self.mode, 'auto_use_percent': self.auto_use_percent,
                    'auto_pool_bytes': self.auto_pool_bytes,
                    'wal_max_bytes': self.wal_limit, 'minimum_free_bytes': self.reserve,
                    'max_statement_payload_bytes': MAX_WRITE_BYTES,
                    'batch_sample_interval_seconds': SAMPLE_INTERVAL_SECONDS,
                    'batch_sample_payload_bytes': SAMPLE_PAYLOAD_BYTES}

    def reject(self, reason):
        with self.lock:
            self.stats['blocked_writes'] += 1
            self.stats['reason'] = reason
        raise self.pressure(reason)

    def pressure(self, reason=None):
        data = self.snapshot()
        reason = reason or data['reason']
        error = StoragePressure(
            f"Storage protection: {reason}; "
            f"database used={data['database_used_bytes']} bytes, WAL={data['wal_bytes']} bytes, "
            f"free={data['free_bytes']} bytes; recording/training paused"
        )
        error.reason = reason
        return error

    def check(self, parameters=(), *, refresh=True):
        payload_bytes = _payload_bytes(parameters)
        if payload_bytes > MAX_WRITE_BYTES:
            self.reject('statement_payload_budget')
        # Ordinary statements and commit sample freshly. Archive storage_batch
        # scopes also refresh on entry/exit. Within these scopes, executemany and the
        # SQLite VM, reuse the shared snapshot for at most 50 ms / 1 MiB of input.
        # The SQLite page cap, per-row payload cap and final commit guard remain.
        with self.lock:
            self._unchecked_payload_bytes += payload_bytes
            due = (refresh or time.monotonic() - self._last_sample_at >= SAMPLE_INTERVAL_SECONDS
                   or self._unchecked_payload_bytes >= SAMPLE_PAYLOAD_BYTES)
            data = dict(self.stats)
        if due:
            data = self.sample()
        maintenance = getattr(self.local, 'maintenance', False)
        if data['level'] == 'blocked':
            self.reject(data['reason'])
        if data['level'] == 'paused' and self.training_process:
            self.reject(data['reason'])
        if maintenance and data.get('free_bytes', 0) < self.hard_reserve:
            self.reject('disk_reserve')
        return data['level']

    def install(self, connection):
        page_size = int(connection.execute('PRAGMA page_size').fetchone()[0])
        # SQLite refuses to shrink below an existing file's page count. The soft
        # guard still pauses growth for oversized legacy files; no blind VACUUM.
        connection.execute(f'PRAGMA max_page_count={max(1, self.db_limit // page_size)}')
        connection.storage_budget = self
        connection.storage_level = None
        self.observe_pages(connection)
        connection.set_progress_handler(connection.storage_progress, 1000)

    def prepare_existing_wal(self):
        wal = Path(str(self.path) + '-wal')
        if not wal.exists() or wal.stat().st_size < min(64 * MIB, self.wal_limit // 2):
            return
        # Recovery precedes schema writes. Otherwise an old oversized WAL could
        # block startup before the periodic worker has a chance to reclaim it.
        with closing(sqlite3.connect(self.path, timeout=0, isolation_level=None)) as c:
            c.execute('PRAGMA busy_timeout=0')
            c.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
            c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()

    def observe_pages(self, connection):
        # Use the base API to avoid recursive admission. Free pages are reusable:
        # retention must not leave recording paused just because file size lags.
        count = int(sqlite3.Connection.execute(connection, 'PRAGMA page_count').fetchone()[0])
        free = int(sqlite3.Connection.execute(connection, 'PRAGMA freelist_count').fetchone()[0])
        size = int(sqlite3.Connection.execute(connection, 'PRAGMA page_size').fetchone()[0])
        with self.lock:
            self.stats['database_used_bytes'] = max(0, count - free) * size


class BudgetConnection(sqlite3.Connection):
    storage_budget = None
    storage_level = None
    storage_update_tables = frozenset()
    storage_writing = 0
    storage_external_authorizer = None
    storage_denial = None
    storage_batch_depth = 0

    @contextmanager
    def storage_batch(self):
        """Share bounded admission samples within one short archive transaction.

        Entry, exit and commit still sample freshly. Per-row payload and VM checks keep
        the existing 50 ms / 1 MiB limits between them. This scope belongs to this
        connection only; other writers and ordinary execute calls are unchanged.
        """
        if self.storage_budget is not None:
            self.storage_budget.check()
        self.storage_batch_depth += 1
        try:
            yield
            if self.storage_budget is not None:
                level = self.storage_budget.check()
                if level != 'normal':
                    self.storage_budget.reject(self.storage_budget.snapshot()['reason'])
        finally:
            self.storage_batch_depth -= 1

    def set_authorizer(self, callback):
        self.storage_external_authorizer = callback
        return super().set_authorizer(self.storage_authorize)

    def storage_authorize(self, action, table, column, database, source):
        if self.storage_external_authorizer is not None:
            result = self.storage_external_authorizer(action, table, column, database, source)
            if result != sqlite3.SQLITE_OK:
                return result
        if (self.storage_level == 'paused' and action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE)
                and table in BULK_TABLES
                and not (action == sqlite3.SQLITE_UPDATE and table in self.storage_update_tables)):
            with self.storage_budget.lock:
                self.storage_budget.stats['blocked_writes'] += 1
            self.storage_denial = self.storage_budget.pressure()
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def storage_progress(self):
        if not self.storage_writing or self.storage_budget is None:
            return 0
        try:
            self.storage_budget.check(refresh=False)
            return 0
        except StoragePressure as exc:
            self.storage_denial = exc
            return 1

    @contextmanager
    def storage_statement(self, sql, parameters=()):
        self.storage_denial = None
        budget = self.storage_budget
        write = budget is not None and _is_write(sql)
        if write:
            level = budget.check(parameters, refresh=not self.storage_batch_depth)
            update_tables = getattr(budget.local, 'update_tables', frozenset())
            # Changing the authorizer invalidates prepared statements. Thus a
            # previously cached INSERT cannot bypass a newly reached soft limit.
            if level != self.storage_level or update_tables != self.storage_update_tables:
                self.storage_level = level
                self.storage_update_tables = update_tables
                super().set_authorizer(self.storage_authorize)
            self.storage_writing += 1
        try:
            yield
            if write:
                budget.check(refresh=not self.storage_batch_depth)
        except sqlite3.DatabaseError as exc:
            # SQLite suppresses exceptions in C callbacks. Translate only a denial
            # issued by our budget; external authorizers retain SQLITE_AUTH semantics.
            if self.storage_denial is not None and getattr(exc, 'sqlite_errorcode', None) in (
                    sqlite3.SQLITE_AUTH, sqlite3.SQLITE_INTERRUPT):
                raise self.storage_denial from exc
            raise
        finally:
            self.storage_denial = None
            if write:
                self.storage_writing -= 1

    def cursor(self, factory=None):
        return super().cursor(factory or BudgetCursor)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql):
        return self.cursor().executescript(sql)

    def __exit__(self, kind, value, tb):
        if kind is None and self.in_transaction and self.storage_budget is not None:
            try:
                self.storage_budget.check()
            except BaseException:
                self.rollback()
                raise
        try:
            return super().__exit__(kind, value, tb)
        finally:
            if self.storage_budget is not None:
                self.storage_budget.observe_pages(self)

    def commit(self):
        if self.in_transaction and self.storage_budget is not None:
            try:
                self.storage_budget.check()
            except BaseException:
                self.rollback()
                raise
        return super().commit()


class BudgetCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        with self.connection.storage_statement(sql, parameters):
            return super().execute(sql, parameters)

    def executemany(self, sql, parameters):
        budget = self.connection.storage_budget
        def checked():
            for row in parameters:
                if budget is not None:
                    level = budget.check(row, refresh=False)
                    if level != self.connection.storage_level:
                        budget.reject('storage_pressure_changed_during_batch')
                yield row
        with self.connection.storage_statement(sql):
            return super().executemany(sql, checked())

    def executescript(self, sql):
        with self.connection.storage_statement('SCRIPT'):
            return super().executescript(sql)


def storage_status(store):
    budget = getattr(store, 'storage_budget', None)
    return budget.snapshot() if isinstance(budget, StorageBudget) else {}


def require_storage_available(store):
    budget = getattr(store, 'storage_budget', None)
    if isinstance(budget, StorageBudget) and budget.sample()['level'] != 'normal':
        raise budget.pressure()


def storage_recording_paused(store):
    # Producers use the existing RAM snapshot, with no disk calls in event->intent.
    return storage_status(store).get('level', 'normal') != 'normal'


def run_storage_write(store, callback, stop_event, cancel_event=None, timeout=90.0):
    """Retry the same rolled-back history batch after a transient WAL pause.

    Called outside SQL/Store locks. The existing checkpoint worker owns recovery;
    low free space, a full database and unrelated SQL errors are never retried here.
    """
    deadline = time.monotonic() + timeout
    while True:
        if stop_event.is_set() or (cancel_event is not None and cancel_event.is_set()):
            raise InterruptedError('Training storage wait cancelled')
        try:
            return callback()
        except StoragePressure as exc:
            budget = getattr(store, 'storage_budget', None)
            if not isinstance(budget, StorageBudget):
                raise
            if getattr(exc, 'reason', None) not in ('wal_budget', 'storage_pressure_changed_during_batch'):
                raise
            data = budget.sample()
            healthy_capacity = (data.get('free_bytes') is not None
                and data['free_bytes'] >= budget.reserve
                and data['database_used_bytes'] < budget.db_limit * .90)
            if (not healthy_capacity or data['reason'] not in (None, 'wal_budget')
                    or time.monotonic() >= deadline):
                raise
            with budget.lock:
                budget.stats['wal_recovery_waits'] = budget.stats.get('wal_recovery_waits', 0) + 1
            stop_event.wait(1.0)


class StorageRetry:
    """A producer wakeup must never bypass persistence-failure cooldown."""
    def __init__(self):
        self.next_attempt = 0.0
        self.last_report = float('-inf')
        self.reason = None
        self.pauses = 0
        self.busy_retries = 0

    def defer(self, exc):
        pressure = isinstance(exc, StoragePressure)
        busy = is_sqlite_busy(exc)
        self.next_attempt = time.monotonic() + (5.0 if pressure else 1.0)
        self.reason = str(exc)
        self.pauses += int(pressure)
        self.busy_retries += int(busy)
        if pressure or busy or time.monotonic() - self.last_report < 60.0:
            return False
        self.last_report = time.monotonic()
        return True

    def recovered(self):
        self.next_attempt = 0.0
        self.reason = None

    def delay(self):
        return max(0.0, self.next_attempt - time.monotonic())

    def snapshot(self):
        return dict(reason=self.reason, pauses=self.pauses, busy_retries=self.busy_retries,
                    retry_in_seconds=self.delay())
