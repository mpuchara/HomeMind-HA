# Release 0.7.12

- Replace semantic/domain/device electrical blacklists with an explicit `unit_of_measurement` filter.
- Consider every parseable HA entity as a context candidate, including phone, car, people, weather, calendar/template/virtual and camera/AI-score entities.
- Keep controllable entities and all siblings on detected actuator devices excluded from learning.
- Exclude only individual entities reporting electrical units such as V, A, W, VA, var, Wh/kWh, Ah and ohm.
- Do not blacklist an entire Shelly/ESPHome device just because one sibling reports electrical telemetry.
- Screen the broad universe historically, then keep only the most predictive compact context for live inference.
- Preserve fast 1/3/10 s short-series features for light/switch agents.
- Run a one-time broad Recorder candidate refresh on this training revision; steady-state maintenance remains limited to QUALIFIED policy context.
