"""Compact read-only diagnostics for the Context Tournament UI.

This extension exists only to make the current Tournament state understandable in
Settings/Diagnostics.  It does not rank sensors, mutate schemas, create ActionIntent,
call Executor, or send Home Assistant services.
"""
import math


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def last_context_update(service, agent):
    """Return the latest successful promotion in a small UI-friendly shape."""
    getter = getattr(service, "promotion_status", None)
    if not callable(getter):
        return None
    try:
        promotion = getter(agent) or {}
    except Exception:
        return None
    promoted = promotion.get("promoted_entity")
    created_ts = _finite(promotion.get("last_promotion_ts"))
    if not promoted or created_ts is None:
        return None
    details = dict(promotion.get("details") or {})
    status = "promoted"
    history_id = None
    history = getattr(service, "schema_history", None)
    if callable(history):
        try:
            for row in history(agent["id"], limit=10) or []:
                if row.get("promoted_entity") != promoted:
                    continue
                row_ts = _finite(row.get("created_ts"))
                if row_ts is not None and abs(row_ts - created_ts) > 1.0:
                    continue
                status = str(row.get("status") or status)
                history_id = row.get("id")
                break
        except Exception:
            pass
    return {
        "created_ts": created_ts,
        "promoted_entity": str(promoted),
        "removed_entity": promotion.get("replaced_entity"),
        "expected_gain": _finite(details.get("gain")),
        "validation_samples": max(0, int(details.get("samples") or 0)),
        "status": status,
        "history_id": history_id,
    }


def install_context_ui_diagnostics(service):
    """Add display-only fields to runtime Context Tournament diagnostics."""
    if getattr(service, "_context_ui_diagnostics_installed", False):
        return service
    original_state_for_agent = service.state_for_agent

    def state_for_agent_with_ui(agent):
        payload = original_state_for_agent(agent)
        payload["last_context_update"] = last_context_update(service, agent)
        return payload

    service.state_for_agent = state_for_agent_with_ui
    service.context_ui_last_update = lambda agent: last_context_update(service, agent)
    service._context_ui_diagnostics_installed = True
    return service
