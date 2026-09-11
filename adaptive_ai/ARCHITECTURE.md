# Adaptive AI v0.5 architecture

```text
Home Assistant entities (whole-home candidate pool)
                 │
                 ├── Entity Registry / area / device relation
                 ├── existing automation trigger/condition hints
                 ├── target-domain sensor needs
                 └── historical precursor timing
                 ↓
        per-agent relevance selector
                 ↓
      up to 28 explicit HA entities
                 ↓
 current value + Δ1m + Δ5m + change recency
 + small interactions (controlled actuator excluded)
                 ↓
       128 explicit dimensions
                 ↓
   multi-horizon contextual RL ensemble
  5s / 15s / 30s / 60s (+ slower heads)
                 ↓
 confidence + historical support + novelty
                 ↓
       Shadow / guarded direct Control
                 ↓
          Home Assistant service call
                 ↓
 user correction / accepted outcome → reward
```

## Context selection

The whole Home Assistant state space remains a candidate pool. Selection is agent-specific and combines:

- existing automation trigger/condition entities for the target,
- same-device and same-area Entity Registry relationships,
- sensor classes expected for the target domain,
- semantic locality,
- historical entities that repeatedly change shortly before target actions.

Selection changes representation only; it never creates a reward or supervised target.

## Temporal representation

Each selected entity receives four explicit slots: current value, short delta, long delta and recent-change strength. The controlled actuator property is excluded from the policy context to prevent target leakage. Time-of-day/day-of-week and a few top-context interaction terms fill the remaining slots; the actuator value is read only by the HOLD/deadband/control gate.

## Multi-horizon offline RL

Historical target actions are replayed against the state that existed before the action separately for every prediction horizon. The same inferred outcome reward updates each horizon head. At runtime, heads compete using expected reward, confidence, support and novelty, with a small penalty against unnecessarily long lead times.

## Context support and novelty

A high policy score is insufficient for Control. Each head tracks historical context statistics and reports:

- **Confidence** — certainty of the action ranking.
- **Historical support** — comparable evidence for the current context/action.
- **Novelty** — distance from contexts observed during learning.

Default Control gates are support >= 20% and novelty <= 85%, in addition to the agent confidence threshold.

## Upgrade behavior

v0.5 keeps the existing long-term SQLite archive. A training-revision migration clears old policy models/historical-experience projections and deterministically rebuilds them from raw archived HA history. Recorder does not need to be fully re-imported.

## Desired-state replay (v0.5.1)

The controlled actuator property is intentionally excluded from policy inputs. It is an action variable, not an environmental observation. Feeding it back creates next-transition leakage (e.g. historical ON states precede OFF transitions). The runtime still reads the current actuator value for HOLD/deadband decisions.

Each historical dwell contributes the normal anticipatory onset sample plus at most three bounded persistence samples per prediction horizon. This makes the policy estimate the state that should be maintained in a context, not merely the next transition event.
