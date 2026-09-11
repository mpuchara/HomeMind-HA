# HomeMind / Adaptive AI 0.7.4

Adaptive AI is a local Home Assistant App that learns desired device states from Recorder history and live human corrections, then calls Home Assistant services directly.

## 0.7.4 — actuator-free context

Every directly controllable Home Assistant entity is excluded from every agent input context. When Entity Registry metadata links multiple entities to a controllable physical device, all sibling entities on that device are excluded too. This removes actuator-to-actuator shortcuts and forces policies to learn from sensors, occupancy, environment, time and other non-actuator context.

A hard runtime guard also zeroes excluded actuator features if an old/stale schema still contains them. Entity Registry membership changes clear in-memory policy schemas so device-level exclusions are refreshed.

The local-first fast-control behavior from 0.7.3 remains: dedicated local occupancy/presence sensors are prioritized, neighbouring-room sensors can remain weaker upstream ON cues, and local occupancy dominates OFF timing.

Upgrading from 0.7.3 bumps the feature/policy/training revision and rebuilds policies automatically from the existing local Adaptive AI archive. No full Recorder re-import is required. Keep `/data` when updating.

Diagnostics show `Controllable-device inputs excluded`, primary local sensor, latest context trigger, upstream cues, HA service latency and acknowledgement timing.

See `CHANGELOG.md` for release details. New agents start in Shadow; validate rebuilt policies before enabling Control on safety-sensitive or high-impact targets.
