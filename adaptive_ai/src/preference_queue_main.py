"""Final runtime wrapper installing preference-aware Candidate evaluation.

The existing ``fast_queue_main`` stack remains authoritative. Preference metrics must be
installed after exact Shadow context/history exists but *before* atomic promotion captures
the standard Candidate status function. This makes the timing/preference gate the real
standard promotion gate while preserving the later explicit custom-promotion override.

Durable provenance is installed through the same official engine-extension hook after the
core lifecycle stack is composed and before any Engine/EventStream/History worker starts.
The observation contract is installed immediately after provenance so event identity and
origin exist before the shared live/replay/Teach feature timeline starts recording.
"""
import fast_queue_main as runtime
from agent_candidate_card_summary import install as install_candidate_card_summary
from agent_candidate_preference_metrics import install as install_candidate_preference_metrics
from agent_candidate_promotion_cycle import install as install_candidate_promotion_cycle
from agent_live_card_refresh import install as install_agent_live_card_refresh
from provenance_runtime import install as install_provenance_runtime
from observation_contract import install as install_observation_contract

core = runtime.core
_original_prepare_engine_extensions = core.prepare_engine_extensions


def prepare_engine_extensions():
    # fast_queue_main imports Candidate extensions inside its preparation function. Patch
    # the immediately-pre-atomic shadow-context installer so preference metrics become
    # part of the composed manager before atomic promotion snapshots manager.status.
    import agent_candidate_shadow_context as shadow_context_module

    original_shadow_context_install = shadow_context_module.install
    installed = {"manager": None}

    def install_shadow_context_then_preference(manager):
        manager = original_shadow_context_install(manager)
        manager = install_candidate_preference_metrics(manager)
        installed["manager"] = manager
        return manager

    shadow_context_module.install = install_shadow_context_then_preference
    try:
        _original_prepare_engine_extensions()
    finally:
        shadow_context_module.install = original_shadow_context_install

    # Engine exists here, but workers have not started yet. Cross-cutting contracts are
    # composed in dependency order: provenance first, then the shared observation schema.
    # No HA event or command can race ahead of either persistent journal.
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
        "Candidate preference confidence and fast timing objective enabled",
        {
            "contract": getattr(manager, "candidate_preference_contract", None),
            "metric": getattr(manager, "candidate_fast_metric", None),
            "half_life_opportunities": getattr(manager, "candidate_preference_half_life_opportunities", None),
            "install_order": "after_candidate_shadow_context_before_atomic_promote",
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
