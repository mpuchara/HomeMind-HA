"""Compatibility wrapper for the 0.12.1 fast-light objective.

The timing extension needs extra promotion-window counters, but it must not invalidate
promotion epochs for unrelated agents.  The implementation installs its timing wrappers
and this shim immediately restores the existing global promotion epoch version.  Timing
models are new in 0.12.1, so they need no legacy epoch migration; non-timing Tournament
state is therefore preserved exactly across the upgrade.
"""
import context_tournament_promotion as promotion_module
import fast_light_objective as objective


def patch_promotion_for_timing(timing_metric_row):
    version = int(getattr(promotion_module, "PROMOTION_EPOCH_VERSION", 1))
    objective._patch_promotion_for_timing(timing_metric_row)
    promotion_module.PROMOTION_EPOCH_VERSION = version


def install(service):
    version = int(getattr(promotion_module, "PROMOTION_EPOCH_VERSION", 1))
    try:
        return objective.install(service)
    finally:
        # Do not reset/relabel existing promotion windows for non-fast/non-timing agents.
        promotion_module.PROMOTION_EPOCH_VERSION = version


# Re-export the deterministic helpers used by tests/simulator without duplicating logic.
TIMING_METRIC_MODE = objective.TIMING_METRIC_MODE
benchmark_summary = objective.benchmark_summary
timing_metric_row_factory = objective.timing_metric_row_factory
timing_utility = objective.timing_utility
