# U04c — observer-only dynamic action scoring on live HA data

Status: draft stacked PR after #353. No training or Control change.

## Implemented

1. U04b perception now publishes immutable-copy room/home embeddings for
   individual requested areas. A snapshot is unavailable if sensor input,
   registry, rebuild or REST reconciliation is pending. The mutable U03
   neural cache remains exclusively owned by the perception worker.
2. After a *completed and healthy* perception event batch, the observer
   sends changed entity IDs to an independent U04c worker. The notification
   never reads SQLite, computes neural features, or runs a policy in
   the WebSocket or perception event handler.
3. U04c uses **existing cached** Engine.dependency_agents / agent_configs
   routing under a brief engine lock. It does not trigger a fresh agent index
   rebuild or full agent scan on an unrelated sensor update.
4. In its own worker, U04c consults the existing DeviceAgent descriptor and
   legal_action_mask, then the U04a typed action adapter. This can use SQLite;
   it never runs on Engine's realtime event thread.
5. The worker evaluates a bounded set of existing agent action_values
   (max eight per target, sampled across the existing ordered action set),
   max four agents per event pass, selected using fair round-robin when
   many agents share one sensor dependency. Select/input_select options
   are sampled directly from bounded index ranges instead of materializing
   an unbounded action_values list. Any larger fanout is counted as
   diagnostics and **not** sent into the inference scheduler.
6. The shared U04a neural scorer receives immutable U03 room/home context
   and exact legal DeviceAgent actions. Only minimum and maximum **raw,
   untrained** projections and counts are exposed through diagnostic status.
7. Separate action-observer queue (256 coalesced changed entity IDs),
   per-agent >=2-second debounce and proportional CPU-yield budget.
8. Independent switch: universal_action_observer_enabled: false in the
   add-on and settings; requires universal_observer_enabled: true. Both
   switches are OFF in the shipped defaults.

## Operating guarantees

- Disabled: no extra action worker, no new model load or action-mask DB read.
- Enabled: observer is diagnostic, never sends commands, never changes
  current Agent/Candidate/Correct, no ActionIntent or Executor call.
- A blocked/empty mask causes abstain and no neural projection.
- If perception is stale, overloaded or has pending changes, U04c skips
  scoring instead of presenting its old embedding as fresh.
- Invalid mask, model or service state raises only inside the sidecar,
  increments observer errors, and cannot break the HA event source.
- Current DeviceAgent.legal_action_mask is the *only* source of legal
  proposals. This mask is advisory and never authorizes dispatch.
- The current U03 and U04a weights are randomized and untrained. Raw
  projections do NOT express action preference, confidence, reward,
  calibration, or policy quality. Do not rank the displayed numbers.
- For safety, U04c only sees the already active production routing index,
  excluding paused/untrained agents. Its snapshots can become outdated
  between the mask and neural read; this is acceptable for diagnostic-only
  scoring and prohibited for physical dispatch.
- Initial bootstrapping of 1000 sensors remains O(1000) but runs in
  low-priority U04b batches; publication of room vectors costs O(rooms)
  per completed batch, to be measured on Pi 4.

## Test instructions

    python -m unittest discover -s tests -p 'test_universal_action_runtime.py' -v

Full CI: Python 3.11 + 3.13, Docker build and package checks.
Run with switches disabled first, and verify event→intent p95
against pinned 0.14.184 on Raspberry Pi 4. Then opt into U04b,
then opt into U04c, verifying that existing HA connection, current
state, Candidate cards and Correct remain unaffected.

## Remaining work for Codex / U05

- Efficient per-room published vector snapshots and CPU/RSS pressure
  validation under 20/100/200 devices and 1000+ sensors.
- Proper event-time-aligned multi-source Recorder/FeatureJournal replay,
  correct preference/actuation provenance and protected manual feedback.
- Train shared action-conditioned network against clearly labeled
  observations; independent future holdout and per-agent validation.
- Preserve all Candidate lineage, Correct point annotations,
  parent comparisons, shadow/no-action isolation and promotion rollback.
- Safety ownership stays with DeviceAgent and Executor; never migrate
  physical dispatch into an ML scorer.
- A publicly readable repo still needs an explicit LICENSE/dependency
  audit before describing the distribution as open source.
