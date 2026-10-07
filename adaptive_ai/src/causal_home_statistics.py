"""Bounded causal trajectory statistics for training without a manual home backfill.

Only direct presence observations train paths. Raw energy, controlled lights and the
current live graph are never used as occupancy labels or past trajectory statistics.
Compressed internal snapshots bound memory; queries replay only the remaining prefix.
"""
import bisect
import hashlib
import json
import sqlite3
import zlib

from context import archived_state
from home_state import RoomBeliefModel
from training_budget import TRAINING_BUDGET


class CausalHomeStatistics:
    CONTRACT = 'causal_home_statistics_v1'
    FIELDS = ('values', 'sources', 'hypotheses', 'pending', 'last_ts', 'revision')
    ROLES = {'pir', 'radar_occupancy', 'occupancy_binary', 'tracker'}

    def __init__(self, connection, context, start, end, *, max_bytes=8 * 1024 * 1024):
        self.conn, self.context = connection, context
        self.start, self.end = float(start), float(end)
        self.entities = sorted(eid for eid in context.relevant_entities()
                               if context.evidence_metadata(eid).get('role') in self.ROLES)
        self.times, self.snapshots = [], []
        self.max_bytes = int(max_bytes)
        self.bytes = self.rows = 0
        try:
            checkpoint = connection.execute(
                'SELECT model FROM home_checkpoints WHERE ts<? ORDER BY ts DESC LIMIT 1',
                (self.start,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if 'no such table' not in str(exc):
                raise
            checkpoint = None
        model = RoomBeliefModel(context.options.get('home_model_half_life_days', 45),
                               json.loads(checkpoint[0]) if checkpoint else None)
        # Existing ON in the initial snapshot is not a fresh arrival.
        for eid in self.entities:
            row = connection.execute(
                'SELECT * FROM entity_history WHERE entity_id=? AND ts<=? '
                'AND COALESCE(received_ts,ts)<=? ORDER BY ts DESC,id DESC LIMIT 1',
                (eid, self.start, self.start),
            ).fetchone()
            if row:
                self._observe(model, dict(row), learn=False)
            TRAINING_BUDGET.checkpoint('home_statistics_seed')
        model.reset_movement_state()
        self._save(model, self.start)
        interval = max(300.0, (self.end - self.start) / 64.0)
        boundary = self.start + interval
        for row in self._rows(self.start, self.end):
            available = self._available(row)
            while boundary < available and boundary < self.end:
                model.expire(boundary)
                self._save(model, boundary)
                boundary += interval
            self._observe(model, row)
            self.rows += 1
        self.identity = hashlib.sha256(
            repr((self.CONTRACT, self.start, self.end, context.registry_revision,
                  tuple(self.entities), self.rows, tuple(self.times))).encode()
            + b''.join(self.snapshots)
        ).hexdigest()[:24]

    @staticmethod
    def _available(row):
        return max(float(row['ts']), float(row.get('received_ts') or row['ts']))

    def _rows(self, lo, hi):
        if not self.entities or hi <= lo:
            return
        marks = ','.join('?' for _ in self.entities)
        # The source set contains only sparse direct-presence channels, not raw radar
        # samples. Receive ordering also admits late older events at their real time.
        cursor = self.conn.execute(
            f'SELECT *,MAX(ts,COALESCE(received_ts,ts)) AS causal_at FROM entity_history '
            f'WHERE entity_id IN ({marks}) AND ts>? AND ts<=? AND COALESCE(received_ts,ts)<=? '
            'UNION ALL '
            f'SELECT *,MAX(ts,COALESCE(received_ts,ts)) AS causal_at FROM entity_history '
            f'WHERE entity_id IN ({marks}) AND ts<=? AND received_ts>? AND received_ts<=? '
            'ORDER BY causal_at,ts,id',
            (*self.entities, lo, hi, hi, *self.entities, lo, lo, hi),
        )
        while True:
            rows = cursor.fetchmany(256)
            if not rows:
                break
            for row in rows:
                yield dict(row)
            TRAINING_BUDGET.checkpoint('home_statistics_stream')

    def _observe(self, model, row, learn=True):
        eid = row['entity_id']
        model.observe(eid, self.context.area_for(eid),
                      self.context.sensor_probability(eid, archived_state(row)),
                      self._available(row), learn=learn,
                      evidence=self.context.evidence_metadata(eid),
                      event_ts=float(row['ts']), received_ts=self._available(row))

    def _save(self, model, timestamp):
        raw = {'stats': model.export(), 'runtime': {name: getattr(model, name) for name in self.FIELDS},
               'area_sources': {area: sorted(ids) for area, ids in model.area_sources.items()},
               'arrivals': list(model.arrivals)}
        blob = zlib.compress(json.dumps(raw, separators=(',', ':')).encode())
        if self.bytes + len(blob) > self.max_bytes:
            if not self.snapshots:
                raise ValueError('initial causal home checkpoint exceeds memory budget')
            return
        self.times.append(float(timestamp))
        self.snapshots.append(blob)
        self.bytes += len(blob)

    def statistics_at(self, timestamp):
        timestamp = float(timestamp)
        if timestamp < self.start:
            # No information from this training window may reach an earlier sample.
            model = RoomBeliefModel(self.context.options.get('home_model_half_life_days', 45))
            return model.graph, model.dwell, model.calibration
        if timestamp > self.end:
            raise ValueError('causal home statistics query outside training window')
        index = max(0, bisect.bisect_right(self.times, timestamp) - 1)
        raw = json.loads(zlib.decompress(self.snapshots[index]))
        model = RoomBeliefModel(self.context.options.get('home_model_half_life_days', 45), raw['stats'])
        for name, value in raw['runtime'].items():
            setattr(model, name, value)
        model.area_sources = {area: set(ids) for area, ids in raw['area_sources'].items()}
        model.arrivals.extend((area, stamp) for area, stamp in raw['arrivals'])
        for row in self._rows(self.times[index], timestamp):
            self._observe(model, row)
        model.expire(timestamp)
        return model.graph, model.dwell, model.calibration

    def status(self):
        return {'contract': self.CONTRACT, 'direct_sources': len(self.entities),
                'source_rows': self.rows, 'snapshots': len(self.times),
                'compressed_bytes': self.bytes}
