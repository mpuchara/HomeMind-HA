# Adaptive AI v0.6

Adaptive AI v0.6 changes the interaction model from multi-second speculative prediction to **fast reactive desired-state control**.

The intended behavior for a light is:

```text
presence / motion / lux changes
        ↓
~75 ms inference debounce
        ↓
desired state inferred immediately
        ↓
Home Assistant service call
        ↓
light changes before the user would normally reach the switch
```

The `~1 s` lead target is relative to the human manual action. The agent does not attempt to predict the physical world one second into the future; it reacts to precursor signals as soon as Home Assistant reports them.

## Confidence in v0.6

The displayed Confidence is no longer only internal model certainty. Offline replay performs chronological pre-update validation and tracks correctness separately for each desired action. Runtime confidence is capped by a conservative lower bound of that historical validation performance.

The agent card now exposes:

- **Confidence** — calibrated confidence used for Control.
- **Validation** — empirical hit rate and sample count for the currently desired action.
- **Support** — amount/similarity of historical evidence.
- **Novelty** — how unfamiliar the current context is.

This prevents a model that frequently predicts an easy/common state such as OFF from showing high confidence for a poorly learned ON decision.

## Context

All usable Home Assistant entities remain candidates. Each agent selects an explicit subset, normally no more than 28. The controlled actuator is excluded from policy inputs to avoid target leakage. Temporal features include current environmental value, short/long deltas and change recency.

## Existing data

Keep the App data directory when updating. v0.6 changes the training revision, so policies are rebuilt from `/data/adaptive_ai.db`. It does not need another complete Recorder bootstrap.

## Existing automations

Keep existing automations while evaluating agents in Shadow. By default Control is blocked if an enabled Home Assistant automation still targets the same entity, preventing two controllers from fighting each other.
