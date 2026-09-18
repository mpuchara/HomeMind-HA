# F24 Product Runtime Benchmark Report

Date: 2026-09-18  
Branch: `task-18-product-benchmark-hardening`  
Base: `19a788c1d4161f7bf0ca891192096ed3d3e9b159`  
Benchmark contract: F24 v2  
Seeds: 11, 23, 37  
Replicas per scenario/split: 1

## Scope

This is the deterministic product-level benchmark for the shipped runtime composition:

`run.sh -> trial_queue_main.py -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py -> main.py`

The benchmark uses separate hidden occupancy/light-need truth and HA-like observations with delay, noise, missing values, multiple reporting modes and `light -> lux` coupling. Future `sensor_moved` changes the Entity Registry area assignment for the same entity. `manual_change` emits an explicit target event with `context.user_id`.

No HA service is dispatched by Shadow/Candidate. The benchmark does not lower qualification thresholds, fabricate a positive `benchmark_score`, promote a challenger or deploy a model.

## Data split

- train: historical demonstrations used for fitting;
- validation: held-out chronological demonstrations used for calibration/selection;
- future test: untouched hidden truth used only for product evaluation.

The split episode IDs are disjoint for every seed.

## Controllers compared

1. `fixed_automation`
2. `production_current` - final composed shipped runtime in Shadow
3. `full_ridge_shadow` - challenger only
4. `conservative_fallback`

## Aggregate results

Values below are means over 3 seeds. CI also stores 95% confidence intervals in the raw JSON artifact.

| Metric | Fixed automation | Production current | Full-ridge Shadow | Conservative fallback |
|---|---:|---:|---:|---:|
| Needed-light fraction | 90.39% | 17.32% | 15.18% | 89.68% |
| False ON seconds | 156.33 | 11.00 | 9.00 | 178.00 |
| Premature OFF events | 1.67 | 32.00 | 35.00 | 1.00 |
| Mean ON delay | 0.12 s | 8.39 s | 9.36 s | 0.64 s |
| Chatter events | 3.00 | 58.67 | 66.00 | 0.67 |
| Corrections / 100 episodes | 200.00 | 294.44 | 300.00 | 147.22 |
| Manual-override violations in 11-tick window | 11.00 | 2.00 | 2.33 | 11.00 |
| Runtime manual-hold ticks | 0.00 | 0.00 | 0.00 | 0.00 |
| Mean inference cost on CI host | 0.0007 ms | 73.89 ms | 3.74 ms | 0.0007 ms |

Interpretation: the current production policy strongly reduces false ON time, but this synthetic future test shows that it does so by becoming too conservative: needed-light coverage collapses and OFF/chatter/correction metrics regress materially. The full-ridge challenger does not solve this trade-off and remains Shadow-only.

## Control qualification

All three seeds are Shadow-qualified under the existing candidate benchmark threshold, but all three fail the stricter binary Control qualification because the 95% Wilson lower bound for action ON is below the unchanged 78% threshold:

| Seed | Validation balanced accuracy | ON lower bound | Control qualification |
|---|---:|---:|---|
| 11 | 88.89% | 77.12% | fail |
| 23 | 88.75% | 76.05% | fail |
| 37 | 88.71% | 75.70% | fail |

This distinction is intentional: historical/validation accuracy is not sufficient evidence for Control.

## Acceptance criteria

Passed:
- `false_on_not_worse_than_fixed`
- `moved_sensor_topology_reaches_runtime`
- `all_required_scenarios_present`

Unmet:
- `needed_light_not_worse_than_fixed_by_more_than_2pp`
- `premature_off_not_worse_than_fixed`
- `corrections_not_worse_than_fixed`
- `manual_override_respected`
- `manual_override_enters_runtime_hold`
- `all_seeds_have_future_control_qualification`

The unmet manual-hold criteria are intentionally retained as benchmark findings. The baseline Engine fixture exercises manual hold, while the final composed runtime benchmark reports `runtime_manual_hold_ticks=0`. Task 18 does not alter runtime learning/control behavior merely to make this benchmark pass; this is recorded as a regression/follow-up item.

## CI verification

GitHub Actions run `35394907836` completed successfully:
- Python 3.11: 882 tests, all passing;
- Python 3.13: 882 tests, all passing;
- compileall: passing;
- JS syntax checks: passing;
- Stage 17 history-cost smoke: passing;
- Stage 18 3-seed final-composition benchmark: passing as a benchmark execution;
- source entrypoint smoke: passing;
- built image smoke: passing;
- anticipation/context-tournament/fast-light simulators: passing.

A successful benchmark job means the benchmark executed deterministically and produced a report. It does **not** mean all product acceptance criteria passed.

## Artifact

CI publishes `product-benchmark-f24-py311/product-benchmark-f24.json` with per-seed metrics, aggregates, 95% confidence intervals, composition descriptor and `unmet_criteria`.

## Migration and compatibility

No production data migration is required. This task does not reinterpret persisted model vectors, labels, Candidate lineage, rollback state or settings. ActionIntent -> Executor ownership remains unchanged. Shadow and unpromoted Candidate remain non-actuating.
