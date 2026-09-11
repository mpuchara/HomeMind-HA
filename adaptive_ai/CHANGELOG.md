# Changelog

## 0.6.0
- Changed the default interaction horizon to ~1 second; ordinary light/switch behavior is reactive to precursor events rather than 5–60 s speculative prediction.
- Reduced realtime inference debounce from 150 ms to 75 ms.
- Added chronological pre-update, action-specific confidence calibration so wrong predictions cannot retain high confidence just because the linear policy is internally certain.
- Added validation hit rate, conservative lower bound and sample count to agent diagnostics.
- Controlled actuator state remains excluded from policy inputs; stable desired-state dwells continue to reinforce the state that should be maintained.
- Shortened auto-agent action cooldowns for fast lights/switches to ~1 second.
- Existing policies are rebuilt from the local archive without repeating the full Recorder bootstrap.

## 0.5.2
- Reworked six agent metrics into a readable 3×2 desktop grid while retaining the mobile 2-column layout.
- Persisted expanded per-agent diagnostics across auto-refresh and page reloads.
- Improved Current/Desired visual emphasis and card spacing.

## 0.5.1
- Fixed target-state leakage that could make an ON device predict OFF because historical OFF transitions usually started from ON.
- Changed offline replay to desired-state learning with bounded stable-dwell experiences.
- Added Current vs Desired state and automation-conflict safety.

## 0.5.0
- Replaced the 192-bucket whole-home hash with 128 explicit, collision-free per-agent features.
- Added agent-specific relevance selection and temporal features.
- Added support and novelty gates, automation priors and realtime direct-control diagnostics.

## 0.4.1
- Added ACTED / HOLD / BLOCKED / WAITING / ERROR control diagnostics and Verify control.

## 0.4.0
- Added realtime WebSocket inference, automation scanning, Entity Registry filtering, broader discovery, sorting and search.
