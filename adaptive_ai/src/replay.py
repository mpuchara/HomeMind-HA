"""Bounded-memory historical views. Large timelines and held-out vectors stay on disk."""
import json
import sqlite3
import tempfile
from context import archived_state, TemporalHistory, state_scalar
from home_state import SharedHomeStateModel


class BoundedUsage:
    """Discovery needs a count and last transition, not the entire target history."""
    def __init__(self):
        self.count, self.last = 0, None

    def append(self, item):
        self.count += 1
        self.last = item

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if index != -1 or self.last is None:
            raise IndexError(index)
        return self.last


class DeferredUpdates:
    def __init__(self, policies):
        self.policies = policies
        self.file = tempfile.TemporaryFile(mode='w+t', encoding='utf-8')
        self.count = 0

    def append(self, update):
        policy, horizon, action, features, reward, ts = update
        self.file.write(json.dumps([policy.agent['id'], horizon, action, features, reward, ts], separators=(',', ':'))+'\n')
        self.count += 1

    def __len__(self):
        return self.count

    def __iter__(self):
        self.file.seek(0)
        for line in self.file:
            aid, horizon, action, features, reward, ts = json.loads(line)
            yield self.policies[aid], horizon, action, {int(k):v for k,v in features.items()}, reward, ts

    def close(self):
        self.file.close()

    def __del__(self):
        self.close()


class HistoricalHomeView:
    def __init__(self, context, raw):
        self.context, self.raw = context, raw
        self.home = SharedHomeStateModel(context.options.get('home_model_half_life_days',45), raw)
        self.stats = self.home.graph, self.home.dwell

    def reset(self):
        self.home = SharedHomeStateModel(self.context.options.get('home_model_half_life_days',45))
        self.home.graph, self.home.dwell = self.stats

    def forecast(self, target, ts):
        return self.home.forecast(self.context.area_for(target), ts)


class SQLiteTemporalTracker:
    """Indexed as-of queries support rewinds without future-state leakage.

    RAM: <=64 samples per selected input plus current mapped occupancy sources.
    Room graph statistics come from the last global checkpoint before this chunk.
    Each agent reads the graph; only global bootstrap / live events learn it.
    """
    def __init__(self, store, watched, context, start, end):
        self.conn = sqlite3.connect(store.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA cache_size=-2048')
        self.watched = sorted(watched)
        self.context = context
        self.end = end
        self.state_map = {}
        self.history = TemporalHistory(maxlen=64)
        try:
            row = self.conn.execute('SELECT model FROM home_checkpoints WHERE ts<? ORDER BY ts DESC LIMIT 1', (start,)).fetchone()
        except sqlite3.OperationalError:
            row = None
        self.home_view = HistoricalHomeView(context, json.loads(row[0]) if row else None)

    def _before(self, eid, ts, count=64):
        cursor = self.conn.execute('SELECT * FROM entity_history WHERE entity_id=? AND ts<=? ORDER BY ts DESC,id DESC LIMIT ?', (eid, ts, count))
        # Explicit hard LIMIT, independent of archive length.
        return list(reversed([dict(r) for r in cursor]))

    def advance(self, ts):
        ts = min(float(ts), self.end)
        self.state_map = {}
        self.history = TemporalHistory(maxlen=64)
        for eid in self.watched:
            rows = self._before(eid, ts)
            for row in rows:
                state = archived_state(row)
                self.state_map[eid] = state
                self.history.add(eid, row['ts'], state)
        view = self.home_view
        view.reset()
        ids = self.context.relevant_entities()
        # Seed occupancy as-of t-30, then stream only the short observed trajectory.
        # Seeding does not create fictitious arrivals at the query boundary.
        for eid in ids:
            rows = self._before(eid, ts-30, 1)
            if rows:
                st = archived_state(rows[0])
                view.home.observe(eid, self.context.area_for(eid), self.context.sensor_probability(eid,st), ts-30, learn=False)
        view.home.arrivals.clear()
        view.home.pending = None
        if ids:
            sql = 'SELECT * FROM entity_history WHERE ts>? AND ts<=? AND entity_id IN (%s) ORDER BY ts,id' % ','.join('?' for _ in ids)
            for r in self.conn.execute(sql, [ts-30, ts] + ids):
                row = dict(r)
                eid = row['entity_id']
                view.home.observe(eid, self.context.area_for(eid), self.context.sensor_probability(eid,archived_state(row)), row['ts'], learn=False)
        self.history.home_context = view

    def _edges(self, eid, lo, hi):
        previous = self._before(eid, lo, 1)
        prev = state_scalar(archived_state(previous[-1])) if previous else None
        for row in self.conn.execute('SELECT * FROM entity_history WHERE entity_id=? AND ts>? AND ts<=? ORDER BY ts,id', (eid,lo,hi)):
            cur = state_scalar(archived_state(dict(row)))
            if prev is not None and cur is not None:
                if cur > .25 and prev <= .25:
                    yield row['ts'], True
                elif cur < -.25 and prev >= -.25:
                    yield row['ts'], False
            prev = cur

    def directional_transition_before(self, eid, at_ts, positive, window):
        latest = None
        for ts, direction in self._edges(eid, at_ts-window, at_ts):
            if direction == positive:
                latest = ts
        return latest

    def first_directional_transition_after(self, eid, start, end, positive):
        for ts, direction in self._edges(eid,start,end):
            if direction == positive:
                return ts
        return None

    def close(self):
        self.conn.close()

    def __del__(self):
        self.close()
