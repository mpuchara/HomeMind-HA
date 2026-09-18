"""Adaptive virtual-presence interpretation for weak local sensor signals.

Stage 10 deliberately does not change Home Assistant sensor configuration.  A small
Bayesian model combines an anonymous arrival prior with one independent local raw signal
and exposes a virtual presence belief.  The existing physical occupancy belief remains
separate; callers may use the virtual belief only as predictive/anticipatory context.

Historical calibration is accepted only from an explicit independent label source.  A
sensor output that was affected by HomeMind's own future threshold adapter is never valid
independent evidence.  The hardware adapter below is a contract only and is disabled by
default; it performs no Home Assistant I/O.
"""
from __future__ import annotations

import copy
import math
import uuid
from collections import deque


RAW_ROLES = {'radar_activity', 'auxiliary'}
DIRECT_BINARY_ROLES = {'pir', 'radar_occupancy', 'occupancy_binary', 'tracker'}


def _clamp(value, lo=0.0, hi=1.0):
    return max(float(lo), min(float(hi), float(value)))


def _finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


class AdaptivePresenceModel:
    """Small calibrated Bayesian fusion model with hysteresis and false-ON budget."""

    VERSION = 2
    FIXED_BOUNDARY = 0.60
    ENTER_THRESHOLD = 0.58
    EXIT_THRESHOLD = 0.34
    BASE_RATE = 0.20
    CALIBRATION_BINS = 5
    FALSE_CONFIRM_SECONDS = 8.0
    FALSE_BUDGET_WINDOW = 15 * 60.0
    FALSE_BUDGET_LIMIT = 3

    def __init__(self, raw=None):
        raw = dict(raw or {})
        self.calibration = {}
        for entity_id, row in (raw.get('calibration') or {}).items():
            bins = []
            for item in (row.get('bins') or [])[:self.CALIBRATION_BINS]:
                bins.append({
                    'count': max(0, int(item.get('count') or 0)),
                    'positive': max(0, int(item.get('positive') or 0)),
                })
            while len(bins) < self.CALIBRATION_BINS:
                bins.append({'count': 0, 'positive': 0})
            self.calibration[str(entity_id)] = {'bins': bins}
        self.calibration_watermarks = {
            str(key): float(value)
            for key, value in (raw.get('calibration_watermarks') or {}).items()
            if _finite(value) is not None
        }
        stored = dict(raw.get('metrics') or {})
        self.metrics = {
            'evaluations': max(0, int(stored.get('evaluations') or 0)),
            'adaptive_on_edges': max(0, int(stored.get('adaptive_on_edges') or 0)),
            'confirmed_early': max(0, int(stored.get('confirmed_early') or 0)),
            'lead_seconds_sum': max(0.0, float(stored.get('lead_seconds_sum') or 0.0)),
            'false_on': max(0, int(stored.get('false_on') or 0)),
            'suppressed_false_budget': max(0, int(stored.get('suppressed_false_budget') or 0)),
        }
        # Runtime-only state. Restart must not resurrect a virtual ON or a pending proof.
        self.live = {}
        self.threshold_tainted_until = {}

    @staticmethod
    def _blank_bins():
        return [{'count': 0, 'positive': 0} for _ in range(AdaptivePresenceModel.CALIBRATION_BINS)]

    def reset_live_state(self):
        self.live = {}
        self.threshold_tainted_until = {}

    def mark_threshold_changed(self, entity_id, until_ts):
        """Mark output as non-independent evidence while HomeMind owns threshold effects."""
        self.threshold_tainted_until[str(entity_id)] = float(until_ts)

    def evidence_is_independent(self, entity_id, ts):
        return float(self.threshold_tainted_until.get(str(entity_id), 0.0)) < float(ts)

    def record_independent_label(self, source_id, raw_value, observed, label_source, ts=0.0):
        """Calibrate one raw channel from an explicitly independent label source."""
        source_id = str(source_id or '').strip()
        label_source = str(label_source or '').strip()
        if not source_id or not label_source:
            raise ValueError('Adaptive presence calibration requires source and label_source')
        if label_source == source_id or label_source.startswith('model:'):
            raise ValueError('Adaptive presence calibration requires independent evidence')
        if not self.evidence_is_independent(label_source, ts):
            raise ValueError('Threshold-modified sensor output is not independent evidence')
        value = _finite(raw_value)
        if value is None or not 0.0 <= value <= 1.0:
            raise ValueError('Raw presence signal must be within [0,1]')
        row = self.calibration.setdefault(source_id, {'bins': self._blank_bins()})
        idx = min(self.CALIBRATION_BINS - 1, int(value * self.CALIBRATION_BINS))
        row['bins'][idx]['count'] += 1
        row['bins'][idx]['positive'] += 1 if bool(observed) else 0
        return self.calibration_summary(source_id)

    def calibration_summary(self, source_id):
        bins = (self.calibration.get(str(source_id)) or {}).get('bins') or self._blank_bins()
        count = sum(int(x.get('count') or 0) for x in bins)
        positives = sum(int(x.get('positive') or 0) for x in bins)
        return {
            'independent_labels': count,
            'positive_labels': positives,
            'bins': [
                {
                    'lo': idx / self.CALIBRATION_BINS,
                    'hi': (idx + 1) / self.CALIBRATION_BINS,
                    'count': int(row.get('count') or 0),
                    'observed_frequency': (
                        float(row.get('positive') or 0) / int(row.get('count') or 1)
                        if int(row.get('count') or 0) else None
                    ),
                }
                for idx, row in enumerate(bins)
            ],
        }

    def _signal_probability(self, source_id, raw_value):
        """Translate a non-probability raw score into a conservative calibrated likelihood."""
        value = _clamp(raw_value)
        bins = (self.calibration.get(str(source_id)) or {}).get('bins') or self._blank_bins()
        idx = min(self.CALIBRATION_BINS - 1, int(value * self.CALIBRATION_BINS))
        row = bins[idx]
        count = int(row.get('count') or 0)
        # Beta(1,1) posterior when independent labels exist.  Before that, use a fixed
        # interpretable score curve rather than pretending the percentage is P(occupied).
        learned = (float(row.get('positive') or 0) + 1.0) / (count + 2.0)
        default = _clamp(0.03 + 0.90 * value, 0.03, 0.93)
        confidence = count / (count + 8.0)
        probability = (1.0 - confidence) * default + confidence * learned
        return probability, confidence, count

    @staticmethod
    def _room_calibration_quality(room_calibration):
        room_calibration = dict(room_calibration or {})
        labels = int(room_calibration.get('independent_labels') or 0)
        brier = _finite(room_calibration.get('brier_score'))
        if labels <= 0 or brier is None:
            return 0.50
        reliability = _clamp(1.0 - 2.0 * brier)
        coverage = min(1.0, labels / 30.0)
        return 0.50 + 0.50 * reliability * coverage

    def _select_raw_source(self, raw_sources):
        rows = []
        for raw in raw_sources or []:
            raw = dict(raw or {})
            value = _finite(raw.get('value'))
            quality = _finite(raw.get('quality'))
            role = str(raw.get('role') or '')
            if value is None or quality is None or role not in RAW_ROLES:
                continue
            if not raw.get('available', True):
                continue
            source_id = str(raw.get('entity_id') or '')
            if not source_id:
                continue
            summary = self.calibration_summary(source_id)
            rows.append((
                _clamp(quality), int(summary['independent_labels']), source_id,
                {**raw, 'value': _clamp(value), 'quality': _clamp(quality)},
            ))
        if not rows:
            return None, []
        # One raw channel is authoritative for MVP.  Multiplying sibling radar-energy
        # channels would falsely treat correlated evidence as independent.
        rows.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return rows[0][3], [item[3] for item in rows[1:]]

    def capability(self, area, sources):
        raw, binary = [], []
        for row in sources or []:
            row = dict(row or {})
            if not row.get('available', True):
                continue
            role = str(row.get('role') or '')
            if role in RAW_ROLES:
                raw.append(str(row.get('entity_id')))
            elif role in DIRECT_BINARY_ROLES:
                binary.append(str(row.get('entity_id')))
        raw = sorted(x for x in raw if x and x != 'None')
        binary = sorted(x for x in binary if x and x != 'None')
        return {
            'version': self.VERSION,
            'area_id': area,
            'mode': 'virtual_threshold' if raw else 'anticipation_only',
            'virtual_threshold_available': bool(raw),
            'arrival_anticipation_available': True,
            'local_threshold_distance_available': bool(raw),
            'raw_sources': raw,
            'binary_sources': binary,
            'reason': 'independent_local_raw_signal_available' if raw else 'no_local_raw_signal',
            'binary_only_limitation': None if raw else (
                'Binary presence has no distance-to-threshold information; use arrival anticipation from other observations.'
            ),
        }

    def _runtime(self, area):
        if area not in self.live:
            self.live[area] = {
                'active': False,
                'early_since': None,
                'false_events': deque(),
            }
        return self.live[area]

    def _prune_false_budget(self, state, ts):
        while state['false_events'] and ts - state['false_events'][0] > self.FALSE_BUDGET_WINDOW:
            state['false_events'].popleft()

    def _count_false_on(self, state, ts):
        state['false_events'].append(float(ts))
        self.metrics['false_on'] += 1
        state['early_since'] = None

    def timing_metrics(self):
        confirmed = int(self.metrics['confirmed_early'])
        edges = int(self.metrics['adaptive_on_edges'])
        false_on = int(self.metrics['false_on'])
        return {
            'confirmed_early': confirmed,
            'mean_lead_seconds': self.metrics['lead_seconds_sum'] / confirmed if confirmed else None,
            'false_on_count': false_on,
            'false_on_cost': false_on / max(1, edges),
            'adaptive_on_edges': edges,
            'suppressed_false_budget': int(self.metrics['suppressed_false_budget']),
        }

    def evaluate(self, area, ts, arrival_prior, trajectory_confidence, raw_sources,
                 room_calibration=None, capability=None):
        """Fuse one arrival prior with one independent raw channel, then apply hysteresis."""
        ts = float(ts)
        prior_raw = _clamp(arrival_prior)
        trajectory = _clamp(trajectory_confidence)
        room_quality = self._room_calibration_quality(room_calibration)
        # Arrival prior remains a prior, not proof. Weak topology/calibration shrinks it
        # toward zero rather than toward 0.5, avoiding fabricated occupancy.
        prior = _clamp(prior_raw * (0.55 + 0.45 * trajectory) * (0.75 + 0.25 * room_quality), 0.001, 0.95)
        chosen, alternatives = self._select_raw_source(raw_sources)
        all_sources = [dict(x) for x in (raw_sources or []) if isinstance(x, dict)]
        cap = dict(capability or self.capability(area, all_sources))
        state = self._runtime(area)
        self._prune_false_budget(state, ts)
        self.metrics['evaluations'] += 1

        if chosen is None:
            if state['early_since'] is not None:
                self._count_false_on(state, ts)
            state['active'] = False
            return {
                'version': self.VERSION,
                'mode': 'anticipation_only',
                'posterior': None,
                'virtual_presence_active': False,
                'arrival_prior': prior_raw,
                'effective_arrival_prior': prior,
                'trajectory_confidence': trajectory,
                'selected_raw_source': None,
                'fixed_boundary': self.FIXED_BOUNDARY,
                'fixed_boundary_active': False,
                'enter_threshold': self.ENTER_THRESHOLD,
                'exit_threshold': self.EXIT_THRESHOLD,
                'suppressed_by_false_on_budget': False,
                'capability': cap,
                'timing': self.timing_metrics(),
                'evidence_independence': 'arrival_prior_and_local_raw_are_separate; no local raw available',
            }

        source_id = str(chosen['entity_id'])
        raw_value = _clamp(chosen['value'])
        quality = _clamp(chosen['quality'])
        signal_p, calibration_confidence, calibration_labels = self._signal_probability(source_id, raw_value)
        base_odds = self.BASE_RATE / (1.0 - self.BASE_RATE)
        signal_odds = signal_p / max(1e-9, 1.0 - signal_p)
        likelihood_ratio = max(0.05, min(20.0, signal_odds / base_odds))
        evidence_strength = quality * (0.65 + 0.35 * calibration_confidence)
        prior_odds = prior / max(1e-9, 1.0 - prior)
        posterior_odds = prior_odds * (likelihood_ratio ** evidence_strength)
        posterior = _clamp(posterior_odds / (1.0 + posterior_odds))
        fixed_active = raw_value >= self.FIXED_BOUNDARY

        previous_active = bool(state['active'])
        budget_blocked = len(state['false_events']) >= self.FALSE_BUDGET_LIMIT
        if state['active']:
            if posterior < self.EXIT_THRESHOLD:
                state['active'] = False
        elif posterior >= self.ENTER_THRESHOLD:
            if budget_blocked:
                self.metrics['suppressed_false_budget'] += 1
            else:
                state['active'] = True

        if state['active'] and not previous_active:
            self.metrics['adaptive_on_edges'] += 1
            if not fixed_active:
                state['early_since'] = ts
        if fixed_active and state['early_since'] is not None:
            lead = max(0.0, ts - float(state['early_since']))
            self.metrics['confirmed_early'] += 1
            self.metrics['lead_seconds_sum'] += lead
            state['early_since'] = None
        elif state['early_since'] is not None and ts - float(state['early_since']) > self.FALSE_CONFIRM_SECONDS:
            self._count_false_on(state, ts)
        if previous_active and not state['active'] and state['early_since'] is not None:
            self._count_false_on(state, ts)

        return {
            'version': self.VERSION,
            'mode': 'virtual_threshold',
            'posterior': posterior,
            'virtual_presence_active': bool(state['active']),
            'arrival_prior': prior_raw,
            'effective_arrival_prior': prior,
            'trajectory_confidence': trajectory,
            'room_calibration_quality': room_quality,
            'selected_raw_source': source_id,
            'raw_signal': raw_value,
            'raw_signal_quality': quality,
            'raw_signal_semantics': 'non_probability_score',
            'raw_calibrated_likelihood': signal_p,
            'raw_calibration_confidence': calibration_confidence,
            'raw_independent_labels': calibration_labels,
            'likelihood_ratio': likelihood_ratio,
            'alternative_raw_sources_not_multiplied': [str(x.get('entity_id')) for x in alternatives],
            'fixed_boundary': self.FIXED_BOUNDARY,
            'fixed_boundary_active': bool(fixed_active),
            'enter_threshold': self.ENTER_THRESHOLD,
            'exit_threshold': self.EXIT_THRESHOLD,
            'suppressed_by_false_on_budget': bool(budget_blocked and not state['active'] and posterior >= self.ENTER_THRESHOLD),
            'false_on_budget': {
                'window_seconds': self.FALSE_BUDGET_WINDOW,
                'limit': self.FALSE_BUDGET_LIMIT,
                'recent_false_on': len(state['false_events']),
            },
            'capability': cap,
            'timing': self.timing_metrics(),
            'evidence_independence': (
                'arrival prior comes from anonymous room trajectory; selected raw role cannot create movement hypotheses; '
                'only one local raw channel is fused'
            ),
        }

    def export(self):
        return {
            'version': self.VERSION,
            'calibration': copy.deepcopy(self.calibration),
            'calibration_watermarks': copy.deepcopy(self.calibration_watermarks),
            'metrics': copy.deepcopy(self.metrics),
            # live hysteresis, pending early proof, false-event timestamps and taint leases
            # are intentionally omitted so restart cannot resurrect virtual presence.
        }

    def merge_statistics(self, other):
        if not isinstance(other, AdaptivePresenceModel):
            return
        for entity_id, row in other.calibration.items():
            target = self.calibration.setdefault(entity_id, {'bins': self._blank_bins()})
            for idx in range(self.CALIBRATION_BINS):
                target['bins'][idx]['count'] += int(row['bins'][idx].get('count') or 0)
                target['bins'][idx]['positive'] += int(row['bins'][idx].get('positive') or 0)
        for key in self.metrics:
            self.metrics[key] += other.metrics.get(key, 0)


class HardwareThresholdAdapterContract:
    """Future threshold-writer contract. Disabled by default and never performs I/O."""

    VERSION = 1

    def __init__(self, enabled=False, whitelist=None, constraints=None,
                 max_changes=4, change_window_seconds=3600.0, lease_ttl_seconds=300.0):
        self.enabled = bool(enabled)
        self.whitelist = set(str(x) for x in (whitelist or []))
        self.constraints = {str(k): dict(v) for k, v in (constraints or {}).items()}
        self.max_changes = max(1, int(max_changes))
        self.change_window_seconds = max(1.0, float(change_window_seconds))
        self.lease_ttl_seconds = max(1.0, float(lease_ttl_seconds))
        self.leases = {}
        self.change_history = {}

    def capability(self, sensor_id=None):
        sensor_id = None if sensor_id is None else str(sensor_id)
        constraint = dict(self.constraints.get(sensor_id) or {}) if sensor_id else None
        return {
            'version': self.VERSION,
            'enabled': self.enabled,
            'default_active': False,
            'sensor_id': sensor_id,
            'whitelisted': bool(sensor_id and sensor_id in self.whitelist),
            'range': None if not constraint else [constraint.get('min'), constraint.get('max')],
            'step': None if not constraint else constraint.get('step'),
            'max_changes': self.max_changes,
            'change_window_seconds': self.change_window_seconds,
            'lease_ttl_seconds': self.lease_ttl_seconds,
            'requires_snapshot': True,
            'restore_required': True,
            'physical_io': False,
            'evidence_rule': 'sensor output after an own threshold change is not independent validation evidence',
        }

    def _constraint(self, sensor_id):
        sensor_id = str(sensor_id)
        if sensor_id not in self.whitelist:
            raise ValueError('sensor_not_whitelisted')
        row = self.constraints.get(sensor_id)
        if not row:
            raise ValueError('sensor_constraints_missing')
        lo, hi, step = _finite(row.get('min')), _finite(row.get('max')), _finite(row.get('step'))
        if lo is None or hi is None or step is None or hi <= lo or step <= 0:
            raise ValueError('invalid_sensor_constraints')
        return lo, hi, step

    def acquire_lease(self, sensor_id, snapshot, current_value, now, ttl_seconds=None):
        if not self.enabled:
            raise RuntimeError('hardware_threshold_adapter_disabled')
        sensor_id = str(sensor_id)
        self._constraint(sensor_id)
        if not isinstance(snapshot, dict) or not snapshot:
            raise ValueError('configuration_snapshot_required')
        now = float(now)
        existing = self.leases.get(sensor_id)
        if existing and float(existing['expires_ts']) > now:
            raise RuntimeError('sensor_lease_busy')
        ttl = min(self.lease_ttl_seconds, max(1.0, float(ttl_seconds or self.lease_ttl_seconds)))
        lease = {
            'lease_id': str(uuid.uuid4()),
            'sensor_id': sensor_id,
            'created_ts': now,
            'expires_ts': now + ttl,
            'snapshot': copy.deepcopy(snapshot),
            'original_value': float(current_value),
            'last_value': float(current_value),
            'changes': 0,
        }
        self.leases[sensor_id] = lease
        return copy.deepcopy(lease)

    def plan_change(self, sensor_id, lease_id, value, now):
        if not self.enabled:
            raise RuntimeError('hardware_threshold_adapter_disabled')
        sensor_id = str(sensor_id)
        lo, hi, step = self._constraint(sensor_id)
        lease = self.leases.get(sensor_id)
        now = float(now)
        if not lease or lease.get('lease_id') != str(lease_id):
            raise RuntimeError('sensor_lease_missing')
        if float(lease['expires_ts']) <= now:
            raise RuntimeError('sensor_lease_expired')
        value = float(value)
        if value < lo - 1e-9 or value > hi + 1e-9:
            raise ValueError('threshold_out_of_range')
        steps = round((value - lo) / step)
        quantized = lo + steps * step
        if abs(quantized - value) > max(1e-9, step * 1e-6):
            raise ValueError('threshold_step_violation')
        history = self.change_history.setdefault(sensor_id, deque())
        while history and now - history[0] > self.change_window_seconds:
            history.popleft()
        if len(history) >= self.max_changes:
            raise RuntimeError('threshold_change_rate_limited')
        history.append(now)
        lease['last_value'] = quantized
        lease['changes'] += 1
        return {
            'contract_version': self.VERSION,
            'lease_id': str(lease_id),
            'sensor_id': sensor_id,
            'value': quantized,
            'expires_ts': float(lease['expires_ts']),
            'physical_io': False,
            'requires_external_adapter_commit': True,
        }

    def restore_plan(self, sensor_id, lease_id, now):
        sensor_id = str(sensor_id)
        lease = self.leases.get(sensor_id)
        if not lease or lease.get('lease_id') != str(lease_id):
            raise RuntimeError('sensor_lease_missing')
        return {
            'contract_version': self.VERSION,
            'sensor_id': sensor_id,
            'lease_id': str(lease_id),
            'snapshot': copy.deepcopy(lease['snapshot']),
            'restore_value': float(lease['original_value']),
            'expired': float(lease['expires_ts']) <= float(now),
            'physical_io': False,
            'requires_external_adapter_commit': True,
        }
