# U04b — opt-in live HA observer integration

Branch: feature/universal-u04b-observer-runtime.
Stack: #349 U01 -> #350 U02 -> #351 U03 -> #352 U04a -> U04b.

## Implemented

- New universal_runtime_observer.py: one daemon thread with a bounded
  coalescing mailbox of most-recent sensor updates (default 2,048 distinct
  entities), one worker-owned IncrementalNeuralHome and a conservative
  error/overflow state.
- The existing Engine only offers *accepted* websocket transitions to
  the observer after its normal state/clock checks. No NumPy work or
  network/SQLite I/O is performed in the ingress callback.
- REST refresh_states submits an authoritative reconciled snapshot, which
  bootstraps the observer after startup and restores completeness after
  mailbox overflow. Snapshot size is capped (default 10,000 sensors);
  oversize snapshots fail explicitly, not as silently partial sets.
- HA Entity/Device Registry updates rebuild room metadata in the observer
  worker, never in the HA event producer.
- Observer worker processes up to 16 changed tokens per batch, and
  cooperatively yields at least 25 ms and 4x its prior batch time after
  each batch (approx. <=20% one-core CPU duty under sustained work;
  not a hard OS-level cgroup/CPU quota). Expirations update on idle tick.
- The observer publishes only lightweight diagnostics via Engine.status():
  enabled/healthy/reconcile_required, pending counts, processed/coalesced
  events, overflow/errors, neural cache counters, physical_actions=0.
- Stop and bounded join are part of normal add-on shutdown after the HA
  WebSocket producer stops.

## Default and enablement

This PR adds universal_observer_enabled: false to both add-on config.yaml
and settings.DEFAULT_OPTIONS. Disabled is the shipped default; there is
no observer worker or new NumPy import during runtime composition when off.

For a voluntary test installation built from this branch only, explicitly
set universal_observer_enabled: true in the Home Assistant add-on
configuration and restart the add-on. Then review the
universal_observer field in the JSON returned by /api/status.
Do NOT enable Control based on this diagnostic.

The current released add-on and main branch are unchanged: this is a
stacked draft PR, not a new installation release.

## Important semantics and known limitations

- The observer is purely diagnostic and its neural weights are random.
  Neither embeddings nor U04a raw action projections are legitimate
  decisions or confidence estimates.
- Only sensor and binary_sensor domains are mirrored; actuator properties
  and any additional context domains require a separate, versioned
  semantic contract. Photometric leakage remains a future learning gate.
- Missing or invalid HA event timestamps are rejected, never silently
  labeled as current observation. The live observer is not a historical
  training source or Correct replay source.
- A mailbox overflow clears pending updates, invalidates healthy state
  and requires a complete REST snapshot. Stale/incomplete embeddings
  must never be treated as authoritative. The REST recovery can take as
  long as the configured reconciliation interval; U04c should request a
  bounded, prioritized resync when overflow is detected.
- A full REST snapshot is O(number of sensors); it runs off the realtime
  WS callback and is applied incrementally by the low-priority worker.
  Under heavy traffic, initial warmup and registry rebuilds take time.
- Client status and inference path remain fully on the original Ridge/
  TinyMLP/Correct/Candidate/Executor implementation. No ActionIntent,
  legal_action_mask evaluation or neural action scoring is invoked by
  the observer. U04a action scoring remains a standalone library.
- No training, autonomous learning, action dispatch, promotions, model
  compatibility migrations or new worker-process IPC is included.
- Only a single instance of the observer may write its neural cache.
  No API exposes the mutable neural model across threads.

## Tests and acceptance

Run the existing complete test matrix (Python 3.11 / 3.13) and Docker
image smoke. Specific tests:

    python -m unittest discover -s tests -p 'test_universal_runtime_observer.py' -v

The tests verify disabled installation, bounded input queues, single
writer behavior, real HA-shaped events, metadata-only area changes,
late packets, tombstones, clean shutdown and recovery by full REST
snapshot. GitHub CI is not a Pi 4 runtime measurement.

U04c: once confirmed with opt-in on a real Pi 4, add an observer-only
dependency-indexed action scorer consuming real DeviceAgent masks in
a *separate bounded worker*; never run arbitrary legal_action_mask/SQL
reads on the WebSocket ingress path. Then extend causal Recorder
training, user Correct and independent Candidate/Shadow evaluation.
