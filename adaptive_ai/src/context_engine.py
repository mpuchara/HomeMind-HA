"""Registry-based room mapping and a single shared live home state."""
import json
import math
import threading
import time
from context import controllable_context_exclusions, electrical_context_exclusions
from home_sources import select_sources
from home_state import SharedHomeStateModel


class ContextEngine:
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
        self.bootstrap_cutoff = 0
        self.bootstrap_delta = None
        self.bootstrap_started = 0
        self.source_details = {}
        raw = None
        if store:
            try:
                raw = json.loads(store.meta_get('shared_home_model_v1', 'null'))
            except (ValueError, TypeError):
                pass
        self.home = SharedHomeStateModel(options.get('home_model_half_life_days', 45), raw)

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
            self.admitted, self.source_details = select_sources(states, self.entities, self.mapping, self.excluded)
            self.source_metadata = {eid: {k:v for k,v in (states[eid].get('attributes') or {}).items()
                                          if k in ('unit_of_measurement','device_class')}
                                    for eid in self.admitted}
            self.registry_revision += 1

    def resolved_registry(self):
        with self.lock:
            return {eid: {**reg, 'area_id': self.mapping.get(eid)} for eid, reg in self.entities.items()}

    def area_for(self, eid):
        return self.mapping.get(eid)

    def relevant_entities(self):
        return sorted(self.admitted & self.mapping.keys())

    @staticmethod
    def probability(eid, state):
        value = str((state or {}).get('state', '')).lower()
        if value in ('unknown', 'unavailable', '', 'none'):
            return None
        if value in ('on', 'home', 'occupied', 'detected', 'true'):
            return 1.0
        if value in ('off', 'not_home', 'away', 'clear', 'false'):
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
            if eid not in self.admitted:
                previous = self.home.sources.get(eid)
                if previous:
                    if self.bootstrap_delta is not None:
                        self.bootstrap_delta.observe(eid, previous[0], None, ts, learn=False)
                    return self.home.observe(eid, previous[0], None, ts, learn=False)
                return False
            probability = self.sensor_probability(eid, state)
            if self.bootstrap_delta is not None and ts > self.bootstrap_started:
                self.bootstrap_delta.observe(eid, self.area_for(eid), probability, ts, learn=learn)
            return self.home.observe(eid, self.area_for(eid), probability, ts,
                                     learn=learn and ts > self.bootstrap_cutoff)

    def sensor_probability(self, eid, state):
        st = dict(state or {})
        st['attributes'] = {**self.source_metadata.get(eid, {}), **(st.get('attributes') or {})}
        return self.probability(eid, st)

    def forecast(self, eid, ts):
        return self.home.forecast(self.area_for(eid), ts)

    def save(self, force=False):
        with self.lock:
            now = time.time()
            if self.store and (force or now-self.last_save >= 60):
                self.store.meta_set('shared_home_model_v1', json.dumps(self.home.export(), separators=(',', ':')))
                self.last_save = now

    def diagnostics(self):
        with self.lock:
            result = self.home.diagnostics(time.time())
            result.update({'mapped_entities': len(self.mapping), 'occupancy_sources': len(self.admitted),
                           'mapped_sources': len(self.admitted & self.mapping.keys()),
                           'unmapped_sources': len(self.admitted - self.mapping.keys()),
                           'source_details': list(self.source_details.values())[:500],
                           'source_details_total': len(self.source_details),
                           'bootstrap_live_updates': self.bootstrap_delta.updated if self.bootstrap_delta else 0,
                           'mapping_error': self.mapping_error,
                           'area_names': {k:v.get('name', k) for k,v in self.areas.items()}})
            return result
