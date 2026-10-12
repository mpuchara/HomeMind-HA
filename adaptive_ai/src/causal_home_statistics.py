"""Bounded causal trajectory statistics for training without a manual home backfill.

Only direct presence observations train paths. Raw energy, controlled lights and the
current live graph are never used as occupancy labels or past trajectory statistics.
Compressed internal snapshots bound memory; queries replay only the remaining prefix.
"""
import bisect
import copy
import hashlib
import json
import sqlite3
import sys
import zlib
from collections import OrderedDict

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
        self._cursors = OrderedDict()
        self._cursor_revision = None
        self.query_calls = self.query_rows = self.cursor_hits = self.cursor_restores = 0
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

    @staticmethod
    def _model_bytes(model):
        # Count the retained Python containers, rather than assuming that a serialized
        # byte is a byte of RAM. Locks are private to each model and are not copied.
        seen = set()
        def size(value):
            if id(value) in seen:
                return 0
            seen.add(id(value))
            result = sys.getsizeof(value)
            if isinstance(value, dict):
                result += sum(size(k) + size(v) for k, v in value.items())
            elif isinstance(value, (list, tuple, set)) or type(value).__name__ == 'deque':
                result += sum(size(v) for v in value)
            return result
        return size({k: v for k, v in vars(model).items() if k != 'lock'})

    @staticmethod
    def _copy_model(model):
        result = RoomBeliefModel(model.half_life / 86400.0)
        for name, value in vars(model).items():
            if name != 'lock':
                setattr(result, name, copy.deepcopy(value))
        return result

    def statistics_at(self, timestamp, *, cursor='default'):
        timestamp = float(timestamp)
        self.query_calls += 1
        if timestamp < self.start:
            # No information from this training window may reach an earlier sample.
            model = RoomBeliefModel(self.context.options.get('home_model_half_life_days', 45))
            return model.graph, model.dwell, model.calibration
        if timestamp > self.end:
            raise ValueError('causal home statistics query outside training window')
        index = max(0, bisect.bisect_right(self.times, timestamp) - 1)
        revision = (
            self.conn.execute('PRAGMA data_version').fetchone()[0],
            self.conn.total_changes,
            self.context.registry_revision,
            getattr(getattr(self.context, 'semantic_reliability', None), 'revision', 0),
            self.context.options.get('home_model_half_life_days', 45),
        )
        if revision != self._cursor_revision:
            self._cursors.clear()
            self._cursor_revision = revision
        previous = self._cursors.pop(cursor, None)
        if previous is not None and previous[0] == index and previous[1] <= timestamp:
            _, lo, model, _ = previous
            self.cursor_hits += 1
        else:
            raw = json.loads(zlib.decompress(self.snapshots[index]))
            model = RoomBeliefModel(self.context.options.get('home_model_half_life_days', 45), raw['stats'])
            for name, value in raw['runtime'].items():
                setattr(model, name, value)
            model.area_sources = {area: set(ids) for area, ids in raw['area_sources'].items()}
            model.arrivals.extend((area, stamp) for area, stamp in raw['arrivals'])
            lo = self.times[index]
            self.cursor_restores += 1
        for row in self._rows(lo, timestamp):
            self._observe(model, row)
            self.query_rows += 1
        model_bytes = self._model_bytes(model)
        remaining = max(0, self.max_bytes - self.bytes)
        if model_bytes <= remaining:
            self._cursors[cursor] = (index, timestamp, model, model_bytes)
            while len(self._cursors) > 2 or sum(item[3] for item in self._cursors.values()) > remaining:
                self._cursors.popitem(last=False)
        # Query-only expiry can teach a missing destination at this timestamp. It must
        # never enter the next prefix, where the legacy replay expires only at actual
        # source events/checkpoint boundaries. Returned dictionaries are private too.
        view = self._copy_model(model)
        view.expire(timestamp)
        return view.graph, view.dwell, view.calibration

    def status(self):
        return {'contract': self.CONTRACT, 'direct_sources': len(self.entities),
                'source_rows': self.rows, 'snapshots': len(self.times),
                'compressed_bytes': self.bytes,
                'query_calls': self.query_calls, 'query_rows': self.query_rows,
                'cursor_hits': self.cursor_hits, 'cursor_restores': self.cursor_restores,
                'cursor_entries': len(self._cursors),
                'cursor_bytes': sum(item[3] for item in self._cursors.values()),
                'max_bytes': self.max_bytes}
