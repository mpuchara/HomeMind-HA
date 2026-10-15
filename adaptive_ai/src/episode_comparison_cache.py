"""Bounded immutable comparison results, invalidated by committed SQLite changes."""
from collections import OrderedDict
import json
import threading


def install_revisions(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS episode_comparison_revisions
                          (agent_id TEXT PRIMARY KEY, revision INTEGER NOT NULL)""")
    # Include direct SQL, retention, moves, key changes and rollback. Trigger writes
    # share the evidence transaction; no uncommitted revision can authorize a hit.
    for table in ('episode_evaluator_episodes', 'episode_evaluator_policy_results'):
        for operation, sources in (('INSERT', ('NEW',)), ('DELETE', ('OLD',)),
                                   ('UPDATE', ('OLD', 'NEW'))):
            statements = []
            for source in sources:
                if table.endswith('_episodes'):
                    selector = f'SELECT {source}.agent_id, 1 WHERE 1'
                else:
                    selector = (f'SELECT agent_id, 1 FROM episode_evaluator_episodes '
                                f'WHERE episode_id={source}.episode_id')
                statements.append('INSERT INTO episode_comparison_revisions(agent_id,revision) '
                                  + selector + ' ON CONFLICT(agent_id) DO UPDATE '
                                  'SET revision=revision+1;')
            connection.execute(f'CREATE TRIGGER IF NOT EXISTS comparison_rev_{table}_{operation.lower()} '
                               f'AFTER {operation} ON {table} BEGIN ' + ' '.join(statements) + ' END')


def revision(connection, agent_id):
    row = connection.execute('SELECT revision FROM episode_comparison_revisions WHERE agent_id=?',
                             (str(agent_id),)).fetchone()
    return int(row[0]) if row else 0


class ComparisonCache:
    def __init__(self, *, max_entries=32, max_bytes=64*1024*1024, max_entry_bytes=16*1024*1024):
        self.lock = threading.Lock()
        self.entries = OrderedDict()
        self.bytes = 0
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.max_entry_bytes = max_entry_bytes
        self.hits = self.misses = self.evictions = self.bypasses = 0

    def get(self, key, token):
        with self.lock:
            entry = self.entries.get(key)
            if entry is None or entry[0] != token:
                self.misses += 1
                return None
            self.entries.move_to_end(key)
            self.hits += 1
            payload = entry[1]
        return json.loads(payload)  # Each caller owns its complete mutable result.

    def put(self, key, token, value):
        if self.max_entries < 1 or min(self.max_bytes, self.max_entry_bytes) < 1:
            with self.lock:
                self.bypasses += 1
            return
        payload = json.dumps(value, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
        size = len(payload)  # ASCII encoding makes byte and character budgets identical.
        with self.lock:
            old = self.entries.pop(key, None)
            if old is not None:
                self.bytes -= old[2]
            if size > min(self.max_bytes, self.max_entry_bytes) or self.max_entries < 1:
                self.bypasses += 1
                return
            while self.entries and (len(self.entries) >= self.max_entries or self.bytes + size > self.max_bytes):
                _, evicted = self.entries.popitem(last=False)
                self.bytes -= evicted[2]
                self.evictions += 1
            self.entries[key] = (token, payload, size)
            self.bytes += size

    def snapshot(self):
        with self.lock:
            return dict(entries=len(self.entries),bytes=self.bytes,hits=self.hits,misses=self.misses,
                        evictions=self.evictions,bypasses=self.bypasses,max_entries=self.max_entries,
                        max_bytes=self.max_bytes,max_entry_bytes=self.max_entry_bytes,
                        invalidation='fresh SQLite agent revision; all episode/result INSERT, UPDATE, DELETE')
