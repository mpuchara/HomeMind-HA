"""Named, verifiable composition for Engine.on_state_changed wrappers.

0.14.134 deliberately does not replace the established wrapper semantics with a new
dispatcher. It makes the existing composition explicit and fail-fast: every migrated
layer registers through this module, duplicate registration is a no-op, unregistered
overwrites are rejected, and the final composition root can assert the exact install
order before runtime workers start.
"""
from __future__ import annotations

from dataclasses import dataclass


CONTRACT_VERSION = 1
EXPECTED_INSTALL_ORDER = (
    "manual_feedback_lifecycle",
    "candidate_shadow",
    "provenance",
    "observation",
)


class StateEventPipelineError(RuntimeError):
    pass


@dataclass
class _Layer:
    name: str
    handler: object
    next_handler: object


def _state(engine):
    state = getattr(engine, "_state_event_pipeline_state", None)
    if state is None:
        base = getattr(engine, "on_state_changed", None)
        if not callable(base):
            raise StateEventPipelineError("engine.on_state_changed is not callable")
        state = {
            "contract_version": CONTRACT_VERSION,
            "base_handler": base,
            "top_handler": base,
            "layers": [],
            "by_name": {},
        }
        engine._state_event_pipeline_state = state
    return state


def install_state_event_wrapper(engine, name, factory):
    """Install one named wrapper around the current registered top handler."""
    name = str(name or "").strip()
    if not name:
        raise StateEventPipelineError("state-event layer name is required")
    if not callable(factory):
        raise StateEventPipelineError(f"state-event layer {name!r} factory is not callable")

    state = _state(engine)
    existing = state["by_name"].get(name)
    if existing is not None:
        return existing.handler

    current = getattr(engine, "on_state_changed", None)
    if current is not state["top_handler"]:
        raise StateEventPipelineError(
            "engine.on_state_changed was replaced outside the named state-event pipeline "
            f"before installing {name!r}"
        )

    handler = factory(current)
    if not callable(handler):
        raise StateEventPipelineError(
            f"state-event layer {name!r} did not return a callable handler"
        )

    layer = _Layer(name=name, handler=handler, next_handler=current)
    state["layers"].append(layer)
    state["by_name"][name] = layer
    state["top_handler"] = handler
    engine.on_state_changed = handler
    engine.state_event_pipeline_snapshot = lambda: state_event_pipeline_snapshot(engine)
    return handler


def state_event_pipeline_snapshot(engine):
    state = _state(engine)
    install_order = [layer.name for layer in state["layers"]]
    return {
        "contract_version": int(state["contract_version"]),
        "install_order_inner_to_outer": install_order,
        "call_entry_order_outer_to_inner": list(reversed(install_order)),
        "registered_layers": len(install_order),
        "top_handler_registered": getattr(engine, "on_state_changed", None)
        is state["top_handler"],
    }


def assert_state_event_pipeline(engine, expected=EXPECTED_INSTALL_ORDER):
    """Fail fast if the shipped event wrapper chain differs from its contract."""
    state = _state(engine)
    expected = tuple(str(name) for name in expected)
    actual = tuple(layer.name for layer in state["layers"])
    if actual != expected:
        raise StateEventPipelineError(
            "unexpected state-event install order: "
            f"expected={expected!r} actual={actual!r}"
        )
    if getattr(engine, "on_state_changed", None) is not state["top_handler"]:
        raise StateEventPipelineError(
            "engine.on_state_changed top handler was replaced outside the named pipeline"
        )

    previous = state["base_handler"]
    for layer in state["layers"]:
        if layer.next_handler is not previous:
            raise StateEventPipelineError(
                f"broken state-event link before layer {layer.name!r}"
            )
        previous = layer.handler
    if previous is not state["top_handler"]:
        raise StateEventPipelineError("state-event pipeline top link is inconsistent")
    return state_event_pipeline_snapshot(engine)
