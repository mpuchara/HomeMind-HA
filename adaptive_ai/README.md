# Adaptive AI v0.5.2

> **v0.5.2 desired-state fix:** the controlled actuator value is no longer used as a policy input. Offline replay trains both anticipatory transition contexts and a bounded set of stable-dwell contexts, so a light that is correctly ON in an occupied room reinforces ON rather than making ON look like a precursor to the next OFF transition.

Adaptive AI is a Home Assistant App that learns local device-control policies from Home Assistant history and live rewards. It acts directly through Home Assistant service calls; it does not generate YAML automations.

## v0.5: selective temporal multi-horizon RL

v0.5 replaces the old 192-bucket whole-home feature hash with a smaller **128-dimensional explicit context** built separately for every agent.

1. Every usable Home Assistant entity is still considered as a context candidate.
2. Each agent selects up to 28 relevant entities using automation structure, Entity Registry area/device relationships, expected sensor classes, semantic locality and historical precursor timing.
3. Selected entities get explicit temporal features: current value, ~1 minute delta, ~5 minute delta and time since the last change.
4. The controlled actuator itself is excluded from policy context to avoid target leakage; its current value is used only when deciding whether the desired state requires a Home Assistant service call.
5. A small set of interaction features captures common combinations without a heavyweight neural network.
6. Separate contextual-RL heads learn several prediction horizons (default 5, 15, 30 and 60 seconds; slower targets automatically add 120 s, 300 s and/or 900 s horizons).
7. Control is gated by **confidence + historical support + context novelty**, not confidence alone.

Existing `/data/adaptive_ai.db` history is reused. Upgrading from v0.4.x rebuilds policies from the local archive and does **not** repeat the full 10-day Recorder import.

## Why this is better than 192 hashed dimensions

The previous hash bounded memory use but unrelated entities could collide in the same bucket. In v0.5, feature slots have explicit meanings and are agent-specific, so `Current learned influences` becomes readable and cross-entity hash collisions disappear.

The vector is smaller because the relevance selector removes unrelated context before inference. With hundreds of HA entities, an agent can still consider the whole home when choosing candidates, while the live policy typically operates on only a few dozen meaningful signals.

## Predictive behavior

Historical actions are replayed against the state that existed *before* the action for each horizon. A 15-second head therefore learns which context tends to precede an accepted action by about 15 seconds, while an HVAC head can learn much longer lead times.

Stable dwell periods are also sampled during offline replay. This teaches the policy that a state which remained accepted for a meaningful period is itself evidence of a desirable state, rather than treating every current state merely as a precursor to the opposite transition.

This does not guarantee that every manual action is predictable. The UI shows the currently selected prediction horizon so the behavior can be evaluated in Shadow first.

## Support and novelty

A high model score is not enough for Control:

- **Confidence** — certainty of the policy decision.
- **Support** — how much comparable historical evidence exists for the current context/action.
- **Novelty** — how unfamiliar the current context is compared with contexts observed during learning.

Defaults require support >= 20% and novelty <= 85%, in addition to the per-agent confidence threshold.

## Sensor recommendations

Recommendations are based on the context actually selected by a given agent, rather than on whether a sensor class exists somewhere else in the house. This makes suggestions such as illuminance, occupancy, CO2 or window sensing more useful for the target being controlled.

## Safety

Auto-created agents remain in **Shadow**. Micro-exploration remains OFF by default. Existing automation conflicts are shown before Control. Control diagnostics report ACTED / HOLD / BLOCKED / WAITING / ERROR and the exact Home Assistant service path used.

## Install in Home Assistant

1. Open **Settings → Apps → App Store**.
2. Open the repositories menu and add this repository URL: `https://github.com/mpuchara/HomeMind-HA`.
3. Refresh the App Store.
4. Install **Adaptive AI**.
5. Start the App and enable **Show in sidebar**.

When upgrading, update the App in place. **Do not uninstall it and do not delete `/data`** if you want to preserve the local history archive and learned policies.
