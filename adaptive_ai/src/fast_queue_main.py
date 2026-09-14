"""HTTP-first entrypoint; runtime extensions load in the background in dependency order."""
import queue_main as queued_runtime
from manual_feedback import install as install_manual_feedback
from manual_feedback_static import install as install_manual_feedback_static

core = queued_runtime.core


def prepare_runtime_extensions():
    # The database is assigned by main before this hook. Domain adapters must still
    # precede imports of engine/history/executor, which capture context functions.
    from device_targets import install as install_device_targets
    install_device_targets()
    from manual_context_learning import install as install_manual_context_learning
    install_manual_context_learning(core)


def prepare_engine_extensions():
    from fast_runtime import install as install_fast_runtime
    from manual_feedback import install_runtime_physical_equivalence
    from manual_feedback_lifecycle import install_runtime as install_lifecycle
    from historical_teach_install import install as install_historical_teach
    from historical_teach_reward import install as install_historical_teach_reward
    changed = install_fast_runtime(core)
    if changed:
        core.STORE.event(None, "info", "fast_runtime_migration",
                         f"Realtime timing applied to {len(changed)} fast agent(s)",
                         {"agents": changed})
    install_runtime_physical_equivalence(core, core.ENGINE)
    install_lifecycle(core)
    install_historical_teach(core)
    install_historical_teach_reward(core)
    core.STORE.event(None, "info", "manual_feedback_ready", "Manual correction feedback path ready", None)


core.prepare_runtime_extensions = prepare_runtime_extensions
core.prepare_engine_extensions = prepare_engine_extensions
# HTTP routes are cheap and need no database; observers are attached by the hook above.
install_manual_feedback(core, attach_runtime=False)
install_manual_feedback_static(core)


if __name__ == "__main__":
    core.main()
