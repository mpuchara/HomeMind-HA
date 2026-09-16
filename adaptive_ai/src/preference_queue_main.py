"""Final runtime composition for preference and episode-aware Candidate evaluation.

The existing ``fast_queue_main`` stack remains authoritative. Preference and episode
metrics are installed after exact Shadow context/history exists but *before* atomic
promotion captures the standard Candidate status function. This keeps the stage-01 named
promotion gates authoritative while allowing stage-05 episode evidence to replace legacy
transition evidence only when independently labelled coverage is sufficient.

EpisodeEvaluator itself is created before the fast runtime extensions, so Live outcomes,
Experiments and Sensor Tournament all share one persistent contract before any worker can
observe HA. Durable provenance and the observation schema are then composed before the
Engine/EventStream/History workers start.
"""
import fast_queue_main as runtime
from agent_candidate_card_summary import install as install_candidate_card_summary
from agent_candidate_preference_metrics import install as install_candidate_preference_metrics
from agent_candidate_promotion_cycle import install as install_candidate_promotion_cycle
from agent_live_card_refresh import install as install_agent_live_card_refresh
from episode_evaluator import EpisodeEvaluator
from episode_evaluator_runtime import (
    install_candidate as install_candidate_episode_evaluator,
    install_core as install_core_episode_evaluator,
    install_tournament as install_tournament_episode_evaluator,
)
from provenance_runtime import install as install_provenance_runtime
from observation_contract import install as install_observation_contract

core = runtime.core
_original_prepare_engine_extensions = core.prepare_engine_extensions


def prepare_engine_extensions():
    # Episode evaluation is a first-class runtime service. Install it before fast_queue_main
    # constructs Tournament/Candidate extensions, so those extensions all bind the same
    # evaluator and no worker can race ahead of the additive episode schema migration.
    evaluator = getattr(core.ENGINE, "episode_evaluator", None)
    if evaluator is None:
        evaluator = EpisodeEvaluator(core.STORE)
    install_core_episode_evaluator(core, evaluator)

    # fast_queue_main imports these installers inside its preparation function. The
    # current architecture therefore requires a temporary composition interception to
    # place the episode adapters at the correct lifecycle points. Both module functions
    # are restored in finally; the installed service methods remain instance-local.
    import agent_candidate_shadow_context as shadow_context_module
    import fast_light_objective_runtime as fast_light_module

    original_shadow_context_install = shadow_context_module.install
    original_fast_light_install = fast_light_module.install
    installed = {"manager": None, "tournament": None}

    def install_fast_light_then_episodes(service):
        service = original_fast_light_install(service)
        service = install_tournament_episode_evaluator(service, evaluator)
        installed["tournament"] = service
        return service

    def install_shadow_context_then_preference(manager):
        manager = original_shadow_context_install(manager)
        manager = install_candidate_preference_metrics(manager)
        # Must precede atomic promotion so manager.status/_comparison_summary captured by
        # the atomic layer already contains episode-aware named gates.
        manager = install_candidate_episode_evaluator(manager, evaluator)
        installed["manager"] = manager
        return manager

    fast_light_module.install = install_fast_light_then_episodes
    shadow_context_module.install = install_shadow_context_then_preference
    try:
        _original_prepare_engine_extensions()
    finally:
        fast_light_module.install = original_fast_light_install
        shadow_context_module.install = original_shadow_context_install

    # Cross-cutting event identity follows the already-created episode evaluator. Neither
    # migration rewrites old vectors/labels; missing old provenance remains unknown.
    install_provenance_runtime(core)
    install_observation_contract(core)

    manager = installed["manager"] or (
        getattr(core.ENGINE, "agent_candidates", None) if core.ENGINE is not None else None
    )
    if manager is None:
        return

    # Card-only decision decoration is deliberately installed after the complete lifecycle
    # stack. Atomic promotion therefore keeps its existing safety snapshot, while the UI
    # receives the latest direct-parent Desired from the same observed Shadow event.
    manager = install_candidate_card_summary(manager)

    # User-facing Candidate names/counters are a display lifecycle layered outside the
    # immutable internal lineage. Promotion may therefore reset visible Gen numbering
    # without reusing historical generation IDs or weakening atomic promotion semantics.
    manager = install_candidate_promotion_cycle(manager)

    # Normal Live-agent cards use the same principle as Candidate cards: fast runtime
    # values are served independently from the heavy diagnostics/status response.
    install_agent_live_card_refresh(core)

    core.STORE.event(
        None, "info", "candidate_preference_metrics_ready",
        "Candidate preference, episode evaluation and fast timing objective enabled",
        {
            "contract": getattr(manager, "candidate_preference_contract", None),
            "metric": getattr(manager, "candidate_fast_metric", None),
            "half_life_opportunities": getattr(manager, "candidate_preference_half_life_opportunities", None),
            "install_order": "episode_core_before_fast_stack;episode_candidate_after_preference_before_atomic_promote",
            "episode_evaluator_contract": 1,
            "episode_domain": "light_power",
            "episode_candidate_contract": getattr(manager, "candidate_episode_contract", None),
            "episode_tournament_contract": (
                getattr(installed.get("tournament"), "_episode_evaluator_installed", False)
            ),
            "episode_action_boundary": "observer_only_no_actionintent_no_executor_dispatch",
            "card_decisions": getattr(manager, "candidate_card_decision_contract", None),
            "candidate_display": getattr(manager, "candidate_display_contract", None),
            "live_agent_cards": bool(getattr(core, "_agent_live_card_refresh_installed", False)),
            "provenance_contract": 1,
            "provenance_install_order": "after_runtime_composition_before_workers",
            "observation_contract": 1,
            "observation_schema_version": 12,
            "observation_policy_version": 11,
            "observation_install_order": "after_provenance_before_workers",
        },
    )


core.prepare_engine_extensions = prepare_engine_extensions


if __name__ == "__main__":
    core.main()
