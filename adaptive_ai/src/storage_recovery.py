"""Small background GC slices for disposable diagnostics, before recording pauses.

Raw replay history, preferences, models and qualification/calibration evidence are
not GC targets. A protection deadline is honored while its feature window exists;
orphaned deadline stamps from already-pruned windows cannot pin events forever.
"""
import math
import threading
import time

from settings import OPTIONS
from sqlite_background import is_sqlite_busy


class StorageRecovery:
    START_FRACTION = .85
    TARGET_FRACTION = .80
    BATCH_ROWS = 128
    MAX_BATCHES = 16
    SLICE_SECONDS = .080
    TABLES = ('provenance_decisions', 'feature_observation_events',
              'feature_windows', 'policy_backend_shadow_events', 'events')

    def __init__(self, store, stop_event):
        self.store, self.stop_event = store, stop_event
        self.lock = threading.Lock()
        self.active = False
        self.cursors = {}
        self.position = 0
        self.follow_up = False
        self.stats = dict(slices=0, deleted=0, scanned=0, busy=0, errors=0,
                          last_error=None, by_table={}, last_duration_ms=0.,
                          reason=None, used_before_bytes=0, used_after_bytes=0)

    @staticmethod
    def hours(key, default):
        try:
            value = float(OPTIONS.get(key, default))
            return min(336., max(1., value)) if math.isfinite(value) else default
        except (ValueError, TypeError):
            return default

    def snapshot(self):
        with self.lock:
            return {**self.stats, 'by_table': dict(self.stats['by_table']),
                    'active': self.active, 'follow_up': self.follow_up,
                    'start_fraction': self.START_FRACTION,
                    'target_fraction': self.TARGET_FRACTION,
                    'batch_rows': self.BATCH_ROWS,
                    'slice_budget_ms': self.SLICE_SECONDS * 1000,
                    'diagnostic_hours': self.hours('storage_diagnostic_retention_hours', 24),
                    'pressure_hours': min(self.hours('storage_pressure_retention_hours', 1),
                                          self.hours('storage_diagnostic_retention_hours', 24)),
                    'protected_history_days': 14}

    def _pressure(self):
        budget = self.store.storage_budget
        with self.store.conn() as connection:
            budget.observe_pages(connection)
        sampled = budget.sample()
        status = budget.snapshot()
        used, limit = status['database_used_bytes'], status['database_max_bytes']
        with self.lock:
            if sampled.get('sample_error'):
                self.active = True
            elif self.active:
                self.active = used >= limit * self.TARGET_FRACTION or sampled['level'] != 'normal'
            else:
                self.active = used >= limit * self.START_FRACTION or sampled['level'] != 'normal'
        return status

    def _spec(self, table, columns, tables, now):
        if table == 'provenance_decisions':
            required = {'created_time', 'dispatch_status', 'teaching_id', 'experiment_id', 'episode_id', 'decision_id'}
            if not required <= columns.keys():
                return None
            extra = "dispatch_status IN ('SHADOW','REJECTED') AND teaching_id IS NULL AND experiment_id IS NULL AND episode_id IS NULL"
            for child in ('provenance_experiences', 'provenance_commands'):
                if child in tables:
                    extra += f' AND NOT EXISTS (SELECT 1 FROM {child} ref WHERE ref.decision_id=provenance_decisions.decision_id)'
            return 'created_time', extra, ()
        if table == 'feature_observation_events':
            if not {'received_time', 'protected_until', 'entity_id', 'event_time'} <= columns.keys():
                return None
            if not {'feature_windows', 'feature_window_entities'} <= tables:
                # A partially installed journal cannot prove that a deadline is orphaned.
                return 'received_time', 'protected_until<?', (now,)
            return ('received_time', '''NOT EXISTS (
                SELECT 1 FROM feature_windows win INDEXED BY idx_storage_window_start
                WHERE win.start_time<=feature_observation_events.event_time
                  AND win.end_time>=feature_observation_events.event_time AND win.protected_until>=?
                  AND EXISTS (SELECT 1 FROM feature_window_entities ref
                    WHERE ref.window_id=win.window_id AND ref.entity_id=feature_observation_events.entity_id))''', (now,))
        if table == 'feature_windows':
            if ('feature_window_entities' not in tables
                    or not {'created_time', 'protected_until', 'window_id'} <= columns.keys()):
                return None
            return 'created_time', 'protected_until<?', (now,)
        if table == 'policy_backend_shadow_events':
            return ('ts', '1', ()) if 'ts' in columns else None
        if table == 'events' and {'created_at', 'level'} <= columns.keys():
            return "julianday(created_at)", "level IN ('info','debug')", ()
        return None

    def _batch(self, connection, table, spec, cutoff):
        expression, eligible, parameters = spec
        cursor = self.cursors.get(table)
        after, args = ('', ()) if cursor is None else (
            f' AND ({expression},rowid)>(?,?)', cursor)
        # Bound candidate selection BEFORE reference checks. A large set of
        # protected rows must not turn a small DELETE into a whole-table scan.
        rows = connection.execute(
            f'SELECT rowid,{expression} FROM {table} WHERE {expression}<?{after} '
            f'ORDER BY {expression},rowid LIMIT ?', (cutoff, *args, self.BATCH_ROWS)).fetchall()
        if not rows:
            return 0, 0, None
        marks = ','.join('?' for _ in rows)
        ids = [row[0] for row in rows]
        if table == 'feature_windows':
            # Child rows are bounded too; parents with remaining children wait
            # for a later slice, avoiding orphaned journal relationships.
            connection.execute(f'DELETE FROM feature_window_entities WHERE rowid IN '
                f'(SELECT rowid FROM feature_window_entities WHERE window_id IN '
                f'(SELECT window_id FROM feature_windows WHERE rowid IN ({marks}) AND {eligible}) LIMIT 4096)',
                (*ids, *parameters))
            eligible += ' AND NOT EXISTS (SELECT 1 FROM feature_window_entities ref WHERE ref.window_id=feature_windows.window_id)'
        removed = connection.execute(f'DELETE FROM {table} WHERE rowid IN ({marks}) AND {eligible}',
                                     (*ids, *parameters)).rowcount
        return max(0, removed), len(rows), (rows[-1][1], rows[-1][0])

    def step(self, now=None):
        started = time.monotonic()
        now = time.time() if now is None else float(now)
        deleted = scanned = 0
        exhausted = set()
        try:
            with self.store.background_sqlite(), self.store.storage_budget.maintenance():
                status = self._pressure()
                with self.lock:
                    self.stats['used_before_bytes'] = status['database_used_bytes']
                    self.follow_up = False
                if status.get('sample_error') or status.get('reason') == 'wal_budget':
                    with self.lock:
                        self.stats['reason'] = ('waiting_for_storage_sample' if status.get('sample_error')
                                                else 'waiting_for_checkpoint')
                    return 0
                hours = self.hours('storage_diagnostic_retention_hours', 24)
                if self.active:
                    hours = min(hours, self.hours('storage_pressure_retention_hours', 1))
                cutoff = now - hours * 3600
                with self.store.conn() as c:
                    tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    specs = {}
                    for table in self.TABLES:
                        if table not in tables:
                            exhausted.add(table)
                            continue
                        columns = {row[1]: row[2] for row in c.execute(f'PRAGMA table_info({table})')}
                        spec = self._spec(table, columns, tables, now)
                        if spec is None:
                            exhausted.add(table)
                        else:
                            specs[table] = spec
                attempted = 0
                for _ in range(self.MAX_BATCHES):
                    if (self.stop_event.is_set() or self.SLICE_SECONDS <= 0
                            or (attempted and time.monotonic() - started >= self.SLICE_SECONDS)
                            or len(exhausted) == len(self.TABLES)):
                        break
                    table = self.TABLES[self.position % len(self.TABLES)]
                    self.position += 1
                    if table in exhausted:
                        continue
                    if not self.store.lock.acquire(blocking=False):
                        with self.lock:
                            self.stats['busy'] += 1
                        break
                    try:
                        # Slow microSD metadata reads must not consume every
                        # slice before even one bounded batch can make progress.
                        attempted += 1
                        with self.store.conn() as c:
                            expression = specs[table][0]
                            c.execute(f'CREATE INDEX IF NOT EXISTS idx_storage_retention_{table} ON {table}({expression})')
                            if table == 'feature_observation_events' and 'idx_storage_window_start' in specs[table][1]:
                                # Seek old intervals directly. Walking every recent
                                # per-entity window for each orphan would be quadratic.
                                c.execute('CREATE INDEX IF NOT EXISTS idx_storage_window_start ON feature_windows(start_time)')
                            bound = cutoff / 86400 + 2440587.5 if table == 'events' else cutoff
                            removed, count, next_cursor = self._batch(c, table, specs[table], bound)
                        # Publish traversal/statistics only after the transaction commits.
                        if count < self.BATCH_ROWS or (table == 'feature_windows' and not removed):
                            self.cursors.pop(table, None)
                            exhausted.add(table)
                        elif next_cursor is not None:
                            self.cursors[table] = next_cursor
                        deleted += removed
                        scanned += count
                        with self.lock:
                            self.stats['by_table'][table] = self.stats['by_table'].get(table, 0) + removed
                    finally:
                        self.store.lock.release()
                    status = self._pressure()
                    if self.active is False and hours != self.hours('storage_diagnostic_retention_hours', 24):
                        break  # Reached the low watermark; do not keep emergency pruning.
                with self.lock:
                    self.follow_up = len(exhausted) < len(self.TABLES)
                    self.stats['reason'] = ('reclaiming_diagnostics' if self.active and self.follow_up else
                        'protected_or_no_disposable_data' if self.active else None)
                    self.stats['used_after_bytes'] = status['database_used_bytes']
                    self.stats['last_error'] = None
        except Exception as exc:
            with self.lock:
                if is_sqlite_busy(exc):
                    self.stats['busy'] += 1
                else:
                    self.stats['errors'] += 1
                self.stats['last_error'] = f'{type(exc).__name__}: {exc}'
        finally:
            with self.lock:
                self.stats['slices'] += 1
                self.stats['deleted'] += deleted
                self.stats['scanned'] += scanned
                self.stats['last_duration_ms'] = (time.monotonic() - started) * 1000
        return deleted
