"""Stage-06 Candidate-only opt-in for physical manual feedback.

This module is intentionally a compatibility shim.  It does *not* wrap Engine methods.
The single physical/manual wrapper lives in ``manual_feedback.install_runtime_physical_equivalence``;
this installer only enables its Candidate-only mode and ensures that contract is present.
"""


def install(core):
    engine = getattr(core, "ENGINE", None)
    store = getattr(core, "STORE", None)
    if engine is None or store is None:
        return

    engine.manual_feedback_candidate_only = True

    from manual_feedback import install_runtime_physical_equivalence
    install_runtime_physical_equivalence(core, engine)

    if getattr(engine, "_manual_feedback_candidate_only_event", False):
        return
    engine._manual_feedback_candidate_only_event = True
    store.event(
        None, "info", "manual_feedback_live_isolation_ready",
        "Physical manual corrections keep immediate priority but learn only through Candidate",
        {
            "live_model_update": False,
            "action_boundary": "unchanged_executor_only",
            "runtime_contract": "single_physical_equivalence_wrapper",
        },
    )
