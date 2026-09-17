# F21 — DeviceAgent / DeviceCapabilities and shared-resource arbitration

Date: 2026-09-17

## Runtime boundary

The shipped runtime remains:

`run.sh -> trial_queue_main.py -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py`

`ActionIntent -> Executor` is still the only physical Home Assistant command path. Stage 15 adds a registry-backed device identity and resource arbiter inside Executor; `device_agents.py` has no HA import and cannot send a service.

## Why this is needed

The old control boundary was keyed primarily by `target_entity`. That is insufficient when:

- one light exposes both `power` and `brightness_pct`;
- two HA entities address one physical device;
- multiple consumers use one radar while one configuration threshold belongs to the sensing layer;
- two long-dynamics actuators influence the same zone;
- an entity changes name/entity_id while its HA Device Registry identity remains stable.

A policy generation remains owned by the existing logical `agent.id`. Stage 15 does **not** re-key historical data, labels, TrialRecords, models or Candidate lineage.

## Identity contract

Logical device identity is resolved in this order:

1. explicit `device_explicit_mappings` row;
2. Home Assistant Entity Registry `device_id`;
3. exact `entity_id` fallback.

Friendly names are never an identity signal.

Existing `agents` rows gain additive fields:

- `logical_device_id`;
- `device_property`;
- `device_contract_version`.

Migration is explicit and idempotent. `migrate_agent_identity()` / `migrate_agent_identities()` update only those additive fields while preserving `agent.id`, `target_entity`, `target_property`, model bytes, history and generation lineage. The final runtime invokes this migration after Entity Registry and Device Registry refreshes; an explicit mapping also immediately migrates matching agents. The descriptor used on the control decision path is read-only, so inspecting capabilities cannot invalidate Executor's fresh-settings snapshot.

An explicit mapping may also set a **logical** `property_name`. For example a physical `number.*` target whose HA operation is still `target_property=value` may be mapped to logical `device_property=brightness_pct`. The physical property is never rewritten, so existing policy/action semantics are not reinterpreted.

## DeviceCapabilities

The runtime descriptor exposes:

- physical HA `device_id` when known;
- logical device id and identity source;
- logical `device_property` and unchanged physical `target_property`;
- all known sibling entities/properties of that logical device;
- area/zone;
- shared resource keys;
- autonomy eligibility and reason;
- perception/configuration ownership;
- process-model backend contract;
- compound power/brightness semantics.

### Generic targets

An **auto-created** `switch`, `number`, `input_number`, `select` or `input_select` is not eligible for Control merely because HA exposes a writable entity. It needs an explicit device mapping with `autonomy_enabled=true`.

A manually created legacy agent remains compatible: manually selecting/configuring the target is treated as the explicit operator description that old installations already relied on. Registry config/diagnostic entities remain excluded from ordinary DeviceAgent Control even when manually created because their ownership belongs to the perception/configuration service.

## Power + brightness

For a dimmable light, `power` and `brightness_pct` share one physical resource. Two separate agents cannot independently own those properties in Control.

Brightness is treated as a complete light command:

- `brightness_pct > 0` -> one `light.turn_on` call with `brightness_pct`;
- `brightness_pct == 0` -> one `light.turn_off` call.

No power call plus brightness call pair is emitted. This avoids intermediate inconsistent states and double dispatch.

## Shared resource arbiter

Resource keys can include:

- `device:<logical_device_id>`;
- explicit `group:<resource_group>`;
- `zone:<area>:thermal` for long-dynamics thermal equipment;
- `zone:<area>:solar_shading` for covers;
- `sensor-config:<logical_device_id>` for perception configuration.

Executor's existing `target_lock(entity)` API is preserved for callers, but it normalizes the entity to the logical device resource. Therefore training/promote/control paths that already use this lock serialize sibling properties/entities.

Immediately before physical dispatch, Executor creates an additive durable **in-flight reservation** in `device_resource_state`. It stores owner agent/intent and `lease_until`; this is the crash/restart safety lease. A second owner cannot enter while that lease is active.

After a successful HA service call the in-flight lease is cleared immediately. The same row retains `last_dispatch_ts`, `last_dispatch_agent_id` and the last action. That separate completed-action record enforces **cross-agent minimum dwell**: a brightness agent cannot immediately fight a power agent on the same lamp. The original same-agent cooldown/pending rules in Executor remain authoritative for repeated commands by the same agent, avoiding duplicate cooldown layers.

On a transport/service failure the in-flight lease is released without recording a successful dispatch. All multi-resource in-memory locks are acquired in sorted canonical order to avoid AB/BA deadlocks.

## Legal action mask / abstain

`DeviceAgentService.legal_action_mask()` is independent of reward. It intersects:

- autonomy eligibility/device description;
- durable shared manual hold;
- active shared in-flight resource lease;
- cross-agent minimum dwell after a completed action;
- existing `legal_value()` user/device bounds and quantization.

If no action remains legal, the contract returns `fallback=abstain`. A high reward/confidence cannot override this result.

Manual priority is shared across conflicting agents. An explicit manual hold recorded for one property of a device blocks sibling-property dispatch as well, including after restart through the existing persistent manual-hold journal.

## Radar / perception configuration

Physical threshold control is still not enabled by Stage 15. The new contract only establishes ownership semantics for the later adapter required by Stage 10.

`sensor-config:<logical_device_id>` has exactly one owner service: `perception_service`. Multiple consumers (for example presence belief and lighting) can register against the same lease, but they do not become independent owners of the radar threshold. The lease also has a configuration snapshot/TTL, matching the future restore requirement.

## HVAC / covers

Stage 15 explicitly distinguishes fast contextual decisions from process dynamics. Climate, cover, humidifier and water-heater descriptors expose `ProcessModelBackendContract v1`:

- state estimation required;
- constraints required;
- rollout horizon required;
- lighting contextual bandit is **not** declared sufficient;
- implementation is currently `ready=false`.

Auto-created long-dynamics agents therefore cannot enter autonomous Control without an explicit operator/device description or a future backend integration. Existing manually configured agents remain compatible with the old guarded path, but the runtime does not claim that their lighting-style bandit is a long-horizon comfort model.

## Additive persistence

Stage 15 adds only:

- `agents.logical_device_id`;
- `agents.device_property`;
- `agents.device_contract_version`;
- `device_explicit_mappings`;
- `device_resource_state` (including `last_dispatch_agent_id` for completed cross-agent dwell);
- `perception_resource_leases`.

No saved policy vector, label, generation, TrialRecord or rollback snapshot is reinterpreted.

## Tests

`tests/test_device_agents.py` covers:

- two agents targeting one lamp (`power` vs `brightness_pct`);
- compound brightness ON/OFF with one HA service plan;
- two HA entities sharing one `device_id`;
- explicit grouping without name guessing;
- logical-id migration without changing `agent.id`;
- explicit logical property mapping without rewriting the physical target property;
- equal friendly names not being merged;
- manual takeover on one property blocking the sibling property;
- restart while a durable in-flight resource lease is active;
- successful dispatch clearing in-flight lease while preserving cross-agent dwell;
- concurrent sibling reservations: one winner, no deadlock;
- two radar consumers sharing one perception-owned configuration lease;
- generic auto-created switch requiring explicit description;
- configuration entity reserved for perception;
- HVAC process-model contract;
- zone-level thermal conflict;
- source-level assertion that `device_agents.py` cannot call HA and Executor uses the arbiter.

The existing Executor/Experiments/provenance/packaged-startup suites also remain part of the regression boundary, including the legacy public `executor.target_call` import.

## Remaining limitations

- Stage 15 does not implement the physical radar-threshold adapter; it only provides the ownership/lease contract that adapter must use.
- The process-model backend for HVAC/covers is a contract only. No MPC/model-based RL controller is installed here.
- Existing entity-level Home Assistant automation handoff journals remain unchanged for rollback compatibility. New Control acquisition is device-aware, while a future migration can consolidate those journal keys once enough production restart data exists.
- Explicit mappings are operator configuration; Stage 15 does not infer physical identity from names, topology guesses or behavioral correlation.
