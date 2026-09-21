"""Registry-based room mapping and shared versioned room belief state."""
import json
import math
import threading
import time
from adaptive_presence import AdaptivePresenceModel, HardwareThresholdAdapterContract
from context import (
    controllable_context_exclusions, electrical_context_exclusions,
    is_fast_reactive_agent,
)
from home_sources import select_sources
from home_state import RoomBeliefModel
from semantic_reliability import SemanticReliabilityModel


class ContextEngine:
    ROOM_MODEL_KEY = 'room_belief_model_v2'
    LEGACY_ROOM_MODEL_KEY = 'shared_home_model_v1'
    SEMANTIC_RELIABILITY_KEY = 'semantic_reliability_v1'

    def __init__(self, options, store=None):
        self.options, self.store = options, store
        self.lock = threading.RLock()
        self.entities, self.devices, self.areas = {}, {}, {}
        self.mapping, self.admitted = {}, set()
        self.excluded = set()
        self.source_metadata = {}
        self.mapping_error = None
        self.registry_revision = 0
        self.last_save = 0
        self._last_saved_room_model_raw = None
        self.bootstrap_cutoff = 0
        self.bootstrap_delta = None
        self.bootstrap_started = 0
        self.source_details = {}
        self.boundary_sources_by_area = {}
        self.room_checkpoint_source = None
        reliability_raw = None
        if store:
            try:
                reliability_raw = json.loads(
                    store.meta_get(self.SEMANTIC_RELIABILITY_KEY, 'null')
                )
            except (ValueError, TypeError):
                reliability_raw = None
        self.semantic_reliability = SemanticReliabilityModel(reliability_raw)
        self.adaptive_presence = AdaptivePresenceModel()
        # Future physical threshold adapter contract only. It performs no I/O and remains
        # disabled unless a later, explicit product stage supplies a whitelist/driver.
        self.hardware_threshold_adapter = HardwareThresholdAdapterContract(enabled=False)
        self._adaptive_cache = {}
        raw = None
        if store:
            for key in (self.ROOM_MODEL_KEY, self.LEGACY_ROOM_MODEL_KEY):
                try:
                    value = json.loads(store.meta_get(key, 'null'))
                except (ValueError, TypeError):
                    value = None
                if isinstance(value, dict):
                    raw = value
                    self.room_checkpoint_source = key
                    break
        self.home = RoomBeliefModel(options.get('home_model_half_life_days', 45), raw)
        if isinstance(raw, dict):
            self._last_saved_room_model_raw = json.dumps(raw, separators=(',', ':'), sort_keys=True)

    def configure(self, states, entities=None, devices=None, areas=None):
        with self.lock:
            if entities is not None:
                self.entities = entities
            if devices is not None:
                self.devices = {d['id']: d for d in devices if d.get('id')}
            if areas is not None:
                self.areas = {a['area_id']: a for a in areas if a.get('area_id')}
            raw = self.options.get('entity_area_mapping', '{}')
            try:
                explicit = json.loads(raw) if isinstance(raw, str) else raw
                self.mapping_error = None
            except (TypeError, ValueError) as exc:
                explicit = {}
                self.mapping_error = str(exc)
            if not isinstance(explicit, dict):
                explicit = {}
            self.mapping = {}
            for eid in set(states) | set(self.entities):
                reg = self.entities.get(eid, {})
                area = reg.get('area_id') or self.devices.get(reg.get('device_id'), {}).get('area_id') or explicit.get(eid)
                if isinstance(area, str) and area:
                    self.mapping[eid] = area
            control, _ = controllable_context_exclusions(states, self.entities)
            electrical, _ = electrical_context_exclusions(states, self.entities)
            self.excluded = control | electrical
            self.admitted, self.source_details = select_sources(
                states, self.entities, self.mapping, self.excluded
            )
            boundary_index = {}
            for source_id, detail in self.source_details.items():
                if not detail.get('selected'):
                    continue
                for target_area in detail.get('boundary_for') or ():
                    boundary_index.setdefault(str(target_area), set()).add(str(source_id))
            self.boundary_sources_by_area = boundary_index
            self.source_metadata = {
                eid: {k: v for k, v in (states[eid].get('attributes') or {}).items()
                      if k in ('unit_of_measurement', 'device_class')}
                for eid in self.admitted
            }
            self.registry_revision += 1
            # Mapping/source changes invalidate virtual hysteresis. Do not carry an ON
            # belief across a remap or a source-role reclassification.
            self.adaptive_presence.reset_live_state()
            self._adaptive_cache.clear()

    def resolved_registry(self):
        with self.lock:
            return {eid: {**reg, 'area_id': self.mapping.get(eid)} for eid, reg in self.entities.items()}

    def area_for(self, eid):
        return self.mapping.get(eid)

    def relevant_entities(self):
        return sorted(self.admitted & self.mapping.keys())

    def boundary_sources_for(self, target_entity):
        area = self.area_for(target_entity)
        if not area:
            return ()
        return tuple(sorted(self.boundary_sources_by_area.get(str(area), ())))

    def evidence_metadata(self, eid):
        return dict(self.source_details.get(eid) or {})

    def prepare_home_reliability(self, home, area, ts):
        """Refresh runtime-only semantic trust factors from Correct calibration.

        This touches only the already-materialized RoomBelief source map. There is no
        database work or whole-HA scan in event->intent inference.
        """
        if not area or home is None:
            return
        sources = getattr(home, 'sources', {})
        for eid in tuple(getattr(home, 'area_sources', {}).get(area, ())):
            source = sources.get(eid)
            if not isinstance(source, dict):
                continue
            detail = self.semantic_reliability.evaluate(
                area, eid, sources, ts,
                freshness_fn=getattr(home, '_freshness', None),
            )
            source['semantic_reliability'] = float(detail.get('factor', 1.0))
            source['semantic_reliability_detail'] = detail

    def record_correct_reliability_feedback(
        self, agent, *, sample_ts, desired, snapshot, supervision_id
    ):
        """Learn semantic source trust from explicit binary Correct only."""
        if not is_fast_reactive_agent(agent):
            return {
                'recorded': False,
                'reason': 'reliability_calibration_is_fast_binary_correct_only',
            }
        area = self.area_for(agent.get('target_entity'))
        result = self.semantic_reliability.record_feedback(
            area, desired, snapshot, supervision_id, sample_ts
        )
        if result.get('recorded'):
            self._adaptive_cache.clear()
            if self.store:
                self.store.meta_set(
                    self.SEMANTIC_RELIABILITY_KEY,
                    json.dumps(
                        self.semantic_reliability.export(),
                        separators=(',', ':'), sort_keys=True,
                    ),
                )
            self.prepare_home_reliability(self.home, area, time.time())
        return result

    def _discard_orphan_movement_state(self):
        # Engine's initial REST snapshot intentionally clears `arrivals` + legacy `pending`
        # so startup states are not interpreted as fresh movement. RoomBelief keeps a richer
        # hypothesis set, therefore mirror that established signal without patching Engine.
        if getattr(self.home, 'hypotheses', None) and self.home.pending is None and not self.home.arrivals:
            self.home.reset_movement_state()

    @staticmethod
    def probability(eid, state):
        """Normalize transport values; the explicit source role decides semantics."""
        value = str((state or {}).get('state', '')).lower()
        if value in ('unknown', 'unavailable', '', 'none'):
            return None
        if value in ('on', 'home', 'occupied', 'detected', 'true', 'open'):
            return 1.0
        if value in ('off', 'not_home', 'away', 'clear', 'false', 'closed'):
            return 0.0
        try:
            value = float(value)
            if not math.isfinite(value):
                return None
            unit = str(((state or {}).get('attributes') or {}).get('unit_of_measurement', ''))
            return max(0.0, min(1.0, value / (100 if unit == '%' or value > 1 else 1)))
        except ValueError:
            return None

    def observe(self, eid, state, ts, learn=True):
        with self.lock:
            self._discard_orphan_movement_state()
            if eid not in self.admitted:
                previous_area = self.home.source_area(eid)
                if previous_area:
                    previous_role = self.home.source_role(eid)
                    evidence = {'role': previous_role} if previous_role else None
                    if self.bootstrap_delta is not None:
                        self.bootstrap_delta.observe(eid, previous_area, None, ts, learn=False, evidence=evidence)
                    changed = self.home.observe(
                        eid, previous_area, None, ts, learn=False, evidence=evidence
                    )
                    self.prepare_home_reliability(self.home, previous_area, ts)
                    self._adaptive_cache.clear()
                    return changed
                return False
            value = self.sensor_probability(eid, state)
            evidence = self.evidence_metadata(eid)
            if self.bootstrap_delta is not None and ts > self.bootstrap_started:
                self.bootstrap_delta.observe(eid, self.area_for(eid), value, ts,
                                             learn=learn, evidence=evidence)
            area = self.area_for(eid)
            changed = self.home.observe(
                eid, area, value, ts,
                learn=learn and ts > self.bootstrap_cutoff,
                evidence=evidence,
            )
            self.prepare_home_reliability(self.home, area, ts)
            self._adaptive_cache.clear()
            return changed

    def sensor_probability(self, eid, state):
        st = dict(state or {})
        st['attributes'] = {**self.source_metadata.get(eid, {}), **(st.get('attributes') or {})}
        return self.probability(eid, st)

    def _adaptive_sources(self, home, area, ts):
        self.prepare_home_reliability(home, area, ts)
        rows = []
        for eid in sorted(getattr(home, 'area_sources', {}).get(area, ())):
            source = dict(getattr(home, 'sources', {}).get(eid) or {})
            if not source:
                continue
            try:
                freshness = float(home._freshness(source, ts))
            except Exception:
                freshness = 0.0
            communication = max(
                0.0, min(1.0, float(source.get('communication_reliability') or 0.0))
            )
            semantic = max(
                0.0, min(1.0, float(source.get('semantic_reliability', 1.0) or 0.0))
            )
            rows.append({
                'entity_id': eid,
                'role': str(source.get('role') or ''),
                'value': source.get('value'),
                'available': bool(source.get('available')),
                'quality': communication * semantic * freshness,
                'communication_reliability': communication,
                'semantic_reliability': semantic,
                'semantic_reliability_detail': source.get('semantic_reliability_detail'),
                'freshness': freshness,
            })
        return rows

    def augment_home_forecast(self, home, area, base_forecast, ts, presence_model=None, cache=None):
        """Add Stage-10 virtual presence without changing physical occupancy_now semantics."""
        result = dict(base_forecast or {})
        if not area:
            result['adaptive_presence'] = {
                'version': AdaptivePresenceModel.VERSION,
                'mode': 'anticipation_only',
                'posterior': None,
                'virtual_presence_active': False,
                'capability': {'mode': 'anticipation_only', 'reason': 'target_area_unmapped'},
            }
            return result
        model = presence_model or self.adaptive_presence
        ts = float(ts)
        key = (
            str(area),
            int(getattr(home, 'revision', 0)),
            int(getattr(self.semantic_reliability, 'revision', 0)),
            round(ts, 3),
        )
        if cache is not None and key in cache:
            adaptive = dict(cache[key])
        else:
            sources = self._adaptive_sources(home, area, ts)
            capability = model.capability(area, sources)
            arrivals = dict(result.get('arrival_probability_by_horizon') or {})
            arrival_prior = float(arrivals.get('3s', result.get('arrival_probability', 0.0)) or 0.0)
            adaptive = model.evaluate(
                area=area,
                ts=ts,
                arrival_prior=arrival_prior,
                trajectory_confidence=float(
                    result.get('arrival_evidence_confidence',
                               result.get('trajectory_confidence', 0.0)) or 0.0
                ),
                raw_sources=sources,
                room_calibration=(home.calibration_metrics() if hasattr(home, 'calibration_metrics') else None),
                capability=capability,
            )
            if cache is not None:
                cache.clear()
                cache[key] = dict(adaptive)
        posterior = adaptive.get('posterior')
        if adaptive.get('virtual_presence_active') and posterior is not None:
            # Existing slots already mean predicted occupancy. Raising only the future
            # horizons avoids reinterpreting persisted occupancy_now weights.
            for name in ('occupancy_in_1s', 'occupancy_in_3s', 'occupancy_in_5s'):
                result[name] = max(float(result.get(name) or 0.0), float(posterior))
        capability = dict(adaptive.get('capability') or {})
        capability['hardware_threshold_adapter'] = self.hardware_threshold_adapter.capability()
        adaptive['capability'] = capability
        result['adaptive_presence'] = adaptive
        result['virtual_presence_probability'] = posterior
        result['virtual_presence_active'] = bool(adaptive.get('virtual_presence_active'))
        result['presence_capability'] = capability
        return result

    def forecast(self, eid, ts):
        with self.lock:
            self._discard_orphan_movement_state()
            area = self.area_for(eid)
            self.prepare_home_reliability(self.home, area, ts)
            base = self.home.forecast(area, ts)
            return self.augment_home_forecast(
                self.home, area, base, ts,
                presence_model=self.adaptive_presence,
                cache=self._adaptive_cache,
            )

    def save(self, force=False):
        with self.lock:
            now = time.time()
            if not self.store or (not force and now - self.last_save < 60):
                return False
            raw = json.dumps(self.home.export(), separators=(',', ':'), sort_keys=True)
            changed = raw != self._last_saved_room_model_raw
            if changed:
                self.store.meta_set(self.ROOM_MODEL_KEY, raw)
                self._last_saved_room_model_raw = raw
                self.room_checkpoint_source = self.ROOM_MODEL_KEY
            self.last_save = now
            return changed

    def diagnostics(self):
        with self.lock:
            now = time.time()
            result = self.home.diagnostics(now)
            capabilities = []
            areas = sorted(set(self.mapping.values()))
            for area in areas[:128]:
                sources = self._adaptive_sources(self.home, area, now)
                capabilities.append(self.adaptive_presence.capability(area, sources))
            result.update({
                'mapped_entities': len(self.mapping),
                'occupancy_sources': len(self.admitted),
                'mapped_sources': len(self.admitted & self.mapping.keys()),
                'unmapped_sources': len(self.admitted - self.mapping.keys()),
                'source_details': list(self.source_details.values())[:500],
                'source_details_total': len(self.source_details),
                'explicit_boundary_sources': sum(
                    len(ids) for ids in self.boundary_sources_by_area.values()
                ),
                'explicit_boundary_target_areas': len(self.boundary_sources_by_area),
                'semantic_reliability': self.semantic_reliability.diagnostics(),
                'bootstrap_live_updates': self.bootstrap_delta.updated if self.bootstrap_delta else 0,
                'mapping_error': self.mapping_error,
                'area_names': {k: v.get('name', k) for k, v in self.areas.items()},
                'checkpoint_key': self.room_checkpoint_source,
                'checkpoint_contract': 'room_belief_v2_additive_legacy_v1_read_only',
                'adaptive_presence': {
                    'version': AdaptivePresenceModel.VERSION,
                    'contract': 'virtual_threshold_no_sensor_configuration_change',
                    'timing': self.adaptive_presence.timing_metrics(),
                    'capabilities': capabilities,
                    'hardware_threshold_adapter': self.hardware_threshold_adapter.capability(),
                },
            })
            return result