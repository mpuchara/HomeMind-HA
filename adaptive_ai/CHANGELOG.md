# Changelog

## 0.7.3
- Prioritize dedicated same-area / same-device occupancy sensors for fast light and switch agents.
- Keep neighbouring-room sensors as weaker upstream cues that may accelerate ON, but do not let them define OFF timing.
- Add fast binary temporal features (2 s / 12 s windows and ~3 s edge recency) instead of minute-scale persistence.
- Causal replay anchors historical ON/OFF actions to the local occupancy edge; delayed legacy automations no longer teach a one-minute OFF delay.
- Stop stable ON reinforcement when the primary local occupancy sensor becomes vacant.
- Reduce realtime inference debounce to 25 ms and schedule only agents affected by the changed context entity.
- Add diagnostics for primary local sensor, last context trigger and upstream early cues.
- Rebuild policies from the existing local archive; no full Recorder re-import is required.

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
