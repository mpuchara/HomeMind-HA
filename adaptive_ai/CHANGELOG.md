# 0.14.27 — 2026-09-18

- Make Correct history genuinely observed-only for live generations. The Correct chart and point inspector now read the recorded `decision_history` plus the target entity's observed state history directly; they no longer invoke Teach-RL policy replay merely to draw the chart.
- Give fresh Home Assistant events strict short-lived priority over historical training. A state change opens a 0.75 s interactive window; the training worker yields at its next checkpoint so Shadow/Control inference is not left behind offline replay.
- Correct chart/point reads request the same cooperative priority window before touching SQLite.
- Cache the moving 30-second Room Belief replay window across forward samples. Rewinds still rebuild causally, but normal chronological replay promotes old window rows into per-entity seeds instead of re-querying every seed at every feature timestamp.
- Preserve observation-contract v12 semantics while advancing the moving Room Belief seed: late fast-journal observations remain gated by event and received time and can become the authoritative seed when their timestamp exits the active 30-second window.
- Add recent 60-second p95 telemetry for inference and event→intent latency. The UI now prefers recent p95 so a one-off startup stall does not remain displayed for hundreds of later decisions.
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
