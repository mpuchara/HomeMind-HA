# HomeMind Adaptive AI 0.12.0

## Sensor Tournament

0.12.0 closes the automatic context-selection loop while keeping the existing Control boundary unchanged. Candidate sensors are discovered from live Home Assistant context, evaluated as non-controlling challengers on strictly future/prequential outcomes, promoted only after sustained incremental predictive gain, and placed under a probation/rollback period after a schema change.

Production defaults for automatic promotion are conservative: at least 40 scored future samples, at least 3 days of observation, at least 3 percentage points cumulative gain, three consecutive non-overlapping 24-hour winning windows, and a 24-hour per-agent promotion cooldown. Replacing a primary sensor requires more than 7 percentage points of gain unless the global threshold is stricter.

A challenger never creates an ActionIntent and never dispatches a Home Assistant service. Promotion changes only the policy feature schema. A schema change affecting a Control agent forces that agent back to Shadow and requires fresh qualification. The exact previous serialized model and schema are retained during probation so a materially worse promoted schema can be rolled back automatically.

## Manual learning remains separate

Wrong decision/manual correction remains immediate action learning. One or two corrections cannot change the feature schema: Teach feature selection still requires at least 12 labels, at least 5 examples in each binary action class, and at least 2 observation days. Teach fine-tuning invalidates the old Control qualification and re-establishes it only from fresh future Shadow evidence.

## Persistence and migration

The application version is 0.12.0. LinUCB persistence remains compatible, so `MultiHorizonPolicy.VERSION` stays at 10. Feature-schema persistence has no new mandatory fields, so `ExplicitFeatureSchema.VERSION` stays at 11.

SQLite migration remains additive. Existing agents, Recorder/local archive history, Teach labels, feedback, policy models and behavior benchmarks are preserved. Sensor Tournament, promotion, quality, schema-history, probation and event-state tables use `CREATE TABLE IF NOT EXISTS`; no destructive migration is introduced by this release.

## Mandatory release validation

The release suite contains explicit acceptance tests for negative calibration, Teach evidence gates, immediate manual learning, non-controlling challengers, rejection/promotion gates, primary-sensor protection, Shadow requalification, rollback with preserved old schema/model, sensor reliability, prequential ordering, Teach requalification, agent isolation and restart persistence.

`tools/simulate_context_tournament.py` adds three deterministic release scenarios:

- **A — good new sensor:** sensor B appears after the initial phase, becomes a challenger, demonstrates 90% future balanced accuracy versus 75% for the champion and is promoted.
- **B — random correlation:** 300 noise sensors include deliberately perfect tiny-sample false positives. The top four may become challengers, but future evidence is ~50% and none can enter the active schema.
- **C — concept drift:** sensor A starts as the active best input. After behavior changes, live discovery makes B the challenger; B proves sustained future gain and the agent automatically transitions from A to B without a manual Rebuild.

Both the existing anticipation simulator and the Sensor Tournament simulator run in the GitHub validation workflow together with the complete Python test suite, JS syntax checks, compileall and Docker smoke test.
