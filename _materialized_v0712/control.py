"""Shared device contract. No HA imports; all times are seconds.

Command acknowledgement is not a measurement of physical process settling.
Learning is a contextual bandit with delayed preference feedback, not a plant model.
"""
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


def legal_value(agent, state, desired):
    """Intersect user limits with current advertised hardware limits and quantize."""
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
    return round(value, 8)


def same_value(a, b, deadband):
    return a is not None and b is not None and abs(a-b) <= max(float(deadband), .05)
