"""Auditable source-role classification for RoomBeliefModel.

Admission and interpretation are separate: a raw radar/activity percentage may be useful
local evidence without being a calibrated occupancy probability. Explicit boundary
precursors can raise arrival belief without asserting occupancy. Remote presence/activity
is trajectory context, never local ground truth. No binary source implies person identity.
"""
import re
from context import entity_capability_tags

OCCUPANCY_CLASSES = {'motion', 'occupancy', 'presence'}
DOOR_CLASSES = {'door', 'opening', 'window', 'garage_door'}
NON_PRESENCE_UNITS = {'mm', 'cm', 'm', 'km', 'ft', 'in', 's', 'ms', 'min', 'h',
                      'dbm', 'db', '°', 'deg', 'hz', 'w', 'v', 'a', 'kwh', 'wh'}

LOCAL_EVIDENCE = 'LOCAL_EVIDENCE'
BOUNDARY_ARRIVAL_PRECURSOR = 'BOUNDARY_ARRIVAL_PRECURSOR'
TRAJECTORY_CONTEXT = 'TRAJECTORY_CONTEXT'
RELIABILITY_CONTEXT = 'RELIABILITY_CONTEXT'
OTHER_CONTEXT = 'OTHER_CONTEXT'

RADAR_ACTIVITY_TERMS = (
    'still energy', 'stationary energy', 'move energy', 'moving energy',
    'radar activity', 'target energy',
)
RADAR_DISTANCE_TERMS = (
    'still target distance', 'stationary target distance',
    'moving target distance', 'move target distance',
)
RELIABILITY_TERMS = (
    'humidity', 'wilgot', 'temperature', 'temperatura',
    'dew point', 'punkt rosy', 'moisture',
)
LOCAL_ACTIVITY_TERMS = (
    'presence', 'occupancy', 'motion', 'pir', 'radar', 'mmwave',
    'stationary energy', 'still energy', 'moving energy', 'move energy',
    'target energy', 'target distance', 'stationary target', 'still target',
    'moving target', 'move target', 'camera score', 'aidetection',
    'ai detection', 'detection score',
)


def _text(eid, state):
    attrs = (state or {}).get('attributes') or {}
    return f"{eid} {attrs.get('friendly_name', '')}".lower().replace('_', ' ')


def _normalize_targets(value, mapping=None):
    if value is None:
        return []
    if isinstance(value, str):
        raw = [part.strip() for part in re.split(r'[,;]', value) if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        raw = [str(part).strip() for part in value if str(part).strip()]
    else:
        raw = [str(value).strip()]
    mapping = mapping or {}
    out = []
    for item in raw:
        resolved = mapping.get(item) or item
        if resolved not in out:
            out.append(resolved)
    return out


def boundary_targets(eid, state, registry, mapping=None):
    """Return explicit target areas/entities for an arrival precursor.

    We intentionally do not infer topology from names. A non-local radar/PIR becomes an
    explicit boundary precursor only when HA registry/state metadata says what it borders.
    """
    attrs = (state or {}).get('attributes') or {}
    raw = (
        registry.get('boundary_for')
        or attrs.get('boundary_for')
        or registry.get('arrival_precursor_for')
        or attrs.get('arrival_precursor_for')
    )
    return _normalize_targets(raw, mapping)


def target_relative_role(eid, state, registry, mapping, target_entity, target_area,
                         source_detail=None):
    """Classify one source relative to a controlled target without leaking remote truth."""
    registry = registry or {}
    mapping = mapping or {}
    source_detail = source_detail or {}
    attrs = (state or {}).get('attributes') or {}
    text = _text(eid, state)
    dc = str(attrs.get('device_class') or registry.get('original_device_class') or '').lower()
    source_area = mapping.get(eid)
    source_role = str(source_detail.get('role') or '').lower()

    if dc in {'humidity', 'temperature', 'moisture'} or any(
        term in text for term in RELIABILITY_TERMS
    ):
        return RELIABILITY_CONTEXT

    explicit = set(boundary_targets(eid, state, registry, mapping))
    if str(target_area or '') in explicit or str(target_entity or '') in explicit:
        return BOUNDARY_ARRIVAL_PRECURSOR

    door_like = (
        source_role in {'door', 'opening', 'boundary'}
        or dc in {'door', 'opening'}
        or bool(re.search(r'\b(door|doors|drzwi|gate|garage|window|okno)\b', text))
    )
    if door_like and target_area and source_area == target_area:
        return BOUNDARY_ARRIVAL_PRECURSOR

    caps = entity_capability_tags(eid, state)
    activity_like = (
        source_role in {
            'pir', 'radar_occupancy', 'occupancy_binary', 'radar_activity',
            'radar_distance', 'tracker', 'auxiliary', 'auxiliary_probability',
        }
        or dc in OCCUPANCY_CLASSES
        or bool(caps & {'occupancy', 'activity'})
        or any(term in text for term in LOCAL_ACTIVITY_TERMS)
    )
    if target_area and source_area == target_area and activity_like:
        return LOCAL_EVIDENCE
    if target_area and source_area and source_area != target_area and activity_like:
        return TRAJECTORY_CONTEXT
    return OTHER_CONTEXT


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
        explicit = (
            dc in OCCUPANCY_CLASSES
            or bool(re.search(
                r'\b(pir|ruch|ruchu|human|person|people|occupancy|presence|motion)\b|obecno|człowiek|czlowiek',
                text,
            ))
            or any(term in text for term in (
                'still target', 'stationary target', 'moving target', 'move target'
            ))
        )
        if not explicit or (dc and dc not in OCCUPANCY_CLASSES):
            return None, None
        radar = any(token in text for token in (
            'radar', 'mmwave', 'ld2410', 'ld2411',
            'still target', 'stationary target', 'moving target',
        ))
        pir = dc == 'motion' or bool(re.search(r'\b(pir|motion|ruch|ruchu)\b', text))
        if radar and not pir:
            return 'radar_occupancy', 'binary'
        if pir:
            return 'pir', 'binary'
        return 'occupancy_binary', 'binary'
    if domain != 'sensor':
        return None, None

    unit = str(attrs.get('unit_of_measurement') or '').lower()
    # Target-distance channels are useful local context but are not occupancy
    # probabilities. Admit only explicitly radar-target distances before the generic
    # distance-unit exclusion below.
    if any(token in text for token in RADAR_DISTANCE_TERMS):
        return 'radar_distance', 'context'

    if unit in NON_PRESENCE_UNITS or any(word in text for word in (
        'distance', 'odleg', 'threshold', 'sensitivity', 'timeout', 'latency',
        'uptime', 'signal strength', 'rssi', 'firmware', 'detection range',
        'illuminance', 'temperature',
    )):
        return None, None

    if any(token in text for token in RADAR_ACTIVITY_TERMS):
        return 'radar_activity', 'score'

    if dc in OCCUPANCY_CLASSES or caps & {'occupancy', 'activity'}:
        # Only an explicit probability claim is treated as probability-like. Detection,
        # camera and occupancy *scores* stay raw auxiliary evidence until independently
        # calibrated by AdaptivePresence.
        if any(token in text for token in ('probability', 'prob ')):
            return 'auxiliary_probability', 'score'
        if any(token in text for token in ('radar', 'mmwave', 'activity', 'energy')):
            return 'radar_activity', 'score'
        return 'auxiliary', 'score'

    if any(token in text for token in ('presence probability', 'occupancy probability')):
        return 'auxiliary_probability', 'score'
    if any(token in text for token in (
        'camera ai detection score', 'camera score', 'ai detection',
        'aidetection', 'detection score', 'presence score', 'occupancy score',
    )):
        return 'auxiliary', 'score'
    return None, None


def source_kind(eid, state, registry):
    """Compatibility API: return broad kind plus rejection reason."""
    role, kind = _role(eid, state, registry)
    if role is None:
        return None, 'not_a_presence_sensor'
    if registry.get('disabled_by') is not None:
        return None, 'disabled_in_ha'
    if (
        registry.get('entity_category') in {'config', 'diagnostic'}
        and role not in {'pir', 'radar_occupancy', 'occupancy_binary', 'door'}
    ):
        return None, 'configuration_or_diagnostic'
    return kind, None


def _semantics(role):
    return {
        'pir': 'event_presence',
        'radar_occupancy': 'stationary_occupancy',
        'occupancy_binary': 'binary_occupancy',
        'radar_activity': 'activity_likelihood',
        'radar_distance': 'distance_context',
        'auxiliary_probability': 'probability_like_score',
        'tracker': 'aggregate_tracker',
        'door': 'transition_only',
        'auxiliary': 'auxiliary_likelihood',
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
        elif (
            reg.get('entity_category') in {'config', 'diagnostic'}
            and role not in {'pir', 'radar_occupancy', 'occupancy_binary', 'door'}
        ):
            role, kind, reason = None, None, 'configuration_or_diagnostic'
        elif eid in excluded and role not in {'pir', 'radar_occupancy', 'occupancy_binary', 'door'}:
            role, kind, reason = None, None, 'actuator_or_electrical'

        attrs = (state.get('attributes') or {}) if isinstance(state, dict) else {}
        boundaries = boundary_targets(eid, state, reg, mapping)
        details[eid] = {
            'entity_id': eid,
            'name': attrs.get('friendly_name') or eid,
            'area_id': mapping.get(eid),
            'platform': reg.get('platform'),
            'device_id': reg.get('device_id'),
            'available': str((state or {}).get('state', '')).lower()
                         not in {'unknown', 'unavailable', '', 'none'},
            'kind': kind,
            'role': role,
            'value_semantics': _semantics(role),
            'calibrated_probability': role == 'auxiliary_probability',
            'occupancy_authority': role in {
                'pir', 'radar_occupancy', 'occupancy_binary',
                'tracker', 'auxiliary_probability',
            },
            'boundary_for': boundaries,
            'reason': reason,
            'selected': bool(role),
        }
        if role:
            candidates[eid] = role

    # Some mmWave integrations name the binary output simply "presence" while exposing
    # sibling raw radar channels. Treat that binary as radar occupancy based on the
    # physical device relationship, not a name guess.
    radar_devices = {
        (registry.get(eid, {}).get('device_id'), mapping.get(eid))
        for eid, role in candidates.items()
        if role in {'radar_activity', 'radar_distance'}
        and registry.get(eid, {}).get('device_id')
    }
    for eid, role in list(candidates.items()):
        device_area = (registry.get(eid, {}).get('device_id'), mapping.get(eid))
        if role == 'occupancy_binary' and device_area in radar_devices:
            candidates[eid] = 'radar_occupancy'
            details[eid].update(
                role='radar_occupancy',
                value_semantics=_semantics('radar_occupancy'),
                calibrated_probability=False,
                occupancy_authority=True,
            )

    radar_owners = {
        (registry.get(eid, {}).get('device_id'), mapping.get(eid))
        for eid, role in candidates.items()
        if role == 'radar_occupancy' and registry.get(eid, {}).get('device_id')
    }
    admitted = set()
    for eid, role in candidates.items():
        admitted.add(eid)
        device_area = (registry.get(eid, {}).get('device_id'), mapping.get(eid))
        if role == 'radar_activity' and device_area in radar_owners:
            details[eid]['reason'] = 'activity_support_only'
            details[eid]['occupancy_authority'] = False
        elif role == 'radar_distance':
            details[eid]['reason'] = 'distance_support_only'
            details[eid]['occupancy_authority'] = False
        else:
            details[eid]['reason'] = 'active' if mapping.get(eid) else 'missing_area'
    return admitted, details
