# 0.14.88 — 2026-09-25

- Start **Stage 9: real Raspberry Pi 4 profiling and final runtime performance gates**.
- Upgrade the packaged Pi profiler to `pi_training_profile_v2` with host identity, Pi-4 detection, temperature, available-memory and load sampling, status-probe failure accounting, probe-loop timing and direct training-worker concurrency observation from `/proc`.
- Keep CPU semantics explicit: one-core CPU and whole-host CPU remain separate so a four-core Pi cannot hide a saturated core behind ambiguous percentages.
- Add `pi4_release_gate_v1`, which requires the same real Raspberry Pi 4 and same release across idle, training, Correct and training+Correct traces. CI/laptop profiles are intentionally rejected as Pi evidence.
- Gate local status responsiveness, Correct history responsiveness, `event_to_intent` p95, training-vs-idle realtime degradation, status failures, one-heavy-worker concurrency, combined CPU/RSS, free memory and temperature.
- Default performance targets are Correct/status p95 <= 500 ms, status/Correct p99 <= 1000 ms, `event_to_intent` p95 <= 500 ms and no more than 2x realtime p95 degradation during training.
- Require actual training evidence in the training scenarios and actual HA event traffic for realtime latency evidence. Missing evidence is `inconclusive`, never a synthetic pass.
- Add the packaged evaluator CLI plus deterministic Stage-9 gate regressions and a reproducible Pi validation procedure in `STAGE9_PI4_VALIDATION.md`.
- **Safety remains unchanged:** a green Stage-9 runtime gate only makes the release eligible for a later explicit canary-rollout change. Stage-7 Offline-RL Candidate promotion to physical Control remains blocked in 0.14.88.
- No Raspberry Pi 4 performance numbers are claimed by CI; real Pi measurements are required after installation.

# 0.14.87 — 2026-09-25

- Complete **Stage 8: final learning lifecycle + stateful Rebuild replay continuation** from the Tiny Neural Policy / Correct / Offline-RL master plan.
- Make incremental learning the normal path: compatible Manual Correct remains a bounded supervised TinyMLP Candidate fine-tune; Stage-7 trusted Automatic Correct remains a bounded conservative Offline-RL child Candidate update.
- Add an explicit lifecycle decision contract with `learning_path` and `rebuild_reason`. Ordinary compatible incremental learning carries no rebuild reason; first model build is identified separately as `initial_model_build`.
- Restrict full Rebuild to explicit/structural causes and expose a stable taxonomy including feature schema/mask changes, backend/action-space changes, corrupted/incompatible persisted models, major drift, repeated incremental failure, feedback-history retraction and explicit manual Rebuild.
- Persist structural causes when agent configuration invalidates a model. Changing selected inputs records `feature_mask_change`; changing the action range records `action_space_change`.
- Propagate `rebuild_reason` through TrainingQueue admission, active/queued status, HTTP responses, HistoryManager diagnostics and lifecycle events while preserving compatibility with older extension/test adapters.
- Keep Candidate lineage rules unchanged. Correct/Rebuild never substitutes a different Live parent or resurrects a discarded Candidate.
- Preserve the Stage-5 Correct chart, multi-point correction batch and as-of reconstruction path; structural neural Correct continues to surface an explicit rebuild reason instead of silently rebuilding.
- Preserve Stage-7 safety: an Offline-RL result remains a Candidate routed through offline gate and Shadow. Stage-7 neural/RL promotion remains blocked until Stage 9 controlled rollout.
- Replace physical multi-hour chunk overlap with **stateful chunk continuation**. The logical overlap remains available for equivalent validation semantics, while already-committed context history is not rescanned.
- Reconstruct only the minimum open-dwell boundary state from target history, then rebuild causal temporal features as-of the actual transition using the existing indexed temporal trackers. Rows at or after the boundary are excluded from the seed to prevent future leakage.
- Preserve Recorder coverage reuse, tail refresh, replay RAM caches, onset/persistence cursor separation, provenance filtering and the one-heavy-training-job contract.
- Add replay diagnostics for logical hours, unique hours scanned, overlap hours avoided, target-only continuation seed rows and seed agents.
- Add deterministic equivalence regressions for effective target-transition selection, duplicate states and exact boundary handling, plus lifecycle tests for incremental/default paths and structural reasons.
- Add a packaged Stage-8 synthetic replay benchmark comparing legacy overlap rows with stateful forward scanning + target-only boundary seed. It is a repository/CI cost probe, not a Raspberry Pi 4 performance claim.
- Raspberry Pi 4 wall-clock, CPU/RSS, websocket responsiveness and full-system profiling remain deliberately deferred to **Stage 9**.

# 0.14.86 — 2026-09-25

- Continue the Tiny Neural Policy / Correct / Offline-RL master plan with **Stage 7: conservative Offline-RL Candidate**.
- Consume **only trusted Stage-6 Automatic Correct experiences**. Unknown/rejected outcomes, lack-of-override evidence and incompatible feature-mask rows are excluded from RL training.
- Add a small pure-Python reward-weighted policy-improvement trainer for the existing TinyMLP instead of introducing Torch/TensorFlow or a second heavyweight value network.
- Keep the exact parent TinyMLP immutable. The child update is bounded by sample/epoch/batch caps, reward and advantage clipping, gradient clipping, early stopping, KL regularization to the parent, parameter-distance regularization and a hard parent-relative-L2 projection.
- Treat Manual Correct as the stronger direct signal. Usable Stage-5 timestamp+desired-action samples for the selected parent are replayed every RL update with an explicit higher supervised weight.
- Require repeated trusted support before an action is considered RL-supported. Offline gates block material probability lift or a new argmax on unsupported actions.
- Use a chronological untouched holdout for the offline gate. Record average trusted reward, parent-relative logged-action reward proxy, estimated reward gain, effective sample-size proxy, parent action agreement, mean/max total-variation drift, unseen-context rate, per-action reward/Q proxy calibration, regression count and training time.
- Explicitly label reward evaluation as a **parent-relative logged-action proxy, not unbiased IPS/OPE** because Stage 6 did not persist the full behavior propensity of the Ridge controller.
- Route Stage-7 output through the existing lifecycle: **Offline RL → child Candidate → offline gate → ordinary TinyMLP Shadow A/B**. No RL result can go directly to Active/Live/Control.
- Add an explicit **Offline RL** generation action for both Live and Candidate parents. The action first shows readiness: trusted/compatible samples, train/holdout split, Manual Correct anchors and action support. Insufficient evidence is rejected before a child is created.
- Preserve direct-parent lineage. A parent model/checksum change or new manual feedback during RL invalidates the pending publication; the child is requeued instead of publishing a stale update.
- Keep one heavy learning slot. The gradient loop + offline evaluation run in a clean low-priority isolated Python worker with bounded RAM/wall time; the realtime parent only validates lineage/checksums and publishes the returned artifact.
- Keep promotion blocked for neural/RL Candidates in Stage 7. Diagnostics now report an explicit `offline_rl_stage7_shadow_only` veto rather than mislabelling an RL Candidate as Stage 4.
- Add Candidate diagnostics for RL run status, trusted/train/holdout samples, reward-gain proxy, parent agreement, action drift and unseen-context rate.
- Add regressions for immutable parent, hard distance limit, Manual Correct dominance over conflicting reward, unsupported-action gating, required comparison metrics, trusted-only Stage-6 input, full Candidate lineage/build/persistence, existing neural Shadow routing and no-online-exploration/no-dispatch source boundaries.
- Add a packaged synthetic Stage-7 Offline-RL benchmark; host metrics are informational and must not be presented as Raspberry Pi 4 or real-world reward-performance measurements.
- Green Stage-7 CI benchmark on the final implementation: 192 train / 64 chronological holdout samples, 2 Manual Correct anchors, 3.45 s host update wall time, parent relative-L2 0.00339, holdout reward-gain proxy +0.0420, 100% parent action agreement, mean/max action drift 0.00363/0.01489 TV, zero unsupported new argmax, zero logged-action regressions, Manual Correct fit 2/2 and trained inference p95 418 µs. These are synthetic GitHub-runner measurements; they are not Pi4 results and the reward metric is not unbiased OPE.
- Green CI benchmark on the 0.14.86 implementation: 192 train + 64 holdout trusted-reward rows, 2 Manual Correct anchors, holdout reward-gain proxy +0.0420, 100% parent action agreement, mean TV drift 0.00363, relative-L2 distance 0.00339, zero unsupported argmax/regressions, ~2.90 s trainer / 3.45 s end-to-end benchmark, ~320 KiB RSS delta and 418 µs inference p95 on the GitHub Linux runner. These are synthetic host-local metrics, not Raspberry Pi 4 measurements and not evidence of real-world energy/reward improvement.

# 0.14.85 — 2026-09-24

- Continue the Tiny Neural Policy / Correct / Offline-RL master plan with **Stage 6: trusted Automatic Correct outcome/reward pipeline only**.
- Keep Manual Correct completely separate: the existing chart, selected timestamps, explicit desired action, durable Correct operation batches and Stage-5 supervised neural fine-tune are unchanged. Teaching intents are not inserted into the Automatic Correct reward buffer.
- Add a durable `automatic_reward_experiences` journal with one-resolution-per-decision/trial semantics. Each row records the exact agent/generation, decision/trial ID, action/value/time, observation window, target/area, Stage-2 observation + feature mask, prediction inputs, background dependencies, outcome/reward sources, proposed/trusted reward, confidence, attribution reason, source event/origin/reliability and unknown/rejected reason.
- Capture the causal Stage-2 observation at accepted action time. Restart-interrupted pending windows become `unknown`; current/future state is never retroactively fabricated into a past action.
- Trust explicit user reversal only when durable provenance proves an exact-target user/user-intent event inside the observation window.
- Trust presence outcomes only when a verified binary/tracker source belongs to the target area, changed inside the exact observation window and meets the configured confidence/source-reliability thresholds. Experiment rewards are restricted further to that trial's explicit `outcome_sources`.
- Reject cross-area presence attribution. Unrelated binary helpers, missing area mapping, unverifiable user events and other weak evidence never become trusted reward.
- Treat **lack of override as unknown**, even though the legacy preference reward proposed a small positive value. Absence of an expected presence event also remains unknown until a later contract can prove continuous sensor coverage/reliability for the whole observation window.
- **Disable scalar reward learning in Stage 6.** Ordinary delayed Automatic Correct rewards no longer call the legacy live-feedback `policy.update` / model-save / feedback path. The existing explicit manual-demonstration supervised path remains separate and intact.
- Keep experiment/Teaching lifecycle compatibility where those paths already have their own explicit contracts; Stage 6 does not reinterpret their user labels as generic scalar reward.
- Add RAM-backed per-agent Automatic Correct summary diagnostics to the ordinary runtime payload, plus an explicit read-only `GET /api/agents/{agent_id}/automatic-correct` audit endpoint for recent full experiences.
- Add agent-card diagnostics for outcome, proposed/trusted reward, source, confidence/reliability, attribution, trial/decision ID, action timestamp, observation window, buffer counts and unknown/rejected reason without adding a new UI poller.
- Keep the physical authority boundary unchanged: Stage 6 creates no ActionIntent, never calls Executor for dispatch and has no direct Home Assistant service path.
- Add functional regressions for deduplication, restart recovery, weak acceptance, exact-target reversal, same-area presence, cross-room false attribution, unrelated binary sources, missing area mapping, trial outcome-source isolation, Stage-2 action snapshot capture, Manual/Automatic Correct separation and the no-reward-learning boundary.
- Add a packaged synthetic Automatic Correct journal benchmark. Host SQLite timings are informational and must not be presented as Raspberry Pi 4 measurements.
- Green CI benchmark on the 0.14.85 implementation: 512 durable rows (128 trusted / 384 unknown), insert p95 1.67 ms, resolve p95 1.97 ms, RAM summary p95 2.24 µs and ~436 KiB RSS delta on the GitHub Linux runner. These are host-local synthetic measurements, not Raspberry Pi 4 acceptance numbers.

# 0.14.84 — 2026-09-24

- Continue the Tiny Neural Policy / Correct / Offline-RL master plan with **Stage 5: existing chart-based Manual Correct + incremental supervised Tiny MLP fine-tune**.
- Preserve the existing Agent/Candidate Correct chart and generation-aware selected-point workflow. No new generic reward button or neural-only Correct UX is introduced.
- Route Correct through the new neural path only when the exact selected direct-parent Candidate genuinely runs a trained, tournament-selected Tiny MLP with a passed offline gate. Ridge/current-backend Correct remains on the established conservative snapshot path.
- Reconstruct every selected neural Correct point from the source model's exact persisted Stage-2 feature mask using causal SQLite historical replay. Missing source features make the point unusable and auditable; current/future state is never fabricated into an old timestamp.
- Persist one correction batch for all selected points with source generation/model checksum, backend, schema/mask, original observed decision, desired decision, as-of observation and unusable reason.
- Reuse the durable `request_id` from Apply Correct as the operation/batch ID. Multiple points submitted together remain one operation; later Correct operations on the same generation contain only newly added/edited labels, while older explicit Correct labels are retained as higher-priority replay protection.
- Make Correct operation recovery idempotent: a replayed durable request returns the already-created child Candidate, and a requeued neural fine-tune safely reuses the same batch ID without duplicate rows or stale sample audit.
- Fine-tune an exact cloned neural parent with a bounded configurable 20–30% correction / 70–80% historical replay mixture, small learning rate, bounded epochs, early stopping, L2 and gradient clipping.
- Protect against catastrophic forgetting with corrected-point fit, untouched historical holdout, parent/child action agreement, regression count/fraction, nearby-context agreement and parent-parameter-distance gates.
- Keep the parent neural model immutable. Failed gates block the child Candidate and preserve the parent; stale feedback requeues the child before publication.
- Structural incompatibility (mask/schema/source revision/model) explicitly switches the Correct path to Full Rebuild and records a visible rebuild reason instead of silently falling back to another backend.
- A passed neural Correct result remains a Candidate in Shadow. Stage 5 still adds no neural Live/Control authority, ActionIntent shortcut, Executor call or direct Home Assistant service path.
- Expose Candidate diagnostics for policy backend, Correct path, Rebuild reason, parent agreement and neural parent distance.
- Add Stage-5 functional regressions for bounded correction weighting, immutable parent, drift gate, disabled online reward updates, causal as-of reconstruction and missing-source audit semantics.
- Add a packaged synthetic incremental-Correct benchmark; host timing is informational and must be rerun on Raspberry Pi for target-class validation.

# 0.14.83 — 2026-09-24

- Continue the Tiny Neural Policy / Correct / Offline-RL master plan with **Stage 4: bounded offline supervised Tiny MLP training + neutral Ridge↔MLP tournament**.
- Train Tiny MLP only inside explicit historical Train/Rebuild benchmark passes. The neural backend still rejects `update(... reward ...)`; no live reward learning or online neural mutation is introduced.
- Reconstruct MLP inputs from the same causal Stage-2 observation mask on the existing historical replay cursors. No second Recorder/history scan is added.
- Use accepted historical target behaviour as supervised imitation labels. Own-command acknowledgements remain excluded by the existing provenance boundary. Onset/persistence samples stay training-only; the chronological validation slice remains untouched until scoring.
- Keep resource use bounded for Raspberry Pi: deterministic training/holdout caps, tiny mini-batches, bounded epochs, L2 regularization, global gradient clipping, early stopping and cooperative `TRAINING_BUDGET` checkpoints.
- Add train-only input normalization persisted with the model. Runtime inference uses the persisted normalization and continues to use the existing action abstraction for binary/discrete/setpoint agents.
- Compare Ridge and MLP on the **same held-out onset rows and the same recorded-behaviour metric**. MLP can win only with identical holdout cardinality, required class coverage, the existing Candidate benchmark threshold, resource gates and a strict positive score gain. A tie stays with Ridge.
- Persist the supervised neural artifact separately from `rl_models`: model+checksum, exact Stage-2 mask, source Ridge revision, trainer report, tournament result and selected backend. Ridge remains the authoritative Live policy.
- Isolated training workers return the neural artifact to the realtime parent. The parent publishes it only after the existing job-id, agent-fingerprint and runtime-topology stale-result checks have passed.
- A tournament-winning MLP may drive **Candidate observed Shadow A/B only after the existing Candidate offline gate passes**. Missing/failed selected-neural inference remains an evidence gap instead of silently falling back to Ridge.
- Neural Candidate Shadow still creates no `ActionIntent`, never invokes Executor and has no Home Assistant service-dispatch path.
- Stage-4 neural winners are explicitly **not promotable to Live/Control yet**. This prevents a misleading promotion that would otherwise copy the Candidate's Ridge baseline while its A/B evidence came from MLP. Neural Live/Control authority remains a later-stage capability.
- Discard/prune cleanup retires the Candidate's isolated neural artifact together with the Candidate lifecycle.
- Existing Ridge learning, Correct behaviour, reward semantics, Candidate offline gate, paired Shadow evidence, `ActionIntent`, Executor and physical Home Assistant control remain unchanged outside the explicit neural Candidate Shadow branch.
- Stage-4 regression suite: **1223 tests**. Synthetic GitHub-host trainer probe (`96→32→16→2`, 3666 parameters, 384 train / 160 holdout, 8 epochs) completed in ~4.24 s, added ~392 KB RSS, serialized to ~81.5 KB, reached 94.27% held-out balanced accuracy and ~0.408 ms trained-inference p95. These are host-local CI measurements, not Raspberry Pi 4 results.

# 0.14.82 — 2026-09-24

- Continue the Tiny Neural Policy / Correct / Offline-RL master plan with **Stage 3: real tiny MLP inference + persistence in Shadow only**.
- Add a first-class `tiny_mlp` policy backend with deterministic initialization, configurable `selected_features -> 32 -> 16 -> actions` layers, float32 parameter storage, forward inference, common model checksum/version envelope and strict observation schema/mask/feature-order validation.
- Keep neural training deliberately disabled in this release. `TinyMLPBackend.update()` fails closed; persisted Stage-3 models carry `trained=false` and zero training samples. Historical supervised training and Ridge/MLP tournament remain Stage 4 work.
- Reuse the Stage-2 deterministic observation mask and semantic live observation vector. The neural output selects only from the existing policy action values, so binary/discrete targets and continuous/setpoint action bins use the current action abstraction without a new actuator path.
- Add an isolated `TinyMLPShadowService` with its own additive SQLite persistence. Compatible models reload after restart with the same checksum/model revision; schema, mask, feature-order, action-space or architecture changes reinitialize only the untrained Shadow copy and never rebuild/replace the Live or Candidate policy.
- Install the observer after the established final runtime composition. The authoritative Ridge/current `process_agent` path runs first and its return value is preserved exactly. Tiny MLP runs only when the agent mode is literally `shadow`; **Control executes zero neural inference calls**.
- Neural errors are fail-open for the existing Ridge path and are surfaced only as `runtime.tiny_mlp_shadow` diagnostics. The service imports neither `ActionIntent` nor Executor/HA dispatch code and has `dispatch_capability=false`, `physical_authority=false`.
- Preserve Manual Correct, Candidate lifecycle/lineage, rewards, historical training, `ActionIntent`, Executor and physical Home Assistant control unchanged.
- Add packaged `tiny_mlp_benchmark.py` plus CI probe for one, 20 and 50 loaded models, repeated p50/p95/p99 inference, current RSS, startup/load, serialization/deserialization and restart persistence. CI measurements are synthetic host timings, not Raspberry Pi 4 results.
- Add Stage-3 regressions for deterministic initialization, binary/setpoint action mapping, persistence/restart, checksum/schema/mask/order guards, Candidate-ID persistence, Control exclusion and fail-open Shadow observation.
- Full PR CI: **1213 tests** pass on Python 3.11 and 3.13; Docker image/smoke and all configured benchmark steps pass. GitHub Linux host MLP probe: p95 ~0.332 ms for 96→32→16→2; 50 loaded models add ~1.28 MB RSS versus probe baseline. These are not Raspberry Pi 4 measurements.

# 0.14.81 — 2026-09-24

- Resume the Tiny Neural Policy / Correct / Offline-RL master plan with **Stage 2: global semantic observation space + deterministic per-agent feature selection**. The version number is shifted forward because 0.14.76–0.14.80 were used for the completed Raspberry Pi performance series.
- Add observation schema v1: every eligible Home Assistant entity contributes six semantic descriptors — current value, 1 s / 10 s / 60 s deltas, freshness and explicit availability — plus cyclic time descriptors and the existing target-area Home Intelligence forecast descriptors.
- Build an installation-wide possible feature catalog after the existing hard controllable/electrical exclusions. At the plan reference scale of 775 eligible entities this is roughly 4.6k semantic feature candidates without feeding all of them to one policy.
- Reuse the established `select_context_entities` logic rather than creating a second context-ranking system. Per-agent masks keep deterministic entity rank plus feature-level score/reason and preserve entity/area provenance.
- Bound selected feature masks to 16–128 with a 96-feature ordinary ceiling. Existing fast-reactive entity selection remains authoritative and unchanged; a small regression fixture selects 59 semantic features while the 775-entity scale fixture selects 65, both inside the Stage-2 target range.
- Add independent observation schema ID and selected mask ID. Import detects incompatible schema, mask version, feature ordering/payload or checksum and reports NEEDS_RETRAIN semantics.
- Add causal `observation_as_of(...)` reconstruction for future Correct/MLP training. Historical trackers are repositioned to the requested timestamp and their causal state/history is authoritative; future state and future-by-receipt observations remain invisible.
- Encode unavailable/unknown sources explicitly with `available=0` plus missing-feature IDs/count rather than silently interpreting unavailable data as a valid zero measurement.
- Keep the entire Stage-2 selector **off the active Ridge event→intent path**. DiagonalLinUCB continues using its existing ExplicitFeatureSchema/features unchanged; Stage-2 masks are lazily materialized by diagnostics/UI or future backends.
- Expose global/selected/missing feature counts, selected IDs/names, selection score/reason, schema ID, mask ID and feature provenance in agent diagnostics/UI.
- Add a 775-entity CI benchmark. It gates exact global cardinality, 32–128 selected features, zero Stage-2 selector calls during active Ridge features→predict and exact semantic parity before/after mask materialization; p50/p95/p99 and comparable p95 ratio are reported for the >10% investigation gate.
- Final Stage-2 functional run #3418: 4661 possible semantic features → 65 selected; selector materialization 19.80 ms p50 / 21.28 ms p95 outside the hot path; Ridge p95 170.14 µs before vs 168.11 µs after mask materialization (ratio 0.988), with zero selector calls and exact decision parity. These are GitHub-host synthetic timings, not Raspberry Pi 4 measurements.
- Add historical causality regressions for direct timestamps, rewinds, unavailable sources and received-time visibility; the existing full Correct/Candidate regression suite remains unchanged.
- No neural model is active yet. No changes to policy/reward semantics, Correct learning, Candidate lifecycle/promotion, ActionIntent, Executor or physical Home Assistant control.
- Full suite target: **1202 tests**.

# 0.14.80 — 2026-09-24

- Isolate CPU-heavy historical replay from realtime Home Assistant/HTTP/control work in **one clean Python worker process**. Recorder refresh, queue admission, lifecycle and physical control remain parent-owned.
- Preserve the one-heavy-job contract: there is never one training process per device. The existing TrainingQueue/HEAVY_JOBS serialization remains authoritative.
- Add versioned training-job descriptor v1 with app/training revision, agent-config fingerprint, structural runtime-context fingerprint, fixed history bounds, state/registry snapshot, automation hints, schema seed, options snapshot and checksum.
- Guard model publication immediately before the atomic SQLite upsert, then revalidate agent configuration and runtime topology/options in the parent before exposing the result to realtime caches.
- Make aborted/stale worker chunks rollback-safe: restore the exact pre-chunk model, lifecycle, benchmark and cursor, then discard historical experiences newer than the restored model watermark.
- Use keyset-paged `entity_history` reads in isolated workers so a long replay does not keep one SQLite read transaction/WAL snapshot open for the whole scan. Normal runtime keeps the previous high-throughput iterator.
- Supervise worker resources without assuming cgroups: best-effort niceness, configurable RSS ceiling, CPU/RSS/read/write diagnostics, bounded polling and terminate→kill fallback.
- Keep the existing cooperative replay budget inside the child process. The process boundary removes Python GIL/allocator/GC competition from the realtime parent; the budget still prevents the child from consuming unlimited host resources.
- Ship `/app/pi_training_profile.py` in the add-on. It collects repeatable **idle / training / Correct / training+Correct / steady-events / burst** traces, including HTTP p50/p95/p99, available runtime latency/backlog metrics, training progress, RSS, process CPU and I/O.
- CPU normalization is explicit: `one_core_percent = CPU_seconds / wall_seconds × 100`; `host_percent = one_core_percent / logical_cpu_count`.
- CI covers a real clean-subprocess historical replay plus worker cancellation/rollback, stale-config rejection, restart lifecycle, guarded model publication and keyset-reader parity.
- No Raspberry Pi 4 performance numbers are claimed from GitHub runners. The packaged profiler is the acceptance tool for before/after measurements on the actual device.
- No changes to replay rewards/order, policy semantics, Correct, Candidate lineage/promotion, ActionIntent, Executor or physical Home Assistant service dispatch.
- Parent-crash protection records parent PID/start-token in the versioned job, stops orphaned replay through a watchdog and refuses model publication after parent loss.
- A configuration change during training forces the newer agent configuration to `needs_retrain` after stale-result rollback; stale qualification can never survive.
- Full suite target: **1189 tests**.

# 0.14.79 — 2026-09-24

- Re-profile historical RoomBelief reconstruction after the 0.14.76–0.14.78 performance fixes and add a bounded shared context layer only around exact duplicate causal requests.
- Share immutable exact-as-of RoomBelief/AdaptivePresence snapshots between the existing onset and persistence replay cursors within one heavy training job. Mutable cursor/model state is never shared.
- Make cache identity causal and versioned: exact timestamp, event/received-time watermarks, visible-row fingerprint, topology revision, semantic-reliability revision, home-checkpoint identity, RoomBelief/AdaptivePresence versions and the active policy/schema/feature contract.
- Keep the established 30-second historical rebuild as the authoritative miss path. Rewind, late received data, future-by-receipt data, topology changes and feature-contract changes are covered by regression tests.
- Bound Raspberry Pi memory by default to 32 snapshots / 8192 source-weighted units; the LRU exists only for one training job and is never persisted.
- Expose cache hits, misses, evictions and actual RoomBelief render executions through historical-training diagnostics.
- Add a deterministic profile matrix for 1/5/20 synthetic agent timelines, 8/32/64 context sensors and static/dynamic histories. CI gates exact forecast parity and render-work reduction; process CPU time is measured directly; wall-clock/RSS remain informational host measurements.
- Final CI profile: all cases preserve exact forecast parity and reduce duplicate RoomBelief renders by 25%. In the representative dynamic 20-agent/64-sensor case CPU falls from 5.7755 s to 4.4976 s (1.284× speedup), with 25% cache hit-rate and 4768/8192 cache units used. Tiny fully-static cases remain sub-second and can be neutral/slightly slower due to snapshot bookkeeping.
- No changes to replay rewards, learned samples, policy update order, Candidate lineage, Correct, ActionIntent, Executor or physical HA dispatch.
- Full suite target: 1176 tests.

# 0.14.78 — 2026-09-24

- Remove the final synchronous SQLite write found on the normal Home Assistant websocket ACK path: own-command `ack_event_id` / `ack_time` provenance now queues in RAM and is persisted by the existing background provenance writer.
- Keep physical-control semantics unchanged. Command reservation/dispatch, Engine pending acknowledgement, reward timing, ActionIntent and Executor safety remain synchronous exactly as before; only audit metadata persistence is deferred.
- Batch queued acknowledgement updates in one transaction. Repeated ACKs preserve the legacy contract: the latest non-null event id wins while the first acknowledgement timestamp remains authoritative.
- Preserve strong explicit-read behavior: reading a provenance decision forces that decision's queued ACK durable before the SQLite SELECT.
- Add an integration regression and CI benchmark that hold the shared Store writer mutex in another thread while delivering a real own-command `state_changed`; websocket ingest must finish without waiting for SQLite and the ACK must still become durable after the lock is released.
- Extend provenance diagnostics with queued/coalesced/flushed ACK counts.
- No changes to replay order, observations, rewards, policies, Candidate lineage, promotion, Correct, ActionIntent, Executor or Home Assistant service dispatch.
- Full suite target: 1167 tests.

# 0.14.77 — 2026-09-24

- Change cooperative training preemption from **checkpoint-scoped** to **burst-epoch-scoped** scheduling. A heavy worker now performs one strict scheduler yield per realtime/user-priority burst and then returns to the normal bounded work/sleep quantum.
- Repeated `state_changed` / realtime-inference extensions inside the same bounded burst no longer multiply sleep by the number of fine-grained replay checkpoints.
- Preserve the existing 450 ms realtime burst cap and 200 ms cooldown. A new burst still forces an immediate yield; an explicit user action such as Correct can escalate an active realtime epoch once.
- Keep the existing 35 ms continuous-work bound and 65% cooperative duty target after the strict realtime yield, so sustained sensor traffic cannot starve historical learning.
- Add deterministic virtual-time coverage for 64 micro-checkpoints, burst extension, realtime→Correct escalation and steady 2/4/10 Hz HA traffic.
- Add `tools/benchmark_training_qos.py` to CI. The benchmark reports wall-clock work/sleep accounting and does not claim Linux process CPU utilisation.
- Preserve the persisted `training_cpu_duty_cycle` option name for compatibility, but expose diagnostics/UI as a **cooperative wall-clock duty target** rather than a CPU-utilisation measurement.
- No changes to replay ordering, samples, rewards, models, Candidate lineage, ActionIntent, Executor or physical-control semantics.

# 0.14.76 — 2026-09-24

- Remove the quadratic Live Correct chart lookup path. Observed Desired and physical Current are now merged in one forward pass over the already ordered streams instead of rebuilding complete timestamp lists for every rendered point.
- Preserve the exact Correct evidence contract: recorded runtime Desired only, physical Current from entity history, direct-parent Candidate comparison, pre-range seed behavior, duplicate timestamp resolution, `None` values and the existing 95-second stale-gap boundary.
- Make ordinary Correct/Teach history reads read-only. They no longer force `Teaching.flush()` and Live Correct no longer executes schema DDL while serving a chart or point request.
- Merge persisted `decision_history` rows with bounded RAM snapshots taken before and after the SQLite SELECT so a concurrent background flush cannot make a freshly observed Desired disappear from the interactive chart.
- Pre-index repeated Teach observed-history as-of lookups as well, keeping the older Teach/point inspector semantics unchanged.
- Add deterministic 1k/2k/4k/8k scaling coverage. CI gates exact old/new output parity plus linear optimized work growth; wall-clock speedup and chart JSON size are reported but are not brittle pass/fail thresholds.
- Add regressions proving Correct reads complete while the shared Python Store writer mutex is held and that interactive reads never invoke `Teaching.flush()`.
- No policy, reward, model, Candidate lineage, ActionIntent, Executor or physical-control semantics change.

# 0.14.75 — 2026-09-24

- Lock the current chart-based **Correct** and Candidate direct-parent behavior as permanent Stage-1 regression contracts; no Correct UX, reward, promotion, ActionIntent or Executor semantics are changed.
- Add a common persisted policy envelope with explicit `policy_backend`, backend/model-format version, feature-schema ID, feature-mask ID and model checksum.
- Preserve all legacy persisted agents: models without an explicit backend remain `diagonal_linucb` and do not require Rebuild merely because the envelope was introduced.
- Reject explicit unknown or unavailable backend identifiers instead of silently loading their payload as the current policy family.
- Align the existing Full Ridge shadow challenger with the same backend envelope while keeping it non-production and non-dispatching.
- Reserve `tiny_mlp` in backend capabilities for later stages only. 0.14.75 contains no neural training, neural inference in the product path or neural physical authority.
- Add a diagnostics-only Pi-oriented backend benchmark harness for inference p50/p95/p99, bounded update cost, model memory, 20/50-agent projections, serialization/deserialization and projected event→intent inference overhead.
- Add nine Stage-1 regression tests; full suite target: 1150 tests.

# 0.14.74 — 2026-09-23

- Fix the missing **Runtime debug log** section in Diagnostics & technical details.
- Explicitly serve `/runtime_debug_ui.js`; 0.14.73 referenced the asset from `index.html` but the base HTTP handler returned 404.
- Add the opt-in runtime debug summary to the hot `/api/status` payload so ON/OFF state, buffer count, event → intent p95 and active spans remain visible across normal UI refreshes.
- Keep the diagnostics read path RAM-only and avoid Engine.status()/historical aggregate reads.
- Add regression coverage for both static asset delivery and hot-status integration.
- Full suite target: 1141 tests.

# 0.14.73 — 2026-09-23

- Speed up explicit historical training without shortening the 7-day training window or changing reward/benchmark semantics.
- Raise the explicit training cooperative CPU duty cycle from 55% to 65% while retaining the 35 ms maximum continuous-work slice.
- Replace the old long realtime blackout behavior with reason-aware QoS: HA state changes request 300 ms priority, inference requests 400 ms, and repeated realtime traffic is capped to a 450 ms burst followed by a 200 ms cooldown in which replay can run.
- Keep longer interactive priority for explicit user workflows such as Correct; only HA realtime reasons use the shorter cap.
- Increase historical-experience persistence batches from 64 to 128 rows to reduce SQLite/WAL commit overhead.
- Increase the shared replay RAM query cache from 8,192 to 16,384 rows so the onset and persistence temporal trackers reuse more SQLite results instead of evicting them.
- Reuse successful Recorder coverage in RAM across repeated Rebuilds: an identical seven-day target/context range is skipped, while later runs fetch only a 30-minute overlap plus the new tail. A timed-out/skipped Recorder slice is never marked as complete coverage.
- Migrate only previously shipped defaults; explicit custom tuning remains authoritative.
- Add regression coverage for bounded realtime event storms and preservation of longer non-realtime interactive priority.
- Full suite target: 1139 tests.

# 0.14.72 — 2026-09-22

- Add an opt-in runtime debug logger inside **Diagnostics & technical details**.
- Show `event → intent p95` for the recent 60-second window and the retained telemetry window directly in Diagnostics.
- Add **Start debug log / Stop debug log** controls. Logging is disabled by default and stores only a bounded in-RAM ring buffer (4096 rows); it does not add disk writes or a new poller.
- While enabled, trace event passes, per-target/per-agent inference, Candidate lifecycle work and every shared `HEAVY_JOBS` owner. The panel surfaces currently active spans with age/thread/context.
- Log every `event_to_intent` latency sample together with the current recent p95 while debug logging is enabled. With logging disabled the hot observation path remains O(1).
- Add **Download log** beside the toggle. The JSON export includes the full bounded trace, active spans, telemetry snapshot, heavy-job owner, TrainingQueue state, Candidate-worker health, engine scheduler/realtime state and active thread list.
- Add seven regressions for disabled overhead, active-span visibility, event→intent logging, heavy-job tracing, API routes, UI controls and runtime instrumentation.
- Full suite target: 1132 tests.

# 0.14.71 — 2026-09-22

- Fix Candidate Correct failing during training with `ValueError: Stable correction base schema is incompatible`.
- Root cause: Stage-3 residual schema evolution tried to semantically remap a persisted stable base whose policy/schema version or feature dimensions did not match the current explicit feature contract. Rejecting the remap was correct for safety, but the exception incorrectly failed the whole Candidate.
- Detect incompatible stored correction bases before conservative Stage-3 fine-tuning and route the hidden Candidate through the existing historical `teach_rl` rebuild under the current schema.
- Preserve the Live parent unchanged while the compatibility rebuild runs; only the hidden Candidate is rebuilt.
- Keep old feature indexes isolated: no old numeric dimension is silently reinterpreted as a current semantic feature label.
- Run the normal full-rebuild offline gate after schema-upgrade training before future Shadow A/B can begin.
- Automatically recover the specific 0.14.70 failed Candidate on startup by requeuing it as `schema_upgrade_rebuild`, preserving its lineage and Correct feedback.
- Add seven regressions for current/legacy schema compatibility, rebuild routing, immediate scheduling, startup recovery and offline-gate coverage. Full suite contains 1125 tests.

# 0.14.70 — 2026-09-22

- Fix Candidate generations that remain indefinitely at `Queued · queue #1` while diagnostics show `Heavy job: idle`.
- The Candidate card's `candidate_worker` queue is a lifecycle queue owned by `AgentCandidateManager`, not the normal historical TrainingQueue. In 0.14.69 an unhandled lifecycle/decorator/SQLite exception could terminate that scheduler thread while the durable Candidate row remained queued, leaving no heavy job to execute it.
- Make the Candidate lifecycle scheduler resilient: per-Candidate, list and maintenance exceptions are caught and retried instead of terminating the scheduler for all generations.
- Add a single-flight recovery worker if the primary Candidate scheduler thread exits unexpectedly. Enqueue and Candidate status reads verify scheduler liveness, so an already-persisted queued Candidate can recover without requiring a new correction.
- Expose Candidate worker health in the Candidate status contract: alive state, heartbeat age, last error, error count and restart count.
- Throttle repeated identical worker-error events to one event per 30 seconds to avoid log storms while retaining diagnostics.
- Keep Candidate Correct semantics unchanged: exact direct-parent snapshot -> conservative correction/schema stages -> offline gate -> future Shadow A/B. No changes to ActionIntent, Executor or Home Assistant service dispatch.
- Add five 0.14.70 worker-recovery regressions, including a functional scheduler-fault recovery test; full suite contains 1118 tests.

# 0.14.69 — 2026-09-22

- Fix delayed Candidate-card hydration when Home Assistant Ingress transiently reports the iframe as hidden during initial navigation.
- Allow the first Candidate lifecycle read even while hidden, and refresh immediately when the document becomes visible or the first Live-agent payload confirms runtime readiness.
- Preserve the existing 4 s Candidate lifecycle cadence; the hydration fix is event-driven and does not add a faster database poller.
- Make Correct Candidate work explicit instead of appearing as an unexplained `Queued` state while the normal historical TrainingQueue is empty.
- Surface a dedicated `candidate_worker` queue contract with queued/active state, queue position, blocker and phase-aware progress on the existing Candidate card.
- Serialize Conservative Correct with the shared `HEAVY_JOBS` gate so snapshot fine-tune/offline scoring cannot silently compete with historical training or discovery.
- Add cooperative `TRAINING_BUDGET` checkpoints across Correct context reconstruction, supervised updates and offline scoring to preserve Ingress/realtime CPU priority.
- Keep Correct semantics unchanged: exact Live snapshot → conservative supervised fine-tune → same-row offline regression gate → future Shadow A/B; no destructive historical rebuild is introduced.
- No reward, schema, parent-model, promotion, ActionIntent, Executor or Home Assistant service semantics are changed.
- Add six regressions covering initial Candidate hydration plus Candidate Correct queue visibility, shared heavy-work serialization and cooperative CPU budgeting.
- Full suite contains 1113 tests.

# 0.14.68 — 2026-09-22

- Fix a Candidate lifecycle deadlock where a hidden Candidate could already own a pending TrainingQueue job while its durable Candidate state remained `queued`.
- Previously, `_start_build()` returned immediately for any non-null `queue.status_for(candidate_id)`; that left the Candidate permanently queued, so Shadow comparison never started and the card stayed at `Future samples 0` / `No fresh Shadow observation yet`.
- Add explicit Candidate queue-claim semantics. Pending Teach jobs are adopted/upgraded atomically to `teach_rl` and the Candidate advances to `building`.
- Replace incompatible not-yet-active pending jobs for feedback-undo full rebuild and Autonomous continuation with the exact requested lifecycle job.
- Never mutate an already-active external/history job. The Candidate manager waits for it to finish and claims the intended build on the next poll.
- Apply the same lifecycle repair to Correct/Teach, feedback-undo full rebuild and Autonomous continuation.
- Preserve the safety contract that `queued` and `building` Candidate weights do not run Shadow inference. Fresh Candidate events begin only after a successful build reaches `comparing` / `ready`.
- No event→intent, ActionIntent or Executor changes.
- Add four regressions covering pending-job adoption, active-job waiting/recovery, full-rebuild replacement and the Shadow-state boundary.
- Full suite contains 1107 tests.

# 0.14.67 — 2026-09-21

- Add bounded, Correct-driven semantic reliability for local presence evidence. The calibrator learns only from explicit binary Correct supervision and remains separate from the action policy.
- Admit humidity, temperature and moisture as RELIABILITY_CONTEXT with zero occupancy authority. Environmental context never means occupied/unoccupied by itself and no fixed “high humidity makes radar bad” rule is encoded.
- Require both-class Correct support and out-of-fold source correctness before learning any context-conditioned trust. Unsupported or ambiguous context stays exactly neutral at reliability factor 1.0.
- Learn local-sensor disagreement as an optional reliability context when Correct evidence shows that disagreement predicts source errors.
- Reliability context may only down-weight supported local evidence; it can never boost a source above its unconditioned quality.
- Keep transport and semantic reliability distinct: communication_reliability still represents source availability/transport health, while semantic_reliability represents Correct-driven evidence trust.
- Apply the same semantic reliability profile in live RoomBelief fusion, Adaptive Presence and causal historical replay.
- Persist bounded reliability calibration separately under `semantic_reliability_v1`; Candidate lineage copies are deduplicated by stable supervision_event_id and cannot inflate support.
- Record each Correct reliability-calibration result in the durable broad-context metadata and surface the latest result in Correct Learning Debug.
- Keep runtime evaluation RAM-only over already-materialized area sources: no SQLite access, no whole-HA scan and no new broad event→intent fanout.
- Restrict presence-reliability learning to fast binary/power agents; arbitrary climate or continuous-target Correct feedback cannot become occupancy supervision.
- Full suite contains 1103 tests.

# 0.14.66 — 2026-09-21

- Recognize real mmWave local signals including `stationary_energy`, still/move energy and explicit stationary/moving target-distance channels.
- Treat radar energy as local raw activity evidence: it improves RoomBelief observability and Adaptive Presence capability but never directly asserts room occupancy.
- Treat target-distance channels as nonoccupancy context only; they are never calibrated occupancy probabilities or direct virtual-presence raw sources.
- Share one target-relative evidence-role contract between RoomBelief and Correct broad snapshots: LOCAL_EVIDENCE, BOUNDARY_ARRIVAL_PRECURSOR, TRAJECTORY_CONTEXT and RELIABILITY_CONTEXT.
- Keep humidity/temperature in RELIABILITY_CONTEXT only, and keep remote PIR/radar activity as TRAJECTORY_CONTEXT rather than target-room occupancy truth.
- Support explicit `boundary_for` / `arrival_precursor_for` metadata as a short-lived runtime-only arrival prior. Boundary evidence can raise arrival probability but never `occupancy_now`, is not serialized, and is cleared with movement state after startup.
- Add sparse target-area → explicit-boundary-source event fanout so a mapped cross-room precursor wakes the target agent immediately without restoring whole-house presence fanout.
- Treat camera/detection/presence scores as raw non-probability evidence until independently calibrated; only explicitly probability-like sources retain probability semantics.
- Preserve automation thresholds such as bathroom >22 / <12 for 3 s as structural baseline metadata only; RoomBelief source semantics do not hardcode those thresholds.
- Keep Stage-5 reliability calibration out of this release. No new SQL/full scans are added to event→intent and Control/Executor authority is unchanged.
- Full suite contains 1093 tests.

# 0.14.65 — 2026-09-21

- Add residual-targeted Correct schema evolution after bounded Stage-2 margin repair leaves explicit supervision unresolved.
- Rank broad historical context against remaining Correct residuals with residual-weighted cross-validation instead of generic whole-home correlation.
- Limit automatic fast-target schema additions to LOCAL_EVIDENCE, BOUNDARY_ARRIVAL_PRECURSOR and TRAJECTORY_CONTEXT; humidity/temperature RELIABILITY_CONTEXT never becomes occupancy proof.
- Add at most two context entities per Candidate build, while preserving configured input filters and the normal schema-capacity limit.
- Support pre-0.14.63 Correct facts with bounded bulk historical as-of reconstruction.
- Migrate existing policy statistics by semantic feature label so shifted interaction slots cannot reinterpret old weights; initialize new feature slots from priors and reset calibration evidence.
- Rebuild both stable parent and schema-changed Candidate offline scores from the same raw entity_history timeline through SQLiteTemporalTracker + each policy's current policy.features, never legacy serialized feature indexes.
- Rebuild opposite-class stability anchors from the same raw historical feature contract after schema evolution.
- Refresh Candidate benchmark provenance from the schema-changed raw-history held-out replay before any later promotion.
- Report missing_context when no eligible discriminator exists or a bounded schema challenger still leaves any explicit supervision unresolved.
- Surface Missing context and automatic schema enrichment on the existing Candidate card without new polling.
- Keep all Stage-3 work on Candidate build paths; realtime event→intent inference and Executor authority are unchanged.
- Full suite contains 1083 tests.

# 0.14.64 — 2026-09-21

- Replace positive-only hard Correct repair with pairwise margin repair: reinforce Desired while explicitly penalizing the strongest competing wrong arm.
- Require a small positive decision margin, not only tie-broken class fit; publish before/after min/mean margins and unresolved supervision IDs.
- Bound repair by global rounds and per-supervision-event round budgets; stop early when fit/margin no longer improves.
- Re-check the originally selected opposite-class stability anchors after hard repair.
- Separate lineage parentage from optimization ancestry: audit/A-B parent stays unchanged, but Candidate weights restart from the newest retained offline-passed Candidate or Root Live.
- Never use an `offline_blocked` / failed Candidate as the next Correct weight base.
- Keep accumulated supervision flowing from the direct lineage parent after the weight reset.
- Expose correction base, margin diagnostics and stop reason through Candidate status and final runtime composition contracts.
- Keep all Stage-2 work on Candidate build paths; realtime event→intent inference and Executor authority are unchanged.
- Add deterministic reproduction showing 12 Desired-only updates cannot cross a strongly established wrong `DiagonalLinUCB` arm while pairwise repair does.
- Full suite contains 1065 tests.

# 0.14.63 — 2026-09-21

- Add a durable generation-independent `supervision_event_id` for Correct/Teach facts and deduplicate active Candidate training rows by supervision event instead of physical lineage copies.
- Capture one bounded historical broad-context snapshot after explicit feedback only, preserving existing target/actuator/electrical exclusions and keeping the event→intent hot path unchanged.
- Persist semantic context roles: `LOCAL_EVIDENCE`, `BOUNDARY_ARRIVAL_PRECURSOR`, `TRAJECTORY_CONTEXT`, `RELIABILITY_CONTEXT`; humidity/temperature remain reliability context rather than occupancy evidence.
- Store a fresh bounded as-of RoomBelief reconstruction plus source ages/recent deltas with each Correct context snapshot.
- Retain readable Home Assistant numeric-state thresholds, hold durations and action services as baseline metadata; automations remain structural priors, never ground truth.
- Add frozen-schema residual diagnostics: fit count/ratio, unresolved Correct supervision IDs and residual class distribution.
- Extend Correct Learning Debug with supervision-lineage counts and per-label broad context.
- Add Stage-1 regression tests for supervision deduplication, exclusions, semantic roles, baseline parsing, residual diagnostics and hot-path isolation.
- Full suite contains 1056 tests.

# 0.14.62 — 2026-09-21

- Fix full **Export debug** failing at the global 12 s GET safety timeout.
- Replace the long synchronous browser GET with a bounded asynchronous export job: POST starts the work, short GETs poll progress, and a final lightweight download endpoint serves the finished JSON.
- Keep the global 12 s read guard unchanged for normal UI/API reads; debug export no longer disables or weakens that protection.
- Allow only one heavy debug export at a time on low-power systems. Repeated clicks for the same agent attach to the existing job; another agent gets a clear conflict instead of starting competing history reconstruction.
- Keep finished reports in RAM for 10 minutes, then expire them automatically. No debug payload is persisted to SQLite.
- Show live progress directly on the button as `Exporting… N%`.
- Yield between reconstructed Correct points so realtime inference/Ingress can continue to make progress.
- Preserve all learning, Candidate, promotion, training and physical-control semantics.
- Full suite contains 1046 tests.

# 0.14.61 — 2026-09-21

- Fix **Export debug** doing nothing in 0.14.60.
- The button helper file was packaged and referenced by `index.html`, but the base HTTP handler did not expose `/debug_export_ui.js`; Ingress therefore returned 404 and the click handler was never installed.
- Serve `debug_export_ui.js` explicitly before runtime readiness gating, matching the other core static assets.
- Keep the 0.14.60 Correct-learning endpoint, export bounds and all learning/runtime semantics unchanged.
- Add a regression that fails if the helper is referenced by the UI but not reachable through the shipped HTTP handler.
- Full suite contains 1042 tests.

# 0.14.60 — 2026-09-21

- Add **Export debug** directly to every Live agent workflow card and every Candidate card.
- One click downloads the full bounded Correct-learning JSON: up to 256 Correct labels, ±120 s context windows and up to 768 raw rows per correction point.
- Keep the export single-flight per button with visible `Exporting…` / `Downloaded ✓` states so repeated clicks cannot launch overlapping historical reconstructions.
- Candidate export now accepts URL-encoded generation references such as `candidate:...` and resolves the same durable lineage as the Live root.
- Package and syntax-check the shared `debug_export_ui.js`; Candidate workflow buttons use responsive wrapping so the extra action does not compress the card.
- No policy, feedback, Candidate, promotion, training or physical-control semantics are changed.
- Add 5 release regressions; full validation suite contains 1041 tests.

# 0.14.59 — 2026-09-21

- Add a bounded, trusted-client-only Correct learning diagnostic endpoint at `/api/agents/{agent_id}/debug/correct-learning`.
- Summary mode exports Candidate lineage, retained model schemas, Correct labels, build/offline evidence, manual-feedback journal state, manual-context scores, Context Tournament state and current room evidence.
- `detail=full` adds per-label feature reconstruction, historical sensor-only comparison, cross-generation predictions and bounded raw context windows around each Correct point.
- Keep diagnostics observational: RoomBelief/AdaptivePresence reconstruction uses a cloned state and does not mutate production hysteresis, false-ON counters, Candidate state, policy weights or HA devices.
- Add hard bounds for labels, context entities, raw rows and history windows; full reconstruction remains opt-in.
- Add 4 regression/contract tests; full validation suite contains 1036 tests.

# 0.14.58 — 2026-09-21

- Fix newly created Correct/Teach Candidates that could remain permanently without Shadow events after build completion.
- Candidate-live polling could cache an empty generation set while the Candidate was still queued/building; synchronous Conservative Correct finishes inside `_start_build`, which previously did not invalidate that cache.
- Invalidate Candidate Shadow lifecycle caches after `_start_build` as well as asynchronous `_finish_build_if_ready`, so the newly observable Candidate joins passive HA-event and heartbeat inference immediately.
- Preserve observed-only Candidate history, offline promotion gates, reward semantics and physical-control isolation; this hotfix changes lifecycle cache visibility only.
- Add 0.14.58 regression coverage; full validation suite now contains 1032 tests.

# 0.14.57 — 2026-09-21

- Change user-visible **Discard** semantics to retire the whole unpromoted Candidate cycle. Hidden ancestor Candidates are no longer resumed after the visible leaf is discarded.
- Preserve discarded lineage metadata for audit, but remove its surrogate agents/models so future Correct/Change decision starts again from the current Live agent.
- Add a one-time migration that detects rollback-shaped branches left by older Discard behavior and retires them instead of allowing Gen 14/15-style resurrection after upgrade.
- Fix the exact Correct failure shown as `KeyError: 'candidate_id'`: deep-lineage enqueue may return a lineage status with `generation_id`, and workflow creation now accepts either status contract.
- Refresh the durable Live-parent model/config snapshot exactly when a new Candidate branches from Live. A Live agent rebuilt from scratch therefore cannot accidentally compare a new Candidate against stale lineage metadata.
- After Discard, the next Candidate cycle starts at **Gen 1** relative to the current Live generation. Full Rebuild remains a Live-agent reset and does not restore retired Candidates.
- Candidate physical-control isolation, paired-evidence gates, promotion checks and historical Correct labels are unchanged.

# 0.14.56 — 2026-09-21

- Fix the Live agent workflow action cache so a mode-only transition from Paused to Shadow immediately changes **Start Shadow** to **Pause Shadow**.
- Include `mode` in the action-render signature; previously the card header could already show SHADOW while the cached action row still reflected PAUSED.
- Add a regression reproducing the real path where training state remains PAUSED and only the agent mode changes.
- No training, policy, Candidate, reward, benchmark or physical-control semantics changed.

# 0.14.55 — 2026-09-21

- Fix Train/Rebuild semantics: **Train** now continues an already trained agent from its saved historical cursor and learns newly available data; it no longer resets a completed model just because progress is already 100%.
- Keep first-ever Train and schema-invalidated `needs_retrain` agents on the safe full-build path.
- Make **Rebuild** the only explicit action that clears the selected Live agent model, benchmark and cursor and replays local history from the beginning.
- Stop the Candidate HTTP layer from turning Live Rebuild into a hidden Candidate generation. After Candidate Discard, Rebuild now stays on the Live agent instead of making the discarded Candidate appear to return.
- Protect lineage integrity: while a current Candidate still exists, Live Rebuild returns a conflict asking the user to Discard or Promote it first.
- Keep full Candidate rebuilds for feedback-undo only; feedback corrections still preserve the immutable Live parent until promotion.

# 0.14.54 — 2026-09-20

- Fix the remaining `context_challenger_started` storm after 0.14.53: ordinary online `policy.update()` no longer rotates `tournament_revision`.
- Online rewards, manual demonstrations and other incremental weight learning still rotate `model_revision` and are persisted normally, but paired future-only Context Tournament evidence is not discarded.
- A genuinely fresh Train/Rebuild/replacement policy still gets a new `tournament_revision`; active schema changes and challenger reselection still invalidate old proof.
- `context_challenger_started` now reports `evaluation_reason` and `evaluation_champion_revision` for immediate diagnosis of any future reset.
- Add regressions proving online learning and lazy decay preserve challenger samples, while a fresh policy instance receives a distinct Tournament identity.

# 0.14.53 — 2026-09-20

- Fix repeated `context_challenger_started` bursts caused by deterministic lazy policy decay rotating the same `model_revision` that Sensor Tournament used as its future-only epoch identity.
- Add a separate persisted `tournament_revision`: real RL learning rotates both model and Tournament revisions, while the 60-second lazy decay rotates only provenance `model_revision`.
- Context Tournament now resets challenger proof only for a real learned champion revision change, active-schema change or challenger reselection; ordinary time decay no longer discards collected challenger evidence.
- Preserve backward compatibility: models without `tournament_revision` fall back to their existing `model_revision` on first load.
- Add regressions proving lazy decay keeps the challenger epoch/samples while a real learned policy update still invalidates old proof.
- Fast-light timing learning remains weight-only and does not rotate either Tournament epoch identity or physical-control semantics.

# 0.14.52 — 2026-09-20

- Speed up historical training replay without changing rewards, labels or policy semantics: incremental FeatureJournal reads now use two disjoint indexed ranges instead of an `event_time OR received_time` predicate that could scan old history on every empty step.
- Add an `(entity_id, received_time, event_time)` index for late-packet replay; ordinary event-time increments continue to use the existing entity/event-time index.
- Preserve the previous exact per-entity newest-64 ordering after merging the two causal branches, including late and out-of-order observations.
- Add regression coverage comparing the new split-range result to the legacy OR query and verifying SQLite chooses the received-time index.
- Include the open PR #130 Sensor Tournament fix: quality persistence now supplies 12 values for 12 columns, with restart/shadow regression coverage.
- Keep the 7-day manual training window, bounded RAM replay cache, Candidate lineage, Correct history and physical-control guards unchanged.

# 0.14.51 — 2026-09-20

- Make Candidate decision tiles last-known-state displays: `Desired`, `Candidate Desired` and Candidate confidence remain available until a newer real decision replaces them.
- Keep the existing 95-second freshness window as metadata only. Freshness no longer turns an observed decision back into `—` on the card.
- Preserve `shadow_active` as a freshness signal, separate from the last observed Candidate decision shown to the user.
- On restart, warm the latest Parent/Candidate decision once from durable decision history and keep subsequent 1-second card polling RAM-first.
- Harden the browser cache so sparse/heavy Candidate status payloads cannot erase a non-null last decision for the same generation.
- Keep paired A/B evidence, promotion gates, Correct history gap semantics and Candidate Executor isolation unchanged.

# 0.14.50 — 2026-09-20

- Fix intermittent Candidate-card `Desired` disappearing while `Candidate Desired` stays visible.
- Treat Parent Desired and Candidate Desired as two independently observed operational values, each with the existing 95-second freshness limit.
- Candidate-only passive/heartbeat observations no longer clear a still-fresh Parent Desired solely because their event ID differs from the Parent's last observation.
- Expose `parent_decision_paired` so diagnostics can distinguish an independently fresh display pair from a true same-event A/B pair.
- Keep paired future A/B evidence, comparison scoring, promotion gates and Candidate Executor isolation strictly same-event-only; no evidence semantics are relaxed.
- Preserve the 0.14.49 Candidate heartbeat, Correct chart fixes, seven-day training window and RAM-first hot paths.

# 0.14.49 — 2026-09-20

- Fix the quiet-home Candidate Shadow heartbeat. The intended 30-second refresh can now observe the same Home Assistant state revision again; revision dedupe still suppresses duplicate event-driven requests.
- Keep sparse Parent/Candidate Desired visible in Correct for the existing 95-second observed-decision validity window. A single real Shadow observation now draws a horizontal state segment instead of an invisible zero-length SVG move.
- Preserve genuine runtime gaps after the stale cutoff. No historical Desired is synthesized and policy replay remains disabled.
- Give direct Parent and Candidate Desired different dash patterns so identical G2/G3 decisions do not completely cover one another.
- Show separate Current, Parent and Candidate point counts in the Correct status line to make missing-generation telemetry immediately visible during debugging.
- Preserve Current history, seven-day training, RAM replay cache, reward/model/promotion semantics, Candidate Executor isolation and physical-control guards from 0.14.48.

# 0.14.48 — 2026-09-20

- Fix the remaining Correct chart Current regression. Recorder state is now projected across the selected range: the last state at/before `Od` is drawn from `Od`, and the last known state is extended to `Do`.
- Do not apply Candidate/Desired's stale-decision timeout to physical Current. A device that stays OFF or ON for ten minutes now renders a continuous Current line instead of two disconnected invisible points.
- Make normal Rebuild reuse the previous feature schema and selection metadata in RAM before learned heads are cleared. Policy weights are still rebuilt from scratch; only expensive sensor-selection work is retained.
- Scope the schema cache to the agent's active explicit training job so global model invalidation cannot accidentally resurrect an old schema.
- When first-time feature screening is genuinely required, stream only policy-admissible context entities plus the target instead of every archived entity.
- Add a bounded per-training replay LRU shared by onset and persistence temporal cursors. Repeated exact small history queries can now be served from RAM; the cache is capped at 8192 rows by default and disappears with the job.
- Keep the 0.14.47 seven-day training window, 55% cooperative duty cycle, 35 ms maximum uninterrupted work slice and realtime preemption unchanged.
- Preserve durable raw history, feedback, model checkpoints, benchmark/evidence and Candidate safety semantics.

# 0.14.47 — 2026-09-20

- Restore the Correct chart's physical Current history. Candidate charts now read Current directly from target entity_history instead of deriving it from Candidate observation rows, so gaps in Candidate inference no longer erase the real device-state curve.
- Fix Gen 2+ / Gen 3+ Candidate realtime routing. Active Candidate observers are indexed by durable lineage root_agent_id rather than agent_candidates.parent_agent_id, which becomes a Candidate surrogate after Gen 1.
- Limit explicit Train/Rebuild to a rolling seven-day history window anchored to current time. Older archive remains durable but no longer multiplies every interactive retrain.
- Clamp interrupted legacy 10+ day training windows forward into the new seven-day window on Resume.
- Remove fixed inter-chunk pauses from explicit selected-agent training with a dedicated agent_training_pause_ms=0 option. Discovery and background Recorder work keep the existing 1500 ms safety pause.
- Increase explicit training cooperative duty from 20% to 55%, reduce maximum uninterrupted Python work from 50 ms to 35 ms, and cap throttle sleeps at 0.5 s. Fresh HA state_changed events still request strict realtime priority.
- Preserve one-heavy-job FIFO, Candidate Executor isolation, durable history/evidence, and all 0.14.46 RAM-first hot-read behavior.

# 0.14.46 — 2026-09-19

- Move the 1 s Candidate live-card path to RAM. Current comes from the websocket-backed Engine state map; Candidate/Parent Desired, confidence and timestamp come from the in-memory generation runtime. SQLite is now only a cold restart/backfill source for these tiles.
- Keep the latest observed decision snapshot for every active Candidate generation in RAM on every inference, even when the existing durable 30 s history heartbeat does not need to write another row.
- Cache the active direct Parent/Candidate A/B edge in RAM so target transitions do not execute a SQLite lookup before outcome evaluation. Lifecycle mutations invalidate the cache synchronously.
- Cache hidden Candidate IDs, including retained lineage surrogates, in RAM. Normal live-agent enumeration and candidate-membership checks no longer query Candidate tables repeatedly.
- Prefer Engine.all_agent_configs for passive Candidate root configuration and Engine.models for already-materialized policy schema reads.
- Preserve persistence boundaries: models, training state, user feedback, promotion state, decision history and paired future evidence remain durable. Active RL policies were already resident in Engine.models, so this release deliberately avoids a second mutable model cache.
- Preserve Executor/HA isolation for Candidate Shadow and all 0.14.45 event-driven fallback semantics.

# 0.14.45 — 2026-09-19

- Fix false errors when switching a trained agent from Paused to Shadow. The successful mode PATCH is now separated from the subsequent layered UI refresh, so a transient renderer problem cannot be reported as a failed mode change or generate duplicate alerts.
- Harden retained Live-card mode/decision nodes and Candidate preference metric tiles against transient DOM replacement during layered refreshes.
- Give Candidate generations a persistent event-driven Shadow fallback instead of relying exclusively on Parent/Live `process_agent`. Active Candidate dependencies are indexed in memory from the target, explicit inputs, Parent schema, Candidate schema and target-area sources.
- Keep websocket work minimal: `state_changed` only queues Candidate observation; policy inference runs later on the existing Candidate worker.
- Prefer and deduplicate against normal Parent inference by Engine state revision. Passive fallback runs only when the same/newer revision was not already observed through the shared Parent path.
- Add a bounded 30-second Candidate Shadow heartbeat for periods where Parent is paused or no relevant event reaches Parent. The heartbeat records failed attempts and never self-wakes into a retry loop.
- Passive Candidate observations never touch Executor or Home Assistant services and never fabricate a Parent prediction. They use explicit `candidate-passive:` event IDs; A/B paired evidence still requires a real shared Parent+Candidate prediction event.
- Preserve training, discovery, reward, benchmark, promotion and physical-control semantics.

# 0.14.44 — 2026-09-19

- Replace misleading per-chunk training progress with a continuous whole-agent training progress contract. Recorder/screening/replay counters remain visible as the current stage and may restart between chunks without resetting the global percentage.
- Add a whole-training ETA derived from real wall-clock end-to-end progress, including Recorder waits, Raspberry-Pi cooperative throttling and completed replay chunks. Current-stage ETA and rows/s remain separate diagnostics.
- Overlay the in-memory global training progress onto the active agent card without adding periodic SQLite writes.
- Add a monotonic TrainingQueue lifecycle revision. Completion of one agent and start of the next forces a fresh cache-bypassing agent-list read, so cards cannot remain stuck on stale TRAINING state.
- Restore lifecycle actions hidden by the Generation Workflow action row: a trained `mode=paused` agent gets **Start Shadow**, an active Shadow can be paused, and `training_state=paused` gets **Resume training**.
- Keep the 20% Pi-safe training CPU duty cycle and 50 ms continuous-work slice unchanged. Long training is reported truthfully rather than accelerated at the cost of Home Assistant/UI responsiveness.
- Preserve discovery, model, reward, benchmark, Candidate, Teach/Correct and physical-control semantics.

# 0.14.43 — 2026-09-19

- Fix the false `Train failed: Cannot set properties of null (setting 'textContent')` alert after a successful manual Train queue admission.
- Make the retained-card P0 renderer tolerate the Generation Workflow layer replacing the original action row. Missing legacy mode controls are now treated as intentional ownership by the newer UI layer.
- Separate Train HTTP admission from the subsequent UI refresh. A renderer/refresh exception after a successful POST is logged for automatic retry instead of being reported as a failed Train request.
- Preserve TrainingQueue, discovery, model, Candidate, Teach/Correct and physical-control semantics.

# 0.14.42 — 2026-09-19

- Fix clean-install discovery completeness: the classifier uses the configured 10-day activity window, so the first low-memory scan now backfills the older part of that same window instead of importing only the most recent 24 hours.
- Keep the deep scan Raspberry-Pi friendly: state/value targets use minimal no-attribute Recorder responses in 24-hour windows; only attribute-only targets such as HVAC setpoints, cover position and humidity require bounded full-state history.
- Repair existing 0.14.41 installations automatically with one post-ready `deep_history_reconcile` pass when the durable full-window marker is missing. Later restarts stay quiet.
- Fix Recorder circuit-breaker data loss: discovery now waits cooperatively through a Recorder backoff and retries the same chunk instead of treating skipped chunks as successful zero-row reads.
- Fix the post-discovery agent-list refresh by using a per-discovery revision request key, bypassing the intentional 3-second `api/agents` polling cache.
- Add discovery reason diagnostics for inactive targets and expose whether the full discovery window has been completed.
- Preserve manual device selection: newly discovered agents remain WAITING/PAUSED and no training starts until the user explicitly presses **Train**.
- Preserve model, reward, qualification, Candidate, Teach/Correct and physical-control semantics.

# 0.14.41 — 2026-09-19

- Restore exactly one automatic low-memory controllable-device discovery pass on a genuinely clean installation, after HTTP/realtime startup is ready. Established installs with existing agents remain quiet and periodic Recorder maintenance stays disabled.
- Restore the pre-async-Rescan activity threshold override of 1 through the full async discovery path, including the TrainingQueue discovery-priority wrapper.
- Separate discovery from training: auto-discovered agents remain WAITING/PAUSED and are never placed into the initial-training queue. The user explicitly chooses which device to train with **Train**.
- Refresh the agent list immediately after a completed discovery run so newly detected devices appear without waiting for a later polling cycle.
- Preserve one-heavy-job resource protection, explicit Train/Resume/Rebuild/Teach queueing, existing model/reward/qualification semantics and physical-control guards.

# 0.14.27 — 2026-09-18

- Make Correct history genuinely observed-only for live generations. The Correct chart and point inspector now read the recorded `decision_history` plus the target entity's observed state history directly; they no longer invoke Teach-RL policy replay merely to draw the chart.
- Give fresh Home Assistant events strict short-lived priority over historical training. A state change opens a 0.75 s interactive window and the actual debounced inference pass extends priority for 1.0 s; the training worker yields at its next checkpoint so Shadow/Control inference is not left behind offline replay.
- Correct chart/point reads request the same cooperative priority window before touching SQLite; saving a Correct label gives its required context-signature reconstruction a 2.0 s interactive window as well.
- Cache the moving 30-second Room Belief replay window across forward samples. Rewinds still rebuild causally, but normal chronological replay promotes old window rows into per-entity seeds instead of re-querying every seed at every feature timestamp.
- Preserve observation-contract v12 semantics while advancing the moving Room Belief seed: late fast-journal observations remain gated by event and received time and can become the authoritative seed when their timestamp exits the active 30-second window.
- Add recent 60-second p95 telemetry for inference and event→intent latency. The UI shows this current window only (or — when there are no recent samples), so a one-off startup stall does not remain displayed for hundreds of later decisions.
- Preserve model, reward, feature-schema, qualification, provenance, Candidate, Correct/Teach, rollback and physical-control semantics. No retraining or raw-history deletion is required.

# 0.14.26 — 2026-09-18

- Fix the post-upgrade cold-start window where the HTTP API exposed a half-built Engine before runtime composition and realtime inference had actually started. Status remains available, but agent/live endpoints now stay in explicit startup state until the runtime is ready.
- Avoid creating a redundant `entity_history(entity_id,ts,id)` F22 index during runtime composition. The existing `entity_history(entity_id,ts)` secondary index already carries the INTEGER PRIMARY KEY/rowid tie-breaker; rebuilding a second index over hundreds of thousands of rows could consume minutes of CPU/I/O on Raspberry Pi before Shadow predictions appeared.
- Reduce incremental replay SQL batches from 150 to 32 entities so one SQLite statement cannot monopolize the process for long stretches even when total query count is already low.
- Remove redundant outer SQLite sorting from bounded UNION replay queries; the causal merge already performs the authoritative Python ordering.
- Lower the shipped explicit-training target from 25% to 20% cooperative duty cycle and the maximum continuous work slice from 75 ms to 50 ms. Values equal to the previous shipped defaults migrate automatically; other explicit user tuning remains unchanged.
- Preserve model, reward, qualification, Candidate, Correct/Teach, provenance, rollback and physical-control semantics. No retraining or history deletion is required.

# 0.14.25 — 2026-09-18

- Replace repeated per-entity temporal as-of reconstruction with bounded incremental replay cursors for selected policy inputs.
- First access and genuine timestamp rewinds use indexed bulk per-entity LIMIT queries; forward movement consumes only newly eligible rows and repeated requests for the same timestamp perform no SQLite read.
- Split historical replay into independent onset/anticipation and dwell-persistence cursors, and evaluate each cursor's requested timestamps in chronological order to avoid artificial rewinds between feature samples.
- Keep at most 64 temporal samples per selected input, including large forward jumps, while retaining bulk rewind support so overlapping agent dwells cannot leak future state into earlier features.
- Preserve the Room Belief replay contract: every query still reconstructs the same causal 30-second home window, but source seeds are fetched in indexed bulk rather than one SELECT per source.
- Extend observation-contract replay incrementally across both event time and received time. Late-arriving fast observations remain invisible before receipt and become visible causally without rebuilding the whole selected-input view.
- Keep the v12 same-timestamp fast-journal/archive merge rule and transition-edge semantics unchanged.
- Expose temporal replay diagnostics in History: SQL query count, forward advances, rewinds and estimated reduction versus the former per-entity as-of query path.
- Preserve reward math, feature schema, policy version, qualification thresholds, provenance gates, raw history, Candidate lineage, Correct/Teach labels, rollback and physical-control guards.

# 0.14.24 — 2026-09-18

- Batch historical replay audit writes into bounded SQLite transactions instead of committing one transaction for every completed dwell. The default batch size is 64 rows and remains configurable.
- Preserve crash safety: replay facts are flushed before model save, and the existing model history watermark still removes any facts committed by a failed pass.
- Load existing historical experience keys once per agent so replay deduplication no longer needs a write transaction merely to discover that a dwell was already processed.
- Batch the matching provenance audit rows as well, and preload replay provenance for target rows so the Stage-06 own-command exclusion does not reintroduce per-dwell SQLite lookups.
- Make feature screening schema-aware: only agents without a persisted model and still using wildcard inputs run the broad precursor scan.
- Reuse the persisted schema for later training chunks and Resume. Explicit Teach/Correct feature selections also bypass the broad whole-home scan.
- Do not even open the screening archive cursor when no agent in the batch needs feature discovery.
- Move broad-screening duplicate suppression into a SQLite change-only stream; raw bounded rows are still retained for fast occupancy/activity driver scoring.
- Preserve the provenance boundary in the batch path: own-command acknowledgements remain excluded before any policy update, and accepted/excluded replay provenance is journaled in bounded batches.
- Add a cooperative checkpoint after expensive target-edge precursor fan-out so one transition cannot monopolize the training worker between archive checkpoints.
- Preserve policy/reward math, qualification thresholds, raw history, Candidate lineage, Correct/Teach labels and the one-heavy-job safety boundary.

# 0.14.23 — 2026-09-18

- Make **Apply Correct** a durable, idempotent two-phase operation: the HTTP path stores a client request ID and returns `202 Accepted` before Candidate orchestration.
- Add a tiny restart-safe workflow-request worker. Requests left in `processing` are re-admitted after restart; repeated delivery of the same request ID never creates a duplicate correction edge.
- Keep the Correct dialog visible while the backend is busy. It now opens from the already-rendered card, retries metadata/history independently, and tracks the durable request after an HTTP timeout instead of blindly submitting the correction again.
- Add priority admission to historical training: `Teach/Correct Candidate` work runs before explicit user Train/Rebuild, which runs before automatic initial training; FIFO order is preserved inside each priority class.
- Do not preempt an already active heavy replay in this release. Interactive work becomes the next admitted heavy job, preserving the single-heavy-job safety boundary.
- Keep models, raw history, Correct/Teach labels, Candidate lineage, settings, generations and rollback state unchanged. No retraining or data migration is required.

# 0.14.22 — 2026-09-18

- Fix a restart regression that marked freshly trained policy-v11/schema-v12 models as NEEDS_RETRAIN before the observation contract was installed.
- Make storage migration version-monotonic: it quarantines only truly legacy pre-v10/schema11 models and leaves exact compatibility decisions to the active observation feature contract.
- Repair agents already affected by 0.14.21 when the persisted model is current and the historical benchmark evidence is still intact: passed benchmarks return to qualified + Shadow; non-passing completed benchmarks return to paused + Shadow.
- Do not auto-repair genuine configuration invalidations: changing input entities or action bounds still clears benchmark evidence and remains NEEDS_RETRAIN until retrained.
- Restore the saved training cursor/progress for repaired current-contract models; raw history, models, feedback, Candidate lineage, TrialRecords, Teach labels and rollback state are preserved.
- No threshold change and no Control bypass; Control still requires qualified state and the existing promotion/control guards.

# 0.14.21 — 2026-09-18

- Complete the post-training lifecycle: every completed policy with a persisted model returns to Shadow, even when its historical benchmark is insufficient for Control.
- Keep Control qualification strict: failed/insufficient benchmark results remain `training_state=paused`, so they can observe in Shadow but cannot enter Control.
- Preserve true Pause for interrupted/error training and explicit user CPU-saving pauses.
- Add explicit Shadow and Pause controls to agent Settings; Shadow is available only when a learned model exists and no rebuild/training is active.
- Wake realtime inference immediately after training finalization so a completed model does not wait for an unrelated HA event before becoming visible.
- No data/model migration or retraining is required for existing persisted policies.

# 0.14.20 — 2026-09-18

- Restore a complete cold-start lifecycle: auto-discovered agents now queue their first historical base-policy training automatically instead of remaining indefinitely in WAITING.
- Keep the first build on the real Live/base agent. Candidate generations are not created until the parent has a persisted model.
- Repair existing auto-created WAITING/PAUSED agents without a model on the next discovery/rescan by admitting them to the existing FIFO.
- Preserve Raspberry Pi resource bounds: exactly one heavy training job at a time, existing training duty-cycle throttling, no bypass of benchmark/qualification or Control safety gates.
- Update status/rescan/UI copy so automatic initial training is visible as active/queued instead of misleading users to press Train for every discovered agent.

# 0.14.19 — 2026-09-18

- Fix a Chromium/Ingress UI freeze during training caused by the confidence diagnostics observing the entire document subtree while also mutating that subtree.
- Make confidence metric text/style writes idempotent and observe only direct Device-agent card replacement; nested confidence decoration can no longer recursively wake its own MutationObserver.
- Keep the existing 2 s confidence refresh, 250 ms lightweight Current/Desired path and training CPU budget unchanged.
- Frontend-only hotfix: no retraining, data migration, policy/reward/schema change or physical-control change is required.

# 0.11.2 — 2026-09-14

- Separate historical Teach from Wrong decision; Wrong decision keeps its existing immediate correction path unchanged.
- Make Teach points persistent supervised RL examples instead of runtime overrides.
- Default the Teach chart to the last 10 minutes and add a visible gray translucent drag selection on the dark chart.
- Re-screen the broad eligible HA context from historical as-of states; allow strongly supported hidden sensors to replace occupied feature slots.
- Queue the normal full offline rebuild on the selected schema, then fine-tune rebuilt LinUCB heads with active Teach examples.
- Replay the same historical window using the new base RL policy after training; Teach labels remain visible as reference points.
- Keep Undo deterministic: remove the supervised label and retrain from history plus the remaining active Teach labels.
- Preserve the single heavy-job FIFO and bounded historical replay for Raspberry Pi operation.
- 218 automated tests pass on Python 3.11 and 3.13; anticipation simulator and Docker smoke test pass.

# 0.11.0 — 2026-09-14

- Reduce agent cards to Shadow/Control, Wrong decision, Settings and Teach; move other operations and diagnostics into Settings.
- Add zoomable historical teaching with precise as-of inspection, target labels and per-agent undo.
- Make button labels independent of base RL matrices: one correction changes a matching decision even after thousands of old samples; undo preserves unrelated learning.
- Route taught decisions through Control Executor with explicit user-label provenance, revalidation and normal device/qualification guards. Shadow dispatches nothing.
- Retire conflicting button labels after a genuine physical user correction; option reordering invalidates old categorical labels.
- Distinguish current-policy historical replay from recorded Desired; record future decisions in bounded batches with 31-day retention.
- Bound dense radar-history replay by indexed sampling; inspect selected points exactly and never borrow future/live home forecasts.
- Refresh Current through a lightweight 500 ms loop and bootstrap cards without waiting for diagnostics; prevent overlapping refreshes.
- 207 automated tests plus local browser checks. No model migration required from 0.10.9.

# 0.10.9 — 2026-09-14

- Add always-visible Naucz: label Desired without sending an HA service or imposing a manual hold, including Shadow and Paused.
- Keep Naucz / popraw as explicit physical Current correction plus learning.
- Persist contextual labels and policy updates; show the resulting model prediction and calibrated confidence.
- Prevent historical-training races; preserve pending physical outcomes and defer schema promotion while they exist.
- Route search through the active renderer; reconcile card ownership by agent ID and respect hidden cards in CSS.
- Preserve the 0.10.8 startup freeze fix; no DOM observer or new polling loop.
- 186 tests passed; local browser verified teaching, correction and search.

# 0.10.6 — 2026-09-13

- Fix crash before HTTP startup: manual context learning accessed core.STORE while it was None.
- Keep entrypoint imports free of database/runtime initialization; prepare adapters and manual learning after Store exists.
- Attach realtime timing and manual event observers before engine, event stream and history workers start.
- Report initialization-wrapper failures through startup status; keep HTTP diagnostics available.
- Make settings.APP_VERSION the canonical runtime version; adapters no longer overwrite it.
- Add four isolated packaged-entrypoint tests, including real HTTP requests during blocked initialization.
- Add network-isolated container readiness smoke check to CI; package checks cover every JS file and current version.
- 174 tests passed. Existing 0.10.5 models, manual correction features and data remain compatible.

# 0.10.1 — 2026-09-13

- Fix frontend startup ReferenceError from the removed toggleExplore function.
- Version every browser script URL to prevent stale/mixed JavaScript after upgrades.
- Execute app.js in a Node VM regression test and verify first API request, refresh timer and UI handlers.
- 153 tests passed; clean browser startup and experiment menu verified on a local fixture.
- Existing models, experiment results and Control configuration are preserved.

# 0.10.0 — 2026-09-13

- Add per-agent context experiment menu: presence, environment and other-device activity.
- Learn online with a separate sparse contextual bandit and interleaved baseline observations.
- Persist budgets, outcomes, model weights and manual-correction backoff across restarts.
- Bound physical probes, preserve Executor guards, and never reward ACK or interrupted observations.
- Retain qualified version-10/schema-11 models without retraining; experiments default off.
- Preserve latest main's FIFO training queue, realtime light timing and transactional Control handoff.
- 152 tests passed, including 25 experiment tests; local browser settings flow verified.

# 0.9.2 — 2026-09-13

- Fix Control transition blocked globally by unrelated unreadable automation configurations.
- Treat partial automation scans as visible diagnostics; disable and verify known target automations.
- Preserve successfully parsed target mappings across failed reads and restarts.
- Refresh cached mappings on successful edits and remove deleted or identity-replaced automations.
- Report missing configuration IDs and invalid responses per automation.
- 107 tests passed, including the HTTP mode-change regression and failed OFF confirmation.
- Retains trained 0.9 models and the bootstrap/presence fixes from 0.9.1.

# 0.9.1 — 2026-09-13

- Fix bootstrap failure after 8192 live events: aggregate bounded live statistics instead of buffering raw events.
- Commit shared model and history checkpoints together; failed installation preserves both.
- Distinguish PIR/presence from radar distance, thresholds, firmware and connectivity.
- Prefer binary presence over raw energy on the same device and area, while preserving agent policy context.
- Recognize custom PIR/motion binary sensors independently of integration brand.
- Add source diagnostics with entity, area and reason; count actual transitions separately from no-arrival trials.
- 97 tests passed, including bootstrap under 12000 concurrent live events and 30000-update bounded-memory regression.
- Existing 0.9 policies are retained. Re-run Home Model bootstrap; agent retraining is not forced.

# 0.9.0 — 2026-09-13

- Modular Context → Shared Home State → PolicyBackend → ActionIntent → Executor → Reward pipeline.
- Shared area transitions and occupancy forecasts at 1/3/5 seconds, manual streaming bootstrap.
- Single audited Executor for device commands, automation takeover and Verify; zero service calls in Shadow.
- Own command ACK recognition, event re-evaluation after in-flight commands and bounded retry.
- Exponential forgetting, age-weighted replay and timing-aware Reward v2.
- Manual training, one heavy job, disk-backed replay and recoverable checkpoints.
- Home Intelligence, inference exports, mathematical contributors and resource telemetry.
- Safe 0.8 migration preserves data, archives incompatible models and requires manual retraining.
- Deterministic inference; micro-exploration remains disabled.
- 84 regression tests and a learned anticipation simulator. Physical HA/Pi 4 targets remain unmeasured.


## 0.8.0 — feature-complete baseline (2026-09-12)

This release promotes the tested 0.7.14 implementation to the first relatively feature-complete 0.8.x baseline. The focus is predictable per-agent training, low Raspberry Pi resource use, broad Home Assistant context and safe Shadow→Control operation.

- Agent discovery no longer starts offline RL training automatically. Each agent waits for an explicit **Train** action.
- Only one training job can run at a time by default.
- Broad historical context screening is streamed from SQLite in bounded batches instead of materializing millions of rows in Python RAM.
- The replay phase materializes only the target plus the context entities selected for the active agent.
- Home Assistant Recorder reads use one worker by default and a longer background yield.
- Temporal state history stores compact state objects and a shorter per-entity deque.
- Rescan discovers targets only; it never starts training.
- Interrupted automatic jobs from 0.7.13 are paused on startup and can be resumed manually.

# Changelog

## 0.7.13
- Fix ESPHome/LD2411 context exclusion: configuration `number`/`select`/`switch` siblings no longer remove `sensor`/`binary_sensor` channels such as `kitchen_presence_presence` from training.
- Keep direct actuators and explicit electrical-unit telemetry excluded while preserving ESPHome sensory siblings.
- Force a new broad historical context rebuild so newly admitted ESPHome sensors are imported from Recorder and can compete during feature screening.
- Add measured replay progress, work counters and phase-specific ETA during long policy rebuilds instead of freezing the UI at 13%.
- Add a preparation pipeline explaining target discovery, context import, sensor screening, historical replay, benchmark and readiness.
- Surface eligible/selected ESPHome context counts and preserved sibling diagnostics.

## 0.7.12
- Make the context candidate universe broad by default: every parseable Home Assistant entity may compete during historical feature screening.
- Remove domain/name/device-class/diagnostic/hidden blacklists from context admission.
- Restrict electrical exclusion to explicit electrical `unit_of_measurement` values only (V/A/W/VA/var/Wh/kWh/Ah/ohm, etc.).
- Stop whole-device electrical blacklisting; a non-electrical sibling remains eligible even on a Shelly/ESPHome device.
- Preserve controllable-device hard exclusion, including Entity Registry siblings of detected actuators.
- Keep live CPU bounded by selecting a compact predictive subset after broad historical screening; fast targets retain 1/3/10 s time-series features.
- Refresh all eligible candidate histories once on the v0.7.12 training revision so phone/car/weather/virtual/custom entities can compete immediately.

## 0.7.11
- Fix ESPHome LD2411/LD24xx Still/Move Energy (%) false-positive electrical classification that could blacklist the entire radar device, including Presence.
- Add first-class numeric activity context for AI detection/radar scores.
- Import fast behavioural histories without 60-second numeric downsampling during model revision/Rebuild.
- Rank binary occupancy and numeric activity drivers against historical target ON/OFF transitions.
- Surface primary/ranked behavioural drivers in diagnostics.
- Preserve dedicated electrical-meter exclusions including Shelly 3EM.

## 0.7.10
- Discover the real occupancy/presence driver of fast lights from historical ON/OFF edge association instead of relying on HA area/name heuristics.
- Reserve historically proven presence drivers in the compact context even when the sensor is in another/unassigned HA area or the legacy automation targets a group/device/script indirectly.
- Use the discovered primary occupancy driver to anchor historical ON/OFF replay, including delayed legacy OFF actions.
- Refresh only occupancy/presence Recorder history before a feature-revision rebuild so previously omitted radars can be rediscovered without a full Recorder bootstrap.
- Add diagnostics for `Primary occupancy driver` and historical edge-association score.
- Treat detected/clear and occupied/unoccupied presence states as strong centered categorical states in addition to on/off.

## 0.7.9
- Narrow electrical-device filtering: a single battery-voltage sensor no longer blacklists useful occupancy/lux/temperature siblings on the same physical device.
- Keep direct electrical telemetry excluded, while whole-device exclusion is now reserved for dedicated meter-like devices such as Shelly 3EM.
- Fast binary agents use a compact causal context (default max 8 entities) and no longer fill unused slots with unrelated whole-home signals.
- Down-weight clock features for fast light/switch agents so short 1/3/10 s sensor sequences dominate simple presence-driven rules.
- Rebuild fast-policy schemas from the existing local archive; full Recorder bootstrap is not repeated.

## 0.7.8
- Fix 0% candidate confidence for reproducible devices whose historical transitions cannot be mapped to a literal HA automation target.
- Candidate qualification now benchmarks chronological held-out **recorded target behaviour**; HA automations remain the primary structural/context prior but are no longer a hard attribution gate.
- Binary ON/OFF benchmark remains balanced across actions, so an always-OFF model still cannot qualify on class imbalance.
- Benchmark diagnostics now report transition origins (`automation_assisted`, `anonymous_external`, `manual`) and detected automation-rule count.
- UI shows **Candidate confidence** while training/paused and **Live confidence** only after realtime inference exists, avoiding misleading 0% cards.
- Training revision v12 rebuilds policies from the existing local archive; no full Recorder bootstrap is required.

## 0.7.7
- Replace CANDIDATE/DORMANT lifecycle with explicit TRAINING / QUALIFIED / PAUSED states.
- Persist per-agent historical training start, cursor, end and progress.
- Add Resume: preserve model/experiences/benchmark and continue from the saved cursor to current data.
- Make Rebuild a true full reset of model/benchmark/cursor followed by complete historical indexing.
- Checkpoint long per-agent indexing in configurable chunks and automatically resume unfinished cursor-based jobs after restart.
- Keep TRAINING and PAUSED agents out of normal realtime inference; only QUALIFIED agents consume steady-state inference/training CPU.
- On completed pass, benchmark >78% moves the agent to QUALIFIED + Shadow; lower/insufficient candidates move to PAUSED.
- Rebuild broadly refreshes eligible Recorder sensor history so newly added sensors can participate; Resume refreshes only target + selected context.

## 0.7.6
- Treat historical Home Assistant automation behaviour as the primary candidate benchmark.
- Add chronological held-out automation imitation scoring; binary targets use balanced ON/OFF accuracy.
- Require >=78% benchmark with sufficient samples for QUALIFIED state.
- Mark lower-confidence/insufficient candidates DORMANT, dim their cards, block Control and skip normal inference/training.
- Add single-agent Rebuild & benchmark from the local archive.
- Reserve context slots for automation trigger/condition entities to improve rule-behaviour reproduction.
- Narrow post-bootstrap Recorder context maintenance to entities used by qualified policies.
- Preserve controllable-device/electrical exclusions and short 1/3/10 s fast time series.

## 0.7.5
- Exclude voltage, current, power, energy, reactive/apparent power, power factor, frequency and related electrical telemetry from every RL context.
- If any entity on a physical device is electrical-meter telemetry, exclude every sibling entity of that device; meters such as Shelly 3EM are therefore invisible to learning.
- Remove power/energy sensors from sensor recommendations.
- Fast light/switch agents now learn primarily from a short event-time series: current context plus ~1 s, 3 s and 10 s deltas.
- Bump the feature/training revision so existing policies rebuild from the local archive without a full Recorder re-import.

## 0.7.4
- Exclude every directly controllable Home Assistant entity from every agent input context.
- Exclude all Entity Registry siblings that belong to a detected controllable physical device, including sensor/diagnostic-style entities exposed by that actuator device.
- Keep the exclusion global across agents: one actuator may never become another agent's predictor.
- Apply a hard runtime zeroing guard for excluded entities so a stale schema cannot reintroduce actuator features.
- Clear in-memory policy schemas when Entity Registry device membership changes.
- Bump the feature/policy/training revision and rebuild policies from the existing local archive without re-importing Recorder history.

## 0.7.3
- Added a local-first fast-control profile for lights/switches: same-area/same-device/semantic occupancy sensors are reserved and ranked above remote automation hints.
- Shortened fast binary temporal features to seconds instead of minutes so neighbouring-room presence cannot keep a light ON long after the dedicated room sensor changed.
- Added causal replay anchors: historical ON/OFF actions are associated with the nearest dedicated local occupancy edge. For OFF, delays inherited from old HA automations are compressed back to the local vacancy edge.
- Stable-dwell replay for fast lights stops reinforcing ON after the primary local occupancy sensor becomes vacant (and OFF after it becomes occupied).
- Upstream/adjacent occupancy may still provide a weak early ON cue, but it cannot define OFF timing.
- WebSocket event debounce reduced to 25 ms and event-driven inference now schedules only agents affected by the changed entity; the 1 s proactive tick remains as fallback.
- Agent diagnostics now expose the primary local sensor, upstream cues and the latest context trigger.
- Training revision bumped; policies rebuild from the existing local archive without a full Recorder re-import.

## 0.7.2
- Fix own HA service echoes being classified as manual override.
- Discard unproven legacy holds; explicit Control releases manual hold.
- Parallel per-target execution, background polling and stale-state protection.
- Cancel superseded unacknowledged light intent without bypassing configured timing.
- Preserve existing model, context selection, calibration and training revision.
- Add control provenance and latency diagnostics.

## 0.7.1
- Control now takes priority by disabling matching automations and stopping their running actions.
- Verify OFF before transitioning; retry runtime failures with backoff. No auto-restore in Shadow.
- Match literal action destinations, excluding condition/template references.

## 0.7.0
- Shared acknowledgement, settling and persistent manual-priority lifecycle.
- Immediate manual preference learning, per-agent timing and context settings.
- Causal as-of replay, purged validation boundary and action-specific confidence.
- Device-limit quantization, duplicate-controller blocking and retry backoff.
- Readable Python sources; additive SQLite migration and connection cleanup.
- See RELEASE_0_7.md and TEST_REPORT.md.

## 0.6.0
- Changed the default interaction horizon to ~1 second; ordinary light/switch behavior is reactive to precursor events rather than 5–60 s speculative prediction.
- Reduced realtime inference debounce from 150 ms to 75 ms.
- Added chronological held-out confidence calibration: recent replay is evaluated before being folded into training, and runtime confidence is capped by empirical backtest reliability.
- Added held-out accuracy/sample count and structural-vs-calibrated confidence diagnostics.
- Manual historical actions use ~1 s precursor context; automation/external transitions use event-time context so immediate automations do not train against pre-trigger states.
- Fixed historical reward so short but normally accepted lighting dwells are positive evidence rather than being punished merely for ending inside the old 90-second window.
- Strong historical negative reward now primarily represents a rapid explicit user correction of an automatic/external action.
- Fixed binary/categorical context encoding so ON/home/open is strongly separated from OFF/not_home/closed, making presence and motion useful precursor signals.
- Removed feedback-window serialization for genuine desired-state reversals and shortened auto-agent action cooldowns for responsive lighting/switch control.
- Reuses the existing history archive and rebuilds policies without repeating the full Recorder bootstrap.

## 0.5.2
- Reworked the six agent metrics from one cramped row into a readable 3×2 grid on desktop while retaining the existing 2-column mobile layout.
- Emphasized Current and Desired state tiles so control intent is easier to scan.
- Persisted expanded per-agent diagnostics across the 4-second UI auto-refresh and across page reloads.
- Improved the diagnostics disclosure header and small card spacing/details without changing the RL/control runtime.

## 0.5.1
- Fixed target-state leakage that could make an ON device predict OFF simply because historical OFF transitions usually started from ON.
- Changed offline replay from transition-only learning to desired-state RL: stable accepted dwells contribute a bounded set of persistence experiences in addition to anticipatory onset experiences.
- The controlled property is no longer fed back as a policy feature; it is used only by the HOLD/control gate.
- Added Current vs Desired state to the agent UI.
- Control now blocks by default while enabled Home Assistant automations still target the same entity, preventing controllers from fighting each other. Shadow remains fully compatible with existing automations.

## 0.5.0
- Replaced the 192-bucket whole-home hash with 128 explicit, collision-free per-agent features.
- Added agent-specific relevance selection from the whole HA candidate pool (automation hints, area/device relations, sensor needs, semantic locality and historical precursor timing).
- Added temporal features: current value, ~1 min delta, ~5 min delta and recent-change recency.
- Target entities now expose their actual controlled property to the policy (brightness, setpoint, position, etc.).
- Added multi-horizon predictive RL heads (5/15/30/60 s by default, with longer target-specific horizons).
- Added historical-support and context-novelty estimates; Control is blocked when support is too low or context is too novel.
- Sensor recommendations now evaluate the context selected for the specific agent instead of the entire house globally.
- Dimmable-light 0% actions now use `light.turn_off` explicitly.
- Reuses the existing long-term history archive; upgrading from v0.4.x rebuilds policies without repeating the full Recorder bootstrap.
- Preserves realtime WebSocket inference, automation priors, direct-control verification, conflict warnings and confidence-first agent sorting from v0.4.x.

## 0.4.1
- Added ACTED / HOLD / BLOCKED / WAITING / ERROR control diagnostics.
- Added Verify control and Home Assistant service-call telemetry.
- Added warnings for enabled automations that also target the same entity.
- Automation/script overrides no longer count as negative human corrections.

## 0.4.0
- Added near-real-time WebSocket inference and predictive offline replay.
- Added read-only automation scanning as structural feature priors.
- Added Entity Registry filtering, broader device discovery, agent search/sorting and manual rescan.
- Reused the v0.3 long-term history archive during policy upgrades.
