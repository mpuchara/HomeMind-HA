
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
