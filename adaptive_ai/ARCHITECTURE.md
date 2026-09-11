# Adaptive AI v0.6 architecture

```text
Home Assistant state_changed event
            │
            ├── presence / motion / lux / door / media / weather / ...
            ▼
      ~75 ms debounce
            ▼
agent-specific explicit context (up to 28 entities / 128 dims)
            ▼
reactive desired-state contextual RL
            ▼
raw policy certainty
      ∩ chronological action-specific validation
            ▼
calibrated confidence + support + novelty
            ▼
Shadow / guarded Control
            ▼
Home Assistant service call
```

## One-second manual lead target

The `1 s` target is relative to a human manual action, not a request to extrapolate the environment one second into the future. When a precursor sensor changes, inference runs immediately. Historical replay therefore trains from the newest environmental context at the action boundary, with the target actuator excluded from inputs.

## Confidence calibration

For accepted historical dwells, each agent performs a chronological pre-update prediction before learning from that dwell. The result is recorded separately for the action the model chose. Displayed/control confidence is capped by a conservative Wilson lower bound of this empirical action-specific hit rate. This prevents frequent OFF states or an overconfident linear model from producing misleading 80–90% confidence.

## Desired-state replay

Stable dwells still provide bounded persistence samples, so the policy learns that `presence=ON + low lux` can mean `desired light=ON`, not merely that ON eventually precedes an OFF transition.

## Upgrade

v0.6 changes the training revision and rebuilds policies from the existing local SQLite archive. It does not require a full Recorder re-import.
