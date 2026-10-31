"""Dynamic, typed, causal observation set for a future universal neural backend.

This module is intentionally NOT installed in the existing Ridge/TinyMLP runtime.
It is a side-effect-free V1 foundation: no Store, HA calls, model publication,
physical dispatch or automatic replacement of the shipped feature contract.

The input cardinality is unbounded by a neural layer width. Per-sensor metadata
and masks are preserved; only same-kind values are pooled together.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import heapq
import math

CONTRACT_VERSION = 1
UNASSIGNED_AREA = "__unassigned__"
INVALID_STATES = frozenset(("", "unknown", "unavailable", "none", "null"))
LOG_LUX = math.log1p(100_000.0)
LOG_CO2 = math.log1p(10_000.0)
POWER_SCALE = math.asinh(10.0)


@dataclass(frozen=True)
class SensorToken:
    entity_id: str
    area_id: str
    kind: str
    value: float
    available: bool
    quality: float
    event_ts: float
    received_ts: float
    expires_at: float | None
    reason: str
    unit: str

    @property
    def mask(self):
        return 1.0 if self.available else 0.0


def _finite(value, name):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _kind(entity_id, attrs):
    domain = entity_id.split(".", 1)[0]
    if domain == "binary_sensor":
        return "binary"
    device_class = str(attrs.get("device_class") or "").lower()
    unit = str(attrs.get("unit_of_measurement") or "").strip().lower()
    if device_class == "temperature":
        return "temperature"
    if device_class == "humidity":
        return "humidity"
    if device_class == "illuminance":
        return "illuminance"
    if device_class == "power":
        return "power"
    if device_class == "carbon_dioxide":
        return "co2"
    # Units are used only when their semantics are unambiguous.
    if unit in ("°c", "°f", "k"):
        return "temperature"
    if unit in ("lx", "lux"):
        return "illuminance"
    if unit in ("w", "kw"):
        return "power"
    if unit == "ppm" and device_class == "carbon_dioxide":
        return "co2"
    return "generic"


def _normalized(raw, kind, unit, calibration):
    if kind == "binary":
        value = str(raw).strip().lower()
        if value not in ("on", "off"):
            raise ValueError("binary state must be on or off")
        return 1.0 if value == "on" else 0.0
    value = _finite(raw, "sensor value")
    if kind == "temperature":
        if unit in ("°c", "c"):
            celsius = value
        elif unit in ("°f", "f"):
            celsius = (value - 32.0) * (5.0 / 9.0)
        elif unit == "k":
            celsius = value - 273.15
        else:
            raise ValueError("unsupported temperature unit")
        return max(-6.0, min(6.0, (celsius - 20.0) / 20.0))
    if kind == "humidity":
        if unit not in ("%", "percent") or not 0.0 <= value <= 100.0:
            raise ValueError("invalid relative humidity")
        return value / 100.0
    if kind == "illuminance":
        if unit not in ("lx", "lux") or value < 0.0:
            raise ValueError("invalid illuminance")
        return min(1.0, math.log1p(value) / LOG_LUX)
    if kind == "power":
        if unit == "kw":
            value *= 1_000.0
        elif unit != "w":
            raise ValueError("unsupported power unit")
        return max(-6.0, min(6.0, math.asinh(value / 1_000.0) / POWER_SCALE))
    if kind == "co2":
        if unit != "ppm" or value < 0.0:
            raise ValueError("invalid CO2")
        return min(1.0, math.log1p(value) / LOG_CO2)
    # Unknown raw scales must not silently impersonate calibrated sensors.
    if not calibration:
        raise ValueError("generic sensor requires explicit calibration")
    center = _finite(calibration.get("center"), "calibration center")
    scale = _finite(calibration.get("scale"), "calibration scale")
    if scale <= 0.0:
        raise ValueError("calibration scale must be positive")
    return max(-6.0, min(6.0, (value - center) / scale))


def encode_sensor(
    entity_id, state, *, area_id=None, at_ts, event_ts, received_ts,
    quality=1.0, max_age_seconds=900.0, calibration=None,
):
    """Encode one HA-like state without using knowledge later than at_ts.

    Timestamp arguments are explicit, to prevent accidentally labeling a replay
    observation with the current clock. Unknown/unsupported readings produce a
    masked token, never a fabricated numeric zero observation.
    """
    entity_id = str(entity_id or "")
    if "." not in entity_id:
        raise ValueError("expected Home Assistant entity_id")
    at = _finite(at_ts, "at_ts")
    event = _finite(event_ts, "event_ts")
    received = _finite(received_ts, "received_ts")
    q = _finite(quality, "quality")
    if not 0.0 <= q <= 1.0:
        raise ValueError("quality must be within 0..1")
    ttl = None if max_age_seconds is None else _finite(max_age_seconds, "max_age_seconds")
    if ttl is not None and ttl <= 0.0:
        raise ValueError("max_age_seconds must be positive")
    attrs = dict((state or {}).get("attributes") or {})
    unit = str(attrs.get("unit_of_measurement") or "").strip().lower()
    kind = _kind(entity_id, attrs)
    raw = (state or {}).get("state")
    reason = "ok"
    if event > at or received > at:
        reason = "future_observation"
    elif raw is None or str(raw).strip().lower() in INVALID_STATES:
        reason = "unavailable"
    elif ttl is not None and at >= event + ttl:
        reason = "stale"
    expires = event + ttl if ttl is not None else None
    value = 0.0
    if reason == "ok":
        try:
            value = _normalized(raw, kind, unit, calibration)
        except ValueError:
            reason = "unsupported_or_uncalibrated"
    return SensorToken(
        entity_id=entity_id,
        area_id=str(area_id) if area_id else UNASSIGNED_AREA,
        kind=kind,
        value=float(value),
        available=reason == "ok",
        quality=q if reason == "ok" else 0.0,
        event_ts=event,
        received_ts=received,
        expires_at=expires,
        reason=reason,
        unit=unit,
    )


class _Pool:
    """Additive statistics; insertion/replacement/removal are O(1) per token."""
    __slots__ = ("count", "available", "weight", "weighted_sum", "weighted_square", "quality")

    def __init__(self):
        self.count = 0
        self.available = 0
        self.weight = 0.0
        self.weighted_sum = 0.0
        self.weighted_square = 0.0
        self.quality = 0.0

    def apply(self, token, sign):
        self.count += sign
        if token.available:
            self.available += sign
            w = token.quality
            self.weight += sign * w
            self.weighted_sum += sign * w * token.value
            self.weighted_square += sign * w * token.value * token.value
            self.quality += sign * w

    def export(self):
        w = max(0.0, self.weight)
        mean = self.weighted_sum / w if w > 1e-12 else 0.0
        variance = max(0.0, self.weighted_square / w - mean * mean) if w > 1e-12 else 0.0
        return {
            "count": self.count,
            "available": self.available,
            "coverage": self.available / self.count if self.count else 0.0,
            "weight": w,
            "mean": mean,
            "std": math.sqrt(variance),
            "mean_quality": self.quality / self.available if self.available else 0.0,
        }


class UniversalObservationIndex:
    """Bounded-work live cache; NOT a historical event archive.

    A snapshot can move only forwards. Historical replay must build a separate
    index from causally eligible event/received-time records. Model training must
    not use this live index to reconstruct an earlier timestamp.
    """
    def __init__(self):
        self.now = float("-inf")
        self._by_entity = {}
        self._by_area = {}
        self._pools = {}
        self._expirations = []

    @property
    def sensor_count(self):
        return len(self._by_entity)

    def _apply(self, token, sign):
        key = (token.area_id, token.kind)
        pool = self._pools.setdefault(key, _Pool())
        pool.apply(token, sign)
        if pool.count == 0:
            del self._pools[key]
        members = self._by_area.setdefault(token.area_id, set())
        if sign > 0:
            members.add(token.entity_id)
        else:
            members.discard(token.entity_id)
            if not members:
                del self._by_area[token.area_id]

    def advance(self, now):
        now = _finite(now, "now")
        if now < self.now:
            raise ValueError("live index cannot rewind")
        self.now = now
        while self._expirations and self._expirations[0][0] <= now:
            expiry, entity_id, event_ts, received_ts = heapq.heappop(self._expirations)
            old = self._by_entity.get(entity_id)
            if (old is None or not old.available or old.expires_at != expiry
                    or old.event_ts != event_ts or old.received_ts != received_ts):
                continue
            self._apply(old, -1)
            stale = replace(old, available=False, quality=0.0, value=0.0, reason="stale")
            self._by_entity[entity_id] = stale
            self._apply(stale, 1)

    def upsert(self, token, *, now):
        if not isinstance(token, SensorToken):
            raise TypeError("expected SensorToken")
        self.advance(now)
        if token.received_ts > self.now or token.event_ts > self.now:
            raise ValueError("cannot insert future observation")
        old = self._by_entity.get(token.entity_id)
        if old is not None and (token.received_ts, token.event_ts) <= (old.received_ts, old.event_ts):
            return False
        if token.available and token.expires_at is not None and token.expires_at <= self.now:
            token = replace(token, value=0.0, quality=0.0, available=False, reason="stale")
        if old is not None:
            self._apply(old, -1)
        self._by_entity[token.entity_id] = token
        self._apply(token, 1)
        if token.available and token.expires_at is not None:
            heapq.heappush(
                self._expirations,
                (token.expires_at, token.entity_id, token.event_ts, token.received_ts),
            )
        self._compact_expirations()
        return True

    def _compact_expirations(self):
        # Repeated updates to a sensor must not retain an unbounded heap of
        # obsolete deadlines. Rebuilding is amortized over many insertions.
        if len(self._expirations) <= max(128, 4 * len(self._by_entity)):
            return
        self._expirations = [
            (t.expires_at, t.entity_id, t.event_ts, t.received_ts)
            for t in self._by_entity.values()
            if t.available and t.expires_at is not None
        ]
        heapq.heapify(self._expirations)

    def diagnostics(self):
        return {
            "contract_version": CONTRACT_VERSION,
            "sensors": len(self._by_entity),
            "areas": len(self._by_area),
            "pools": len(self._pools),
            "pending_expiration_entries": len(self._expirations),
        }

    def remove(self, entity_id):
        old = self._by_entity.pop(str(entity_id), None)
        if old is None:
            return False
        self._apply(old, -1)
        return True

    def tokens(self, area_id=None):
        if area_id is None:
            ids = self._by_entity
        else:
            ids = self._by_area.get(str(area_id), ())
        return tuple(self._by_entity[eid] for eid in sorted(ids))

    def pool(self, area_id=None):
        """Dynamic kind-keyed set representation, deterministic and order independent."""
        keys = sorted(self._pools)
        if area_id is not None:
            keys = [key for key in keys if key[0] == str(area_id)]
        return tuple({
            "area_id": area,
            "kind": kind,
            **self._pools[(area, kind)].export(),
        } for area, kind in keys)
