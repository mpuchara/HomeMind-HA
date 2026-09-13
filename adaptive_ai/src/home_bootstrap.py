"""Explicit global bootstrap, streamed through the same single heavy-job gate.

Daily snapshots allow agent replay to use only home statistics learned BEFORE its
training window. This avoids leakage of validation trajectories into forecasts.
"""
import json
import threading
import time
import sqlite3
import tempfile
from contextlib import closing
import math
from pathlib import Path
from context import archived_state
from context_engine import ContextEngine
from home_state import SharedHomeStateModel
from telemetry import HEAVY_JOBS, rss_mb


class HomeBootstrap:
    def __init__(self, context, history, store):
        self.context, self.history, self.store = context, history, store
        self.cancel_event = threading.Event()
        self.status = {'state': 'IDLE', 'progress': 0, 'rows': 0, 'rows_per_second': 0,
                       'eta_seconds': None}
        self.thread = None
        with store.conn() as c:
            c.execute('CREATE TABLE IF NOT EXISTS home_checkpoints (ts REAL PRIMARY KEY, model TEXT NOT NULL)')

    def start(self, import_recorder=True):
        if not HEAVY_JOBS.acquire('home_bootstrap'):
            return False
        self.cancel_event.clear()
        self.history.job_cancel_event = self.cancel_event
        self.status = {'state': 'IMPORTING' if import_recorder else 'TRAINING', 'progress': 0, 'rows': 0}
        self.thread = threading.Thread(target=self._run, args=(import_recorder,), daemon=True, name='home-bootstrap')
        self.thread.start()
        return True

    def cancel(self):
        self.cancel_event.set()

    def _check(self):
        if self.cancel_event.is_set() or self.history.stop_event.is_set():
            raise InterruptedError('Bootstrap cancelled; live model retained')
        memory = rss_mb()
        if memory is not None and memory > 500:
            raise MemoryError('500 MB RSS training limit reached')

    def _run(self, import_recorder):
        started = time.monotonic()
        try:
            ids = self.context.relevant_entities()
            if not ids:
                raise ValueError('No mapped occupancy/activity sources; assign HA areas first')
            with self.context.lock:
                cutoff = time.time()
                self.context.bootstrap_started = cutoff
                self.context.bootstrap_delta = self.context.home.live_delta(cutoff)
                mapping = {eid: self.context.area_for(eid) for eid in ids}
                metadata = {eid: dict(self.context.source_metadata.get(eid, {})) for eid in ids}
            begin = cutoff - float(self.context.options.get('history_bootstrap_days', 10))*86400
            if import_recorder:
                windows = self.history._time_windows(begin, cutoff, max_hours=1)
                total = math.ceil((cutoff-begin)/3600)*((len(ids)+5)//6)
                done = 0
                for lo, hi in windows:
                    for offset in range(0, len(ids), 6):
                        self._check()
                        self.history._fetch_history_resilient(ids[offset:offset+6], lo, hi,
                            minimal=True, no_attributes=True, source='ha_home_bootstrap')
                        done += 1
                        self.status.update(progress=.4*done/max(1,total), recorder_chunks=done)
                        self.cancel_event.wait(.02)
            self.status['state'] = 'TRAINING'
            model = SharedHomeStateModel(self.context.options.get('home_model_half_life_days',45))
            next_checkpoint = begin + 86400
            rows = 0
            with tempfile.TemporaryDirectory(prefix='homemind-bootstrap-') as temp:
                path = str(Path(temp) / 'checkpoints.sqlite')
                with closing(sqlite3.connect(path)) as checkpoint:
                    checkpoint.execute('CREATE TABLE home_checkpoints (ts REAL PRIMARY KEY, model TEXT NOT NULL)')
                    for row in self.store.archive_iter(begin, cutoff, ids, chunk_size=256):
                        self._check()
                        ts = row['ts']
                        if ts >= next_checkpoint:
                            checkpoint.execute('INSERT OR REPLACE INTO home_checkpoints VALUES (?,?)',
                                               (next_checkpoint, json.dumps(model.export(), separators=(',', ':'))))
                            next_checkpoint = ts + 86400
                        eid = row['entity_id']
                        state = archived_state(row)
                        # Minimal Recorder responses omit units: use current metadata.
                        state['attributes'] = {**metadata[eid], **state['attributes']}
                        model.observe(eid, mapping[eid], ContextEngine.probability(eid,state), ts)
                        rows += 1
                        if rows % 256 == 0:
                            elapsed = max(.001, time.monotonic()-started)
                            fraction = max(0, min(1, (ts-begin)/max(1,cutoff-begin)))
                            self.status.update(rows=rows, progress=.4+.6*fraction, rows_per_second=rows/elapsed,
                                               eta_seconds=elapsed*(1-fraction)/max(.001,fraction))
                            self.cancel_event.wait(.001)
                    model.expire(cutoff)
                    self._check()
                    checkpoint.commit()
                # Fold bounded live statistics into history, never retain every raw event.
                # Commit checkpoints and model together; failure leaves both old versions.
                with self.context.lock, self.context.home.lock:
                    self._check()
                    if mapping != {eid: self.context.area_for(eid) for eid in self.context.relevant_entities()}:
                        raise RuntimeError('Presence sources or areas changed during bootstrap; restart bootstrap with the new mapping')
                    model.merge_statistics(self.context.bootstrap_delta)
                    raw = json.dumps(model.export(), separators=(',', ':'))
                    with self.store.conn() as destination:
                        destination.execute('ATTACH DATABASE ? AS bootstrap', (path,))
                        destination.execute('DELETE FROM home_checkpoints')
                        destination.execute('INSERT INTO home_checkpoints SELECT * FROM bootstrap.home_checkpoints')
                        destination.execute("INSERT INTO app_meta(key,value) VALUES('shared_home_model_v1',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (raw,))
                    current = self.context.home
                    current.graph, current.dwell = model.graph, model.dwell
                    current.updated = model.updated
                    current.revision += 1
                    current.last_decay_ts = model.last_decay_ts
                    self.context.bootstrap_cutoff = cutoff
                    self.context.bootstrap_delta = None
                    self.context.last_save = time.time()
            self.status.update(state='READY', progress=1, rows=rows, eta_seconds=0,
                               rows_per_second=rows/max(.001,time.monotonic()-started))
        except InterruptedError as exc:
            self.status.update(state='CANCELLED', error=str(exc))
        except Exception as exc:
            self.status.update(state='ERROR', error=str(exc))
        finally:
            with self.context.lock:
                self.context.bootstrap_delta = None
            self.history.job_cancel_event = None
            HEAVY_JOBS.release('home_bootstrap')
