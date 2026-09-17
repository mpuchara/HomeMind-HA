"""Shipped entrypoint for the explicit final runtime composition.

Runtime path remains:
    run.sh -> trial_queue_main.py -> preference_queue_main.py -> fast_queue_main.py -> queue_main.py

Stage 16 moves final Stage-11/13/14/15 composition into ``runtime_composition`` so this
entrypoint no longer stacks another hand-written ``prepare_engine_extensions`` wrapper.
The startup/train and quiet-start guards are installed after final composition is bound.
0.14.17 then adds the read-side UI lifeline so heavy historical training cannot starve
Ingress/status diagnostics. All guards are bound before ``core.main()`` starts lifecycle.
"""
import preference_queue_main as runtime
from runtime_composition import bind_final_composition
from startup_train_guard import install as install_startup_train_guard
from release_016_guard import install as install_release_016_guard
from release_017_ui_lifeline import install as install_release_017_ui_lifeline


core = runtime.core
RUNTIME_COMPOSITION_ROOT = bind_final_composition(runtime)
install_startup_train_guard(runtime)
install_release_016_guard(runtime)
install_release_017_ui_lifeline(runtime)


if __name__ == "__main__":
    core.main()
