# U03 — Shared Deep Sets neural encoder (observer-only)

**Branch:** feature/universal-neural-u03-shared-encoder, stacked on U02 (#350).
Do not merge to main before U01, U02, U03 are reviewed and all required CI
jobs succeed.

## Architecture (implemented)

```
HA SensorToken (U01+U02)        variable cardinality
   |
   +-- typed [value, availability, quality, weighted value, kind one-hot]
   |                shared float32 token MLP (11 -> 24 -> 16)
   v
room weighted Deep Sets pool (incremental float64 accumulators)
   |
   +-- mean neural token vector + count/coverage/quality
   |                shared float32 room MLP (19 -> 24 -> 32)
   v
room vector, per room (32)       variable room count
   |
   +-- mean room vector + room/sensor counts and coverage
   |                shared float32 home MLP (35 -> 24 -> 32)
   v
home vector (32)                fixed width; NO action policy here
```

Defaults shown above, NOT a validated model-size optimum. All weights are
initially deterministic random numbers, **untrained**. They are not usable
for actual control. The model is implemented for architecture and
performance validation before U04/U05 training and evaluation.

### Why no fixed number of inputs?

- All tokens use the same trained parameters *once trained*; there is no
  input coordinate reserved for a particular HA entity_id.
- Each room has an arbitrary token set, and the house has an arbitrary
  room set. Aggregation is permutation invariant up to floating-point
  rounding; unit tests compare reordered sets.
- Embedding dimensions and memory per token are independent of home size;
  RAM still grows linearly with the number of observed sensors and rooms.

### Why not rescan every sensor on a HA event?

`UniversalObservationIndex` gains `get` and `drain_changes`. Only changed
entity IDs (also stale TTL expiry and removal) enter the neural cache.
The affected room(s) are refreshed, and the O(embedding_width) home
aggregates are updated. Requesting `home()` repeatedly with no new
observation returns cached values. Consumers MUST call
`IncrementalNeuralHome.upsert/advance/remove` (single writer);
direct external writes to its `.index` would invalidate coherence.

Model weights are serialized with a versioned JSON-safe checkpoint;
incompatible shapes, kinds or nonfinite values are rejected. No pickle,
no network call and no production model migration are involved.

### Missing/stale semantics

`SensorToken.available=False` and a genuine zero reading produce different
input vectors. A missing token still contributes to room coverage and
quality statistics; its numerical value is not treated as observed zero.
Expiry invalidates only the affected sensor and room; no all-house rescan.
Input event timestamps and quality must be validated by U01/U02 before use.

### Safety

- U03 makes **zero action recommendations** and touches neither
  `ActionIntent` nor `Executor`.
- It does **not** implement training, Offline RL, Correct, Candidate Shadow,
  confidence calibration, reward attribution or model promotion.
- Unit-specific photometric leakage, target-actuator effects and historical
  label provenance remain U04/U05 release gates. Only sensor domains are
  admitted by the U02 HA adapter; more sensor roles require a typed adapter.
- HA out-of-order delivery and historical replay must be mediated by U02;
  do not feed current Registry metadata into old timestamps.

### Tests and profiling

```sh
python -m unittest discover -s tests -p 'test_universal_neural_encoder.py' -v
python tools/benchmark_universal_encoder.py --sizes 5,100,1000 --updates 300
```

The bench is synthetic and reports host timings and recomputation counters.
It is **not** a release performance gate and does not demonstrate that 6
Shadow agents on a physical Raspberry Pi 4 achieve a given event-to-decision
p95. That can only be measured after the observer is integrated.

## U04 task split

1. Integrate one observer-only event adapter with the ordered HA ingress,
   respecting live cache ownership, disabled by default, with bounded
   CPU budget and no effects on existing agents.
2. Create dynamic `DeviceCapability` and typed proposed-action contract
   (device/property/allowed-value/safety metadata). Do not encode a fixed
   number of device outputs into neural weights.
3. Train a common action-conditioned scorer on causally eligible logged
   experiences. Explicitly separate observation state, executed action,
   user command, candidate prediction, actual outcome and reward.
4. Re-run full supervised Correct/Candidate/promotion/rollback parity,
   comparison to existing production policy and Pi4 p95/CPU/RSS gates
   before any control-mode enablement.
