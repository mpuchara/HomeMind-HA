# Universal Neural Engine — migration plan

Status: **stage U01 (opt-in library only)**, based on HomeMind-HA 0.14.184.

## Objective

A public Home Assistant add-on must not encode the number/order of sensors or
devices in neural layer dimensions. The same shared weights should accept a
dynamic set of typed observations and score a dynamic set of legal
(device, property, action) candidates. Compute and memory still have physical
limits; the contract does not promise unlimited resources on Raspberry Pi 4.

The shipped Ridge / TinyMLP observation, Candidate, Correct and Executor paths
remain authoritative and unchanged throughout U01.

## U01: implemented in universal_observation.py

- Explicit versioned `SensorToken`: entity, area, semantic kind, normalized
  value, availability mask, quality, event/received timestamps and expiry.
- Unit-specific, bounded transforms: binary, temperature °C/°F/K, humidity %,
  illuminance lx, power W/kW, and CO2 ppm.
- Generic numeric readings require an explicit calibration (center and scale).
  No accidental assumption that different raw radar scales are equivalent.
- Unknown/unavailable/future/stale/unsupported values are *masked*, not treated
  as genuine numeric zeros. Their reason remains available for diagnostics.
- `UniversalObservationIndex` maintains per-room, per-kind additive weighted
  set statistics, with O(1) statistical pool changes and O(log N) expiration-heap updates; periodic heap compaction is amortized.
  Its output cardinality depends on *types and rooms*, not a fixed feature mask.
  Both the original tokens and pooled statistics remain accessible.
- Repeated sensor updates compact stale expiry deadlines, keeping the heap
  bounded relative to active sensors. The V1 live index is single-writer and must be owned by the ordered event consumer, not mutated concurrently by inference workers. No SQLite, network or neural inference
  is called by this module.

**Causality:** `encode_sensor` requires explicit event/received/decision times.
The live index advances only forwards and cannot reconstruct history. Offline
training and Correct must replay only eligible observations into an isolated
index, respecting both event_time and received_time. A saved model must pin the
normalizer, input semantic contract and any learned calibration version.

**Meaning:** a "motion off" binary token is *not* proof of an empty room. This
layer describes sensor readings, not the higher-level RoomBelief interpretation.
Avoid using target actuator state or actuator-induced lux as a label for
whether an action was needed. The old physical safety gates remain intact.

## Next independently reviewable stages

1. **U02 — semantic registry and replay parity:** map HA device classes,
   custom sensors, roles and area/device registry to typed tokens; causal
   historical replay + tests for late/out-of-order records, topology changes,
   unknown units, privacy boundaries and old schema migration. The live index
   must only be fed by the event stream *after* persistence/ordering semantics
   have been preserved.
2. **U03 — shared neural room encoder:** small permutation-invariant Deep Sets
   encoder on individual tokens with optional per-kind pools, event-driven
   invalidation and bounded room caches. Validate equivalence of streaming
   and full recomputation; test 5/50/500/1000 sensors, multiple areas and
   degraded/offline sensors. Keep inference strictly observer-only.
3. **U04 — dynamic action scoring:** common action-conditioned policy takes
   room/home context, device type/capabilities and one proposed legal action.
   Enumerate or generate a *bounded* per-target candidate set. Never permit
   neural outputs to bypass ActionIntent, device capability guards, leases,
   manual overrides, cooldowns or Executor.
4. **U05 — learning and A/B:** train frozen shared encoder + individual
   preference adapters first; validate separate Candidate generations,
   multi-point Correct, shadow predictions, calibration, reward provenance,
   future holdouts, promotion and rollback. Then cautiously evaluate shared
   encoder updates for cross-device regressions.
5. **U06 — performance and release:** real Raspberry Pi 4 event-to-decision
   p50/p95/p99 with 6 shadow agents vs pinned 0.14.184 baseline; 20/100/200
   targets; CPU/RSS/thermal; training and Correct under load; graceful
   overload, safe restart and stable Home Assistant connection. First merge
   only after U01 tests pass; installation changes are separate.

## U01 acceptance

Run with the repository's test import path:

```sh
PYTHONPATH=adaptive_ai/src:tests python -m unittest -v tests/test_universal_observation.py
```

If unittest module path resolution does not work in the local checkout,
use `python tests/test_universal_observation.py` with the same PYTHONPATH.

U01 has **no effect on a running add-on** until a later stage explicitly
installs the feature. No new release number, image or installable package is
claimed by U01. Performance comparisons must be measurements, not estimates.

## Open-source distribution gate

Before declaring HomeMind-HA open source, select and commit an explicit
repository LICENSE, audit third-party dependencies and bundled assets,
publish contribution/security/privacy documentation and reproducible builds.
A publicly readable repository alone grants no general redistribution rights.
