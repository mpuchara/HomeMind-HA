# F24 Product Runtime Benchmark Report

Date: 2026-09-18  
Branch: `fix-manual-override-runtime-hold`  
Base: `121e69ada10de92578da0bedd90a9c30b1831c39`  
Benchmark contract: F24 v2  
Seeds: 11, 23, 37  
Replicas per scenario/split: 1

## Scope

This is the deterministic product-level benchmark for the shipped runtime composition:

`run.sh -> trial_queue_main.py -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py -> main.py`

The benchmark uses separate hidden occupancy/light-need truth and HA-like observations with delay, noise, missing values, multiple reporting modes and `light -> lux` coupling. Future `sensor_moved` changes the Entity Registry area assignment for the same entity. `manual_change` emits an explicit target event with `context.user_id`.

No HA service is dispatched by Shadow/Candidate. The benchmark does not lower qualification thresholds, fabricate a positive `benchmark_score`, promote a challenger or deploy a model.

This follow-up fixes the production fast-runtime exception that used to suppress manual hold for lights/switches. The F24 scenario, thresholds and acceptance logic are unchanged.

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
| Needed-light fraction | 90.39% | 14.12% | 15.18% | 89.68% |
| False ON seconds | 156.33 | 5.33 | 9.00 | 178.00 |
| Premature OFF events | 1.67 | 26.33 | 35.00 | 1.00 |
| Mean ON delay | 0.12 s | 20.52 s | 9.36 s | 0.64 s |
| Chatter events | 3.00 | 43.33 | 66.00 | 0.67 |
| Corrections / 100 episodes | 200.00 | 261.11 | 300.00 | 147.22 |
| Manual-override violations in 11-tick window | 11.00 | 0.00 | 2.33 | 11.00 |
| Runtime manual-hold ticks | 0.00 | 115.00 | 0.00 | 0.00 |
| Mean inference cost on CI host | 0.0008 ms | 71.75 ms | 3.74 ms | 0.0007 ms |

The manual-priority regression is fixed: all three seeds observe the explicit user event, production has zero violations in the 11-tick protected window, and the final composed runtime enters manual hold. Per-seed production hold counts are 155, 155 and 35 ticks.

The benchmark clock is continuous between synthetic episodes and the production light hold remains the unchanged 300 s. Therefore a hold can remain active beyond the 11-tick assertion window and into later synthetic episodes, depending on shuffled scenario order. This is intentionally reported rather than shortened or reset for the benchmark. It makes the aggregate production policy even more conservative: false ON decreases further, while needed-light coverage and ON latency worsen.

The full-ridge challenger still does not solve the quality trade-off and remains Shadow-only.

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
- `manual_override_respected`
- `manual_override_enters_runtime_hold`
- `production_shadow_reaches_executor`
- `moved_sensor_topology_reaches_runtime`
- `all_required_scenarios_present`

Unmet:
- `needed_light_not_worse_than_fixed_by_more_than_2pp`
- `premature_off_not_worse_than_fixed`
- `corrections_not_worse_than_fixed`
- `all_seeds_have_future_control_qualification`

The manual criteria now pass without changing F24. Remaining unmet criteria stay visible and do not trigger automatic model deployment.

## CI verification

GitHub Actions run `35398707349` completed successfully for the runtime fix before the later test-cost-only optimization:
- Python 3.11: 883 tests, all passing;
- Python 3.13: 883 tests, all passing;
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

No production data migration is required. Existing explicit `explicit_user_v8` manual holds are no longer cleared by fast timing normalization at startup. Persisted model vectors, labels, Candidate lineage, rollback state and settings are not reinterpreted. ActionIntent -> Executor ownership remains unchanged. Shadow and unpromoted Candidate remain non-actuating.
