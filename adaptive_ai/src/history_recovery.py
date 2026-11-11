"""Bounded, read-only source salvage into a disposable training generation.

No repair SQL is ever run on the source. SQLite's read transaction includes WAL.
Missing physical keys are mapped to time ONLY using a separately readable covering
index; physical row order is never interpreted as chronological order.
"""
from contextlib import closing
import json
import hashlib
import os
import math
from pathlib import Path
import sqlite3
import time

from sqlite_background import is_sqlite_corruption

CONTRACT = "training-history-generation-v1"
CORRECT_TABLES = ('rl_feedback', 'teaching_labels', 'teaching_rl_labels',
    'manual_context_feedback', 'manual_feedback_journal', 'manual_feedback_effects',
    'agent_correct_operations', 'tiny_mlp_correct_batches', 'tiny_mlp_correct_samples')


def correct_fingerprint(conn):
    digest = hashlib.sha256()
    for table in CORRECT_TABLES:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (table,)).fetchone():
            digest.update(table.encode())
            for row in conn.execute(f'SELECT * FROM {quote(table)} ORDER BY rowid'):
                digest.update(json.dumps(tuple(row), sort_keys=True, default=str).encode())
    return digest.hexdigest()


class RecoveryUnavailable(RuntimeError):
    preserve_training_state = True


def quote(name):
    return '"' + name.replace('"', '""') + '"'


class RecoveryBudget:
    def __init__(self, options=None, cancelled=lambda: False, progress=lambda **kw: None):
        options = options or {}
        self.attempts = 0
        self.limit = max(1, min(16384, int(options.get("max_recovery_attempts", 4096))))
        self.depth = max(1, min(48, int(options.get("max_recovery_depth", 32))))
        self.seconds = max(1, min(600, float(options.get("max_recovery_wall_seconds", 120))))
        self.memory = max(1, min(32, int(options.get("max_recovery_memory_mb", 8)))) * 1048576
        self.disk = max(16, min(2048, int(options.get("max_recovery_disk_mb", 512)))) * 1048576
        self.started = time.monotonic()
        self.cancelled, self.progress = cancelled, progress

    def check(self, attempt=False):
        from training_budget import TRAINING_BUDGET
        TRAINING_BUDGET.checkpoint('history_recovery')
        if self.cancelled():
            raise InterruptedError("History recovery cancelled")
        if time.monotonic() - self.started > self.seconds:
            raise RecoveryUnavailable("History recovery wall budget exhausted; previous model retained")
        if attempt:
            self.attempts += 1
            if self.attempts > self.limit:
                raise RecoveryUnavailable("History recovery attempt budget exhausted")


def prepare_generation(source, destination, entities, start, end, *, options=None,
                       cancelled=lambda: False, progress=lambda **kw: None, http_gaps=(), agent_id=None):
    """Copy readable metadata and selected history, fail closed on unknown loss.

    destination must be a fresh per-job path. Interrupted files are never accepted:
    only a committed recovery_manifest with status READY is usable by publication.
    """
    destination = Path(destination)
    if destination.exists():
        raise RecoveryUnavailable("Training generation already exists")
    budget = RecoveryBudget(options, cancelled, progress)
    entities = set(entities)
    report = dict(contract=CONTRACT, status="BUILDING", source=str(source),
                  agent_id=agent_id, recovered_records=0, skipped_records=None,
                  gaps=[], attempts=0, archive_status="HISTORY_OK")
    def source_signature():
        signature = []
        for suffix in ('', '-wal'):
            path = Path(str(source)+suffix)
            stat = path.stat() if path.exists() else None
            # Opening a WAL-mode read transaction may create an empty WAL; it
            # contains no committed frames and is equivalent to an absent file.
            signature.append([stat.st_size, stat.st_mtime_ns] if stat and (not suffix or stat.st_size > 32) else None)
        return signature
    signature = source_signature()
    cache_key = hashlib.sha256(str(Path(source).resolve()).encode()).hexdigest()[:16]
    cache_path = destination.parent / ('.quarantine-'+cache_key+'.json')
    try:
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cache = {}
    cached_ranges = cache.get('ranges', []) if cache.get('signature') == signature else []
    with closing(sqlite3.connect(Path(source).resolve().as_uri()+"?mode=ro", uri=True)) as src, \
            closing(sqlite3.connect(destination)) as dst:
        src.row_factory = sqlite3.Row
        src.execute("PRAGMA query_only=ON")
        src.execute(f"PRAGMA cache_size=-{budget.memory // 1024}")
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.execute(f"PRAGMA cache_size=-{budget.memory // 1024}")
        dst.execute(f"PRAGMA max_page_count={budget.disk // 4096}")
        # Enforce cancellation/wall time even inside SQLite sorting, index build
        # and quick_check, before control returns to the Python batch loop.
        def interrupted():
            return int(cancelled() or time.monotonic()-budget.started > budget.seconds)
        src.set_progress_handler(interrupted,10000)
        dst.set_progress_handler(interrupted,10000)
        src.execute("BEGIN")
        schema = src.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
        report['correct_fingerprint'] = correct_fingerprint(src)
        original_model = src.execute('SELECT model_json FROM rl_models WHERE agent_id=?', (agent_id,)).fetchone() if agent_id else None
        report['model_fingerprint'] = hashlib.sha256(original_model[0].encode()).hexdigest() if original_model else None
        for item in schema:
            if item['type'] == 'table' and not item['name'].startswith('sqlite_'):
                dst.execute(item['sql'])
        def copy_rows(sql, params, target, *, history=False):
            budget.check()
            with closing(src.execute(sql, params)) as cursor:
                columns = [x[0] for x in cursor.description]
                insert = f"INSERT OR IGNORE INTO {quote(target)} ({','.join(map(quote, columns))}) VALUES ({','.join('?' for _ in columns)})"
                while True:
                    budget.check()
                    batch = cursor.fetchmany(128)
                    if not batch:
                        break
                    if sum(sum(len(v) if isinstance(v, (str, bytes)) else 16 for v in r) for r in batch) > budget.memory:
                        raise RecoveryUnavailable("History recovery row batch exceeds memory budget")
                    if history:
                        batch = [r for r in batch if r['entity_id'] in entities and float(r['ts']) <= end]
                        for row in batch:
                            if not math.isfinite(float(row['ts'])):
                                raise RecoveryUnavailable("Non-finite archive timestamp")
                            json.loads(row['attributes_json'])
                    dst.executemany(insert, batch)
        # Metadata is never partially salvaged: corrupt Correct/model/config tables
        # require an explicit separate migration, rather than silent preference loss.
        for item in schema:
            if item['type'] == 'table' and not item['name'].startswith('sqlite_') and item['name'] != 'entity_history':
                copy_rows(f"SELECT * FROM {quote(item['name'])}", (), item['name'])
        dst.execute("SAVEPOINT history_copy")
        try:
            copy_rows("SELECT * FROM entity_history WHERE ts<=?", (end,), "entity_history", history=True)
            dst.execute("RELEASE history_copy")
        except sqlite3.DatabaseError as error:
            dst.execute("ROLLBACK TO history_copy")
            dst.execute("RELEASE history_copy")
            if not is_sqlite_corruption(error):
                raise
            report.update(archive_status="ARCHIVE_CORRUPTED", sqlite_errorcode=getattr(error, 'sqlite_errorcode', None),
                          sqlite_errorname=getattr(error, 'sqlite_errorname', None), recovery_strategy="rowid_bisection")
            print('[history] training_corruption_detected '+json.dumps(dict(agent_id=agent_id,
                entity_id='unknown_until_covering_witness',training_phase='history_recovery',
                archive_query_type='selected_history',query_start_ts=start,query_end_ts=end,
                sqlite_errorcode=getattr(error,'sqlite_errorcode',None),sqlite_errorname=getattr(error,'sqlite_errorname',None),
                recovery_strategy='bounded_rowid_bisection',recovery_result='started')),flush=True)
            progress(message="History corruption detected; recovering physical key ranges", phase_detail="Bounded read-only SQLite recovery")
            # Materialize the independent covering witness on disk, never in a Python
            # history list. Index-only corruption is harmless if the table is readable.
            dst.execute("CREATE TABLE recovery_witness(id INTEGER PRIMARY KEY,entity_id TEXT,ts REAL)")
            witness_ok = False
            for index in ("idx_entity_history_entity_ts", "sqlite_autoindex_entity_history_1"):
                if not any(x['name'] == index for x in schema) and index != 'sqlite_autoindex_entity_history_1':
                    continue
                dst.execute("SAVEPOINT witness_copy")
                try:
                    copy_rows(f"SELECT id,entity_id,ts FROM entity_history INDEXED BY {quote(index)}", (), "recovery_witness")
                    witness_ok = True
                    dst.execute("RELEASE witness_copy")
                    break
                except sqlite3.DatabaseError as exc:
                    dst.execute("ROLLBACK TO witness_copy")
                    dst.execute("RELEASE witness_copy")
                    if not is_sqlite_corruption(exc) and 'no such index' not in str(exc):
                        raise
            try:
                maximum = src.execute("SELECT id FROM entity_history NOT INDEXED ORDER BY id DESC LIMIT 1").fetchone()
                maximum = int(maximum[0]) if maximum else 0
            except sqlite3.DatabaseError as exc:
                if not is_sqlite_corruption(exc) or not witness_ok:
                    raise
                maximum = int(dst.execute("SELECT COALESCE(MAX(id),0) FROM recovery_witness").fetchone()[0])
            skipped = []
            def recover(lo, hi, depth=0):
                for left, right in cached_ranges:
                    if left <= lo and right >= hi:
                        skipped.append((lo, hi))
                        return
                    if left <= hi and right >= lo:
                        if lo < left:
                            recover(lo, left-1, depth)
                        skipped.append((max(lo,left), min(hi,right)))
                        if hi > right:
                            recover(right+1, hi, depth)
                        return
                budget.check(attempt=True)
                dst.execute("SAVEPOINT physical_range")
                try:
                    copy_rows("SELECT * FROM entity_history NOT INDEXED WHERE id>=? AND id<=? ORDER BY id", (lo, hi), "entity_history", history=True)
                    dst.execute("RELEASE physical_range")
                except sqlite3.DatabaseError as exc:
                    dst.execute("ROLLBACK TO physical_range")
                    dst.execute("RELEASE physical_range")
                    if not is_sqlite_corruption(exc):
                        raise
                    if hi == lo or depth >= budget.depth:
                        skipped.append((lo, hi))
                        return
                    mid = (lo + hi) // 2
                    recover(lo, mid, depth+1)
                    recover(mid+1, hi, depth+1)
            for lo in range(1, maximum+1, 8192):
                recover(lo, min(maximum, lo+8191))
                progress(message=f"Recovering history: physical keys {min(maximum,lo+8191)}/{maximum}",
                         work_done=min(maximum,lo+8191), work_total=maximum, work_unit="archive keys")
            if source_signature() == signature:
                temp = cache_path.with_suffix('.'+destination.name+'.tmp')
                temp.write_text(json.dumps(dict(signature=signature,ranges=skipped)),encoding='utf-8')
                os.replace(temp, cache_path)
            report['quarantine_cache_hit'] = bool(cached_ranges)
            if skipped and not witness_ok:
                report['gaps'].append(dict(entity_id="*", start_ts=start, end_ts=end,
                    source="local_archive", failure_reason="HISTORY_UNKNOWN", recovery_attempts=budget.attempts))
            elif skipped:
                # Inspect each missing key through a HEALTHY independent index. ts
                # boundaries are evidence, not an assumption about physical page order.
                for lo, hi in skipped:
                    for row in dst.execute("SELECT entity_id,MIN(ts),MAX(ts) FROM recovery_witness WHERE id BETWEEN ? AND ? GROUP BY entity_id", (lo, hi)):
                        if row[0] in entities and row[1] <= end:
                            report['gaps'].append(dict(entity_id=row[0], start_ts=float(row[1]), end_ts=float(row[2]),
                                source="local_archive", failure_reason="HISTORY_GAP_SQLITE_CORRUPT", recovery_attempts=budget.attempts))
        for item in schema:
            if item['type'] == 'index':
                dst.execute(item['sql'])
        # HTTP failure is never interpreted as no events, even if local rows exist.
        for gap in http_gaps:
            if gap.get('entity_id') in entities:
                report['gaps'].append(dict(entity_id=gap['entity_id'], start_ts=float(gap.get('start_ts', gap.get('start'))),
                    end_ts=float(gap.get('end_ts', gap.get('end'))), source="ha_recorder",
                    failure_reason=('HISTORY_GAP_HTTP500' if not gap.get('failure_reason') or 'HTTP500' in gap.get('failure_reason','') else 'HISTORY_UNKNOWN'),
                    recovery_attempts=0))
        report['recovered_records'] = dst.execute("SELECT COUNT(*) FROM entity_history").fetchone()[0]
        report['attempts'] = budget.attempts
        report['recovery_wall_seconds'] = round(time.monotonic()-budget.started,6)
        report['availability'] = ('PARTIAL' if report['gaps'] else 'HISTORY_OK')
        # Recovered rows are immutable for the duration of this generation. For a
        # partial archive, each clean segment gets a fresh history projection below.
        dst.execute("CREATE TABLE recovery_rows AS SELECT * FROM entity_history")
        dst.execute("CREATE INDEX recovery_rows_ts ON recovery_rows(ts)")
        if dst.execute("SELECT 1 FROM sqlite_master WHERE name='feature_observation_events'").fetchone():
            dst.execute('CREATE TABLE recovery_observation_rows AS SELECT * FROM feature_observation_events')
        report['status'] = "READY"
        dst.execute("CREATE TABLE recovery_manifest (payload TEXT NOT NULL)")
        dst.execute("INSERT INTO recovery_manifest VALUES (?)", (json.dumps(report),))
        dst.commit()
        if dst.execute("PRAGMA quick_check").fetchone()[0] != 'ok':
            raise RecoveryUnavailable("Recovered training generation failed quick_check")
    return report


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
        conn.execute("DELETE FROM entity_history")
        conn.execute("INSERT INTO entity_history SELECT * FROM recovery_rows WHERE ts>=? AND ts<=? AND (received_ts IS NULL OR received_ts<=?)", (lo, hi, hi))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='recovery_observation_rows'").fetchone():
            conn.execute('DELETE FROM feature_observation_events')
            conn.execute('INSERT INTO feature_observation_events SELECT * FROM recovery_observation_rows WHERE event_time>=? AND event_time<=? AND received_time<=?', (lo,hi,hi))
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
        if correct_fingerprint(conn) != manifest.get('correct_fingerprint'):
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
