"""Presence admission for the shared room model, independent of policy inputs."""
import re
from context import entity_capability_tags

OCCUPANCY_CLASSES = {'motion', 'occupancy', 'presence'}
NON_PRESENCE_UNITS = {'mm', 'cm', 'm', 'km', 'ft', 'in', 's', 'ms', 'min', 'h',
                      'dbm', 'db', '°', 'deg', 'hz', 'w', 'v', 'a', 'kwh', 'wh'}


def source_kind(eid, state, registry):
    """Return a sensing kind and an auditable rejection reason."""
    domain = eid.split('.', 1)[0]
    if domain not in {'sensor', 'binary_sensor', 'person', 'device_tracker'}:
        return None, 'not_a_presence_sensor'
    attrs = (state or {}).get('attributes') or {}
    dc = str(attrs.get('device_class') or registry.get('original_device_class') or '').lower()
    text = f"{eid} {attrs.get('friendly_name', '')}".lower().replace('_', ' ')
    caps = entity_capability_tags(eid, state)
    explicit_binary = domain == 'binary_sensor' and (
        dc in OCCUPANCY_CLASSES or bool(re.search(r'\b(pir|ruch|ruchu|human|person|people|occupancy|presence|motion)\b|obecno|człowiek|czlowiek', text))
        or any(term in text for term in ('still target', 'moving target', 'move target')))
    if not (explicit_binary or dc in OCCUPANCY_CLASSES or caps & {'occupancy', 'activity'}):
        return None, 'not_a_presence_sensor'
    if registry.get('disabled_by') is not None:
        return None, 'disabled_in_ha'
    if registry.get('entity_category') in {'config', 'diagnostic'} and not explicit_binary:
        return None, 'configuration_or_diagnostic'
    if domain == 'binary_sensor':
        # Battery/connectivity/problem channels can inherit a device's "presence" name.
        if dc and dc not in OCCUPANCY_CLASSES:
            return None, 'non_presence_device_class'
        return 'binary', None
    if domain in {'person', 'device_tracker'}:
        return 'tracker', None
    unit = str(attrs.get('unit_of_measurement') or '').lower()
    if unit in NON_PRESENCE_UNITS or any(word in text for word in
        ('distance', 'odleg', 'threshold', 'sensitivity', 'timeout', 'latency', 'uptime',
         'signal strength', 'rssi', 'firmware', 'detection range', 'illuminance', 'temperature')):
        return None, 'measurement_is_not_occupancy'
    if dc and dc not in OCCUPANCY_CLASSES and dc not in {'enum'}:
        return None, 'non_presence_device_class'
    return 'score', None


def select_sources(states, registry, mapping, excluded):
    candidates, details = {}, {}
    for eid, state in states.items():
        reg = registry.get(eid, {})
        kind, reason = source_kind(eid, state, reg)
        if reason == 'not_a_presence_sensor':
            continue
        if eid in excluded and kind != 'binary':
            kind, reason = None, 'actuator_or_electrical'
        details[eid] = {'entity_id': eid, 'name': (state.get('attributes') or {}).get('friendly_name') or eid,
                        'area_id': mapping.get(eid), 'platform': reg.get('platform'),
                        'available': str(state.get('state','')).lower() not in {'unknown','unavailable','','none'},
                        'kind': kind, 'reason': reason, 'selected': bool(kind)}
        if kind:
            candidates[eid] = kind
    # A radar's calibrated binary presence output owns occupancy. Raw energy remains
    # available to the agent policy, but cannot pin a room ON despite that output.
    primary = {(registry[eid]['device_id'], mapping.get(eid))
               for eid, kind in candidates.items() if kind == 'binary'
               and registry.get(eid, {}).get('device_id')}
    admitted = set()
    for eid, kind in candidates.items():
        device = registry.get(eid, {}).get('device_id')
        if kind == 'score' and device and (device, mapping.get(eid)) in primary:
            details[eid].update(selected=False, reason='binary_presence_on_same_device')
        else:
            admitted.add(eid)
            details[eid]['reason'] = 'active' if mapping.get(eid) else 'missing_area'
    return admitted, details
