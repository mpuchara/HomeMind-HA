"""HA semantic adapter and causal FeatureJournal replay for UniversalObservation.

Observer-only U02. This module never connects to HA, touches a production Store,
modifies existing models, or publishes an ActionIntent. A journal read is explicitly
bounded and performed on a caller-owned SQLite connection.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Mapping

from universal_observation import (
    CONTRACT_VERSION as TOKEN_CONTRACT_VERSION,
    UniversalObservationIndex,
    encode_sensor,
)

JOURNAL_CONTRACT_VERSION = 1
ALLOWED_DOMAINS = frozenset(("sensor", "binary_sensor"))
MAX_ATTRIBUTES_BYTES = 32768
DEFAULT_MAX_RECORDS = 50000


def _finite(value, field):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid {field}") from exc
    if not math.isfinite(result):
        raise ValueError(f"invalid {field}")
    return result


def _table(value, id_key):
    """HA registry supports lists of registry entries and entity-id keyed maps."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        result = {}
        for key, row in value.items():
            if not isinstance(row, Mapping):
                raise ValueError(f"invalid registry entry: {key}")
            item = dict(row)
            item.setdefault(id_key, key)
            result[str(item[id_key])] = item
        return result
    result = {}
    for row in value:
        if not isinstance(row, Mapping) or not row.get(id_key):
            raise ValueError(f"invalid registry {id_key}")
        item = dict(row)
        result[str(item[id_key])] = item
    return result


@dataclass(frozen=True)
class SemanticRegistry:
    """Immutable-by-convention snapshot of HA entity and device registries.

    A caller must pass the snapshot that was known at the inference/replay time.
    It is not correct to supply today's area registry for yesterday's replay.
    """
    entities: Mapping
    devices: Mapping
    calibrations: Mapping
    revision: str

    @classmethod
    def create(cls, *, entities=None, devices=None, calibrations=None, revision="unregistered"):
        e = _table(entities, "entity_id")
        d = _table(devices, "id")
        c = {}
        for key, value in (calibrations or {}).items():
            if not isinstance(value, Mapping):
                raise ValueError("calibration entry must be an object")
            c[str(key)] = dict(value)
        return cls(e, d, c, str(revision))

    def area_for(self, entity_id):
        row = self.entities.get(str(entity_id)) or {}
        explicit = row.get("area_id")
        if explicit:
            return str(explicit)
        device = self.devices.get(str(row.get("device_id") or "")) or {}
        area = device.get("area_id")
        return str(area) if area else None

    def calibration_for(self, entity_id):
        return self.calibrations.get(str(entity_id))

    def enrich(self, entity_id, state):
        """Use stored observation attributes first, registry metadata second."""
        attrs = dict((state or {}).get("attributes") or {})
        row = self.entities.get(str(entity_id)) or {}
        if not attrs.get("device_class") and row.get("original_device_class"):
            attrs["device_class"] = row["original_device_class"]
        return {"state": (state or {}).get("state"), "attributes": attrs}

    def encode(self, entity_id, state, *, at_ts, event_ts, received_ts, quality=1.0,
               max_age_seconds=900.0):
        entity_id = str(entity_id)
        if entity_id.partition(".")[0] not in ALLOWED_DOMAINS:
            raise ValueError(f"entity domain is not a sensor: {entity_id}")
        return encode_sensor(
            entity_id, self.enrich(entity_id, state),
            area_id=self.area_for(entity_id),
            at_ts=at_ts, event_ts=event_ts, received_ts=received_ts,
            quality=quality,
            max_age_seconds=max_age_seconds,
            calibration=self.calibration_for(entity_id),
        )


@dataclass(frozen=True)
class RegistryEpoch:
    effective_ts: float
    received_ts: float
    registry: SemanticRegistry

    def __post_init__(self):
        _finite(self.effective_ts, "registry effective time")
        _finite(self.received_ts, "registry received time")
        if not isinstance(self.registry, SemanticRegistry):
            raise TypeError("registry epoch requires SemanticRegistry")


class RegistryTimeline:
    """Time-versioned topology; no future registry assignments leak into replay."""
    def __init__(self, epochs=()):
        self._epochs = tuple(sorted(
            epochs, key=lambda e: (e.effective_ts, e.received_ts, e.registry.revision)
        ))
        if len({(e.effective_ts, e.received_ts) for e in self._epochs}) != len(self._epochs):
            raise ValueError("ambiguous registry epochs at identical timestamps")
        self._unknown = SemanticRegistry.create()

    def as_of(self, at_ts):
        at = _finite(at_ts, "at_ts")
        best = None
        for epoch in self._epochs:
            if epoch.effective_ts <= at and epoch.received_ts <= at:
                best = epoch
            if epoch.effective_ts > at:
                break
        return best.registry if best is not None else self._unknown


@dataclass(frozen=True)
class JournalObservation:
    event_key: str
    entity_id: str
    event_ts: float
    received_ts: float
    state: str | None
    attributes: Mapping
    quality: float = 1.0
    source: str = "feature_journal"

    @property
    def ready_ts(self):
        return max(self.event_ts, self.received_ts)

    @property
    def order(self):
        # A late older event must not replace a later physical event.
        return (self.event_ts, self.received_ts, self.event_key)


def journal_observation(row):
    """Decode a real feature_observation_events row; never use current HA state."""
    if hasattr(row, "keys"):
        row = {key: row[key] for key in row.keys()}
    if not isinstance(row, Mapping):
        raise ValueError("journal row must be a mapping")
    if int(row.get("contract_version", 0)) != JOURNAL_CONTRACT_VERSION:
        raise ValueError("unsupported FeatureJournal contract")
    entity_id = str(row.get("entity_id") or "")
    event_key = str(row.get("event_key") or "")
    if not event_key or entity_id.partition(".")[0] not in ALLOWED_DOMAINS:
        raise ValueError("invalid journal entity or event_key")
    attrs = row.get("attributes_json", "{}")
    if isinstance(attrs, str):
        if len(attrs.encode("utf-8")) > MAX_ATTRIBUTES_BYTES:
            raise ValueError("oversized journal attributes")
        try:
            attrs = json.loads(attrs)
        except (TypeError, ValueError) as exc:
            raise ValueError("malformed journal attributes") from exc
    if not isinstance(attrs, Mapping):
        raise ValueError("journal attributes must be a JSON object")
    quality = _finite(row.get("quality", 1.0), "journal quality")
    if not 0.0 <= quality <= 1.0:
        raise ValueError("journal quality must be in 0..1")
    state = row.get("state")
    if state is not None and not isinstance(state, str):
        raise ValueError("journal state must be a string or tombstone")
    return JournalObservation(
        event_key, entity_id, _finite(row.get("event_time"), "event_time"),
        _finite(row.get("received_time"), "received_time"),
        state, dict(attrs), quality, str(row.get("source") or "feature_journal"),
    )


def read_feature_journal(conn, entity_ids, *, at_ts, max_records=DEFAULT_MAX_RECORDS):
    """Bounded, read-only SQLite adapter for the existing FeatureJournal schema.

    A full replay needs its earliest causally valid seed: truncating history with
    LIMIT would silently change its meaning. Raise on overflow instead. The caller
    supplies entity IDs, a stable read transaction/snapshot and retention policy.
    """
    at = _finite(at_ts, "at_ts")
    cap = int(max_records)
    if cap <= 0:
        raise ValueError("max_records must be positive")
    ids = sorted({str(entity) for entity in entity_ids})
    if any(entity.partition(".")[0] not in ALLOWED_DOMAINS for entity in ids):
        raise ValueError("only sensor domains are accepted")
    seen = {}
    for offset in range(0, len(ids), 64):
        chunk = ids[offset:offset + 64]
        bind = ",".join("?" for _ in chunk)
        sql = (
            "SELECT event_key, contract_version, entity_id, event_time, "
            "received_time, state, attributes_json, source, quality "
            "FROM feature_observation_events "
            f"WHERE entity_id IN ({bind}) AND event_time <= ? AND received_time <= ? "
            "ORDER BY received_time, event_time, event_key LIMIT ?"
        )
        remaining = cap - len(seen)
        for row in conn.execute(sql, (*chunk, at, at, remaining + 1)):
            observation = journal_observation(row)
            existing = seen.get(observation.event_key)
            if existing is not None and existing != observation:
                raise ValueError("conflicting duplicate journal event_key")
            seen[observation.event_key] = observation
            if len(seen) > cap:
                raise ValueError("FeatureJournal replay row cap exceeded; increase explicitly")
    return tuple(sorted(
        seen.values(), key=lambda item: (item.ready_ts, item.event_ts,
                                          item.received_ts, item.event_key)
    ))


class CausalObservationReplay:
    """Incremental historical snapshots respecting BOTH event and receipt time.

    This is separate from the live index. Seeking backwards rebuilds causal state.
    A topology change re-encodes selected observations and may be O(sensors).
    No reward, Correct or training model consumes this path until later gating.
    """
    CONTRACT_VERSION = 1

    def __init__(self, observations, *, registry_timeline=None, max_age_seconds=900.0):
        unique = {}
        for row in observations:
            if not isinstance(row, JournalObservation):
                row = journal_observation(row)
            existing = unique.get(row.event_key)
            if existing is not None and existing != row:
                raise ValueError("conflicting duplicate journal event_key")
            unique[row.event_key] = row
        self._rows = tuple(sorted(
            unique.values(), key=lambda item: (item.ready_ts, item.event_ts,
                                                item.received_ts, item.event_key)
        ))
        self.registry_timeline = registry_timeline or RegistryTimeline()
        self.max_age_seconds = max_age_seconds
        self._reset()

    def _reset(self):
        self._index = UniversalObservationIndex()
        self._cursor = 0
        self._best = {}
        self._registry = None
        self._at = float("-inf")

    def _token(self, row, at_ts):
        return self._registry.encode(
            row.entity_id, {"state": row.state, "attributes": row.attributes},
            at_ts=at_ts, event_ts=row.event_ts, received_ts=row.received_ts,
            quality=row.quality, max_age_seconds=self.max_age_seconds,
        )

    def _rebuild(self, at_ts):
        self._index = UniversalObservationIndex()
        for entity_id in sorted(self._best):
            row = self._best[entity_id]
            if row.state is not None:
                self._index.upsert(self._token(row, at_ts), now=at_ts)

    def advance(self, at_ts):
        at = _finite(at_ts, "at_ts")
        if at < self._at:
            self._reset()
        registry = self.registry_timeline.as_of(at)
        topology_changed = registry is not self._registry
        self._registry = registry
        if not topology_changed:
            self._index.advance(at)
        dirty = set()
        while self._cursor < len(self._rows) and self._rows[self._cursor].ready_ts <= at:
            row = self._rows[self._cursor]
            self._cursor += 1
            previous = self._best.get(row.entity_id)
            if previous is not None and row.order <= previous.order:
                continue
            self._best[row.entity_id] = row
            dirty.add(row.entity_id)
        if topology_changed:
            self._rebuild(at)
        else:
            for entity_id in sorted(dirty):
                row = self._best[entity_id]
                if row.state is None:
                    self._index.remove(entity_id)
                else:
                    self._index.upsert(self._token(row, at), now=at)
        self._at = at
        return self._index

    def snapshot(self, at_ts):
        index = self.advance(at_ts)
        return {
            "version": self.CONTRACT_VERSION,
            "token_contract_version": TOKEN_CONTRACT_VERSION,
            "timestamp": self._at,
            "registry_revision": self._registry.revision,
            "tokens": index.tokens(),
            "pools": index.pool(),
            "indexed_events": self._cursor,
            "sensor_count": index.sensor_count,
        }
