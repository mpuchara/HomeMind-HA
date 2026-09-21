"""Training button semantics shared by queued and fallback HTTP paths.

Train is incremental once an agent has both a persisted model and a historical cursor.
Rebuild is the only user action that intentionally clears learned state and replays the
available local history from the beginning. A schema-invalidated agent (needs_retrain)
still requires a rebuild because its old model cannot be continued safely.
"""


def train_request_mode(agent, has_model):
    """Return (rebuild, resumed) for an explicit Train request."""
    has_cursor = agent.get("training_cursor_ts") is not None
    requires_rebuild = str(agent.get("training_state") or "") == "needs_retrain"
    incremental = bool(has_model and has_cursor and not requires_rebuild)
    return (not incremental), incremental
