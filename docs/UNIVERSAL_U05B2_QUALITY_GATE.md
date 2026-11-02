# U05b.2: offline quality evidence and unlabeled scoring

Base: U05b.1 PR #357 at `8d869fcffd8c5440546c8b82806ae163fcacd076`.
Production reference remains 0.14.232 / `f7384ba5476f26155ecb138034cc3350fa5bfda2`.
This increment provides the U05b.2 dataset/quality gate. Publication-time fresh
evidence, validated checkpoint lifecycle and operational collection are still
required; a passing offline report does not activate a model or complete U05b.

## Unlabeled inference

`OfflineActionContext` is a frozen projection of the existing U05a observation
and ActionSet, not a second provenance store. It has observation/source/registry
versions, timestamp, typed tokens, target and legal actions. It has **no desired
action, supervision label or episode label**. `context_from_sample` is a helper
for offline datasets; callers can construct the context directly from qualified
historical receipts without inventing a desired action.

`CoreScorer.scores_context` returns raw logits. `rank_context` exposes only raw
projections, `confidence=None`, `recommendation=None` and false control/promotion
flags. Empty legal actions abstain, and one legal action is a diagnostic
projection; training still needs at least two actions to have a ranking loss.
`scores(sample)` remains a compatible helper but ignores the sample's label.

Context admission checks immutable tuples, clock/version/source consistency,
duplicate actions, common descriptor/resource/current-state metadata, typed
binary/categorical values, finite observations and known historical metadata.
The same U03/U04 feature calculation is used by training and inference. No
replay, current HA lookup, new action authority or live policy is introduced.

## Deterministic validation contract

`validate_core_artifact(checkpoint, current_dataset, policy=ValidationPolicy())`
reloads the checksum-checked frozen weights, verifies the U05a content identity,
dataset and supervision revision, exact used sample manifest and chronological
split. It recomputes train-only normalization and rejects altered normalization
even when an artifact has been re-sealed with a coherent checksum. Predictions
and metrics are recomputed from weights; stored `after.accuracy` cannot fake a
passing report. Checksums provide integrity, not authentication of an untrusted
artifact producer or proof that no human peeked at the holdout.

The report binds artifact checksum, dataset/revision, encoder id, policy id,
baseline id, support, outcomes, slices and its own content checksum. It is a
conditional offline evidence report, not a new model checkpoint format.
Source weights affect learning and train-only modal baseline fitting; they
never multiply statistical evidence counts.

### Independent units and default support

Policy must be declared before examining holdout results. Defaults:

* `family_alpha=.05`: explicit primary improvement test error budget; not a
  required accuracy percentage or per-action model confidence.
* `accuracy_half_width=.25`: a deliberately broad support precision target.
  The two-sided Hoeffding support floor for bounded independent unit accuracies
  is `ceil(log(2/alpha)/(2*half_width**2))`, giving **30 units**.
* `independence_window_seconds=60`: a temporal grouping guard matching the
  shipped short-context time scale. It is not a claim that every 60 seconds
  creates independent observations. Increase it for slower correlated processes.

Whole episodes are indivisible. Their intervals of fixed time buckets are merged
when overlapping, including long episodes that span other episodes. Thus many
Correct points or renamed episodes in one burst cannot create independent votes.
Missing episode identity is insufficient evidence. A train/holdout embargo at
least as large as the declared window is required. Existing `chronological_split`
still excludes cross-partition episodes and labels not known by the training cut.

Every training semantic group requires enough train units for its neutral
baseline. Every device/property seen in either used partition needs future
coverage. The per-slice support floor allocates alpha across K device/property
slices: `ceil(log(2*K/alpha)/(2*half_width**2))`. For K=1/10/30/200 the required
future support per slice is 30/48/57/72 units. Global support cannot substitute
for a missing kitchen or bathroom validation slice.

These precision-motivated floors assume independently qualified units. Distinct
episode ids and elapsed time do not prove independence, causality or completeness.
The caller must supply verified offline evidence; historical metadata collection
gaps remain ineligible. Precision/support calculations and the primary sign test
are separate checks, not a universal joint deployment-confidence guarantee.

### Train-only neutral baseline

For each domain/property/action kind the validator fits a modal action template
using training labels only, with each independent unit contributing total mass
one. Categorical templates use semantic option labels rather than option indices.
Numeric templates use relative range position. The baseline alone may project
that modal position to the nearest currently legal proposal, with deterministic
ties; **Correct targets are never rounded or changed**.

The validator compares this modal policy with keep-current on training units
and chooses the better baseline per semantic group; a tie chooses keep-current.
Current values absent from the legal set abstain. No holdout labels select the
baseline, its template, normalization, epoch or network weights. An unseen
holdout semantic group cannot obtain a guessed baseline.

This is a simple neutral baseline, not a claim that the network beats installed
Ridge/TinyMLP. Existing-policy comparison and broader affected-agent acceptance
remain required by the later Candidate/Shadow lifecycle.

### Acceptance and regression checks

Every temporal/episode unit contributes one accuracy average. Improvement signs
are paired within units; ties are omitted from the existing repository's exact
one-sided sign-test/binomial tail (`exact_paired_mlp_win_p_value`). For multi-point
units this is a sign test on unit differences, not independent row McNemar.
Acceptance requires positive mean gain and a primary p-value no greater than
declared alpha. A perfect model tied with a perfect baseline still fails.

Observed negative gains in any device/property or action-class slice reject the
model, even when global accuracy improves. This conservative zero-observed-loss
guard prevents e.g. improved ON accuracy hiding more false activations. Class
slice counts/errors are reported, but no separate per-class precision bound is
claimed. No observed regression is not proof that true regression risk is zero.

States are `passed`, `validation_failed`, `insufficient_evidence`,
`dataset_invalidated`, `invalid_artifact` and `cancelled`, with explicit reasons.
An exact model-score tie abstains and counts as incorrect in accuracy; raw scores
are never called calibrated confidence.

## Safety and current limits

Both passing reports and core checkpoints retain `trained=false`,
`control_eligible=false` and `promotion_eligible=false`. No runtime/HTTP hooks,
HA services, ActionIntent/Executor calls, Candidate updates, SQL reads, migrations,
new dependencies, installation release, version bump or automatic merges occur.
Engine, RuntimeComposition, Correct/Candidate/Shadow, DeviceAgent and the shipped
training-budget implementation are unchanged.

The gate reuses the core's bounded 32 MiB feature cache and cooperative budget;
it does not start workers. Input/model objects remain unchanged. A caller must
provide current U05a evidence. There is no atomic publication transaction,
single-use holdout ledger or producer authentication yet: those are U05b.3 work.
Repeated evaluation after human/model selection against the same holdout cannot
be presented as a fresh independent validation result.

The report's scope is the pinned manifest, not every agent in an HA installation.
The current 2048-sample U05a cap cannot supply 72 independent future units to each
of 200 devices in one manifest. Such cases intentionally remain insufficient;
future bounded/chunked validation aggregation needs a separate contract. Do not
relax thresholds, omit affected devices or fill gaps with current HA state.

## Validation evidence

35 focused core/gate tests passed locally, including 21 new U05b.2 tests.
They cover default real-model acceptance, normalization/metric tampering,
baseline ties, repeated episodes/time bursts, temporal embargo, missing device
support, holdout noninterference, false-ON/device regressions, cancellation and
unlabeled zero/single-action inference. The deterministic one-device fixture
has 43 future units: accuracy 1.0 versus baseline .48837, 22 wins and no losses,
exact p-value 2.384185791015625e-7. This is a synthetic numerical test, not a
measured home-control benefit.

`tools/benchmark_universal_validation.py` covers 5/1, 50/10, 200/30 and 1000/200
sensor/device scenarios with explicitly synthetic 120-second-separated events.
Only the one-device case has sufficient slice support. The other three report
`insufficient_evidence`, rather than converting optimizer completion into
acceptance. Host validation times were .037/.591/2.701/14.780 seconds respectively.
Full receipts, policies, missing coverage and platform/source identity are in
`benchmarks/UNIVERSAL_U05B2_HOST.json`. These are WSL host values, not Pi4 or
HA/UI/event-to-intent acceptance. Insufficient cohorts still pay bounded feature
audit cost; fail-fast metadata admission is a later performance opportunity.

```python
from universal_scorer_validation import validate_core_artifact, ValidationPolicy

# Declare policy before looking at future results; evidence must be current.
report = validate_core_artifact(core_checkpoint, qualified_dataset,
                               policy=ValidationPolicy())
assert report['promotion_eligible'] is False
```

See `UNIVERSAL_U05B_HANDOFF.md` for exact commits, full-suite/CI references and
the remaining U05b.3/4 work.
