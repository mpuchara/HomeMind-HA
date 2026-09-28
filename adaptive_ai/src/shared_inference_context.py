"""One causal inference context shared by Ridge and Tiny MLP.

The two policy backends keep their independent feature schemas and model semantics. This
module shares only immutable inputs/work that are semantically identical for one decision:
- one inference timestamp,
- one Home Context forecast per target/timestamp,
- one materialized view of each live TemporalHistory deque,
- memoized TemporalHistory.previous/last_change_ts reads.

It is intentionally per-decision and bounded by the entities touched in that decision.
Nothing is retained across HA revisions, so there is no cross-event invalidation problem.
"""
from __future__ import annotations

import math
import threading


_MISSING = object()


class SharedHomeContext:
    def __init__(self, provider):
        self._provider = provider
        self._lock = threading.RLock()
        self._forecast_cache = {}
        self.forecast_hits = 0
        self.forecast_misses = 0

    def forecast(self, target_entity, timestamp):
        if self._provider is None or not callable(getattr(self._provider, "forecast", None)):
            return {}
        key = (str(target_entity), float(timestamp))
        with self._lock:
            cached = self._forecast_cache.get(key, _MISSING)
            if cached is not _MISSING:
                self.forecast_hits += 1
                return dict(cached)
        value = dict(self._provider.forecast(target_entity, float(timestamp)) or {})
        with self._lock:
            self._forecast_cache[key] = dict(value)
            self.forecast_misses += 1
        return value

    def __getattr__(self, name):
        if self._provider is None:
            raise AttributeError(name)
        return getattr(self._provider, name)


class SharedInferenceTemporal:
    """Per-decision proxy; preserves the temporal API while memoizing read-only work."""

    CONTRACT_VERSION = 1

    def __init__(self, temporal, timestamp, *, home_provider=None):
        self._temporal = temporal
        self.timestamp = float(timestamp)
        provider = home_provider
        if provider is None:
            provider = getattr(temporal, "home_context", None)
        self.home_context = (
            provider if isinstance(provider, SharedHomeContext) else SharedHomeContext(provider)
        )
        self.samples = getattr(temporal, "samples", {})
        self._samples_cache = {}
        self._previous_cache = {}
        self._last_change_cache = {}
        self.sample_hits = 0
        self.sample_misses = 0
        self.previous_hits = 0
        self.previous_misses = 0
        self.last_change_hits = 0
        self.last_change_misses = 0

    def samples_for(self, entity_id):
        key = str(entity_id)
        cached = self._samples_cache.get(key, _MISSING)
        if cached is not _MISSING:
            self.sample_hits += 1
            return cached
        rows = tuple((getattr(self._temporal, "samples", {}) or {}).get(key) or ())
        self._samples_cache[key] = rows
        self.sample_misses += 1
        return rows

    def previous(self, entity_id, at_ts):
        key = (str(entity_id), float(at_ts))
        cached = self._previous_cache.get(key, _MISSING)
        if cached is not _MISSING:
            self.previous_hits += 1
            return cached
        previous = getattr(self._temporal, "previous", None)
        value = previous(entity_id, float(at_ts)) if callable(previous) else None
        self._previous_cache[key] = value
        self.previous_misses += 1
        return value

    @staticmethod
    def _fallback_key(fallback_state):
        if fallback_state is None:
            return None
        if not isinstance(fallback_state, dict):
            return id(fallback_state)
        return (
            fallback_state.get("entity_id"),
            fallback_state.get("last_changed"),
            fallback_state.get("last_updated"),
            fallback_state.get("state"),
        )

    def last_change_ts(self, entity_id, fallback_state=None):
        key = (str(entity_id), self._fallback_key(fallback_state))
        cached = self._last_change_cache.get(key, _MISSING)
        if cached is not _MISSING:
            self.last_change_hits += 1
            return cached
        method = getattr(self._temporal, "last_change_ts", None)
        value = method(entity_id, fallback_state) if callable(method) else None
        self._last_change_cache[key] = value
        self.last_change_misses += 1
        return value

    def diagnostics(self):
        home = self.home_context
        return {
            "contract_version": self.CONTRACT_VERSION,
            "timestamp": self.timestamp if math.isfinite(self.timestamp) else None,
            "sample_entities_materialized": len(self._samples_cache),
            "sample_hits": self.sample_hits,
            "sample_misses": self.sample_misses,
            "previous_hits": self.previous_hits,
            "previous_misses": self.previous_misses,
            "last_change_hits": self.last_change_hits,
            "last_change_misses": self.last_change_misses,
            "forecast_hits": int(getattr(home, "forecast_hits", 0) or 0),
            "forecast_misses": int(getattr(home, "forecast_misses", 0) or 0),
        }

    def __getattr__(self, name):
        if self._temporal is None:
            raise AttributeError(name)
        return getattr(self._temporal, name)


def shared_inference_temporal(temporal, timestamp, *, home_provider=None):
    if isinstance(temporal, SharedInferenceTemporal):
        # Reuse only when the timestamp is exactly the same causal decision.
        if abs(float(temporal.timestamp) - float(timestamp)) <= 1e-9:
            return temporal
    return SharedInferenceTemporal(
        temporal, float(timestamp), home_provider=home_provider
    )
