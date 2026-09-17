#!/usr/bin/env python3
"""Executable bootstrap for the F24 product runtime benchmark.

The synthetic event timeline is intentionally deterministic and historical.  The
production Engine and Executor must therefore share the same event-time clock while an
intent is created and validated; otherwise Executor TTL checks would compare synthetic
2023 timestamps with the runner wall clock and every Shadow intent would look expired.

Keep the clock bridge outside production modules.  It changes benchmark composition only
and preserves all production TTL/qualification thresholds.
"""
import benchmark_product_runtime as core


# Executor imported now_ts by value. Delegate it dynamically to engine.now_ts so the
# existing per-event patch in ProductionShadow.decide reaches both halves of the real
# ActionIntent -> Executor path.
core.executor_module.now_ts = lambda: core.engine_module.now_ts()

# Re-export the benchmark contract so tests and callers use the corrected composition.
SCENARIOS = core.SCENARIOS
SENSORS = core.SENSORS
DEFAULT_SEEDS = core.DEFAULT_SEEDS
_truth = core._truth
observation = core.observation
build_training_data = core.build_training_data
run_seed = core.run_seed
_quality_view = core._quality_view
run = core.run
engine_module = core.engine_module
executor_module = core.executor_module


if __name__ == "__main__":
    core.main()
