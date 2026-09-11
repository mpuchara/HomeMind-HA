# Changelog

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
