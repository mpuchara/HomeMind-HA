"""Auditable source-role classification for RoomBeliefModel.

Admission and interpretation are separate: a raw radar activity percentage may be useful
movement evidence without being a calibrated occupancy probability. Door events can inform
movement topology without asserting occupancy. No binary source implies person identity.
"""
import re
from context import entity_capability_tags

OCCUPANCY_CLASSES = {'motion', 'occupancy', 'presence'}
DOOR_CLASSES = {'door', 'opening', 'window', 'garage_door'}
NON_PRESENCE_UNITS = {'mm', 'cm', 'm', 'km', 'ft', 'in', 's', 'ms', 'min', 'h',
                      'dbm', 'db', '°', 'deg', 'hz', 'w', 'v', 'a', 'kwh', 'wh'}


def _text(eid, state):
    attrs = (state or {}).get('attributes') or {}
    return f"{eid} {attrs.get('friendly_name', '')}".lower().replace('_', ' ')


def _role(eid, state, registry):
    domain = eid.split('.', 1)[0]
    attrs = (state or {}).get('attributes') or {}
    dc = str(attrs.get('device_class') or registry.get('original_device_class') or '').lower()
    text = _text(eid, state)
    caps = entity_capability_tags(eid, state)
    if domain in {'person', 'device_tracker'}:
        return 'tracker', 'tracker'
    if domain == 'binary_sensor' and (
        dc in DOOR_CLASSES or re.search(r'\b(door|doors|drzwi|gate|garage|window|okno)\b', text)
    ):
        return 'door', 'door'
    if domain == 'binary_sensor':
        explicit = (dc in OCCUPANCY_CLASSES
                    or bool(re.search(r'\b(pir|ruch|ruchu|human|person|people|occupancy|presence|motion)\b|obecno|człowiek|czlowiek', text))
                    or any(term in text for term in ('still target', 'moving target', 'move target')))
        if not explicit or (dc and dc not in OCCUPANCY_CLASSES):
            return None, None
        radar = any(token in text for token in ('radar', 'mmwave', 'ld2410', 'ld2411', 'still target', 'moving target'))
        pir = dc == 'motion' or bool(re.search(r'\b(pir|motion|ruch|ruchu)\b', text))
        if radar and not pir:
            return 'radar_occupancy', 'binary'
        if pir:
            return 'pir', 'binary'
        return 'occupancy_binary', 'binary'
    if domain != 'sensor':
        return None, None
    unit = str(attrs.get('unit_of_measurement') or '').lower()
    if unit in NON_PRESENCE_UNITS or any(word in text for word in
        ('distance', 'odleg', 'threshold', 'sensitivity', 'timeout', 'latency', 'uptime',
         'signal strength', 'rssi', 'firmware', 'detection range', 'illuminance', 'temperature')):
        return None, None
    if any(token in text for token in ('still energy', 'move energy', 'moving energy', 'radar activity', 'target energy')):
        return 'radar_activity', 'score'
    if dc in OCCUPANCY_CLASSES or caps & {'occupancy', 'activity'}:
        if any(token in text for token in ('probability', 'prob ', 'detection score', 'presence score', 'occupancy score')):
            return 'auxiliary_probability', 'score'
        if any(token in text for token in ('radar', 'mmwave', 'activity', 'energy')):
            return 'radar_activity', 'score'
        return 'auxiliary', 'score'
    if any(token in text for token in ('camera ai detection score', 'presence probability', 'occupancy probability')):
        return 'auxiliary_probability', 'score'
    return None, None


def source_kind(eid, state, registry):
    """Compatibility API: return broad kind plus rejection reason."""
    role, kind = _role(eid, state, registry)
    if role is None:
        return None, 'not_a_presence_sensor'
    if registry.get('disabled_by') is not None:
        return None, 'disabled_in_ha'
    if registry.get('entity_category') in {'config', 'diagnostic'} and role not in {'pir', 'radar_occupancy', 'occupancy_binary', 'door'}:
        return None, 'configuration_or_diagnostic'
    return kind, None


def _semantics(role):
    return {
        'pir': 'event_presence', 'radar_occupancy': 'stationary_occupancy',
        'occupancy_binary': 'binary_occupancy', 'radar_activity': 'activity_likelihood',
        'auxiliary_probability': 'probability_like_score', 'tracker': 'aggregate_tracker',
        'door': 'transition_only', 'auxiliary': 'auxiliary_likelihood',
    }.get(role)


def select_sources(states, registry, mapping, excluded):
    candidates, details = {}, {}
    for eid, state in states.items():
        reg = registry.get(eid, {})
        role, kind = _role(eid, state, reg)
        reason = None
        if role is None:
            continue
        if reg.get('disabled_by') is not None:
            role, kind, reason = None, None, 'disabled_in_ha'
        elif reg.get('entity_category') in {'config', 'diagnostic'} and role not in {'pir', 'radar_occupancy', 'occupancy_binary', 'door'}:
            role, kind, reason = None, None, 'configuration_or_diagnostic'
        elif eid in excluded and role not in {'pir', 'radar_occupancy', 'occupancy_binary', 'door'}:
            role, kind, reason = None, None, 'actuator_or_electrical'
        attrs = (state.get('attributes') or {}) if isinstance(state, dict) else {}
        details[eid] = {
            'entity_id': eid, 'name': attrs.get('friendly_name') or eid,
            'area_id': mapping.get(eid), 'platform': reg.get('platform'),
            'device_id': reg.get('device_id'),
            'available': str((state or {}).get('state', '')).lower() not in {'unknown', 'unavailable', '', 'none'},
            'kind': kind, 'role': role, 'value_semantics': _semantics(role),
            'calibrated_probability': role == 'auxiliary_probability',
            'occupancy_authority': role in {'pir', 'radar_occupancy', 'occupancy_binary', 'tracker', 'auxiliary_probability'},
            'reason': reason, 'selected': bool(role),
        }
        if role:
            candidates[eid] = role

    # Some mmWave integrations name the binary output simply "presence" while exposing
    # sibling still/move-energy channels. Treat that binary as radar occupancy based on
    # the device relationship, not a name guess.
    radar_devices = {(registry.get(eid, {}).get('device_id'), mapping.get(eid))
                     for eid, role in candidates.items() if role == 'radar_activity'
                     and registry.get(eid, {}).get('device_id')}
    for eid, role in list(candidates.items()):
        device_area = (registry.get(eid, {}).get('device_id'), mapping.get(eid))
        if role == 'occupancy_binary' and device_area in radar_devices:
            candidates[eid] = 'radar_occupancy'
            details[eid].update(role='radar_occupancy', value_semantics=_semantics('radar_occupancy'),
                                calibrated_probability=False, occupancy_authority=True)

    radar_owners = {(registry.get(eid, {}).get('device_id'), mapping.get(eid))
                    for eid, role in candidates.items() if role == 'radar_occupancy'
                    and registry.get(eid, {}).get('device_id')}
    admitted = set()
    for eid, role in candidates.items():
        admitted.add(eid)
        device_area = (registry.get(eid, {}).get('device_id'), mapping.get(eid))
        if role == 'radar_activity' and device_area in radar_owners:
            details[eid]['reason'] = 'activity_support_only'
            details[eid]['occupancy_authority'] = False
        else:
            details[eid]['reason'] = 'active' if mapping.get(eid) else 'missing_area'
    return admitted, details
