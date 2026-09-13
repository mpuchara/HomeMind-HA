"""Shared, bounded online room-transition statistics; no HA or policy dependencies.

One aggregate trajectory, not person identification. Simultaneous occupants can make
it ambiguous. A pending next-room trial includes a no-arrival outcome after 30 s.
Only observations at/before query time participate. Live occupancy is never restored
from disk: a restart must observe fresh HA states.
"""
import math
import copy
import threading
from collections import deque

FEATURE_NAMES = ('occupancy_now', 'occupancy_in_1s', 'occupancy_in_3s',
                 'occupancy_in_5s', 'arrival_probability',
                 'departure_probability', 'trajectory_confidence')


class SharedHomeStateModel:
    VERSION = 1
    MAX_AREAS = 128
    MAX_CONTEXTS = 2048
    GAP = 30.0

    def __init__(self, half_life_days=45, raw=None):
        self.half_life = max(1.0, float(half_life_days)) * 86400
        self.graph = {}
        self.dwell = {}
        self.values = {}
        self.sources = {}
        self.area_sources = {}
        self.arrivals = deque(maxlen=2)
        self.pending = None
        self.updated = 0
        self.last_ts = 0.0
        self.last_decay_ts = 0.0
        self.revision = 0
        self.lock = threading.RLock()
        if raw and raw.get('version') == self.VERSION:
            for row in raw.get('graph', [])[:self.MAX_CONTEXTS]:
                key = tuple(row['context'])
                self.graph[key] = {'ts': float(row['ts']), 'outcomes': {
                    k: [float(x) for x in v] for k, v in row['outcomes'].items()}}
            self.dwell = dict(raw.get('dwell', {}))
            self.updated = int(raw.get('updates', 0))
            self.last_decay_ts = float(raw.get('last_decay_ts', 0))

    def _factor(self, age):
        return math.exp(-math.log(2) * max(0, age) / self.half_life)

    def _decay_row(self, row, ts):
        if ts - row['ts'] < 60:
            return
        factor = self._factor(ts - row['ts'])
        for bins in row['outcomes'].values():
            for i in range(len(bins)):
                bins[i] *= factor
        row['ts'] = ts
        self.last_decay_ts = max(self.last_decay_ts, ts)

    def _record(self, context, destination, delay, ts, weight=1):
        if context not in self.graph:
            if len(self.graph) >= self.MAX_CONTEXTS:
                # Hard bounded storage, evict only on insertion, never on hot queries.
                oldest = min(self.graph, key=lambda k: self.graph[k]['ts'])
                del self.graph[oldest]
            self.graph[context] = {'ts': ts, 'outcomes': {}}
        row = self.graph[context]
        self._decay_row(row, ts)
        if destination not in row['outcomes'] and len(row['outcomes']) >= 15:
            # Aggregate the long tail into unknown/other, preserving denominators.
            destination = None
        bins = row['outcomes'].setdefault(destination or '', [0.0] * 7)
        bucket = min(6, max(0, int(math.ceil(delay))))
        bins[bucket] += weight

    def _resolve(self, destination, ts):
        if not self.pending:
            return
        contexts, started = self.pending
        delay = ts - started
        if delay > self.GAP:
            destination = None
        for key in contexts:
            self._record(key, destination, delay, ts)
        self.pending = None

    def expire(self, ts, learn=True):
        with self.lock:
            if self.pending and ts - self.pending[1] > self.GAP:
                if learn:
                    self._resolve(None, ts)
                else:
                    self.pending = None

    def observe(self, entity_id, area, probability, ts, learn=True):
        """Update one source and one area (bounded sources/area); ignore old events."""
        if not area:
            return False
        ts = float(ts)
        with self.lock:
            previous = self.sources.get(entity_id)
            if previous and ts <= previous[2]:
                return False
            if area not in self.values and len(self.values) >= self.MAX_AREAS:
                return False
            if entity_id not in self.sources and len(self.sources) >= 4096:
                return False
            self.expire(ts, learn)
            old = self.values.get(area, {}).get('p', 0.0)
            if previous and previous[0] != area:
                old_area = previous[0]
                self.area_sources.get(old_area, set()).discard(entity_id)
                remaining = [self.sources[eid][1] for eid in self.area_sources.get(old_area, ())
                             if self.sources[eid][1] is not None]
                self.values[old_area]['p'] = max(remaining) if remaining else 0.0
                self.values[old_area]['known'] = bool(remaining)
            self.sources[entity_id] = (area, probability, ts)
            self.area_sources.setdefault(area, set()).add(entity_id)
            # Only inspect this area's admitted sensors, not the whole house.
            probs = [self.sources[eid][1] for eid in self.area_sources[area]
                     if self.sources[eid][1] is not None]
            p = max(probs) if probs else 0.0
            slot = self.values.setdefault(area, {'p': 0, 'arrival': None, 'departure': None})
            slot['known'] = bool(probs)
            slot['p'] = p
            entered = p >= .5 and old < .5
            departed = p < .5 and old >= .5
            if entered:
                if learn:
                    self._resolve(area, ts)
                previous_area = self.arrivals[-1][0] if self.arrivals and ts - self.arrivals[-1][1] <= self.GAP else None
                self.arrivals.append((area, ts))
                contexts = [(area,)]
                if previous_area and previous_area != area:
                    contexts.append((previous_area, area))
                self.pending = (contexts, ts)
                slot['arrival'] = ts
            elif departed:
                slot['departure'] = ts
                if learn and slot['arrival'] is not None:
                    duration = min(600, max(0, int(math.ceil(ts - slot['arrival']))))
                    row = self.dwell.setdefault(area, {'ts': ts, 'outcomes': {}})
                    self._decay_row(row, ts)
                    bins = row['outcomes'].setdefault('duration', [0.0] * 61)
                    bins[min(60, duration // 10)] += 1
            self.last_ts = max(self.last_ts, ts)
            self.updated += int(learn)
            self.revision += 1
            return entered or departed

    def _active_row(self, ts):
        if not self.arrivals or ts - self.arrivals[-1][1] > self.GAP:
            return None, 0.0
        context = tuple(a for a, _ in self.arrivals)
        row = self.graph.get(context)
        if row and sum(sum(b) for b in row['outcomes'].values()) >= 4:
            return row, max(0, ts - self.arrivals[-1][1])
        return self.graph.get((context[-1],)), max(0, ts - self.arrivals[-1][1])

    def forecast(self, area, ts):
        with self.lock:
            slot = self.values.get(area, {})
            now = slot.get('p', 0.0)
            row, age = self._active_row(ts)
            arrivals = []
            support = 0.0
            for horizon in (1, 3, 5):
                hits = trials = 0.0
                if row:
                    factor = self._factor(ts - row['ts'])
                    for destination, bins in row['outcomes'].items():
                        for delay, count in enumerate(bins):
                            # Last bucket means >5 s or no arrival, never an early hit.
                            if delay > age or delay == 6:
                                trials += count * factor
                            if destination == area and age < delay <= min(5, age + horizon):
                                hits += count * factor
                    support = trials
                arrivals.append(hits / (trials + 2.0))
            departure = 0.0
            dwell = self.dwell.get(area)
            if now >= .5 and dwell and slot.get('arrival') is not None:
                elapsed = max(0, ts - slot['arrival'])
                bins = dwell['outcomes'].get('duration', [])
                alive = sum(n for i, n in enumerate(bins) if (i + 1) * 10 > elapsed)
                soon = sum(n for i, n in enumerate(bins) if elapsed < (i + 1) * 10 <= elapsed + 5)
                factor = self._factor(ts - dwell['ts'])
                departure = soon * factor / (alive * factor + 2)
            conf = support / (support + 8.0)
            # Probabilities are model estimates, never policy confidence.
            return dict(zip(FEATURE_NAMES, [now] + [now*(1-departure*h/5) + (1-now)*p for h,p in zip((1,3,5),arrivals)] +
                            [arrivals[-1], departure, conf])) | {
                'area_id': area, 'known': bool(slot.get('known')), 'support': support}

    def export(self):
        with self.lock:
            # Copy via explicit lists; no mutable live graph escapes the lock.
            return {'version': self.VERSION, 'updates': self.updated,
                    'last_decay_ts': self.last_decay_ts,
                    'graph': [{'context': list(k), 'ts': v['ts'], 'outcomes':
                               {d: list(b) for d, b in v['outcomes'].items()}}
                              for k, v in self.graph.items()],
                    'dwell': {k: {'ts': v['ts'], 'outcomes': {d: list(b) for d, b in v['outcomes'].items()}}
                              for k, v in self.dwell.items()}}

    def live_delta(self, cutoff):
        """Fixed-size sufficient statistics for events during a long bootstrap."""
        with self.lock:
            delta = SharedHomeStateModel(self.half_life / 86400)
            delta.values = copy.deepcopy(self.values)
            delta.sources = dict(self.sources)
            delta.area_sources = {k: set(v) for k, v in self.area_sources.items()}
            delta.arrivals = deque(self.arrivals, maxlen=2)
            delta.pending = copy.deepcopy(self.pending)
            delta.last_ts = self.last_ts
            # An already expired trial belongs to history, not post-cutoff feedback.
            delta.expire(cutoff, learn=False)
            return delta

    def merge_statistics(self, delta):
        """Merge disjoint observations, aligning decay timestamps before addition."""
        with self.lock, delta.lock:
            for target, incoming, limit in ((self.graph, delta.graph, self.MAX_CONTEXTS),
                                             (self.dwell, delta.dwell, self.MAX_AREAS)):
                for key, source in incoming.items():
                    if key not in target:
                        if len(target) >= limit:
                            del target[min(target, key=lambda k: target[k]['ts'])]
                        target[key] = copy.deepcopy(source)
                        continue
                    row = target[key]
                    ts = max(row['ts'], source['ts'])
                    left, right = self._factor(ts-row['ts']), self._factor(ts-source['ts'])
                    for bins in row['outcomes'].values():
                        for i in range(len(bins)):
                            bins[i] *= left
                    for destination, bins in source['outcomes'].items():
                        if destination not in row['outcomes'] and len(row['outcomes']) >= 15:
                            destination = ''
                        merged = row['outcomes'].setdefault(destination, [0.0]*len(bins))
                        for i, value in enumerate(bins):
                            merged[i] += value*right
                    row['ts'] = ts
            self.updated += delta.updated
            self.last_decay_ts = max(self.last_decay_ts, delta.last_decay_ts)

    def diagnostics(self, ts):
        with self.lock:
            transitions = []
            for key, row in self.graph.items():
                total = sum(sum(b) for b in row['outcomes'].values())
                factor = self._factor(ts-row['ts'])
                for dst, bins in row['outcomes'].items():
                    if dst:
                        transitions.append({'path': list(key) + [dst], 'probability': sum(bins)*factor/(total*factor+2),
                                            'weight': sum(bins)*self._factor(ts-row['ts'])})
            return {'areas': len(self.values), 'edges': len(transitions),
                    'updates': self.updated, 'revision': self.revision,
                    'half_life_days': self.half_life / 86400,
                    'top_transitions': sorted(transitions, key=lambda r:r['weight'], reverse=True)[:10]}
