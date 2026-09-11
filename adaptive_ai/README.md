# Adaptive AI v0.6.0

Adaptive AI is a Home Assistant App that learns local desired-state policies from Home Assistant history and live reward. It calls Home Assistant services directly; it does not generate YAML automations.

## v0.6: reactive desired-state RL

The goal is **not** to predict a light change 30–60 seconds in advance. The goal is to react to a precursor event (presence, motion, lux, door, media state, etc.) fast enough that the desired device state is applied roughly **one second before a person would normally act manually**.

The runtime subscribes to Home Assistant `state_changed` events and evaluates agents after a short 75 ms debounce. Historical learning uses the environmental context at the action boundary; the controlled actuator itself is excluded from policy inputs.

## Calibrated confidence

v0.6 fixes the problem where a policy could show 80–90% confidence while making obviously wrong desired-state predictions. Confidence is now capped by chronological, pre-update validation on historical accepted dwells. Validation is action-specific, so an agent that is good at predicting OFF but poor at ON cannot borrow confidence from the easier action.

The UI exposes:

- **Confidence** — conservative calibrated confidence used by Control.
- **Validation** — empirical historical hit rate and sample count for the currently desired action.
- **Support** — amount/similarity of historical evidence.
- **Novelty** — how unfamiliar the current context is.

A mathematically certain model with poor validation remains low-confidence.

## Context representation

All usable HA entities remain candidates. Each agent selects up to 28 relevant entities using automation structure, Entity Registry relations, expected sensor classes, semantic locality and historical precursor timing. Selected entities get explicit temporal features (current value, ~1 minute delta, ~5 minute delta and time since change) in a 128-dimensional collision-free vector.

## Safety

Auto-created agents start in **Shadow**. Existing automation conflicts are shown and Control is blocked by default while enabled HA automations still target the same entity. Micro-exploration is OFF by default.

## Install

Add this repository in **Settings → Apps → App Store → Repositories**:

`https://github.com/mpuchara/HomeMind-HA`

Then install **Adaptive AI**. When upgrading, update in place and keep `/data` to preserve the long-term local archive.
