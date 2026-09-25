"""Training button semantics shared by queued and fallback HTTP paths.

Stage 8 keeps the legacy tuple helper for compatibility, but the authoritative decision
also exposes learning_path and rebuild_reason. Normal compatible Train is incremental;
only first-build or explicit/structural incompatibility enters the destructive replay path.
"""

from learning_lifecycle import training_request_decision


def train_request_decision(agent, has_model, **kwargs):
    return training_request_decision(agent, has_model, **kwargs)


def train_request_mode(agent, has_model):
    """Compatibility return (rebuild, resumed) for existing callers/tests."""
    decision = train_request_decision(agent, has_model)
    return bool(decision["rebuild"]), bool(decision["resumed"])
