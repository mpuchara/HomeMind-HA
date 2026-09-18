# Stage 13 — calibrated confidence semantics and promotion metrics (F13, F24)

Date: 2026-09-17

## Runtime path

The shipped runtime path remains:

`run.sh -> trial_queue_main.py -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py -> main.py`

Stage 13 is installed in `trial_queue_main.prepare_engine_extensions()` after TrialRecord/Candidate composition and before workers start. It adds diagnostics, calibration journals and promotion gates only. It does not create an `ActionIntent`, does not call Home Assistant and does not bypass `Executor`.

## Why this change

Several previous fields used the word `confidence` for different quantities. In particular:

- `structural_confidence` is a heuristic decision-strength/gating score based on margin, uncertainty, coverage and support;
- legacy `preference_confidence` is a decayed Wilson-style preference-alignment score;
- room occupancy fields such as `occupancy_in_3s` are probability-like forecasts;
- `historical_support` is coverage, not quality;
- `last_expected_reward` is utility, not probability;
- held-out policy accuracy is an empirical quality diagnostic.

Stage 13 keeps legacy numeric fields readable for compatibility, but attaches explicit semantics and stops presenting them as probabilities of comfort/correctness.

## Canonical metric vocabulary

Contract version: `confidence_contract_v2`.

1. `presence_probability`
   - probability claim in `[0,1]`;
   - calibrated only from independent labelled episodes;
   - reports Brier score and reliability bins.

2. `forecast_uncertainty`
   - uncertainty score in `[0,1]`;
   - not a probability of failure.

3. `expected_action_utility`
   - expected reward/utility;
   - not a probability.

4. `data_coverage`
   - evidence/context support;
   - not action quality.

5. `empirical_policy_quality`
   - fixed future, paired child-vs-parent episode quality/cost with uncertainty intervals;
   - ON and OFF evidence are kept separate;
   - automation replay may screen/select but is not final calibration evidence.

6. `preference_alignment`
   - weighted preference-alignment lower-bound style score;
   - not a probability of comfort.

7. `decision_strength`
   - compatibility name for the legacy structural/gating score;
   - not a probability of comfort or correctness.

The same descriptor is exposed from Candidate status, Live runtime/status, BUILD_INFO and the UI overlay.

## Probability calibration

`ProbabilityCalibrationJournal` stores probability episodes in additive table `confidence_probability_episodes`.

A usable calibration row requires:

- a stable `episode_id`;
- a model key;
- a scope (for example room/area);
- timestamp;
- prediction in `[0,1]`;
- observed binary outcome;
- independent source kind.

Training/model-derived labels are persisted as non-independent but are excluded from calibration claims. Repeating the same episode id is idempotent and does not increase evidence.

`ConfidenceCalibrationService` is available as `engine.confidence_calibration`. For presence calibration the supported runtime entry point is `record_presence(...)`; it requires a stable episode id and reports through `presence_report(...)`.

The report includes:

- Brier score;
- 10-bin reliability table;
- mean prediction and observed frequency;
- calibration gap and overconfidence flag;
- raw episode count;
- dependency/decay-adjusted effective sample size.

## Dependence, decay and effective N

Rows use a bounded half-life weighting. Episodes in the same dependency cluster (or the same default 30 s scope/time cluster) are capped so a burst of correlated observations cannot count as many independent trials.

Effective sample size is calculated from the weights as `(sum w)^2 / sum(w^2)`. Promotion criteria use effective evidence rather than only raw row count.

## Candidate selection versus final evaluation

Candidate selection and final evaluation are disjoint.

`confidence_evaluation_epochs` stores a versioned epoch keyed by:

- parent generation;
- child generation;
- candidate model revision;
- confidence contract version;
- backend key.

The epoch is created only after the existing challenger-selection evidence reaches its required level. Its `selection_cutoff_ts` is then frozen.

Only later future episodes with an explicitly independent calibration label may enter the Stage-13 final evaluation. The first prefix reaching the fixed evidence target is locked in `final_end_ts` **whether quality passes or fails**. Subsequent UI polling or later observations do not enlarge the declared final test. A failed holdout therefore cannot be healed by waiting for easier rows.

Changing model revision creates a new epoch. The backend identity is recorded with the epoch; a backend/model change therefore requires new future evaluation rather than inheriting an old reliability claim.

## ON/OFF safety and abstention

Version 2 requires:

- at least 12 effective independent final episodes overall;
- at least 4 effective independent OFF episodes;
- at least 4 effective independent ON episodes.

These are additional to the existing Candidate gates; they do not reduce any pre-existing threshold. If the existing selection layer requires more than 12 episodes, Stage 13 waits for that larger selection requirement before freezing the cutoff.

OFF-only evidence cannot qualify ON. Missing evidence produces `abstain_insufficient_independent_evidence` / Shadow fallback rather than a fabricated score.

Final promotion requires more than evidence count. On the exact same locked rows Stage 13 computes:

- child empirical action quality;
- parent empirical action quality;
- paired child-minus-parent quality delta with a 95% interval;
- separate ON and OFF paired deltas.

The hard gate passes only when evidence is sufficient and the overall, ON and OFF paired lower bounds are not worse than the configured regression tolerance (default 3 percentage points). The independent final evaluation gate remains non-overridable.

## UI and compatibility

Legacy API fields remain present so stored clients are not broken. The UI renames:

- `Confidence` / `Live confidence` / `Candidate confidence` -> `Decision strength`;
- `Preference confidence` -> `Preference alignment`;
- `Behaviour benchmark` -> `Held-out policy quality`.

Live diagnostics separately show decision strength, expected action utility, data coverage, 3 s presence probability, forecast uncertainty and the old validation diagnostic. Candidate diagnostics additionally show the fixed independent final evaluation and separate ON/OFF future safety.

## Persistence / migration

Migration is additive only:

- `confidence_probability_episodes`;
- `confidence_evaluation_epochs`;
- additive calibration metadata on `candidate_generation_pairs`: `evidence_kind`, `calibration_eligible`, `dependency_cluster`, `calibration_outcome`, `calibration_parent_correct`, `calibration_child_correct`, `calibration_source_id`.

Old Candidate-pair rows are preserved and default to `legacy_unclassified` / `calibration_eligible=0`; they are never silently upgraded to independent calibration evidence.

Raw transition fields (`outcome`, `parent_correct`, `child_correct`) remain immutable behaviour history. Independent labels live in the separate `calibration_*` overlay. A direct HA user transition can populate that overlay; an independent EpisodeEvaluator label may also populate it through the Stage-13 recorder. Anonymous/external target transitions remain screening-only.

## Tests

Deterministic tests cover:

- OFF-only evidence cannot qualify ON;
- replaying the same calibration episode does not increase evidence;
- training-fit labels do not become independent calibration samples;
- 12 easy episodes from one room do not establish quality in another room;
- correlated bursts reduce effective sample size;
- inflated probability forecasts are detected by Brier/reliability calibration;
- inflated decision strength is flagged without calling it a probability;
- challenger-selection data and final-test data are disjoint;
- `final_end_ts` remains locked after completion;
- backend/model revision requires a new evaluation epoch;
- Live runtime exposes the same separated semantics;
- BUILD_INFO, UI and runtime use the same Stage-13 contract constants.

## Limitations

- The probability service requires an actually independent labelled outcome source; Stage 13 deliberately does not invent one from the model's own prediction or a repeated training label.
- The dependence correction is intentionally conservative and simple: explicit cluster when available, otherwise a 30 s scope/time bucket.
- Policy intervals are episode-level Wilson-style intervals using effective N; they are not a causal treatment-effect interval.
- Existing legacy validation statistics remain visible only as diagnostics. They are not substituted for the fixed Stage-13 future promotion test.
- No physical Home Assistant experiment or production backend switch is performed by this stage.

## Current-stack hardening after Stages 05–12

The original Stage-13 implementation had two current-stack gaps:

1. a final test was considered passed when enough rows existed, even if the Candidate performed worse on the fixed future holdout;
2. every external target transition in `candidate_generation_pairs` could be treated as independent policy quality, even when it was only an automation transition.

Contract v2 fixes both. Candidate selection may still use broader behavioural screening, but final calibration accepts only explicit independent evidence. The shipped runtime currently classifies a direct user target change as an independent preference label. Anonymous/external transitions are not calibration evidence. The contract also accepts a separately supplied `episode_evaluator_independent` label without rewriting the raw target-transition fact.

Final comparison is paired on identical future episodes. The result reports `child`, `parent`, `paired_delta` and separate `per_action_delta.OFF/ON` structures. `promotion_quality_passed` is the promotion-facing result; `sufficient_evidence` alone is never a pass.

Changing the policy backend or model revision creates a new evaluation identity, so a future production move from DiagonalLinUCB to another backend cannot inherit an older final-calibration epoch.

## Updated deterministic coverage

In addition to the original tests, v2 covers:

- anonymous automation transitions do not complete final calibration;
- direct user transitions are classified as independent evidence;
- raw transition history and calibration label fields remain separate;
- legacy pair-table migration preserves rows and marks them non-calibrating;
- a Candidate with sufficient ON/OFF evidence but worse paired future quality fails;
- that failed window locks and later easy observations cannot heal it;
- an independent EpisodeEvaluator label can be attached idempotently without rewriting the raw transition.