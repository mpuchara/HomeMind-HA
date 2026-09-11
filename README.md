# HomeMind / Adaptive AI 0.7.3

Adaptive AI is a local Home Assistant App that learns desired device states from Recorder history and live human corrections, then calls Home Assistant services directly instead of generating YAML automations.

## 0.7.3 — local-first fast lighting

This release improves fast targets such as stairs and corridor lights. Dedicated same-area / same-device occupancy sensors now have priority over neighbouring-room context. A kitchen sensor can still serve as a weak early cue for turning stairs lights ON, but the dedicated stairs sensor dominates the room's current occupancy and OFF timing.

Fast binary context uses 2 s / 12 s temporal windows and ~3 s edge recency. Historical replay is anchored to the nearest local occupancy edge, so an old Home Assistant automation that turned a light OFF a minute late no longer teaches the agent that minute-long delay.

Realtime inference debounce is 25 ms and context events wake only agents that actually use the changed entity. End-to-end latency still includes Home Assistant, Zigbee/Thread/Wi-Fi and the physical device.

Upgrading from 0.7.2 automatically rebuilds policies from the existing Adaptive AI local archive because the training representation changed. It does **not** require a full Recorder re-import. Keep `/data` when updating.

Diagnostics now expose the primary local sensor, last context trigger, upstream early cues, HA service latency and device acknowledgement time.

See [Polish installation guide](INSTALACJA_PL.md), [release notes](RELEASE_0_7.md), [architecture](adaptive_ai/ARCHITECTURE.md), [changelog](adaptive_ai/CHANGELOG.md) and [test report](TEST_REPORT.md).

This is a Home Assistant Supervisor App. New agents start in Shadow. Validate rebuilt policies before enabling Control on higher-impact targets.
