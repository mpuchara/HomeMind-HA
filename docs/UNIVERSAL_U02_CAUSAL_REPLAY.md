# U02 — HA semantic registry and causal replay

Status: observer-only stacked change on U01; no installation/runtime switch.

## Surfaces and ownership

- universal_ha_adapter.SemanticRegistry accepts actual Home Assistant Entity
  Registry and Device Registry lists or id-keyed dictionaries. Entity area
  wins over device area. Missing area is explicit ("__unassigned__").
- Original device_class from the relevant registry snapshot is a fallback only.
  Recorded observation attributes take precedence. Numeric sensor readings
  without known units/calibration are masked, never guessed.
- encode_ha_state_change accepts the data part of HA "state_changed" with
  ISO-8601 timezone-aware last_updated/last_changed; returns a typed token,
  deletion tombstone, or skips a non-sensor domain. The ingress remains the
  authoritative source of actual received_ts, ordering and persistence.
- read_feature_journal accepts a *caller-owned* SQLite connection and reads
  the existing feature_observation_events schema (contract 1). It does not
  change SQLite pragmas or tables. It explicitly fails if max_records is
  exceeded: silently cutting the prefix would corrupt replay semantics.
- CausalObservationReplay uses max(event_time,received_time) as the earliest
  observation knowledge time and selects the newest event_time per entity
  (received_time resolves an event-time tie). It rebuilds on rewind or
  topology epoch change; otherwise it advances a bounded live-like set index.

## Correctness contract

1. At replay time T, only records with event_time <= T AND received_time <= T
   are admissible.
2. A late older event must never overwrite a more recent event-time reading.
3. A registry change is visible only when both effective_ts and received_ts
   are <= T. If no historical registry is available, area identity is unknown;
   do not apply the current topology retroactively.
4. Inference-time inputs stay separate from labels, Correct and user
   preferences. Actuator readings are excluded from this U02 sensor adapter.
   A sensor influenced by an actuator (e.g. lamp-dependent lux) is NOT
   automatically safe to learn causal need from.
5. Event keys are idempotent; conflicting reused keys raise explicitly.
   Invalid units/unknown values are masked but retained in diagnostics.
6. Live index writes must be serialized by a single ordered consumer; this
   module does not itself implement a HA ingress or a durable event queue.

## Important current boundaries

This step does not reconstruct all 14 days of full Recorder/Archive history.
FeatureJournal is a narrower source whose retention and protected windows
are controlled by the shipped runtime. A correct long historical replay must
merge archive, FeatureJournal and metadata epochs with the existing precedence
and causality rules in the later integration stage. Neither the current HA
registry nor the current HA state may be substituted for missing historical
data.

This step deliberately introduces no neural network, live inference hook,
agent migration, model promotion, new config toggles or HA service calls.

## Review and tests

The new tests cover normal units, HA ISO timestamps, out-of-order delivery,
late receipts, deletion, duplicate keys, invalid attributes, bounded SQLite
reads, registry changes, rewinds, stale expiry and different event iteration
orders. The existing full unittest discovery/compileall should also pass.

The next stage U03 should add a permutation-invariant shared neural encoder
without requiring a fixed input sensor count; it should first operate in
Shadow and measure event-to-decision p95 and CPU/RSS on Raspberry Pi 4.
