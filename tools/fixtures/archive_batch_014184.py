"""Frozen archive_batch from 91c7343 (0.14.184), benchmark reference only."""
import json

def archive_batch(self, rows):
    if not rows:
        return 0

    def pack(row):
        if len(row) == 6:
            entity_id, ts, state, attrs, user_id, source = row
            received_ts = None
        elif len(row) == 7:
            entity_id, ts, state, attrs, user_id, source, received_ts = row
        else:
            raise ValueError("archive row must contain 6 legacy fields or 7 fields with received_ts")
        received_ts = float(received_ts) if received_ts is not None else None
        return (
            entity_id, float(ts), received_ts, None if state is None else str(state),
            json.dumps(attrs or {}, separators=(",", ":"), ensure_ascii=False),
            user_id, source,
        )

    packed = [pack(row) for row in rows]
    with self.lock, self.conn() as c:
        revision = self._next_entity_history_revision(c)
        versioned = [(*item, revision) for item in packed]
        c.executemany(
            """INSERT INTO entity_history
               (entity_id,ts,received_ts,state,attributes_json,context_user_id,source,
                mutation_revision)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(entity_id,ts) DO UPDATE SET
                 received_ts=CASE
                   WHEN entity_history.source='live' THEN entity_history.received_ts
                   WHEN excluded.source='live' THEN excluded.received_ts
                   ELSE COALESCE(entity_history.received_ts, excluded.received_ts)
                 END,
                 state=excluded.state,
                 attributes_json=CASE WHEN excluded.attributes_json!='{}' THEN excluded.attributes_json ELSE entity_history.attributes_json END,
                 context_user_id=COALESCE(excluded.context_user_id, entity_history.context_user_id),
                 source=CASE WHEN excluded.source='ha_history_full' THEN excluded.source ELSE entity_history.source END,
                 mutation_revision=excluded.mutation_revision""",
            versioned,
        )
    return len(packed)


import shutil
import time
from pathlib import Path

def sample(self):
    try:
        if self.mode == 'auto' and self.auto_pool_bytes is None:
            self._configure_auto()
        db = self.path.stat().st_size if self.path.exists() else 0
        wal_path = Path(str(self.path) + '-wal')
        wal = wal_path.stat().st_size if wal_path.exists() else 0
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
