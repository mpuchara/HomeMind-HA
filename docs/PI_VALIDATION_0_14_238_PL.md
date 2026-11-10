# HomeMind 0.14.238 — Recorder HTTP 500 / start-state lookback test

This is a **test build**, not a confirmed field fix. Fresh installations only.

## Precisely what changed

Home Assistant REST `/api/history/period` normally includes a synthesized state preceding the requested window. This activates an additional Recorder SQL lookback per entity. Large or damaged Recorder databases can fail this query with HTTP 500 even when the requested window is a minimum 30-minute slice. Discovery only needs actual state transitions to identify active devices, so v0.14.238 explicitly adds `skip_initial_state=` to **every** discovery request, not only right-hand split children. This avoids the extra lookback without omitting actual transitions. Full requests still include attribute-only changes (`significant_changes_only=0`).

## Verification

- Tests verify that the first discovery chunk succeeds against a simulated endpoint which throws HTTP 500 whenever the start-state SQL path is invoked.
- Existing Recorder slice, boundary, deduplication and HTTP 500 fail-closed tests must pass.
- The actual user's HA Core HTTP 500 root cause is unconfirmed: a failure with `skip_initial_state=` still present requires the exact `[recorder] history_http_error` line from the add-on and the matching HA Core traceback. The UI only contains `DiscoveryRecorderHTTP500` summary, not that diagnostic line.

## Safety and installation

Do not merge to main while Gate D is BLOCKER. Install manually on a fresh test installation with a full HA backup. Do not expose HA tokens in diagnostic logs. If 500 persists, report `[recorder] history_http_error` and HA Core exception at the same UTC timestamp. Do not claim success until a real Pi can complete discovery.
