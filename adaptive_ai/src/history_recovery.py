"""Bounded, read-only source salvage into a disposable training generation.

No repair SQL is ever run on the source. SQLite's read transaction includes WAL.
Missing physical keys are mapped to time ONLY using a separately readable covering
index; physical row order is never interpreted as chronological order.
"""
from contextlib import closing
import json
import hashlib
import math
import sqlite3

from sqlite_background import is_sqlite_corruption

CONTRACT = "training-history-generation-v2"
CORRECT_TABLES = ('rl_feedback', 'teaching_labels', 'teaching_rl_labels',
    'manual_context_feedback', 'manual_feedback_journal', 'manual_feedback_effects',
    'agent_correct_operations', 'tiny_mlp_correct_batches', 'tiny_mlp_correct_samples')


def correct_predicate(conn, table, agent_ids):
    if not agent_ids:
        return '', ()
    columns = {r[1] for r in conn.execute(f'PRAGMA table_info({quote(table)})')}
    marks = ','.join('?' for _ in agent_ids)
    relations = [c for c in ('agent_id','root_agent_id','parent_agent_id') if c in columns]
    if relations:
        return '('+' OR '.join(c+f' IN ({marks})' for c in relations)+')',tuple(agent_ids)*len(relations)
    if table=='manual_feedback_effects':
        return f'feedback_id IN (SELECT feedback_id FROM manual_feedback_journal WHERE agent_id IN ({marks}) OR root_agent_id IN ({marks}))',tuple(agent_ids)*2
    if table=='tiny_mlp_correct_samples':
        nested, params = correct_predicate(conn,'tiny_mlp_correct_batches',agent_ids)
        return 'batch_id IN (SELECT batch_id FROM tiny_mlp_correct_batches WHERE '+nested+')',params
    raise RecoveryUnavailable(f'Unknown Correct dependency layout: {table}')


def correct_fingerprint(conn, agent_ids=None, on_row=None):
    digest = hashlib.sha256()
    for table in CORRECT_TABLES:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (table,)).fetchone():
            digest.update(table.encode())
            predicate, params = correct_predicate(conn,table,agent_ids)
            where = ' WHERE '+predicate if predicate else ''
            for row in conn.execute(f'SELECT * FROM {quote(table)}'+where+' ORDER BY rowid', params):
                if on_row is not None:
                    on_row(table)
                digest.update(json.dumps(tuple(row), sort_keys=True, default=str).encode())
    return digest.hexdigest()


class RecoveryUnavailable(RuntimeError):
    preserve_training_state = True


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def prepare_generation(*args, **kwargs):
    from training_generation import prepare_generation as prepare
    return prepare(*args, **kwargs)


def safe_segments(report, start, end):
    """Global trust boundaries: conservative across all selected context entities."""
    gaps = sorted((max(start, g['start_ts']), min(end, g['end_ts'])) for g in report['gaps']
                  if g['start_ts'] <= end and g['end_ts'] >= start)
    segments, cursor = [], start
    for lo, hi in gaps:
        if lo > cursor:
            segments.append((cursor, math.nextafter(lo, -math.inf)))
        cursor = max(cursor, math.nextafter(hi, math.inf))
    if cursor < end:
        segments.append((cursor, end))
    return segments


def project_segment(path, lo, hi):
    """No seed, context checkpoint, late receipt or dwell can cross a gap."""
    with closing(sqlite3.connect(path)) as conn, conn:
        is_view = conn.execute("SELECT type FROM sqlite_master WHERE name='entity_history'").fetchone()[0] == 'view'
        if is_view:
            conn.execute('UPDATE recovery_projection SET lo=?,hi=? WHERE id=1',(lo,hi))
        else:
            # Compatibility for a v1 generation prepared by an older worker.
            conn.execute("DELETE FROM entity_history")
            conn.execute("INSERT INTO entity_history SELECT * FROM recovery_rows WHERE ts>=? AND ts<=? AND (received_ts IS NULL OR received_ts<=?)", (lo, hi, hi))
        for table in ('home_checkpoints', 'adaptive_presence_checkpoints'):
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                conn.execute(f"DELETE FROM {quote(table)}")
        # Context remains unknown until each historically present entity has an
        # actual causal synchronization point in this segment. Never interpret
        # a missing seed as a verified absence/comfort observation.
        required = conn.execute('SELECT COUNT(DISTINCT entity_id) FROM recovery_rows').fetchone()[0]
        seeds = conn.execute('SELECT entity_id,MIN(MAX(ts,COALESCE(received_ts,ts))) FROM entity_history GROUP BY entity_id').fetchall()
        if len(seeds) != required or not seeds:
            return None
        return max(float(r[1]) for r in seeds)


LIFECYCLE_COLUMNS = ('training_state', 'mode', 'benchmark_score', 'benchmark_samples',
    'benchmark_source', 'benchmark_detail_json', 'benchmark_updated_at',
    'training_window_start_ts', 'training_cursor_ts', 'training_window_end_ts',
    'training_progress', 'training_updated_at')


def publish_generation(store, path, agent_id, expected_fingerprint, fingerprint):
    """Publish policy, experiences, cursor, benchmark and neural record in ONE WAL transaction.

    Correct/config/candidates/other agents are never replaced by stale worker copies.
    Metadata write corruption aborts the transaction; source repair is a separate task.
    """
    from tiny_mlp_shadow import ensure_tables
    ensure_tables(store)
    with store.lock, store.conn() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS training_generation_backup(agent_id TEXT PRIMARY KEY,agent_json TEXT,model_json TEXT,neural_json TEXT)')
    with store.lock, store.conn() as conn:
        conn.execute("ATTACH DATABASE ? AS trained", (str(path),))
        conn.execute("BEGIN IMMEDIATE")
        current = store.get_agent_config(agent_id)
        if fingerprint(current) != expected_fingerprint:
            raise RecoveryUnavailable("Agent changed before atomic publication")
        manifest = json.loads(conn.execute("SELECT payload FROM trained.recovery_manifest").fetchone()[0])
        if manifest['status'] != 'TRAINED':
            raise RecoveryUnavailable("Incomplete training generation")
        boundary=(manifest.get('snapshot_lifecycle') or {}).get('boundary')
        if boundary:
            from training_snapshot import validate
            validate(conn,boundary)
        if correct_fingerprint(conn, manifest.get('correct_agent_ids')) != manifest.get('correct_fingerprint'):
            raise RecoveryUnavailable('Correct changed during training; previous model retained, retry with new labels')
        previous_model = conn.execute('SELECT model_json FROM rl_models WHERE agent_id=?', (agent_id,)).fetchone()
        current_digest = hashlib.sha256(previous_model[0].encode()).hexdigest() if previous_model else None
        if manifest.get('agent_id') and current_digest != manifest.get('model_fingerprint'):
            raise RecoveryUnavailable('Live model changed during training; previous state retained, retry required')
        previous_neural = conn.execute('SELECT * FROM tiny_mlp_shadow_models WHERE agent_id=?', (agent_id,)).fetchone()
        conn.execute('INSERT OR REPLACE INTO training_generation_backup VALUES(?,?,?,?)', (agent_id,json.dumps(current),
            previous_model[0] if previous_model else None,json.dumps(dict(previous_neural)) if previous_neural else None))
        row = conn.execute("SELECT model_json FROM trained.rl_models WHERE agent_id=?", (agent_id,)).fetchone()
        if row is None:
            raise RecoveryUnavailable("No saved trained model; previous model retained")
        model = json.loads(row[0])
        conn.execute("DELETE FROM historical_experiences WHERE agent_id=?", (agent_id,))
        columns = [r[1] for r in conn.execute('PRAGMA table_info(historical_experiences)') if r[1] != 'id']
        names = ','.join(map(quote, columns))
        conn.execute(f"INSERT INTO historical_experiences ({names}) SELECT {names} FROM trained.historical_experiences WHERE agent_id=? ORDER BY id", (agent_id,))
        model['_history_watermark'] = conn.execute("SELECT COALESCE(MAX(id),0) FROM historical_experiences WHERE agent_id=?", (agent_id,)).fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO rl_models SELECT agent_id,?,updated_at FROM trained.rl_models WHERE agent_id=?", (json.dumps(model), agent_id))
        values = conn.execute('SELECT '+','.join(LIFECYCLE_COLUMNS)+' FROM trained.agents WHERE id=?', (agent_id,)).fetchone()
        conn.execute('UPDATE agents SET '+','.join(x+'=?' for x in LIFECYCLE_COLUMNS)+' WHERE id=?', (*values, agent_id))
        if conn.execute("SELECT 1 FROM trained.sqlite_master WHERE name='tiny_mlp_shadow_models'").fetchone():
            conn.execute("DELETE FROM tiny_mlp_shadow_models WHERE agent_id=?", (agent_id,))
            conn.execute("INSERT INTO tiny_mlp_shadow_models SELECT * FROM trained.tiny_mlp_shadow_models WHERE agent_id=?", (agent_id,))
    store.touch_agent_index()
