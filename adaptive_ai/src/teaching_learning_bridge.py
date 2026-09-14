"""Make Teach corrections first-class supervised feedback for the live agent.

The core Teaching layer deliberately keeps corrections retractable.  This bridge adds the
missing learning path around it:

* a correction made on the historical chart is also recorded by the full-context manual
  observer, so sensors outside the current compact schema can become relevant;
* repeated Teach corrections may refresh the selected context schema immediately;
* active teaching labels survive a conservative schema migration by matching the shared
  semantic features instead of requiring an identical set of feature keys.

The corrected Desired remains authoritative immediately through Teaching.  Context/schema
learning is additional generalisation; it never delays the correction itself.
"""
import math
import time


def _critical(name):
    text = str(name).lower().replace('_', ' ')
    return any(token in text for token in (
        'occupancy', 'presence', 'motion', 'obecno', 'door', 'window', 'contact',
        'moving target', 'still target', 'move target', 'radar', 'activity',
    ))


def semantic_distance(left, right):
    """Distance tolerant of normal sensor drift and compatible schema migrations.

    ``right`` is the stored teaching signature.  At least half of its semantic features
    must still exist after a schema change.  A motion/presence direction flip is always a
    hard mismatch, so broadening the match cannot turn an occupied-room example into an
    empty-room command.
    """
    left = dict(left or {})
    right = dict(right or {})
    if not left or not right:
        return None

    old_options = {k: v for k, v in right.items() if str(k).startswith('target_options:')}
    new_options = {k: v for k, v in left.items() if str(k).startswith('target_options:')}
    if old_options or new_options:
        if old_options != new_options:
            return None

    old_keys = {k for k in right if not str(k).startswith('target_options:')}
    new_keys = {k for k in left if not str(k).startswith('target_options:')}
    shared = old_keys & new_keys
    if not shared:
        return None
    # A label must retain enough of the context that gave it meaning.  This allows one or
    # two sensor replacements while preventing a stale label from binding to a new model.
    if len(shared) / max(1, len(old_keys)) < 0.50:
        return None

    old_critical = {k for k in old_keys if _critical(k)}
    if old_critical and not (old_critical & shared):
        return None

    weighted_sq = 0.0
    total_weight = 0.0
    max_generic = 0.0
    for key in shared:
        try:
            old_value = float(right[key])
            new_value = float(left[key])
        except (TypeError, ValueError):
            return None
        diff = abs(new_value - old_value)
        critical = _critical(key)
        if critical:
            if old_value * new_value < -0.05 and diff > 0.60:
                return None
            if diff > 0.90:
                return None
            weight = 3.0
        else:
            if diff > 1.20:
                return None
            max_generic = max(max_generic, diff)
            weight = 1.0
        weighted_sq += weight * diff * diff
        total_weight += weight

    if total_weight <= 0:
        return None
    rms = math.sqrt(weighted_sq / total_weight)
    try:
        from settings import OPTIONS
        max_rms = float(OPTIONS.get('teaching_context_rms', 0.20))
        max_analogue = float(OPTIONS.get('teaching_context_max_analogue_delta', 0.75))
    except Exception:
        max_rms, max_analogue = 0.20, 0.75
    return rms if rms <= max_rms and max_generic <= max_analogue else None


def _historical_broad_state(core, engine, timestamp):
    """Reconstruct broad HA context at one Teach-chart point.

    Historical Teaching previously loaded only entities already selected by the policy.
    That made it impossible for a correction to teach the agent that a *different* sensor
    was the real driver.  A user click is rare, so indexed as-of reads across the current
    HA entity universe are an acceptable bounded cost here.
    """
    from context import HistoricalTemporalTracker

    with engine.lock:
        entity_ids = list(engine.state_map.keys())
    # Keep the click path bounded on very large installations while comfortably covering
    # normal homes/buildings and the manual observer's own 512-entity cap.
    entity_ids = entity_ids[:768]
    rows = []
    with core.STORE.conn() as c:
        for eid in entity_ids:
            row = c.execute(
                "SELECT * FROM entity_history WHERE entity_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                (eid, float(timestamp)),
            ).fetchone()
            if row:
                rows.append(dict(row))
    tracker = HistoricalTemporalTracker(sorted(rows, key=lambda r: (r['ts'], r['id'])))
    tracker.advance(float(timestamp))
    return tracker.state_map


def install(core):
    if getattr(core, '_TEACHING_LEARNING_BRIDGE_INSTALLED', False):
        return
    if core.ENGINE is None or core.STORE is None:
        return

    import teaching as teaching_module
    import manual_context_learning as manual_context

    # Use the same matcher from history replay, live inference, physical supersession and
    # Executor validation.  This keeps one coherent definition of "same enough context".
    teaching_module.distance = semantic_distance

    cls = teaching_module.Teaching
    original_teach = cls.teach

    def teach(self, engine, agent, desired=None, sample_ts=None):
        # Capture the broad context independently from the compact policy schema.  For a
        # historical chart correction this is an as-of snapshot, never today's state.
        if sample_ts is None:
            with engine.lock:
                broad_states = dict(engine.state_map)
        else:
            timestamp = self.timestamp(sample_ts)
            broad_states = _historical_broad_state(core, engine, timestamp)

        result = original_teach(self, engine, agent, desired=desired, sample_ts=sample_ts)
        fresh = core.STORE.get_agent_config(agent['id']) or agent
        rejected = None
        try:
            rejected = float(engine.runtime.get(agent['id'], {}).get('last_prediction'))
        except (TypeError, ValueError):
            pass

        learning = manual_context.observe(
            core, fresh, broad_states, float(result['desired_value']), rejected=rejected,
            source='teach_history' if sample_ts is not None else 'teach_live',
            user_id='teach-ui', refresh_policy=True,
        )
        result['context_learning'] = learning

        # A schema refresh may have happened after the label was stored.  Re-evaluate now;
        # semantic_distance keeps the correction valid across compatible migrations.
        self.refresh(engine, fresh)
        core.STORE.event(
            fresh['id'], 'info', 'teaching_supervised_context',
            'Teach correction recorded as supervised context feedback',
            {
                'label_id': result.get('label_id'),
                'sample_ts': result.get('sample_ts'),
                'schema_changed': bool(learning.get('schema_changed')),
                'added': learning.get('added') or [],
                'removed': learning.get('removed') or [],
                'scores': learning.get('scores') or {},
            },
        )
        return result

    cls.teach = teach
    core._TEACHING_LEARNING_BRIDGE_INSTALLED = True
    core.STORE.event(
        None, 'info', 'teaching_learning_bridge_ready',
        'Teach corrections now feed supervised context learning', None,
    )
