"""Final runtime wrapper installing preference-aware Candidate evaluation.

The existing fast_queue_main stack remains authoritative. This wrapper adds the
preference/timing metric after the full Candidate lifecycle has been composed, without
changing the underlying training, lineage or atomic-promotion layers.
"""
import fast_queue_main as runtime
from agent_candidate_preference_metrics import install as install_candidate_preference_metrics

core = runtime.core
_original_prepare_engine_extensions = core.prepare_engine_extensions


def prepare_engine_extensions():
    _original_prepare_engine_extensions()
    manager = getattr(core.ENGINE, "agent_candidates", None) if core.ENGINE is not None else None
    if manager is None:
        return
    install_candidate_preference_metrics(manager)
    core.STORE.event(
        None, "info", "candidate_preference_metrics_ready",
        "Candidate preference confidence and fast timing objective enabled",
        {
            "contract": getattr(manager, "candidate_preference_contract", None),
            "metric": getattr(manager, "candidate_fast_metric", None),
            "half_life_opportunities": getattr(manager, "candidate_preference_half_life_opportunities", None),
        },
    )


core.prepare_engine_extensions = prepare_engine_extensions


if __name__ == "__main__":
    core.main()
