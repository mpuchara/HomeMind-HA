# Audit task 04 — live/history/Teach observation parity

Date: 2026-09-16

Scope: F03, F08–F09 and the feature-representation findings from audit section 6.4.

## Production execution path

The packaged add-on still starts through `run.sh -> preference_queue_main.py -> fast_queue_main.py`.
`preference_queue_main.prepare_engine_extensions()` composes the existing runtime first, installs durable provenance, then installs the observation contract before EventStream, History and control workers start. Executor remains the only physical Home Assistant service boundary.

The observation contract replaces one shared feature API used by live inference, replay and Teach. It does not add a second policy or dispatch path.

## Versioned feature contract

Policy version is raised from 10 to 11 and explicit feature schema from 11 to 12. Existing stored models are not read under the new schema. Agents whose persisted model uses an older policy/schema are marked `needs_retrain` and `paused`; their old `rl_models` bytes, feedback, history, labels, generations and rollback data remain on disk unchanged.

Each selected entity receives explicit channels:

- `value`
- `valid`
- `communication_age`
- `event_age`
- `quality`
- three trend channels
- `time_since_edge`
- non-ordinal categorical bits

The shared home-state tail now also contains an explicit `home:known` feature. A model estimate of occupancy 0 is therefore distinguishable from an unknown home-state estimate.

Because the feature dimension budget remains fixed, v12 selects fewer entities than the old four-slots-per-entity schema rather than silently increasing RAM/CPU/model size.

## Missingness and edges

`unknown`, `unavailable`, missing and numeric zero are separate observations. Missingness sets `valid=0`; numeric zero has `valid=1`.

Unavailable samples do not manufacture value transitions. Trend and edge reconstruction skip invalid values, so a `0 -> unavailable -> 0` sequence does not look like a physical edge.

When a fast numeric feature does not have enough high-resolution lag evidence, metadata contains `reconstruction_complete=false` and a concrete reason such as `high_resolution_numeric_history_unavailable`. Teach refuses to create a context signature from an incompletely reconstructable point.

## Time axis and delayed events

Feature extraction uses event time for physical state chronology and received time as the knowledge cutoff. A replay query at time T cannot use a sample whose event occurred before T but was only received after T.

Live sample metadata and replay-restored metadata use the same canonical `attributes.__hm_*` shape. A compatibility read remains for the brief top-level `_hm_*` representation so a rolling process restart does not invalidate an already buffered sample.

The base Engine remains authoritative for event acceptance. It rejects a `state_changed` event whose `last_updated` is older than the current state. The v12 high-resolution journal now records a websocket event only after that exact sample is accepted into the live temporal buffer; an out-of-order event rejected live is therefore not introduced later by replay.

## Physical units and categories

Numeric observations are canonicalized before scaling. Temperature supports Celsius, Fahrenheit and Kelvin and converts to Celsius; equivalent C/F measurements produce equivalent value/trend features. Pressure and selected common physical units are normalized similarly.

Open categorical states no longer occupy an ordered numeric axis. They use deterministic signed categorical bits, while common semantic states such as on/off, occupied/unoccupied and heat/cool keep explicit centered semantic values. This removes the old interpretation that two arbitrary category hashes have a meaningful numeric ordering.

## Bounded high-resolution retention

The long-term `entity_history` table is not widened or changed into an unbounded high-rate archive.

A separate additive `feature_observation_events` buffer is used only for entities consumed by enabled fast policies. Defaults:

- 24 hours high-resolution retention,
- maximum 2,048 events per entity,
- 50,000 events globally.

Decision/correction windows are stored in additive `feature_windows` / `feature_window_entities` tables. Defaults:

- 12 seconds before and 6 seconds after the anchor,
- protection for 7 days,
- maximum 512 windows per agent.

New events that fall into an already open window receive the same protection. Retention remains globally bounded.

## Shared live/replay/Teach extraction

`MultiHorizonPolicy.features`, historical `SQLiteTemporalTracker` and `Teaching.point_context/signature` use the same schema v12 extractor. Historical replay merges sparse `entity_history` with the bounded high-resolution buffer for selected features and observes both event-time and received-time cutoffs.

Teach signatures also persist `meta:feature_schema_version`, `meta:policy_version`, `meta:home_known` and a signature-contract marker. This keeps an old Teaching label from being silently interpreted as if it belonged to a newer feature representation. The brief early-v12 labels that were already created without these metadata remain valid because their feature values were already produced by schema v12; no old label is rewritten.

The feature metadata exposes reconstruction status rather than silently substituting a fictitious lag.

## Regression coverage

Acceptance tests cover:

- unknown != numeric zero,
- Celsius and Fahrenheit equivalence,
- unavailable gaps do not create physical edges,
- arbitrary categories are non-ordinal,
- `home:known` is explicit,
- live and replay vectors are identical on a millisecond sequence,
- Teach produces the same signature from that live/replay timeline and carries explicit schema/policy versions,
- a delayed sample is not visible before its received time,
- out-of-order samples rejected by the live temporal contract are not eligible for the high-resolution journal,
- sparse fast numeric context is marked non-reconstructable,
- per-entity and global retention are bounded,
- protected decision windows survive ordinary short retention,
- old policy/schema bytes are preserved while migration moves the agent to `needs_retrain`.

No test connects to or controls a real Home Assistant instance.

## Limitations

The bounded high-resolution buffer only improves faithful reconstruction from the moment schema v12 is running (or where equally precise Recorder history exists). Old sparse 30-second numeric history cannot be made millisecond-accurate retrospectively; those points are explicitly marked incomplete instead of being invented.

The categorical bit representation is non-ordinal but finite, so arbitrary open categories can theoretically collide. Known HA semantic states are represented explicitly, and select/option ordering remains separately guarded by the existing Teaching target-options fingerprint. A future schema revision can introduce per-entity categorical vocabularies if collision-free open-category identity becomes necessary.
