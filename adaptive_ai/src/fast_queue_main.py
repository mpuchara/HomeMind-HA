"""Adaptive AI entrypoint with FIFO training, device adapters and realtime timing."""
from device_targets import install as install_device_targets

# Domain-specific Home Assistant semantics must be registered before queue_main imports
# engine/history/executor symbols with ``from context import ...``.
install_device_targets()

# 0.10.5 finalizes manual-correction semantics while keeping policy/schema revisions
# intact and reporting the package version consistently through the runtime API.
import settings
settings.APP_VERSION = "0.10.5"

import queue_main as queued_runtime
from fast_runtime import install as install_fast_runtime
from manual_context_learning import install as install_manual_context_learning
from manual_feedback import install as install_manual_feedback
from manual_feedback_lifecycle import install as install_manual_feedback_lifecycle
from manual_feedback_static import install as install_manual_feedback_static

core = queued_runtime.core
_original_initialize_runtime = core.initialize_runtime


def initialize_runtime():
    _original_initialize_runtime()
    if core.runtime_available():
        changed = install_fast_runtime(core)
        if changed:
            core.STORE.event(None, "info", "fast_runtime_migration",
                             f"Realtime timing applied to {len(changed)} fast agent(s)",
                             {"agents": changed})


core.initialize_runtime = initialize_runtime
# Install the broad manual observer before feedback handlers so every correction can
# promote context before the +/- policy update is applied.
install_manual_context_learning(core)
install_manual_feedback(core)
install_manual_feedback_lifecycle(core)
install_manual_feedback_static(core)


if __name__ == "__main__":
    core.main()
