"""Bounded background cleanup of replay/audit data, never durable preferences."""
import threading
import time
from datetime import datetime, timezone

# Explicit allowlist. A timestamp column alone is not permission to erase a table.
RETENTION = {
    'entity_history': 'ts', 'historical_experiences': 'created_at', 'events': 'created_at',
    'provenance_events': 'event_time', 'provenance_history_links': 'event_time',
    'provenance_decisions': 'created_time', 'provenance_experiences': 'created_time',
    'provenance_commands': 'expires_time', 'provenance_command_contexts': 'expires_time',
    'feature_observation_events': 'received_time', 'feature_windows': 'created_time',
    'decision_history': 'ts', 'candidate_generation_decisions': 'ts',
    'candidate_generation_pairs': 'outcome_ts', 'adaptation_episode_observations': 'ts',
    'adaptation_environment_snapshots': 'ts', 'episode_evaluator_episodes': 'end_ts',
    'automatic_reward_experiences': 'created_ts', 'policy_backend_shadow_events': 'ts',
    'home_checkpoints': 'ts', 'model_backups': 'saved_at',
    'confidence_probability_episodes': 'ts',
}


class StorageRetention:
    POLL_SECONDS = 60
    BATCH_ROWS = 128
    MAX_BATCHES = 16

    def __init__(self, store, days=14):
        self.store = store
        self.days = max(1, min(14, float(days)))
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.stats = dict(runs=0, deleted=0, errors=0, last_error=None, reclaimed_pages=0)
        self._known_tables = set()

    def snapshot(self):
        with self.lock:
            return {**self.stats, 'history_days': self.days,
                    'batch_rows': self.BATCH_ROWS,
                    'alive': bool(self.thread and self.thread.is_alive())}

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name='adaptive-ai-storage-retention')
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.ident is not None and self.thread is not threading.current_thread():
            self.thread.join()

    def _delete_batch(self, c, table, predicate, parameters, order, tables):
        relation = {'feature_windows': ('window_id', 'feature_window_entities'),
                    'episode_evaluator_episodes': ('episode_id', 'episode_evaluator_policy_results')}.get(table)
        if relation and relation[1] in tables:
            key, child = relation
            rows = c.execute(f'SELECT rowid,{key} FROM {table} WHERE {predicate} ORDER BY {order} LIMIT ?', (*parameters, self.BATCH_ROWS)).fetchall()
            if not rows:
                return 0
            slots = ','.join('?' for _ in rows)
            keys = [r[1] for r in rows]
            # Index the child key instead of repeatedly scanning all surviving
            # windows for orphans. Delete children before parents, respecting FK.
            c.execute(f'DELETE FROM {child} WHERE rowid IN (SELECT rowid FROM {child} WHERE {key} IN ({slots}) LIMIT ?)', (*keys, 4096))
            cursor = c.execute(f'DELETE FROM {table} WHERE rowid IN ({slots}) AND NOT EXISTS (SELECT 1 FROM {child} child WHERE child.{key}={table}.{key})', [r[0] for r in rows])
        else:
            cursor = c.execute(f'DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {predicate} ORDER BY {order} LIMIT ?)', (*parameters, self.BATCH_ROWS))
        return max(0, cursor.rowcount)

    def run_once(self, now=None):
        now = time.time() if now is None else float(now)
        cutoff = now - self.days * 86400
        deleted = 0
        # Short transactions and bounded busy waits. Never hold a read cursor over
        # multiple batches, and never create a second copy of the whole database.
        with self.store.background_sqlite(), self.store.storage_budget.maintenance():
            with self.store.conn() as c:
                tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            for table, column in RETENTION.items():
                if self.stop_event.is_set():
                    break
                if table not in tables:
                    continue
                with self.store.lock, self.store.conn() as c:
                    columns = {r[1]: str(r[2]).upper() for r in c.execute(f'PRAGMA table_info({table})').fetchall()}
                    if column not in columns:
                        continue
                    text_time = 'TEXT' in columns[column]
                    expression = f'julianday({column})' if text_time else column
                    if table not in self._known_tables:
                        c.execute(f'CREATE INDEX IF NOT EXISTS idx_storage_retention_{table} ON {table}({expression})')
                        # Expiring evidence must invalidate cached qualification;
                        # keep durable revision counters and evaluation epochs.
                        if table == 'candidate_generation_pairs' and 'confidence_pair_revisions' in tables:
                            c.execute("""CREATE TRIGGER IF NOT EXISTS trg_storage_pair_delete
                                AFTER DELETE ON candidate_generation_pairs BEGIN
                                UPDATE confidence_pair_revisions
                                SET selection_revision=selection_revision+1,
                                    calibration_revision=calibration_revision+1
                                WHERE parent_generation_id=OLD.parent_generation_id
                                  AND child_generation_id=OLD.child_generation_id;
                                END""")
                        if table == 'confidence_probability_episodes' and 'confidence_probability_revisions' in tables:
                            c.execute("""CREATE TRIGGER IF NOT EXISTS trg_storage_probability_delete
                                AFTER DELETE ON confidence_probability_episodes BEGIN
                                UPDATE confidence_probability_revisions SET revision=revision+1
                                WHERE metric_id=OLD.metric_id AND model_key=OLD.model_key
                                  AND scope_id=OLD.scope_id;
                                END""")
                    bound = (datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
                             if text_time else cutoff)
                    extra = ''
                    if table == 'entity_history':
                        # One causal seed per entity is enough to reconstruct a
                        # state unchanged beyond the retained window at the replay boundary.
                        extra = ' AND EXISTS (SELECT 1 FROM entity_history newer WHERE newer.entity_id=entity_history.entity_id AND newer.ts>entity_history.ts AND newer.ts<?)'
                    elif table == 'home_checkpoints':
                        extra = ' AND ts < (SELECT MAX(ts) FROM home_checkpoints WHERE ts<?)'
                    parameters = (bound, bound) if extra else (bound,)
                    predicate = f'{expression} < '+('julianday(?)' if text_time else '?')+extra
                    removed = self._delete_batch(c, table, predicate, parameters, expression, tables)
                    deleted += removed
                self._known_tables.add(table)
                # More than one short commit is needed to keep up with radar events.
                for _ in range(self.MAX_BATCHES - 1):
                    if removed < self.BATCH_ROWS or self.stop_event.is_set():
                        break
                    with self.store.lock, self.store.conn() as c:
                        removed = self._delete_batch(c, table, predicate, parameters, expression, tables)
                        deleted += removed
            with self.store.lock, self.store.conn() as c:
                before = int(c.execute('PRAGMA page_count').fetchone()[0])
                if int(c.execute('PRAGMA auto_vacuum').fetchone()[0]) == 2:
                    # SQLite may reclaim one page per invocation; bound both the
                    # number of calls and transaction size on slow storage.
                    for _ in range(32):
                        c.execute('PRAGMA incremental_vacuum(1)').fetchall()
                    after = int(c.execute('PRAGMA page_count').fetchone()[0])
                else:
                    after = before
            with self.lock:
                self.stats['runs'] += 1
                self.stats['deleted'] += deleted
                self.stats['reclaimed_pages'] += max(0, before - after)
                self.stats['last_error'] = None
        self.store.storage_budget.sample()
        from storage_files import prune_completed
        from settings import DATA_DIR
        jobs = DATA_DIR / 'training_jobs'
        if jobs.is_dir():
            prune_completed(jobs)
        return deleted

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:
                with self.lock:
                    self.stats['errors'] += 1
                    self.stats['last_error'] = f'{type(exc).__name__}: {exc}'
            self.stop_event.wait(self.POLL_SECONDS)
