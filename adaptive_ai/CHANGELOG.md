# Changelog

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
