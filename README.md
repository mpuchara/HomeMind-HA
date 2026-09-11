# HomeMind / Adaptive AI 0.7.4

Adaptive AI is a local Home Assistant App that learns desired device states from Recorder history and live human corrections, then calls Home Assistant services directly instead of generating YAML automations.

## 0.7.4 — actuator-free context

Agent inputs are now strictly sensor/environment context. Any Home Assistant entity that is itself controllable is excluded from learning and inference for every agent. When Entity Registry metadata identifies a physical device as controllable, all sibling entities on that same device are excluded as well.

This prevents actuator-to-actuator shortcuts such as one lamp, switch, setpoint, cover, fan or media-player state becoming a predictor for another controller. A stale policy schema is also protected by a runtime zeroing guard, so excluded actuator features cannot silently return after an upgrade.

The local-first fast-lighting behavior from 0.7.3 remains: dedicated same-area / same-device occupancy sensors have priority, neighbouring-room sensors can remain weaker early ON cues, and local occupancy dominates OFF timing.

Upgrading from 0.7.3 changes the feature/policy/training revision, so policies rebuild automatically from the existing Adaptive AI local archive. A full Recorder re-import is not required. Keep `/data` when updating.

Diagnostics expose how many controllable-device inputs were excluded, together with the primary local sensor, last context trigger, upstream early cues, HA service latency and device acknowledgement time.

See [changelog](adaptive_ai/CHANGELOG.md) and [app documentation](adaptive_ai/README.md).

This is a Home Assistant Supervisor App. New agents start in Shadow; validate rebuilt policies before enabling Control on higher-impact targets.
