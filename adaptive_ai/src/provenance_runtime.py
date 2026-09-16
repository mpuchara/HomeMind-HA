"""Production composition for provenance contract v1.

This installer runs from the existing runtime-extension hook before Engine/EventStream/
History workers consume events.  It decorates the established contracts rather than
adding a second dispatcher: Executor remains the only HA command boundary.
"""
from __future__ import annotations

import threading

from provenance import ProvenanceJournal, UNKNOWN
from rewards import RewardEngine
from settings import now_ts, parse_ts

_TLS = threading.local()


def _event_time(state, fallback=None):
    return parse_ts((state or {}).get("last_updated") or (state or {}).get("last_changed")) or float(
        now_ts() if fallback is None else fallback
    )


def _origin(journal, state):
    command = journal.match_command_state(state)
    if command:
        # Commands explicitly marked as user-intent stay human intent even though the HA
        # state transition itself is an echo of a HomeMind service call.
        return ("user_intent" if command.get("status") == "user_intent" else "own_command"), command
    context = (state or {}).get("context") or {}
    if context.get("user_id") and not context.get("parent_id"):
        return "user", None
    return UNKNOWN, None


def _latest_trigger(engine, aid):
    rt = engine.runtime.get(aid) or {}
    entities = list((rt.get("context_meta") or {}).get("trigger_entities") or [])
    if not entities and getattr(engine, "last_trigger_entity", None):
        entities = [engine.last_trigger_entity]
    latest = None
    for eid in entities:
        row = getattr(engine, "_provenance_latest_events", {}).get(eid)
        if row and (latest is None or row[0] > latest[0]):
            latest = row
    return latest[1] if latest else None


def _experiment_id(engine, intent):
    if not getattr(intent, "experiment_token", ""):
        return None
    aid = intent.agent_id
    trial = getattr(engine.experiments, "prepared", {}).get(aid)
    if trial and trial.get("token") == intent.experiment_token:
        return trial.get("trial_id")
    active = engine.experiments._get(aid).get("active")
    if active and active.get("token") == intent.experiment_token:
        return active.get("trial_id")
    return None


def _real_origin_counts(store, journal, agent_id):
    counts = {}
    with store.conn() as c:
        rows = c.execute(
            """SELECT h.target_history_id,e.entity_id,e.ts
               FROM historical_experiences h JOIN entity_history e ON e.id=h.target_history_id
               WHERE h.agent_id=?""",
            (str(agent_id),),
        ).fetchall()
    for row in rows:
        provenance = journal.history_provenance(row["entity_id"], row["ts"])
        origin = str(provenance.get("origin") or UNKNOWN)
        counts[origin] = counts.get(origin, 0) + 1
    return counts


def install(core):
    engine, store = core.ENGINE, core.STORE
    if engine is None or store is None or getattr(engine, "_provenance_contract_installed", False):
        return engine

    journal = ProvenanceJournal(store)
    engine.provenance = journal
    engine._provenance_latest_events = {}
    engine._provenance_contract_installed = True

    # --- Event contract -----------------------------------------------------
    original_on_state_changed = engine.on_state_changed

    def on_state_changed(data):
        entity_id = (data or {}).get("entity_id")
        state = (data or {}).get("new_state")
        if not entity_id:
            return original_on_state_changed(data)
        received = now_ts()
        event_time = _event_time(state, received)
        origin, command = _origin(journal, state)
        event_id, inserted = journal.record_event(
            entity_id, state, event_time=event_time, received_time=received,
            source="ha_state_changed", origin=origin,
        )
        # A retried websocket event is the same observation, not another learning event.
        # Initial state after restart is already recovered by refresh_states before the
        # websocket worker starts, so suppressing an existing event cannot erase state.
        if not inserted:
            return None
        engine._provenance_latest_events[entity_id] = (event_time, event_id)
        previous_event = getattr(_TLS, "event_id", None)
        previous_origin = getattr(_TLS, "event_origin", None)
        _TLS.event_id, _TLS.event_origin = event_id, origin
        try:
            result = original_on_state_changed(data)
        finally:
            _TLS.event_id, _TLS.event_origin = previous_event, previous_origin
        if command and command.get("decision_id"):
            journal.mark_ack(command["decision_id"], event_id=event_id, ack_time=event_time)
        return result

    engine.on_state_changed = on_state_changed

    original_queue_archive = engine._queue_archive_state

    def queue_archive_state(state, force=False):
        entity_id = (state or {}).get("entity_id")
        if entity_id:
            event_time = _event_time(state)
            known = journal.history_provenance(entity_id, event_time)
            if not known.get("event_id"):
                origin, _ = _origin(journal, state)
                event_id, _ = journal.record_event(
                    entity_id, state, event_time=event_time, received_time=now_ts(),
                    source="ha_poll", origin=origin,
                )
                engine._provenance_latest_events[entity_id] = (event_time, event_id)
        return original_queue_archive(state, force=force)

    engine._queue_archive_state = queue_archive_state

    # --- Persistent command evidence ---------------------------------------
    original_record_command = engine.record_command

    def record_command(agent, value, response=None):
        result = original_record_command(agent, value, response)
        command_id = getattr(_TLS, "command_id", None)
        decision_id = getattr(_TLS, "decision_id", None)
        if command_id is None:
            command_id = journal.reserve_command(
                agent, value, decision_id=decision_id, created_time=now_ts()
            )
            _TLS.command_id = command_id
            if getattr(_TLS, "command_origin", None) == "user_intent":
                with store.lock, store.conn() as c:
                    c.execute(
                        "UPDATE provenance_commands SET status='user_intent' WHERE command_id=?",
                        (command_id,),
                    )
        else:
            journal.dispatch_command(command_id, response=response, dispatched_time=now_ts())
            if decision_id:
                journal.mark_dispatch(decision_id, value, dispatched_time=now_ts(), episode_id=decision_id)
            _TLS.command_id = None
        return result

    engine.record_command = record_command

    original_own_echo = engine.own_command_echo

    def own_command_echo(agent, state, current):
        return bool(original_own_echo(agent, state, current) or journal.match_command_state(state))

    engine.own_command_echo = own_command_echo

    original_service = engine.executor._service

    def service(domain, action, data):
        try:
            return original_service(domain, action, data)
        except Exception:
            journal.fail_command(getattr(_TLS, "command_id", None))
            _TLS.command_id = None
            raise

    engine.executor._service = service

    # --- Decision contract --------------------------------------------------
    original_submit = engine.executor.submit

    def submit(intent, features=None, action_index=None):
        features = features or {}
        policy = engine.models.get(intent.agent_id)
        if policy is None:
            agent = store.get_agent_config(intent.agent_id)
            policy = engine.policy(agent) if agent else None
        schema_export = policy.schema.export() if policy is not None else {}
        generation = journal.generation_for_agent(intent.agent_id)
        rt = engine.runtime.get(intent.agent_id) or {}
        experiment_id = _experiment_id(engine, intent)
        manifest = {
            "schema": schema_export,
            "features": {str(k): float(v) for k, v in features.items()},
            "policy_head": int(intent.policy_head),
            "context_revision": int(intent.context_revision),
            "target_revision": int(intent.target_revision),
            "context_dependencies": [list(x) for x in intent.context_dependencies],
        }
        journal.record_decision(
            decision_id=intent.intent_id,
            created_time=intent.created_at,
            agent_id=intent.agent_id,
            generation_id=(generation or {}).get("generation_id"),
            trigger_event_id=_latest_trigger(engine, intent.agent_id),
            model_version=intent.policy_version,
            model_revision=intent.model_revision,
            schema_version=schema_export.get("version"),
            schema_revision=(getattr(policy, "selection_meta", {}) or {}).get("schema_revision") if policy else None,
            reward_version=RewardEngine.VERSION,
            feature_manifest=manifest,
            allowed_actions=list(getattr(policy, "actions", []) or []),
            chosen_action=intent.desired_value,
            model_desired=rt.get("baseline_prediction", intent.desired_value),
            teaching_id=intent.teaching_id or None,
            teaching_desired=intent.desired_value if intent.teaching_id else None,
            experiment_id=experiment_id,
            # LinUCB production choose() is deterministic here.  No propensity is
            # available, so the optional probability deliberately stays NULL.
            action_probability=None,
        )
        old_decision = getattr(_TLS, "decision_id", None)
        old_command = getattr(_TLS, "command_id", None)
        _TLS.decision_id, _TLS.command_id = intent.intent_id, None
        try:
            result = original_submit(intent, features, action_index)
        finally:
            _TLS.decision_id = old_decision
            # Any successful command is closed by record_command(response). A leftover
            # reservation means no successful dispatch reached that second boundary.
            leftover = getattr(_TLS, "command_id", None)
            if leftover:
                journal.fail_command(leftover)
            _TLS.command_id = old_command
        journal.mark_decision_status(intent.intent_id, result.get("status"), result.get("reason"))
        pending = (engine.runtime.get(intent.agent_id) or {}).get("pending")
        if result.get("status") == "ACCEPTED" and pending:
            pending["decision_id"] = intent.intent_id
            pending["episode_id"] = intent.intent_id
            pending["experiment_id"] = experiment_id
        return result

    engine.executor.submit = submit

    # --- Idempotent feedback and outcome linkage ---------------------------
    original_reward_pending = engine._reward_pending

    def reward_pending(agent, rt, reward, reason, user_id=None, experience=None):
        pending = experience if experience is not None else rt.get("pending")
        if not pending:
            return None
        if pending.get("experiment") or pending.get("teaching_id"):
            return original_reward_pending(agent, rt, reward, reason, user_id, experience)
        decision_id = pending.get("decision_id")
        episode_id = pending.get("episode_id") or decision_id
        event_id = getattr(_TLS, "event_id", None) or _latest_trigger(engine, agent["id"])
        event = journal.event(event_id) or {}
        key_base = decision_id or f"legacy:{agent['id']}:{pending.get('started_ts')}:{pending.get('action_index')}"
        experience_key = f"feedback:{key_base}:{reason}"
        with store.lock:
            if journal.experience_exists(experience_key):
                if rt.get("pending") is pending:
                    rt["pending"] = None
                return False
            policy = engine.policy(agent)
            policy.update(
                int(pending.get("policy_head") or min(policy.horizons)),
                pending["action_index"], pending["features"], reward,
            )
            inserted = journal.commit_feedback_model(
                experience_key=experience_key,
                agent_id=agent["id"], model=policy.serialize(),
                action_index=pending["action_index"], action_value=pending["action_value"],
                reward=reward, reason=reason, features=pending["features"], user_id=user_id,
                decision_id=decision_id, source_event_id=event_id,
                episode_id=episode_id, experiment_id=pending.get("experiment_id"),
                source="live_feedback", origin=event.get("origin") or UNKNOWN,
                metadata={"reward_components": dict(rt.get("reward_components_pending") or {})},
            )
        if not inserted:
            if rt.get("pending") is pending:
                rt["pending"] = None
            return False
        rt["last_reward_components"] = rt.pop("reward_components_pending", {})
        rt["last_reward"] = reward
        rt["last_reward_reason"] = reason
        if rt.get("pending") is pending:
            rt["pending"] = None
        if decision_id:
            journal.mark_outcome(decision_id, reward, reason)
        store.event(
            agent["id"], "info" if reward >= 0 else "warning", "rl_reward",
            f"RL reward {reward:+.2f}: {reason}",
            {"reward": reward, "reason": reason, "action_value": pending["action_value"],
             "user_id": user_id, "decision_id": decision_id, "experience_key": experience_key},
        )
        return True

    engine._reward_pending = reward_pending

    # Link target ACKs and explicit physical demonstrations to their event provenance.
    original_process_agent = engine.process_agent

    def process_agent(agent, state_map, changed_entities=None):
        aid = agent["id"]
        rt = engine.runtime.get(aid) or {}
        before_pending = rt.get("pending")
        before_ack = (before_pending or {}).get("acknowledged_ts")
        event_id = None
        if agent.get("target_entity") in set(changed_entities or ()):
            row = engine._provenance_latest_events.get(agent["target_entity"])
            event_id = row[1] if row else None
        old_event = getattr(_TLS, "event_id", None)
        old_origin = getattr(_TLS, "event_origin", None)
        if event_id:
            event = journal.event(event_id) or {}
            _TLS.event_id, _TLS.event_origin = event_id, event.get("origin") or UNKNOWN
        try:
            result = original_process_agent(agent, state_map, changed_entities)
        finally:
            _TLS.event_id, _TLS.event_origin = old_event, old_origin
        after_rt = engine.runtime.get(aid) or {}
        after_pending = after_rt.get("pending")
        if before_pending is after_pending and before_pending and before_ack is None and before_pending.get("acknowledged_ts") is not None:
            journal.mark_ack(
                before_pending.get("decision_id"), event_id=event_id,
                ack_time=before_pending.get("acknowledged_ts"),
            )
        if event_id:
            event = journal.event(event_id) or {}
            if event.get("origin") in {"user", "user_intent"}:
                # Physical/UI user demonstrations are already persisted by the existing
                # learning path. Add a stable provenance row without replaying learning.
                for row in store.list_feedback(aid, limit=4):
                    if "manual demonstration" not in str(row.get("reason") or ""):
                        continue
                    journal.record_experience(
                        experience_key=f"manual:{event_id}:{aid}:{row['id']}",
                        agent_id=aid, source="manual_demonstration", origin=event.get("origin") or UNKNOWN,
                        source_event_id=event_id, action_index=row.get("action_index"),
                        action_value=row.get("action_value"), reward=row.get("reward"),
                        features=row.get("features"), metadata={"feedback_id": row.get("id"), "user_id": row.get("user_id")},
                    )
                    break
        return result

    engine.process_agent = process_agent

    # --- Experiment idempotency --------------------------------------------
    original_begin = engine.experiments.begin
    original_finish = engine.experiments._finish

    def begin(agent, intent, states=None):
        ok = original_begin(agent, intent, states)
        if ok:
            data = engine.experiments._get(agent["id"])
            trial = data.get("active")
            if trial:
                trial["decision_id"] = intent.intent_id
                trial["episode_id"] = intent.intent_id
                trial["idempotency_key"] = "experiment:" + str(trial.get("trial_id"))
                engine.experiments._save(agent["id"])
        return ok

    def finish(aid, reward, reason):
        data = engine.experiments._get(aid)
        trial = dict(data.get("active") or {})
        if not trial:
            return original_finish(aid, reward, reason)
        outcome_key = "experiment:" + str(trial.get("trial_id")) + ":outcome"
        # A restored/retried finish with the same trial cannot update the contextual
        # bandit twice. Normally active=None already provides this guard; the durable key
        # closes the restart/retry case explicitly.
        if journal.experience_exists(outcome_key):
            data["active"] = None
            engine.experiments._save(aid)
            return None
        result = original_finish(aid, reward, reason)
        outcome = engine.experiments._get(aid).get("last_outcome") or {}
        journal.record_experience(
            experience_key=outcome_key, agent_id=aid, source="experiment",
            origin="experiment", decision_id=trial.get("decision_id"),
            experiment_id=trial.get("trial_id"), episode_id=trial.get("episode_id"),
            action_index=trial.get("arm"), action_value=trial.get("value"), reward=reward,
            features=trial.get("x"), metadata=outcome,
        )
        if trial.get("decision_id"):
            journal.mark_outcome(trial.get("decision_id"), reward, reason, outcome.get("at"))
        return result

    engine.experiments.begin = begin
    engine.experiments._finish = finish

    # --- Historical replay provenance --------------------------------------
    original_add_historical = store.add_historical_experience

    def add_historical_experience(agent_id, target_history_id, action_index, action_value,
                                  reward, dwell_seconds, features, user_id=None):
        with store.conn() as c:
            history = c.execute(
                "SELECT entity_id,ts FROM entity_history WHERE id=?", (int(target_history_id),)
            ).fetchone()
        provenance = journal.history_provenance(history["entity_id"], history["ts"]) if history else {
            "origin": UNKNOWN, "source": UNKNOWN, "event_id": None,
        }
        origin = str(provenance.get("origin") or UNKNOWN)
        if origin == "own_command":
            # The row remains in the raw archive and still advances the target trajectory,
            # but it can never become an independent behavioural demonstration.
            journal.record_experience(
                experience_key=f"history-excluded:{agent_id}:{target_history_id}",
                agent_id=agent_id, source="historical_replay_excluded", origin=origin,
                source_event_id=provenance.get("event_id"), action_index=action_index,
                action_value=action_value, reward=None, features=features,
                metadata={"target_history_id": int(target_history_id), "reason": "own_command_ack"},
            )
            return False
        inserted = original_add_historical(
            agent_id, target_history_id, action_index, action_value,
            reward, dwell_seconds, features,
            # Old context_user_id is not promoted to a manual origin. Only a provenance
            # event explicitly classified as user/user_intent may retain user identity.
            user_id if origin in {"user", "user_intent"} else None,
        )
        if inserted:
            journal.record_experience(
                experience_key=f"history:{agent_id}:{target_history_id}",
                agent_id=agent_id, source="historical_replay", origin=origin,
                source_event_id=provenance.get("event_id"), action_index=action_index,
                action_value=action_value, reward=reward, features=features,
                metadata={"target_history_id": int(target_history_id), "dwell_seconds": float(dwell_seconds)},
            )
        return inserted

    store.add_historical_experience = add_historical_experience

    # Legacy benchmark code historically guessed manual/automation provenance. Replace
    # only that diagnostic field at persistence boundaries with counts derived from the
    # durable journal. No historical row is rewritten.
    original_set_partial = store.set_partial_benchmark

    def set_partial_benchmark(agent_id, stat):
        stat = dict(stat or {})
        stat["origin_counts"] = _real_origin_counts(store, journal, agent_id)
        return original_set_partial(agent_id, stat)

    store.set_partial_benchmark = set_partial_benchmark

    original_set_training_state = store.set_training_state

    def set_training_state(agent_id, state, score=None, samples=0, source=None, detail=None, demote_control=False):
        if source == "recorded-behaviour" and isinstance(detail, dict):
            detail = dict(detail)
            counts = _real_origin_counts(store, journal, agent_id)
            detail["origin_counts"] = counts
            nested = dict(detail.get("counts") or {})
            nested["origin_counts"] = counts
            detail["counts"] = nested
        return original_set_training_state(
            agent_id, state, score=score, samples=samples, source=source,
            detail=detail, demote_control=demote_control,
        )

    store.set_training_state = set_training_state

    core.STORE.event(
        None, "info", "provenance_contract_ready",
        "Durable event/decision/experience provenance contract enabled",
        {"contract_version": 1, "own_command_restart_evidence": True,
         "unknown_is_preserved": True, "action_probability": "optional"},
    )
    return engine
