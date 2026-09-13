"""Adaptive AI entrypoint with FIFO training, device adapters and realtime timing."""
from device_targets import install as install_device_targets

# Domain-specific Home Assistant semantics must be registered before queue_main imports
# engine/history/executor symbols with ``from context import ...``.
install_device_targets()

# 0.10.3 changes the feedback/UI path only, so keep policy/schema revisions intact while
# reporting the package version consistently through the runtime API.
import settings
settings.APP_VERSION = "0.10.3"

import queue_main as queued_runtime
from fast_runtime import install as install_fast_runtime
from manual_feedback import install as install_manual_feedback
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
install_manual_feedback(core)
install_manual_feedback_static(core)


if __name__ == "__main__":
    core.main()
