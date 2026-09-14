"""Force a promoted Sensor Tournament schema back through Shadow before Control.

A feature-schema promotion changes the representation used by the live policy.  Even when
all pre-promotion challenger gates passed, the resulting migrated policy must not remain
in Control automatically.  This extension therefore performs one narrow lifecycle step:

    Control -> Shadow

for the single agent whose automatic Sensor Tournament promotion just changed schema.
Other agents are never touched.  Agents already in Shadow remain in Shadow; paused agents
remain paused.

The mode switch is persisted before releasing the Control handoff.  That ordering is a
safety boundary: Executor re-reads the agent mode before dispatch and will treat any
already-created intent as SHADOW once the mode has changed.  The previous automation
handoff is then released so controllers disabled for Control can be restored.
"""


PROMOTION_REASON = "sensor_tournament_promotion"
REQUALIFICATION_REASON = "schema_changed_shadow_requalification"


def _latest_history(service, agent_id):
    rows = service.schema_history(str(agent_id), limit=1)
    return dict(rows[0]) if rows else None


def install_promotion_shadow_requalification(service):
    """Wrap final Tournament observation and demote only the promoted Control agent."""
    if getattr(service, "_promotion_shadow_requalification_installed", False):
        return service
    if not hasattr(service, "schema_history"):
        raise RuntimeError("Schema history must be installed before Shadow requalification")

    original_observe = service.observe_shadow

    def observe_with_shadow_requalification(agent, state_map=None, changed_entities=None):
        aid = str(agent["id"])
        before_history = _latest_history(service, aid)
        before_history_id = int(before_history.get("id") or 0) if before_history else 0

        result = original_observe(agent, state_map, changed_entities)

        latest = _latest_history(service, aid)
        if not latest or int(latest.get("id") or 0) <= before_history_id:
            return result
        if latest.get("reason") != PROMOTION_REASON or latest.get("status") != "promoted":
            return result
        if list(latest.get("old_schema") or []) == list(latest.get("new_schema") or []):
            return result

        current = service.store.get_agent_config(aid)
        if not current or current.get("mode") != "control":
            return result

        # Persist Shadow first. Executor reads the fresh store row at submit time, so a
        # prediction created before this wrapper finishes cannot cross into HA Control.
        service.store.update_agent(aid, {"mode": "shadow"})

        release_error = None
        restored = []
        executor = getattr(service.engine, "executor", None)
        if executor is not None and hasattr(executor, "release_control"):
            try:
                restored = executor.release_control(current, reason=REQUALIFICATION_REASON) or []
            except Exception as exc:
                # Stay in Shadow even if restoring an external controller fails.  Returning
                # to Control would be the unsafe fallback here.
                release_error = f"{type(exc).__name__}: {exc}"

        runtime = getattr(service.engine, "runtime", None)
        if isinstance(runtime, dict):
            rt = runtime.setdefault(aid, {})
            rt["decision_state"] = "shadow"
            rt["decision_reason"] = "Feature schema changed; Shadow requalification required"
            rt["schema_requalification"] = {
                "required": True,
                "history_id": int(latest["id"]),
                "promoted_entity": latest.get("promoted_entity"),
                "removed_entity": latest.get("removed_entity"),
                "reason": REQUALIFICATION_REASON,
            }

        event_data = {
            "history_id": int(latest["id"]),
            "promoted_entity": latest.get("promoted_entity"),
            "removed_entity": latest.get("removed_entity"),
            "old_mode": "control",
            "new_mode": "shadow",
            "restored_controllers": list(restored),
            "release_error": release_error,
        }
        try:
            level = "warning" if release_error else "info"
            message = (
                "Schema changed; agent moved from Control to Shadow, but previous controllers could not be fully restored"
                if release_error else
                "Schema changed; agent moved from Control to Shadow for requalification"
            )
            service.store.event(aid, level, "context_schema_requalification_shadow", message, event_data)
        except Exception:
            pass

        try:
            service.engine.wake_event.set()
        except Exception:
            pass
        return result

    service.observe_shadow = observe_with_shadow_requalification
    service._promotion_shadow_requalification_installed = True
    return service
