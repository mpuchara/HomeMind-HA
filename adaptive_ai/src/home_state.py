"""Versioned, bounded probabilistic room belief and aggregate movement model.

Stage 08 replaces the single-trajectory assumptions of SharedHomeStateModel with an
interpretable RoomBeliefModel. It deliberately does *not* identify people from binary
sensors. Multiple rooms may be occupied simultaneously and movement is represented by a
small set of weighted, anonymous path hypotheses.

Only statistical graph/dwell/calibration state is checkpointed. Live sensor states,
movement hypotheses and stale ON values are never restored after restart. Historical
replay uses the same observe/forecast time rules as live runtime.
"""
from __future__ import annotations

import copy
import math
import threading
from collections import deque


# Keep legacy feature names stable so saved policy vectors are not reinterpreted.
FEATURE_NAMES = (
    'occupancy_now', 'occupancy_in_1s', 'occupancy_in_3s', 'occupancy_in_5s',
    'arrival_probability', 'departure_probability', 'trajectory_confidence',
)


ROLE_PARAMS = {
    'pir': dict(active=.96, stale_after=20.0, half_life=18.0, observability=.60,
                movement=.70, semantics='event_presence'),
    'radar_occupancy': dict(active=.99, stale_after=120.0, half_life=240.0, observability=.96,
                            movement=1.0, semantics='stationary_occupancy'),
    'occupancy_binary': dict(active=1.0, stale_after=90.0, half_life=180.0, observability=.82,
                             movement=.85, semantics='binary_occupancy'),
    'radar_activity': dict(active=.30, stale_after=4.0, half_life=5.0, observability=.35,
                           movement=.20, semantics='activity_likelihood'),
    'auxiliary_probability': dict(active=1.0, stale_after=20.0, half_life=30.0,
                                  observability=.55, movement=.25,
                                  semantics='probability_like_score'),
    'tracker': dict(active=.95, stale_after=180.0, half_life=300.0, observability=.75,
                    movement=.55, semantics='aggregate_tracker'),
    'door': dict(active=0.0, stale_after=5.0, half_life=5.0, observability=.15,
                 movement=.35, semantics='transition_only'),
    'auxiliary': dict(active=.20, stale_after=10.0, half_life=15.0, observability=.25,
                      movement=.10, semantics='auxiliary_likelihood'),
}


class RoomBeliefModel:
    VERSION = 2
    LEGACY_VERSION = 1
    MAX_AREAS = 128
    MAX_CONTEXTS = 2048
    MAX_HYPOTHESES = 8
    MAX_SOURCES = 4096
    GAP = 30.0
    MIN_HYPOTHESIS_MASS = .08

    def __init__(self, half_life_days=45, raw=None):
        self.half_life = max(1.0, float(half_life_days)) * 86400
        self.graph = {}
        self.dwell = {}
        self.calibration = self._empty_calibration()
        self.values = {}
        self.sources = {}
        self.area_sources = {}
        self.arrivals = deque(maxlen=8)
        self.hypotheses = []
        self.pending = None
        self.updated = 0
        self.last_ts = 0.0
        self.last_decay_ts = 0.0
        self.revision = 0
        self.migrated_from = None
        self.lock = threading.RLock()
        self._load_checkpoint(raw)

    @staticmethod
    def _empty_calibration():
        return {
            'count': 0,
            'sum_brier': 0.0,
            'bins': [dict(count=0, sum_prediction=0.0, sum_observed=0.0) for _ in range(5)],
        }

    def _load_checkpoint(self, raw):
        if not isinstance(raw, dict):
            return
        version = int(raw.get('version') or 0)
        if version not in (self.LEGACY_VERSION, self.VERSION):
            return
        self.migrated_from = self.LEGACY_VERSION if version == self.LEGACY_VERSION else None
        for row in raw.get('graph', [])[:self.MAX_CONTEXTS]:
            try:
                key = tuple(str(x) for x in row['context'])
                self.graph[key] = {
                    'ts': float(row['ts']),
                    'outcomes': {str(k): [float(x) for x in v] for k, v in row['outcomes'].items()},
                }
            except (KeyError, TypeError, ValueError):
                continue
        for area, row in list((raw.get('dwell') or {}).items())[:self.MAX_AREAS]:
            try:
                self.dwell[str(area)] = {
                    'ts': float(row['ts']),
                    'outcomes': {str(k): [float(x) for x in v] for k, v in row['outcomes'].items()},
                }
            except (KeyError, TypeError, ValueError):
                continue
        if version == self.VERSION and isinstance(raw.get('calibration'), dict):
            source = raw['calibration']
            bins = source.get('bins') or []
            self.calibration = self._empty_calibration()
            self.calibration['count'] = int(source.get('count') or 0)
            self.calibration['sum_brier'] = float(source.get('sum_brier') or 0.0)
            for idx, row in enumerate(bins[:5]):
                self.calibration['bins'][idx] = {
                    'count': int(row.get('count') or 0),
                    'sum_prediction': float(row.get('sum_prediction') or 0.0),
                    'sum_observed': float(row.get('sum_observed') or 0.0),
                }
        self.updated = int(raw.get('updates') or 0)
        self.last_decay_ts = float(raw.get('last_decay_ts') or 0.0)

    def _factor(self, age):
        return math.exp(-math.log(2) * max(0.0, float(age)) / self.half_life)

    def _decay_row(self, row, ts):
        if ts - row['ts'] < 60:
            return
        factor = self._factor(ts - row['ts'])
        for bins in row['outcomes'].values():
            for i in range(len(bins)):
                bins[i] *= factor
        row['ts'] = ts
        self.last_decay_ts = max(self.last_decay_ts, ts)

    def _record(self, context, destination, delay, ts, weight=1.0):
        weight = max(0.0, float(weight))
        if weight <= 0 or not context:
            return
        context = tuple(str(x) for x in context[-2:])
        if context not in self.graph:
            if len(self.graph) >= self.MAX_CONTEXTS:
                del self.graph[min(self.graph, key=lambda k: self.graph[k]['ts'])]
            self.graph[context] = {'ts': float(ts), 'outcomes': {}}
        row = self.graph[context]
        self._decay_row(row, float(ts))
        key = str(destination or '')
        if key not in row['outcomes'] and len(row['outcomes']) >= 15:
            key = ''
        bins = row['outcomes'].setdefault(key, [0.0] * 7)
        bucket = min(6, max(0, int(math.ceil(max(0.0, float(delay))))))
        bins[bucket] += weight

    @staticmethod
    def _params(role):
        return ROLE_PARAMS.get(str(role or ''), ROLE_PARAMS['auxiliary'])

    def source_area(self, entity_id):
        row = self.sources.get(entity_id)
        return row.get('area') if isinstance(row, dict) else None

    def source_role(self, entity_id):
        row = self.sources.get(entity_id)
        return row.get('role') if isinstance(row, dict) else None

    def _freshness(self, source, ts):
        if not source.get('available') or float(source.get('ts') or 0) > float(ts):
            return 0.0
        params = self._params(source.get('role'))
        age = max(0.0, float(ts) - float(source.get('state_since_ts') or source.get('ts') or ts))
        try:
            active = float(source.get('value')) >= .5
        except (TypeError, ValueError):
            active = False
        if not active:
            return 1.0
        stale_after = float(params['stale_after'])
        if age <= stale_after:
            return 1.0
        half = max(1e-6, float(params['half_life']))
        return math.exp(-math.log(2) * (age - stale_after) / half)

    def _fuse_room(self, area, ts):
        rows, positive, calibrated, raw_activity, absence = [], [], [], [], []
        observability_terms, direct_active = [], []
        for eid in sorted(self.area_sources.get(area, ())):
            source = self.sources.get(eid)
            if not source or float(source.get('ts') or 0) > float(ts):
                continue
            role = str(source.get('role') or 'auxiliary')
            params = self._params(role)
            available = bool(source.get('available'))
            comm = max(0.0, min(1.0, float(source.get('communication_reliability') or 0.0)))
            fresh = self._freshness(source, ts)
            value = source.get('value')
            try:
                q = max(0.0, min(1.0, float(value))) if value is not None else None
            except (TypeError, ValueError):
                q = None
            observability_terms.append(float(params['observability']) * comm if available else 0.0)
            contribution = 0.0
            if available and q is not None:
                if role in {'pir', 'radar_occupancy', 'occupancy_binary', 'tracker'}:
                    if q >= .5:
                        contribution = float(params['active']) * comm * fresh
                        positive.append(contribution)
                        direct_active.append((role, contribution, eid))
                    else:
                        strength = comm * {'pir': .15, 'radar_occupancy': .95,
                                           'occupancy_binary': .75, 'tracker': .70}.get(role, .25)
                        absence.append(strength)
                        contribution = -strength
                elif role == 'auxiliary_probability':
                    adjusted = .5 + (q - .5) * comm * fresh
                    calibrated.append(max(0.0, min(1.0, adjusted)))
                    contribution = adjusted - .5
                elif role in {'radar_activity', 'auxiliary'}:
                    contribution = q * float(params['active']) * comm * fresh
                    raw_activity.append(contribution)
            rows.append({
                'entity_id': eid,
                'role': role,
                'value_semantics': params['semantics'],
                'available': available,
                'communication_reliability': comm,
                'evidence_age_seconds': max(0.0, float(ts) - float(source.get('state_since_ts') or source.get('ts') or ts)),
                'evidence_freshness': fresh,
                'contribution': contribution,
            })

        remaining = 1.0
        for term in observability_terms:
            remaining *= 1.0 - max(0.0, min(.99, term))
        observability = 1.0 - remaining if observability_terms else 0.0
        if positive:
            rest = 1.0
            for item in positive:
                rest *= 1.0 - max(0.0, min(1.0, item))
            occupancy = 1.0 - rest
        elif calibrated:
            occupancy = sum(calibrated) / len(calibrated)
        elif raw_activity:
            occupancy = min(.45, sum(raw_activity))
        elif absence:
            occupancy = 0.0
        else:
            occupancy = .5

        entropy = 0.0
        if 0.0 < occupancy < 1.0:
            entropy = -(occupancy * math.log2(occupancy) + (1 - occupancy) * math.log2(1 - occupancy))
        conflict = min(1.0, max(positive) * max(absence)) if positive and absence else 0.0
        if len(calibrated) > 1:
            conflict = max(conflict, min(1.0, max(calibrated) - min(calibrated)))
        uncertainty = max(entropy, 1.0 - observability, conflict)
        return {
            'occupancy': max(0.0, min(1.0, occupancy)),
            'uncertainty': max(0.0, min(1.0, uncertainty)),
            'observability': max(0.0, min(1.0, observability)),
            'known': bool(observability >= .25),
            'evidence_sources': rows,
            'direct_active': direct_active,
        }

    def _topology_prior(self, path, destination, ts):
        row = self.graph.get(tuple(path[-2:])) or self.graph.get((path[-1],))
        if not row:
            return 1.0
        factor = self._factor(float(ts) - float(row['ts']))
        totals = {dst: sum(bins) * factor for dst, bins in row['outcomes'].items()}
        total = sum(totals.values())
        if total < 2.0:
            return 1.0
        return (totals.get(str(destination), 0.0) + .5) / (total + .5 * max(1, len(totals)))

    def _stationary_anchor(self, area, ts):
        for eid in self.area_sources.get(area, ()):
            source = self.sources.get(eid) or {}
            if source.get('role') != 'radar_occupancy' or not source.get('available'):
                continue
            try:
                active = float(source.get('value')) >= .5
            except (TypeError, ValueError):
                active = False
            if active and self._freshness(source, ts) >= .75:
                return True
        return False

    def _prune_hypotheses(self, ts):
        alive = [h for h in self.hypotheses
                 if float(h.get('mass') or 0) >= self.MIN_HYPOTHESIS_MASS
                 and float(ts) - float(h.get('ts') or 0) <= self.GAP]
        alive.sort(key=lambda h: (-float(h['mass']), -float(h['ts']), tuple(h['path'])))
        self.hypotheses = alive[:self.MAX_HYPOTHESES]
        self._refresh_pending_compat()

    def _refresh_pending_compat(self):
        if not self.hypotheses:
            self.pending = None
            return
        newest = max(self.hypotheses, key=lambda h: float(h.get('ts') or 0))
        self.pending = ([tuple(newest['path'])], float(newest['ts']))

    def _movement_enter(self, area, ts, movement_weight=1.0, learn=True):
        candidates = []
        for hypothesis in self.hypotheses:
            age = float(ts) - float(hypothesis['ts'])
            if age < 0 or age > self.GAP or hypothesis['area'] == area:
                continue
            prior = self._topology_prior(hypothesis['path'], area, ts)
            candidates.append((float(hypothesis['mass']) * max(.05, prior), hypothesis))
        new_hypotheses = []
        if candidates:
            candidates.sort(key=lambda item: (-item[0], tuple(item[1]['path'])))
            total_score = sum(score for score, _ in candidates) or 1.0
            if len(candidates) == 1:
                movement_mass = .95 * max(.2, min(1.0, movement_weight))
                if self._stationary_anchor(candidates[0][1]['area'], ts):
                    movement_mass = min(movement_mass, .35)
            else:
                movement_mass = .70 * max(.2, min(1.0, movement_weight))
            used = 0.0
            for score, hypothesis in candidates:
                assignment = min(float(hypothesis['mass']), movement_mass * score / total_score)
                if assignment <= 0:
                    continue
                if learn:
                    self._record(tuple(hypothesis['path']), area,
                                 max(0.0, float(ts) - float(hypothesis['ts'])), ts, assignment)
                hypothesis['mass'] = max(0.0, float(hypothesis['mass']) - assignment)
                path = tuple((tuple(hypothesis['path']) + (area,))[-2:])
                new_hypotheses.append({'path': path, 'area': area, 'ts': float(ts), 'mass': assignment})
                used += assignment
            external = max(0.0, 1.0 - used)
            if external >= self.MIN_HYPOTHESIS_MASS:
                new_hypotheses.append({'path': (area,), 'area': area, 'ts': float(ts), 'mass': external})
        else:
            new_hypotheses.append({'path': (area,), 'area': area, 'ts': float(ts), 'mass': 1.0})
        self.hypotheses.extend(new_hypotheses)
        self.arrivals.append((area, float(ts)))
        self._prune_hypotheses(ts)

    def expire(self, ts, learn=True):
        with self.lock:
            remaining = []
            for hypothesis in self.hypotheses:
                age = float(ts) - float(hypothesis['ts'])
                if age > self.GAP:
                    if learn and float(hypothesis.get('mass') or 0) > 0:
                        self._record(tuple(hypothesis['path']), None, age, ts, hypothesis['mass'])
                else:
                    remaining.append(hypothesis)
            self.hypotheses = remaining
            self._prune_hypotheses(ts)

    def reset_movement_state(self):
        with self.lock:
            self.arrivals.clear()
            self.hypotheses.clear()
            self.pending = None

    def reset_live_state(self):
        with self.lock:
            self.values = {}
            self.sources = {}
            self.area_sources = {}
            self.reset_movement_state()
            self.last_ts = 0.0

    def observe(self, entity_id, area, probability, ts, learn=True, evidence=None):
        if not area:
            return False
        ts = float(ts)
        evidence = dict(evidence or {})
        role = str(evidence.get('role') or ('occupancy_binary' if probability in (0, 1, 0.0, 1.0) else 'auxiliary'))
        params = self._params(role)
        with self.lock:
            previous = self.sources.get(entity_id)
            if previous and ts <= float(previous.get('ts') or 0):
                return False
            if area not in self.values and len(self.values) >= self.MAX_AREAS:
                return False
            if entity_id not in self.sources and len(self.sources) >= self.MAX_SOURCES:
                return False
            self.expire(ts, learn)
            old_area = previous.get('area') if previous else None
            old_p = float(self.values.get(area, {}).get('p', self._fuse_room(area, ts)['occupancy']))
            available = probability is not None
            if not available:
                communication = 0.0
            elif previous and not previous.get('available'):
                communication = .60
            elif previous:
                communication = min(1.0, float(previous.get('communication_reliability') or .6) + .15)
            else:
                communication = 1.0
            same_state = bool(previous and previous.get('available') == available
                              and previous.get('value') == probability and previous.get('role') == role)
            state_since = float(previous.get('state_since_ts')) if same_state else ts
            self.sources[entity_id] = {
                'area': area, 'value': probability, 'ts': ts, 'state_since_ts': state_since,
                'available': available, 'communication_reliability': communication,
                'role': role, 'value_semantics': evidence.get('value_semantics') or params['semantics'],
            }
            if old_area and old_area != area:
                self.area_sources.get(old_area, set()).discard(entity_id)
            self.area_sources.setdefault(area, set()).add(entity_id)
            if old_area and old_area != area:
                prior = self._fuse_room(old_area, ts)
                old_slot = self.values.setdefault(old_area, {'p': .5, 'arrival': None, 'departure': None})
                old_slot.update(p=prior['occupancy'], known=prior['known'],
                                observability=prior['observability'], uncertainty=prior['uncertainty'])
            belief = self._fuse_room(area, ts)
            slot = self.values.setdefault(area, {'p': .5, 'arrival': None, 'departure': None})
            slot.update(p=belief['occupancy'], known=belief['known'], observability=belief['observability'],
                        uncertainty=belief['uncertainty'])
            new_p = float(belief['occupancy'])
            entered = bool(belief['direct_active'] and new_p >= .5 and old_p < .5)
            departed = bool(new_p < .5 and old_p >= .5 and available)
            if entered:
                self._movement_enter(area, ts, float(params.get('movement') or 0.0), learn=learn)
                slot['arrival'] = ts
            elif departed:
                slot['departure'] = ts
                if learn and slot.get('arrival') is not None:
                    duration = min(600, max(0, int(math.ceil(ts - float(slot['arrival'])))))
                    row = self.dwell.setdefault(area, {'ts': ts, 'outcomes': {}})
                    self._decay_row(row, ts)
                    bins = row['outcomes'].setdefault('duration', [0.0] * 61)
                    bins[min(60, duration // 10)] += 1
            self.last_ts = max(self.last_ts, ts)
            self.updated += int(bool(learn))
            self.revision += 1
            self._refresh_pending_compat()
            return entered or departed

    def _active_hypotheses(self, ts):
        active = [copy.deepcopy(h) for h in self.hypotheses
                  if 0 <= float(ts) - float(h['ts']) <= self.GAP and float(h['mass']) > 0]
        if active:
            return active
        recent = [(a, t) for a, t in self.arrivals if 0 <= float(ts) - float(t) <= self.GAP]
        if recent:
            path = tuple(a for a, _ in recent[-2:])
            return [{'path': path, 'area': path[-1], 'ts': float(recent[-1][1]), 'mass': 1.0}]
        return []

    def _arrival_forecast(self, area, ts):
        hypotheses = self._active_hypotheses(ts)
        probabilities, support = [], 0.0
        for horizon in (1, 3, 5):
            hits = trials = 0.0
            for hypothesis in hypotheses:
                path = tuple(hypothesis['path'])
                row = self.graph.get(path) or self.graph.get((path[-1],))
                if not row:
                    continue
                age = max(0.0, float(ts) - float(hypothesis['ts']))
                factor = self._factor(float(ts) - float(row['ts'])) * float(hypothesis['mass'])
                for destination, bins in row['outcomes'].items():
                    for delay, count in enumerate(bins):
                        weighted = float(count) * factor
                        if delay > age or delay == 6:
                            trials += weighted
                        if destination == area and age < delay <= min(5.0, age + horizon):
                            hits += weighted
            support = max(support, trials)
            probabilities.append(hits / (trials + 2.0))
        return probabilities, support, hypotheses

    def _departure_forecast(self, area, ts, occupancy):
        slot, dwell = self.values.get(area, {}), self.dwell.get(area)
        if occupancy < .5 or not dwell or slot.get('arrival') is None:
            return 0.0
        elapsed = max(0.0, float(ts) - float(slot['arrival']))
        bins = dwell['outcomes'].get('duration', [])
        alive = sum(n for i, n in enumerate(bins) if (i + 1) * 10 > elapsed)
        soon = sum(n for i, n in enumerate(bins) if elapsed < (i + 1) * 10 <= elapsed + 5)
        factor = self._factor(float(ts) - float(dwell['ts']))
        return soon * factor / (alive * factor + 2.0)

    def forecast(self, area, ts):
        with self.lock:
            ts = float(ts)
            belief = self._fuse_room(area, ts) if area else {
                'occupancy': .5, 'uncertainty': 1.0, 'observability': 0.0,
                'known': False, 'evidence_sources': [], 'direct_active': [],
            }
            now = float(belief['occupancy'])
            arrivals, support, hypotheses = self._arrival_forecast(area, ts) if area else ([0, 0, 0], 0.0, [])
            departure = self._departure_forecast(area, ts, now) if area else 0.0
            occupancy_h = [max(0.0, min(1.0, now * (1 - departure * h / 5.0) + (1 - now) * p))
                           for h, p in zip((1, 3, 5), arrivals)]
            trajectory_confidence = support / (support + 8.0)
            if area:
                slot = self.values.setdefault(area, {'p': now, 'arrival': None, 'departure': None})
                slot.update(p=now, known=belief['known'], observability=belief['observability'],
                            uncertainty=belief['uncertainty'])
            return dict(zip(FEATURE_NAMES, [now] + occupancy_h + [arrivals[-1], departure, trajectory_confidence])) | {
                'area_id': area, 'known': bool(belief['known']), 'support': support,
                'uncertainty': belief['uncertainty'], 'observability': belief['observability'],
                'evidence_sources': belief['evidence_sources'],
                'arrival_probability_by_horizon': {'1s': arrivals[0], '3s': arrivals[1], '5s': arrivals[2]},
                'movement_hypotheses': [
                    {'path': list(h['path']), 'mass': float(h['mass']),
                     'age_seconds': max(0.0, ts - float(h['ts']))}
                    for h in sorted(hypotheses, key=lambda x: -float(x['mass']))[:self.MAX_HYPOTHESES]
                ],
                'model_version': self.VERSION,
            }

    def record_calibration_label(self, prediction, observed, source):
        source = str(source or '').strip()
        if not source or source.startswith('model:'):
            raise ValueError('Calibration requires an independent label source')
        p = float(prediction)
        if not math.isfinite(p) or not 0 <= p <= 1:
            raise ValueError('Calibration prediction must be within [0,1]')
        y = 1.0 if bool(observed) else 0.0
        with self.lock:
            idx = min(4, int(p * 5))
            row = self.calibration['bins'][idx]
            row['count'] += 1
            row['sum_prediction'] += p
            row['sum_observed'] += y
            self.calibration['count'] += 1
            self.calibration['sum_brier'] += (p - y) ** 2
            return self.calibration_metrics()

    def calibration_metrics(self):
        with self.lock:
            count = int(self.calibration.get('count') or 0)
            bins = []
            for idx, row in enumerate(self.calibration.get('bins') or []):
                n = int(row.get('count') or 0)
                bins.append({
                    'lo': idx / 5.0, 'hi': (idx + 1) / 5.0, 'count': n,
                    'mean_prediction': (row['sum_prediction'] / n) if n else None,
                    'observed_frequency': (row['sum_observed'] / n) if n else None,
                })
            return {'independent_labels': count,
                    'brier_score': (float(self.calibration.get('sum_brier') or 0.0) / count) if count else None,
                    'bins': bins}

    def export(self):
        with self.lock:
            return {
                'version': self.VERSION, 'model': 'RoomBeliefModel', 'updates': self.updated,
                'last_decay_ts': self.last_decay_ts,
                'graph': [{'context': list(k), 'ts': v['ts'],
                           'outcomes': {d: list(b) for d, b in v['outcomes'].items()}}
                          for k, v in self.graph.items()],
                'dwell': {k: {'ts': v['ts'], 'outcomes': {d: list(b) for d, b in v['outcomes'].items()}}
                          for k, v in self.dwell.items()},
                'calibration': copy.deepcopy(self.calibration),
            }

    def live_delta(self, cutoff):
        with self.lock:
            delta = RoomBeliefModel(self.half_life / 86400)
            delta.values = copy.deepcopy(self.values)
            delta.sources = copy.deepcopy(self.sources)
            delta.area_sources = {k: set(v) for k, v in self.area_sources.items()}
            delta.arrivals = deque(self.arrivals, maxlen=8)
            delta.hypotheses = copy.deepcopy(self.hypotheses)
            delta.last_ts = self.last_ts
            delta._refresh_pending_compat()
            delta.expire(cutoff, learn=False)
            return delta

    def merge_statistics(self, delta):
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
                    ts = max(float(row['ts']), float(source['ts']))
                    left, right = self._factor(ts - float(row['ts'])), self._factor(ts - float(source['ts']))
                    for bins in row['outcomes'].values():
                        for i in range(len(bins)):
                            bins[i] *= left
                    for destination, bins in source['outcomes'].items():
                        destination = str(destination)
                        if destination not in row['outcomes'] and len(row['outcomes']) >= 15:
                            destination = ''
                        merged = row['outcomes'].setdefault(destination, [0.0] * len(bins))
                        for i, value in enumerate(bins):
                            merged[i] += float(value) * right
                    row['ts'] = ts
            for idx in range(5):
                left, right = self.calibration['bins'][idx], delta.calibration['bins'][idx]
                left['count'] += int(right.get('count') or 0)
                left['sum_prediction'] += float(right.get('sum_prediction') or 0.0)
                left['sum_observed'] += float(right.get('sum_observed') or 0.0)
            self.calibration['count'] += int(delta.calibration.get('count') or 0)
            self.calibration['sum_brier'] += float(delta.calibration.get('sum_brier') or 0.0)
            self.updated += delta.updated
            self.last_decay_ts = max(self.last_decay_ts, delta.last_decay_ts)

    def diagnostics(self, ts):
        with self.lock:
            transitions, topology = [], {}
            for key, row in self.graph.items():
                total = sum(sum(b) for b in row['outcomes'].values())
                factor = self._factor(float(ts) - float(row['ts']))
                for dst, bins in row['outcomes'].items():
                    if not dst:
                        continue
                    weight = sum(bins) * factor
                    transitions.append({'path': list(key) + [dst],
                                        'probability': weight / (total * factor + 2.0), 'weight': weight})
                    topology[(key[-1], dst)] = topology.get((key[-1], dst), 0.0) + weight
            active_rooms = []
            for area in sorted(self.values):
                f = self.forecast(area, ts)
                if f['occupancy_now'] >= .5:
                    active_rooms.append({'area_id': area, 'occupancy_now': f['occupancy_now'],
                                         'uncertainty': f['uncertainty']})
            return {
                'model': 'RoomBeliefModel', 'version': self.VERSION, 'migrated_from': self.migrated_from,
                'areas': len(self.values), 'edges': len(transitions), 'updates': self.updated,
                'revision': self.revision, 'half_life_days': self.half_life / 86400,
                'active_rooms': active_rooms, 'anonymous_movement_hypotheses': len(self.hypotheses),
                'top_transitions': sorted(transitions, key=lambda r: r['weight'], reverse=True)[:10],
                'topology_edges': [{'from': origin, 'to': destination, 'weight': weight}
                                   for (origin, destination), weight in sorted(topology.items(), key=lambda item: -item[1])[:20]],
                'calibration': self.calibration_metrics(),
            }


# Compatibility alias: live and replay share one implementation and one time contract.
SharedHomeStateModel = RoomBeliefModel
