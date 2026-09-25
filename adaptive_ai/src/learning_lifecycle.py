"""Stage-8 learning lifecycle decisions.

The boolean rebuild flag is retained for compatibility with the historical training
engine. rebuild_reason is the auditable product contract: ordinary incremental
learning has no rebuild reason, while every destructive/full replay names why it is needed.

An initial model build still uses the old rebuild machinery internally, but is explicitly
classified as initial_model_build rather than pretending that a prior model was rebuilt.
"""

STRUCTURAL_REBUILD_REASONS = frozenset({
    "feature_schema_change",
    "feature_mask_change",
    "backend_change",
    "action_space_change",
    "model_corruption",
    "incompatible_persisted_model",
    "major_drift",
    "repeated_incremental_failure",
    "feedback_history_retraction",
    "explicit_manual_rebuild",
})

INITIAL_MODEL_BUILD = "initial_model_build"


def normalize_rebuild_reason(reason, *, explicit=False, initial=False):
    if initial:
        return INITIAL_MODEL_BUILD
    value = str(reason or "").strip().lower()
    aliases = {
        "full_rebuild": "explicit_manual_rebuild" if explicit else "incompatible_persisted_model",
        "manual_rebuild": "explicit_manual_rebuild",
        "manual_feedback_undo_rebuild": "feedback_history_retraction",
        "needs_retrain": "incompatible_persisted_model",
        "schema_change": "feature_schema_change",
        "mask_change": "feature_mask_change",
    }
    value = aliases.get(value, value)
    if not value:
        value = "explicit_manual_rebuild" if explicit else "incompatible_persisted_model"
    if value not in STRUCTURAL_REBUILD_REASONS:
        value = "incompatible_persisted_model"
    return value


def training_request_decision(agent, has_model, *, explicit_rebuild=False, rebuild_reason=None):
    """Return the final Stage-8 lifecycle decision for an explicit Train/Rebuild action."""
    agent = dict(agent or {})
    has_cursor = agent.get("training_cursor_ts") is not None
    state = str(agent.get("training_state") or "")

    if explicit_rebuild:
        return {
            "rebuild": True,
            "resumed": False,
            "learning_path": "rebuild",
            "rebuild_reason": normalize_rebuild_reason(
                rebuild_reason, explicit=True
            ),
        }

    if not has_model:
        return {
            "rebuild": True,
            "resumed": False,
            "learning_path": "initial_build",
            "rebuild_reason": INITIAL_MODEL_BUILD,
        }

    if state == "needs_retrain":
        reason = (
            rebuild_reason
            or agent.get("rebuild_reason")
            or (agent.get("benchmark_detail") or {}).get("rebuild_reason")
            or "incompatible_persisted_model"
        )
        return {
            "rebuild": True,
            "resumed": False,
            "learning_path": "rebuild",
            "rebuild_reason": normalize_rebuild_reason(reason),
        }

    if has_cursor:
        return {
            "rebuild": False,
            "resumed": True,
            "learning_path": "incremental_replay",
            "rebuild_reason": None,
        }

    return {
        "rebuild": True,
        "resumed": False,
        "learning_path": "rebuild",
        "rebuild_reason": "incompatible_persisted_model",
    }


def correct_learning_path(*, structural_reason=None):
    """Manual Correct is incremental unless a named structural incompatibility exists."""
    if not structural_reason:
        return {
            "rebuild": False,
            "learning_path": "incremental_supervised_finetune",
            "rebuild_reason": None,
        }
    return {
        "rebuild": True,
        "learning_path": "rebuild",
        "rebuild_reason": normalize_rebuild_reason(structural_reason),
    }
