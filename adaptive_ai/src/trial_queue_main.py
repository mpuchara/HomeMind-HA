"""Shipped entrypoint for the explicit final runtime composition.

Runtime path remains:
    run.sh -> trial_queue_main.py -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py

Stage 16 moves final Stage-11/13/14/15 composition into ``runtime_composition`` so this
entrypoint no longer stacks another hand-written ``prepare_engine_extensions`` wrapper.
The release startup/train guard is installed only after that final composition is bound,
but before ``core.main()`` starts the HTTP/runtime lifecycle.
"""
import preference_queue_main as runtime
from runtime_composition import bind_final_composition
from startup_train_guard import install as install_startup_train_guard


core = runtime.core
RUNTIME_COMPOSITION_ROOT = bind_final_composition(runtime)
install_startup_train_guard(runtime)


if __name__ == "__main__":
    core.main()
