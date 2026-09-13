"""Shared device contract. No HA imports; all times are seconds.

Command acknowledgement is not a measurement of physical process settling.
Learning is a contextual bandit with delayed preference feedback, not a plant model.
"""
import hashlib
import json
import math
from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Timing:
    acknowledgement: float
    settling: float
    manual_hold: float


def timing_for(agent):
    domain = agent['target_entity'].split('.')[0]
    defaults = {
        'climate': Timing(60, 900, 3600),
        'water_heater': Timing(60, 1800, 7200),
        'humidifier': Timing(30, 300, 1800),
        'cover': Timing(120, 120, 900),
        'fan': Timing(20, 30, 900),
    }.get(domain, Timing(10, 1, 300))
    return Timing(*[float(agent.get(key) or default) for key, default in zip(
        ('ack_timeout', 'settling_seconds', 'manual_hold_seconds'), asdict(defaults).values())])


REVIEW_REQUIRED_DOMAINS = {'number', 'input_number', 'select', 'input_select'}
MAX_COMMAND_DELTA = {
    ('light', 'brightness_pct'): 50.0,
    ('fan', 'percentage'): 40.0,
    ('cover', 'position'): 35.0,
    ('media_player', 'volume_pct'): 25.0,
    ('climate', 'temperature'): 2.0,
    ('water_heater', 'temperature'): 5.0,
    ('humidifier', 'humidity'): 10.0,
}


def capability_fingerprint(agent, state):
    attrs = (state or {}).get('attributes') or {}
    domain = str(agent.get('target_entity') or '').split('.', 1)[0]
    raw = {
        'domain': domain,
        'property': agent.get('target_property'),
        'entity': agent.get('target_entity'),
        'min': float(agent.get('min_value') or 0),
        'max': float(agent.get('max_value') or 0),
    }
    if domain in ('select', 'input_select'):
        raw['options'] = list(attrs.get('options') or [])
    elif domain in ('number', 'input_number'):
        raw.update(device_min=attrs.get('min'), device_max=attrs.get('max'), step=attrs.get('step'), unit=attrs.get('unit_of_measurement'))
    packed = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(packed.encode('utf-8')).hexdigest()[:24]


def review_status(store, agent, state):
    domain = str(agent.get('target_entity') or '').split('.', 1)[0]
    required = domain in REVIEW_REQUIRED_DOMAINS
    approved = store.meta_get('control_reviewed:' + agent['id'], '0') == '1'
    current = capability_fingerprint(agent, state) if state else None
    saved = store.meta_get('control_review_fingerprint:' + agent['id'], '') or None
    match = (not required) or bool(approved and current and current == saved)
    return {
        'profile': domain,
        'approval_required': required,
        'approved': approved,
        'fingerprint_match': match,
        'ready': (not required) or bool(approved and match),
    }


def set_review_approval(store, agent, state, approved):
    approved = bool(approved)
    store.meta_set('control_reviewed:' + agent['id'], '1' if approved else '0')
    store.meta_set('control_review_fingerprint:' + agent['id'], capability_fingerprint(agent, state) if approved else '')
    return review_status(store, agent, state)


def _current_value_for_guard(agent, state):
    if not state:
        return None
    attrs = state.get('attributes') or {}
    domain = str(agent.get('target_entity') or '').split('.', 1)[0]
    prop = str(agent.get('target_property') or '')
    try:
        if prop == 'power':
            return 0.0 if str(state.get('state')).lower() in ('off', 'idle', 'unavailable', 'unknown') else 1.0
        if domain == 'light' and prop == 'brightness_pct':
            if str(state.get('state')).lower() == 'off': return 0.0
            return None if attrs.get('brightness') is None else float(attrs['brightness']) * 100.0 / 255.0
        if prop == 'temperature': return None if attrs.get('temperature') is None else float(attrs['temperature'])
        if prop == 'position': return None if attrs.get('current_position') is None else float(attrs['current_position'])
        if prop == 'percentage': return None if attrs.get('percentage') is None else float(attrs['percentage'])
        if prop == 'volume_pct': return None if attrs.get('volume_level') is None else float(attrs['volume_level']) * 100.0
        if prop == 'humidity': return None if attrs.get('humidity') is None else float(attrs['humidity'])
        if domain in ('number', 'input_number') and prop == 'value': return float(state.get('state'))
        if domain in ('select', 'input_select') and prop == 'option_index':
            options = list(attrs.get('options') or [])
            return float(options.index(str(state.get('state'))))
    except (TypeError, ValueError):
        return None
    return None


def legal_value(agent, state, desired):
    """Intersect user/device limits, quantize, then apply deterministic command bounds."""
    value = float(desired)
    if not math.isfinite(value):
        raise ValueError('Non-finite action')
    attrs = state.get('attributes') or {}
    domain = agent['target_entity'].split('.')[0]
    prop = agent['target_property']
    lo, hi = float(agent['min_value']), float(agent['max_value'])
    if prop in ('brightness_pct', 'position', 'percentage', 'volume_pct', 'humidity'):
        lo, hi = max(lo, 0), min(hi, 100)
    if prop == 'power':
        lo, hi = max(lo, 0), min(hi, 1)
    low_key, high_key = ('min_temp', 'max_temp') if prop == 'temperature' else ('min', 'max')
    if prop == 'humidity':
        low_key, high_key = 'min_humidity', 'max_humidity'
    hardware_lo = float(attrs.get(low_key, lo))
    hardware_hi = float(attrs.get(high_key, hi))
    lo, hi = max(lo, hardware_lo), min(hi, hardware_hi)
    if prop == 'option_index':
        lo, hi = max(lo, 0), min(hi, len(attrs.get('options') or []) - 1)
    if not all(math.isfinite(x) for x in (lo, hi)) or lo > hi:
        raise ValueError('User limits do not intersect device limits')
    step = attrs.get('target_temp_step', 0) if prop == 'temperature' else 0
    if domain in ('number', 'input_number'):
        step = attrs.get('step', 0)
    if prop in ('brightness_pct', 'position', 'volume_pct', 'humidity', 'option_index', 'power'):
        step = 1
    if prop == 'percentage':
        step = attrs.get('percentage_step') or 1
    value = min(hi, max(lo, value))
    step = float(step or 0)
    if step > 0 and math.isfinite(step):
        origin = hardware_lo if domain in ('number', 'input_number') or prop == 'temperature' else 0
        first = math.ceil((lo - origin) / step - 1e-9)
        last = math.floor((hi - origin) / step + 1e-9)
        if first > last:
            raise ValueError('No legal device step inside user limits')
        value = origin + min(last, max(first, round((value-origin)/step))) * step
    value = round(value, 8)

    if domain in REVIEW_REQUIRED_DOMAINS:
        from storage import STORE
        status = review_status(STORE, agent, state)
        if not status['approved']:
            raise ValueError('Explicit review is required for this generic target before Control')
        if not status['fingerprint_match']:
            raise ValueError('Device options or numeric limits changed since Control review')

    limit = MAX_COMMAND_DELTA.get((domain, prop))
    if domain in ('number', 'input_number') and prop == 'value':
        span = max(0.0, float(agent['max_value']) - float(agent['min_value']))
        limit = max(float(attrs.get('step') or 0) * 2.0, span * 0.10, 0.01)
    current = _current_value_for_guard(agent, state)
    if current is not None and limit is not None:
        delta = abs(float(value) - float(current))
        if delta > float(limit) + 1e-9:
            raise ValueError(f'Requested step {delta:.3g} exceeds command guard {float(limit):.3g}')
    return value


def same_value(a, b, deadband):
    return a is not None and b is not None and abs(a-b) <= max(float(deadband), .05)
