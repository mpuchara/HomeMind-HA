# Adaptive AI v0.6.0

Adaptive AI is an experimental Home Assistant App that learns local control policies from Home Assistant history and live feedback, then acts directly through HA service calls. It does not generate YAML automations.

## v0.6: reactive 1-second control

The default interaction horizon is now **~1 second**. The goal is not to guess that someone will enter a room tens of seconds in advance. The goal is:

```text
occupancy / motion / other precursor changes
                ↓
Home Assistant WebSocket event
                ↓
Adaptive AI inference (~75 ms debounce)
                ↓
desired state
                ↓
HA service call
```

For example, when occupancy becomes `ON`, the lighting agent should have a chance to turn the light on before the user reaches for the switch.

## Confidence is now calibrated

A large Q-value margin is no longer enough to show high confidence. During policy rebuild, the newest part of local history is held out chronologically and used as a backtest before those samples are folded into final training.

The UI therefore separates:

- **Calibrated confidence** — capped by held-out reliability.
- **Structural confidence** — certainty of the linear RL model itself.
- **Held-out accuracy** and sample count.
- **Historical support** and **context novelty**.

A policy that is systematically wrong on recent unseen history should not be able to display 90% confidence.

## Desired-state learning

The controlled actuator itself is excluded from policy inputs. Stable accepted states reinforce the desired state rather than only teaching the next transition.

Historical reward is domain-aware. A light that stays on for 15 seconds is not considered a failed action merely because it changed within the old 90-second correction window. Strong negative historical evidence is primarily a rapid explicit user correction of an automatic/external action.

Binary/categorical context is centred (`ON=+1`, `OFF=-1`) so presence and motion transitions are strong signals rather than tiny numeric differences.

Manual historical actions use roughly one-second precursor context. Actions produced by existing Home Assistant automations use event-time context, because an immediate automation may react to the same presence/motion event that the agent should learn from.

## Context

Every usable HA entity remains a candidate. Each agent selects a compact relevant subset from the whole installation using:

- automation trigger/condition structure,
- Entity Registry area/device relationships,
- sensor capability fit,
- semantic locality,
- historical precursor timing.

The live model uses 128 explicit dimensions with current value, temporal deltas, recency and selected interactions.

## Safety

Auto-created agents start in **Shadow**. Control is gated by calibrated confidence, historical support and context novelty. Existing HA automations that target the same entity are shown as conflicts and block Control by default.

Use **Verify control** to confirm that an agent can reach the target through the same Home Assistant service path used by live Control.

## Install

1. Open **Settings → Apps → App Store** in Home Assistant.
2. Add repository: `https://github.com/mpuchara/HomeMind-HA`
3. Refresh the App Store.
4. Install or update **Adaptive AI**.
5. Start the App and optionally enable **Show in sidebar**.

When upgrading, update in place. **Do not uninstall the App or delete `/data`** if you want to preserve the long-term local history archive.
