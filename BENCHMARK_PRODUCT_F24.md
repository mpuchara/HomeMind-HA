# F24 Product Runtime Benchmark Report

Date: 2026-09-19  
Branch: `fix-f24-needed-light-photometric-leakage`  
Base: `6d1631ae11e5d269f6160df654cf2a4a5ff8b7df`  
Release candidate: `0.14.30`  
Benchmark contract: F24 v2  
Seeds: 11, 23, 37  
Replicas per scenario/split: 1

## Scope

This is the deterministic product-level benchmark for the shipped runtime composition:

`run.sh -> trial_queue_main.py -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py -> main.py`

The benchmark keeps hidden occupancy/light-need truth separate from HA-like observations. Observations include delay, noise, missing values, multiple reporting modes, `light -> lux` coupling, a moved-sensor topology change and an explicit manual target event with `context.user_id`.

Shadow/Candidate never dispatch Home Assistant services. F24 scenarios, thresholds, acceptance criteria and qualification gates are unchanged in this work.

## Product changes evaluated

Two production-runtime issues were fixed without changing F24:

1. **Fast-light feature contract v2**
   - an illuminance sensor no longer feeds lamp-emitted light back as if it were causal ambient context;
   - while a light is ON, v2 uses the last causal pre-action ambient lux when available;
   - startup ON without a causal lux baseline remains unknown instead of using self-emitted lux;
   - nominal fast-light transport metadata is centred so repeated healthy `valid/quality/communication_age` columns do not accumulate an OFF class-frequency bias;
   - already persisted schemas without a feature-contract marker remain contract v1, so old vectors are not reinterpreted.

2. **Asymmetric fast-light OFF confirmation**
   - ON remains immediate;
   - only `historical_policy_bootstrap` OFF from an already-ON light must remain continuously recommended for 6 seconds;
   - a physical manual OFF, scoped instruction, explicit preference and experiment bypass this confirmation;
   - the filter is runtime-only and does not rewrite model weights, labels, history, Candidate lineage or rollback data.

The existing 300-second explicit-user manual hold remains unchanged.

## Data split

- train: historical demonstrations used for fitting;
- validation: held-out chronological demonstrations used for calibration/selection;
- future test: untouched hidden truth used only for product evaluation.

Split episode IDs remain disjoint for every seed.

## Aggregate results

Means over seeds 11, 23 and 37. CI stores 95% confidence intervals in the raw JSON artifact.

| Metric | Fixed automation | Production current | Full-ridge Shadow | Conservative fallback |
|---|---:|---:|---:|---:|
| Needed-light fraction | 90.39% | **79.36%** | 33.81% | 89.68% |
| False ON seconds | 156.33 | **91.33** | 19.33 | 178.00 |
| Premature OFF events | 1.67 | **1.67** | 45.33 | 1.00 |
| Mean ON delay | 0.12 s | 13.42 s | 1.67 s | 0.64 s |
| Chatter events | 3.00 | **1.33** | 87.33 | 0.67 |
| Corrections / 100 episodes | 200.00 | **127.78** | 319.44 | 147.22 |
| Manual-override violations in 11-tick window | 11.00 | **0.00** | 4.33 | 11.00 |
| Runtime manual-hold ticks | 0.00 | 115.00 | 0.00 | 0.00 |
| Mean inference cost on CI host | 0.0008 ms | 70.51 ms | 3.17 ms | 0.0007 ms |

Relative to the post-manual-hold baseline, production `premature_off_events` falls from 26.33 to 1.67, `needed_light_fraction` rises from 14.12% to 79.36%, chatter falls from 43.33 to 1.33 and corrections fall from 261.11 to 127.78 per 100 episodes.

The 6-second OFF confirmation was retained because it is the first tested duration that satisfies the unchanged premature-OFF acceptance criterion. Longer delays were not introduced merely to improve benchmark numbers.

## Per-seed production result

| Seed | Needed-light | False ON s | Premature OFF | Chatter | Corrections / 100 |
|---|---:|---:|---:|---:|---:|
| 11 | 75.80% | 75 | 2 | 2 | 116.67 |
| 23 | 71.53% | 92 | 1 | 1 | 133.33 |
| 37 | 90.75% | 107 | 2 | 1 | 133.33 |

Every seed reaches the final Shadow Executor path for all 720 decision ticks; fallback use is zero.

## Control qualification

All three seeds now pass the unchanged binary Control qualification. The existing requirement remains a 95% Wilson lower bound above 78% for every action.

| Seed | Validation balanced accuracy | Lowest per-action Wilson bound | Control qualification |
|---|---:|---:|---|
| 11 | 86.80% | 82.52% | pass |
| 23 | 86.92% | 82.52% | pass |
| 37 | 86.80% | 82.52% | pass |

No qualification threshold was lowered.

## Acceptance criteria

Passed:
- `false_on_not_worse_than_fixed`
- `premature_off_not_worse_than_fixed`
- `corrections_not_worse_than_fixed`
- `manual_override_respected`
- `manual_override_enters_runtime_hold`
- `production_shadow_reaches_executor`
- `moved_sensor_topology_reaches_runtime`
- `all_seeds_have_future_control_qualification`
- `all_required_scenarios_present`

Unmet:
- `needed_light_not_worse_than_fixed_by_more_than_2pp`

An unmet criterion remains a valid benchmark finding and never triggers automatic deployment or threshold relaxation.

## Manual priority

All three seeds observe the explicit user target event. Production has zero violations inside the 11-tick protected manual window and enters the unchanged authoritative manual hold. Per-seed production hold counts remain 155, 155 and 35 ticks because the synthetic clock continues across shuffled future episodes.

The OFF confirmation never weakens manual priority: once the user physically turns the light OFF, the current target is already OFF and the confirmation filter is bypassed.

## CI verification

GitHub Actions run `35428266370` validates the 0.14.30 startup-performance changes on the unchanged F24 v2 contract:
- Python 3.11: 894 tests, all passing;
- Python 3.13: 894 tests, all passing;
- compileall: passing;
- JS syntax checks: passing;
- Stage 17 history-cost smoke: passing;
- Stage 18 unchanged 3-seed final-composition F24: passing as an execution;
- source entrypoint smoke: passing;
- built image smoke: passing;
- anticipation/context-tournament/fast-light simulators: passing.

A successful benchmark job means the deterministic benchmark executed correctly. Product acceptance is represented by the explicit criteria above.

## Artifact

CI publishes `product-benchmark-f24-py311/product-benchmark-f24.json` with per-seed metrics, aggregates, 95% confidence intervals, runtime-composition descriptor and `unmet_criteria`.

## Migration and compatibility

No destructive production migration is introduced.

Persisted observation schemas without `feature_contract_version` continue to deserialize as contract v1 and keep their old vector semantics. Fresh/rebuilt policies use contract v2. Existing raw history, explicit feedback, Candidate lineage, generation history, rollback state and settings remain preserved.

ActionIntent -> Executor ownership is unchanged. Shadow and unpromoted Candidate remain non-actuating.
