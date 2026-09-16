"""Make explicit/manual device teaching available throughout the agent lifecycle.

Qualified explicit HA-user changes are handled by Engine.process_agent plus the
manual_feedback wrapper because that path understands pending Control actions. This
bridge covers waiting/paused/needs-retrain agents only when HA supplies explicit user
identity. A physical state transition without ``user_id`` is provenance-ambiguous and
must not silently become a user demonstration. Explicit UI corrections are mapped
separately as ``user_intent`` by the provenance command registry.
"""
import math
import time


def _trainable_now(agent):
    # Avoid mutating the policy while the historical training worker owns it.
    return str(agent.get('training_state') or 'waiting') != 'training'


def _manual_origin(state):
    ctx = (state or {}).get('context') or {}
    if ctx.get('parent_id'):
        return None, None
    if ctx.get('user_id'):
        return 'explicit_user', str(ctx.get('user_id'))
    # No user id is not evidence of manual intent. It may be a device, integration,
    # automation, restored state or command echo whose context mapping is unavailable.
    return None, None


def install_runtime(core):
    """Attach the explicit-user state bridge after adapters and before workers start."""
    if not core.runtime_available():
        return
    import manual_feedback as feedback
    from context import target_value
    from manual_context_learning import observe as observe_manual_context

    engine = core.ENGINE
    if getattr(engine, '_manual_lifecycle_events_installed', False):
        return
    original_state_changed = engine.on_state_changed

    def on_state_changed(data):
        entity_id = data.get('entity_id') if isinstance(data, dict) else None
        new_state = data.get('new_state') if isinstance(data, dict) else None
        with engine.lock:
            old_state = engine.state_map.get(entity_id) if entity_id else None
        original_state_changed(data)
        if not entity_id or not new_state:
            return
        origin, user_id = _manual_origin(new_state)
        if not origin:
            return
        for agent in core.STORE.list_agent_configs():
            if not agent.get('enabled') or agent.get('target_entity') != entity_id or not _trainable_now(agent):
                continue
            # Explicit HA-user changes on qualified agents are already processed by
            # Engine.process_agent, so this bridge only fills non-qualified lifecycle
            # states without creating a second learning update.
            if origin == 'explicit_user' and agent.get('training_state') == 'qualified':
                continue
            before = target_value(old_state, agent['target_property']) if old_state else None
            after = target_value(new_state, agent['target_property'])
            if before is None or after is None:
                continue
            try:
                before, after = float(before), float(after)
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(before) and math.isfinite(after)) or feedback._same(agent, before, after):
                continue
            if engine.own_command_echo(agent, new_state, after):
                # Includes corrections initiated from the app and ordinary HomeMind
                # Control commands. Both were already attributed elsewhere.
                continue
            with engine.lock:
                state_map = dict(engine.state_map)
            rt = engine.runtime.setdefault(agent['id'], {})
            actor = user_id
            predicted = rt.get('last_prediction')

            context_learning = observe_manual_context(
                core, agent, state_map, after, rejected=predicted,
                source=origin, user_id=actor,
            )
            negative = feedback._negative_prediction(
                engine, agent, state_map, predicted, after, actor,
                f'manual correction: rejected prediction ({origin})'
            )
            feedback._positive_demonstration(
                engine, agent, state_map, after, actor,
                f'manual demonstration ({origin})'
            )
            engine.set_manual_hold(agent, rt, time.time())
            rt['last_change_origin'] = 'manual_user'
            rt['last_manual_correction_source'] = origin
            core.STORE.event(
                agent['id'], 'info', 'manual_lifecycle_demo',
                f'Manual teaching {before} → {after} recorded ({origin})',
                {'before': before, 'after': after, 'user_id': user_id, 'origin': origin,
                 'negative_applied': bool(negative), 'context_learning': context_learning,
                 'training_state': agent.get('training_state')},
            )

    engine.on_state_changed = on_state_changed
    engine._manual_lifecycle_events_installed = True
    core.STORE.event(None, 'info', 'manual_lifecycle_ready',
                     'Explicit-user teaching is active across the agent lifecycle; no-user changes stay unknown', None)


def install(core):
    """Compatibility hook for runtimes that initialize through a wrapper."""
    if getattr(core, '_manual_feedback_lifecycle_installed', False):
        return
    base_initialize = core.initialize_runtime

    def initialize_runtime():
        base_initialize()
        install_runtime(core)

    core.initialize_runtime = initialize_runtime
    core._manual_feedback_lifecycle_installed = True
