"""Shipped entrypoint for the explicit final runtime composition.

Runtime path remains:
    run.sh -> trial_queue_main.py -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py

Stage 16 moves final Stage-11/13/14/15 composition into ``runtime_composition`` so this
entrypoint no longer stacks another hand-written ``prepare_engine_extensions`` wrapper.
"""
import preference_queue_main as runtime
from runtime_composition import bind_final_composition


core = runtime.core
RUNTIME_COMPOSITION_ROOT = bind_final_composition(runtime)


if __name__ == "__main__":
    core.main()
