# Adaptive AI v0.5

After updating from v0.4.x, keep the App data directory. Adaptive AI will rebuild policies from `/data/adaptive_ai.db`; it should not need another complete Recorder import.

The main agent metrics are now:

- **Confidence**: policy certainty.
- **Support**: amount/similarity of historical evidence for the current context and action.
- **Novelty**: how unfamiliar the current context is compared with training history.
- **Prediction horizon**: how far ahead the selected policy head is trying to anticipate the action.

All usable HA entities remain candidates. Each agent selects an explicit subset, normally no more than 28. Historical precursor relevance is normalized by background reporting frequency so fast-updating sensors do not dominate feature selection. The UI shows which entities were selected and which features currently influence the decision.
