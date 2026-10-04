"""Named, verifiable composition for Engine.process_agent wrappers.

0.14.135 removes Candidate ownership of the process_agent monkey-patch without
changing the established before/base/after call semantics. The shared registry owns
the single wrapper installation point and exposes a snapshot for final composition.
"""
from __future__ import annotations

from dataclasses import dataclass


CONTRACT_VERSION = 1
EXPECTED_INSTALL_ORDER = (
    "manual_feedback_physical_equivalence",
    "context_tournament_shadow",
    "teach_rl_rebenchmark",
    "candidate_observation",
    "provenance",
    "observation",
    "tiny_mlp_shadow",
)


class ProcessAgentPipelineError(RuntimeError):
    pass


@dataclass
class _Layer:
    name: str
    handler: object
    next_handler: object


def _same_callable(left, right):
    if left is right:
        return True
    left_self, right_self = getattr(left, "__self__", None), getattr(right, "__self__", None)
    left_func, right_func = getattr(left, "__func__", None), getattr(right, "__func__", None)
    return (
        left_self is not None
        and left_self is right_self
        and left_func is not None
        and left_func is right_func
    )


def _state(engine):
    state = getattr(engine, "_process_agent_pipeline_state", None)
    if state is None:
        base = getattr(engine, "process_agent", None)
        if not callable(base):
            raise ProcessAgentPipelineError("engine.process_agent is not callable")
        state = {
            "contract_version": CONTRACT_VERSION,
            "base_handler": base,
            "top_handler": base,
            "layers": [],
            "by_name": {},
        }
        engine._process_agent_pipeline_state = state
    return state


def install_process_agent_wrapper(engine, name, factory):
    """Install one named wrapper around the current process_agent handler."""
    name = str(name or "").strip()
    if not name:
        raise ProcessAgentPipelineError("process-agent layer name is required")
    if not callable(factory):
        raise ProcessAgentPipelineError(f"process-agent layer {name!r} factory is not callable")

    state = _state(engine)
    existing = state["by_name"].get(name)
    if existing is not None:
        return existing.handler

    current = getattr(engine, "process_agent", None)
    if not _same_callable(current, state["top_handler"]):
        raise ProcessAgentPipelineError(
            "engine.process_agent was replaced outside the named process-agent pipeline "
            f"before installing {name!r}"
        )

    handler = factory(current)
    if not callable(handler):
        raise ProcessAgentPipelineError(
            f"process-agent layer {name!r} did not return a callable handler"
        )

    layer = _Layer(name=name, handler=handler, next_handler=current)
    state["layers"].append(layer)
    state["by_name"][name] = layer
    state["top_handler"] = handler
    engine.process_agent = handler
    engine.process_agent_pipeline_snapshot = lambda: process_agent_pipeline_snapshot(engine)
    return handler


def process_agent_pipeline_snapshot(engine):
    state = _state(engine)
    install_order = [layer.name for layer in state["layers"]]
    return {
        "contract_version": int(state["contract_version"]),
        "install_order_inner_to_outer": install_order,
        "call_entry_order_outer_to_inner": list(reversed(install_order)),
        "registered_layers": len(install_order),
        "top_handler_registered": _same_callable(
            getattr(engine, "process_agent", None), state["top_handler"]
        ),
    }


def assert_process_agent_pipeline(engine, expected=EXPECTED_INSTALL_ORDER):
    """Fail fast if process_agent wrapper ownership/order drifts."""
    state = _state(engine)
    expected = tuple(str(name) for name in expected)
    actual = tuple(layer.name for layer in state["layers"])
    if actual != expected:
        raise ProcessAgentPipelineError(
            "unexpected process-agent install order: "
            f"expected={expected!r} actual={actual!r}"
        )
    if not _same_callable(
        getattr(engine, "process_agent", None), state["top_handler"]
    ):
        raise ProcessAgentPipelineError(
            "engine.process_agent top handler was replaced outside the named pipeline"
        )

    previous = state["base_handler"]
    for layer in state["layers"]:
        if not _same_callable(layer.next_handler, previous):
            raise ProcessAgentPipelineError(
                f"broken process-agent link before layer {layer.name!r}"
            )
        previous = layer.handler
    if not _same_callable(previous, state["top_handler"]):
        raise ProcessAgentPipelineError("process-agent pipeline top link is inconsistent")
    return process_agent_pipeline_snapshot(engine)
