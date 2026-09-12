# HomeMind / Adaptive AI 0.7.12

Adaptive AI is a local Home Assistant App that learns desired device states from Recorder history and live corrections, then calls Home Assistant services directly.

## 0.7.12 — broad all-entity context, unit-only electrical filter

This release removes semantic/domain/device blacklists from the learning candidate pool. Every parseable Home Assistant entity is allowed to compete as historical context unless it belongs to a detected controllable device or its `unit_of_measurement` is explicitly electrical (for example V, A, W, VA, var, Wh/kWh, Ah or ohm).

This means phone data, people/device trackers, weather, cars, calendar/template/virtual entities, camera/AI scores, ESPHome radar channels and custom sensors are all valid candidates. Names such as `energy`/`power`, `device_class`, diagnostic/hidden status and unusual domains no longer disqualify an entity by themselves.

The runtime still stays compact: full historical indexing screens the broad candidate universe, while each agent keeps only the most predictive context features for live inference. Fast light/switch agents continue to use short 1/3/10 s series, so CPU remains focused on a small selected model rather than every entity at runtime.

On upgrade, the new training revision performs a one-time historical refresh of all eligible context candidates, then rebuilds policies and applies the existing >78% qualification / PAUSED lifecycle.
