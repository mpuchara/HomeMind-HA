"""Diagnostic-only archive overview: bounded pages, no startup or training gate."""
import json
import sqlite3
import threading
import time

KEY = 'archive_overview_v1'


def initial(store):
    try:
        saved = json.loads(store.meta_get(KEY, 'null'))
    except (ValueError, TypeError):
        saved = None
    result = dict(saved) if isinstance(saved, dict) else dict(
        n=0, min_ts=None, max_ts=None, entities=0, days=0., by_source={})
    result.update(stale=True, refreshing=False, semantics='diagnostic paged overview; never training bounds')
    return result


def refresh(store, stop, publish, *, page_rows=4096, pause=.04):
    """Release each read statement before yielding; never pin a whole-archive WAL view.

    Concurrent edits can change already visited rows. This overview is explicitly
    approximate during concurrent mutation; training/replay always use fresh SQL.
    """
    connection = sqlite3.connect(store.path, timeout=.25)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA cache_size=-2048')
        connection.execute('PRAGMA mmap_size=0')
        ceiling = connection.execute('SELECT MAX(id) FROM entity_history').fetchone()[0] or 0
        cursor = 0
        count, low, high, entities, sources = 0, None, None, set(), {}
        truncated = False
        while cursor < ceiling and not stop.is_set():
            rows = connection.execute(
                'SELECT entity_id,source,COUNT(*),MIN(ts),MAX(ts),MAX(id) FROM '
                '(SELECT id,entity_id,source,ts FROM entity_history WHERE id>? AND id<=? '
                'ORDER BY id LIMIT ?) GROUP BY entity_id,source',
                (cursor, ceiling, max(1, min(16384, int(page_rows))))).fetchall()
            if not rows:
                break
            for eid, source, n, lo, hi, last in rows:
                count += n
                low = lo if low is None else min(low, lo)
                high = hi if high is None else max(high, hi)
                if len(entities) < 8192 or eid in entities:
                    entities.add(eid)
                else:
                    truncated = True
                if len(sources) < 256 or source in sources:
                    sources[source] = sources.get(source, 0) + n
                else:
                    truncated = True
                cursor = max(cursor, last)
            # No transaction/cursor survives this wait; writers and WAL truncation
            # can proceed. Memory scales with entities/sources, not archive rows.
            if stop.wait(pause):
                return None
        if stop.is_set():
            return None
        result = dict(n=count, min_ts=low, max_ts=high, entities=len(entities),
                      days=(high-low)/86400. if low is not None and high is not None else 0.,
                      by_source=sources, measured_at=time.time(), stale=True, refreshing=False,
                      cardinality_truncated=truncated,
                      semantics='paged diagnostic overview; concurrent edits may change totals')
        publish(result)
        try:
            with store.background_sqlite():
                store.meta_set(KEY, json.dumps(result))
        except sqlite3.Error:
            pass  # The RAM overview remains useful under storage pressure.
        return result
    finally:
        connection.close()


def start(history, store):
    existing = getattr(history, '_archive_overview_thread', None)
    if existing is not None and existing.is_alive():
        return False
    def run():
        if history.stop_event.wait(20.):
            return
        with history.lock:
            history.archive_cache = {**history.archive_cache, 'refreshing': True}
        def publish(value):
            with history.lock:
                history.archive_cache = value
        try:
            refresh(store, history.stop_event, publish)
        except Exception as exc:
            with history.lock:
                history.archive_cache = {**history.archive_cache, 'refreshing': False,
                                         'refresh_error': type(exc).__name__}
    history._archive_overview_thread = threading.Thread(
        target=run, name='adaptive-ai-archive-overview', daemon=True)
    history._archive_overview_thread.start()
    return True
