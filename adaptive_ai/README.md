# HomeMind / Adaptive AI 0.7.3

Adaptive AI is a local Home Assistant App that learns desired device states from Recorder history and live human corrections, then calls Home Assistant services directly.

## 0.7.3 — local-first fast lighting

This release fixes a timing problem visible on fast lighting targets such as stairs and corridors.

For binary light/switch agents, the policy now reserves and strongly prioritizes dedicated same-area / same-device occupancy and presence sensors. A neighbouring-room sensor may still be useful as an early upstream cue for turning a light ON, but it is deliberately weaker and does not determine when the local room becomes vacant.

Fast binary context uses second-scale temporal features (2 s / 12 s windows and ~3 s edge recency) instead of carrying occupancy influence for minutes. Historical replay is also causal: if an old Home Assistant automation switched the light OFF a minute after the dedicated stairs sensor became vacant, training is anchored to the local vacancy edge rather than learning that one-minute delay.

The realtime inference debounce is 25 ms and event-driven evaluation schedules only agents affected by the changed context entity. Actual end-to-end latency still includes the Home Assistant event path and the physical device/network latency.

Upgrading from 0.7.2 changes the training revision, so policies are rebuilt automatically from the existing local Adaptive AI archive. A full Recorder re-import is not required. Keep `/data` when updating.

Diagnostics expose the primary local sensor, the last context trigger, upstream early cues, HA service latency and device acknowledgement time.

See `CHANGELOG.md` for release details. New agents start in Shadow; validate the rebuilt policy there before enabling Control on safety-sensitive or high-impact targets.
