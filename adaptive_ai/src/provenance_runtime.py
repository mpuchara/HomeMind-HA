"""Production composition for provenance contract v1.

This installer runs from the existing runtime-extension hook before Engine/EventStream/
History workers consume events. It decorates the established contracts rather than
adding a second dispatcher: Executor remains the only HA command boundary.
"""
from __future__ import annotations

from contextlib import contextmanager
import threading
import time

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
        return str(command.get("command_origin") or "own_command"), command
    context = (state or {}).get("context") or {}
    if context.get("user_id") and not context.get("parent_id"):
        return "user", None
    # A physical state change without a user id is deliberately ambiguous. Automation,
    # integration and direct-device changes cannot be distinguished reliably from the HA
    # state object alone.
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

    # Shadow provenance is audit data, not part of the physical safety boundary. Buffer
    # it and write one transaction per batch so SQLite latency never sits in event->intent.
    deferred_lock = threading.RLock()
    deferred_event = threading.Event()
    deferred_rows = []
    deferred_stats = {
        "queued": 0,
        "flushed": 0,
        "flushes": 0,
        "max_queue": 0,
    }
    event_stats = {"flushed": 0, "flushes": 0, "errors": 0}
    generation_cache = {}
    schema_cache = {}

    def cached_generation(agent_id):
        aid = str(agent_id)
        revision = int(getattr(store, "_provenance_generation_revision", 0))
        cached = generation_cache.get(aid)
        if cached is not None and int(cached[0]) == revision:
            return cached[1]
        generation = journal.generation_for_agent(aid)
        generation_cache[aid] = (revision, generation)
        return generation

    def cached_schema(agent_id, policy):
        if policy is None:
            return {}
        key = (str(agent_id), str(getattr(policy, "model_revision", "")))
        cached = schema_cache.get(key)
        if cached is not None:
            return cached
        exported = policy.schema.export()
        # Keep only the current revision for one agent.
        for old in [item for item in schema_cache if item[0] == key[0] and item != key]:
            schema_cache.pop(old, None)
        schema_cache[key] = exported
        return exported

    def flush_deferred(agent_id=None):
        aid = None if agent_id is None else str(agent_id)
        with deferred_lock:
            if not deferred_rows:
                return 0
            if aid is None:
                batch = list(deferred_rows)
                deferred_rows.clear()
            else:
                batch = [row for row in deferred_rows if str(row.get("agent_id")) == aid]
                if not batch:
                    return 0
                deferred_rows[:] = [
                    row for row in deferred_rows if str(row.get("agent_id")) != aid
                ]
        inserted = journal.record_decisions_batch(batch)
        with deferred_lock:
            deferred_stats["flushed"] += len(batch)
            deferred_stats["flushes"] += 1
        return inserted

    def queue_deferred(row):
        with deferred_lock:
            deferred_rows.append(dict(row))
            deferred_stats["queued"] += 1
            deferred_stats["max_queue"] = max(
                int(deferred_stats["max_queue"]), len(deferred_rows)
            )
            wake = len(deferred_rows) >= 32
        if wake:
            deferred_event.set()

    def deferred_snapshot():
        with deferred_lock:
            decision = {**deferred_stats, "pending": len(deferred_rows)}
        return {
            **decision,
            "decisions": decision,
            "events": {
                **event_stats,
                "pending": journal.pending_event_count(),
                "dropped": int(getattr(journal, "_dropped_pending_events", 0) or 0),
            },
        }

    def flush_event_provenance():
        total = 0
        while journal.pending_event_count():
            written = journal.flush_events_batch(512)
            if not written:
                break
            total += int(written)
        if total:
            event_stats["flushed"] += total
            event_stats["flushes"] += 1
        return total

    def provenance_writer():
        while not engine.stop_event.is_set():
            deferred_event.wait(0.5)
            deferred_event.clear()
            try:
                flush_event_provenance()
                flush_deferred()
            except Exception as exc:
                event_stats["errors"] += 1
                try:
                    store.event(
                        None, "warning", "provenance_batch_flush_failed",
                        f"Deferred provenance flush failed: {type(exc).__name__}: {exc}",
                        None,
                    )
                except Exception:
                    pass
                time.sleep(0.1)
        try:
            flush_event_provenance()
            flush_deferred()
        except Exception:
            pass

    # Manual feedback and explicit provenance reads can force durability before lookup.
    store._flush_provenance_decisions = flush_deferred
    store._flush_provenance_events = flush_event_provenance
    engine.provenance_deferred_snapshot = deferred_snapshot
    threading.Thread(
        target=provenance_writer,
        name="adaptive-ai-provenance-writer",
        daemon=True,
    ).start()

    @contextmanager
    def command_origin(origin):
        previous = getattr(_TLS, "command_origin", None)
        _TLS.command_origin = str(origin or "own_command")
        try:
            yield
        finally:
            _TLS.command_origin = previous

    # Explicit UI corrections use this scope. Ordinary Executor control never sets it and
    # therefore remains own_command. Exposing a narrow context manager avoids a second
    # service boundary or caller-specific SQL writes.
    engine.provenance_command_origin = command_origin

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
        event_id, _inserted = journal.record_event(
            entity_id, state, event_time=event_time, received_time=received,
            source="ha_state_changed", origin=origin,
        )
        # Duplicate suppression is RAM-first for current events and warmed from recent
        # durable rows at startup. Event provenance itself is batch-durable; the physical
        # Control boundary remains synchronous in decision/command persistence below.
        if journal.event_processed(event_id):
            return None
        # Keep origin beside the durable event id so downstream inference wrappers never
        # need a provenance SELECT merely to classify the target event.
        engine._provenance_latest_events[entity_id] = (event_time, event_id, origin)
        previous_event = getattr(_TLS, "event_id", None)
        previous_origin = getattr(_TLS, "event_origin", None)
        _TLS.event_id, _TLS.event_origin = event_id, origin
        try:
            result = original_on_state_changed(data)
            if command and command.get("decision_id"):
                journal.mark_ack(command["decision_id"], event_id=event_id, ack_time=event_time)
            journal.mark_event_processed(event_id, processed_time=now_ts())
            return result
        finally:
            _TLS.event_id, _TLS.event_origin = previous_event, previous_origin

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
            else:
                event_id = known.get("event_id")
                origin = str(known.get("origin") or UNKNOWN)
            engine._provenance_latest_events[entity_id] = (event_time, event_id, origin)
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
                agent, value, decision_id=decision_id, created_time=now_ts(),
                command_origin=getattr(_TLS, "command_origin", None) or "own_command",
            )
            _TLS.command_id = command_id
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

    def decision_payload(intent, features):
        # process_agent has already materialized the policy for any live inference.
        # Provenance is observational here; never reopen agent configuration from SQLite
        # simply to decorate a Shadow intent.
        policy = engine.models.get(intent.agent_id)
        schema_export = cached_schema(intent.agent_id, policy)
        generation = cached_generation(intent.agent_id)
        rt = engine.runtime.get(intent.agent_id) or {}
        experiment_id = _experiment_id(engine, intent)
        return {
            "decision_id": intent.intent_id,
            "created_time": intent.created_at,
            "agent_id": intent.agent_id,
            "generation_id": (generation or {}).get("generation_id"),
            "trigger_event_id": _latest_trigger(engine, intent.agent_id),
            "model_version": intent.policy_version,
            "model_revision": intent.model_revision,
            "schema_version": schema_export.get("version"),
            "schema_revision": (
                (getattr(policy, "selection_meta", {}) or {}).get("schema_revision")
                if policy else None
            ),
            "reward_version": RewardEngine.VERSION,
            "feature_manifest": {
                "schema": schema_export,
                "features": {str(k): float(v) for k, v in (features or {}).items()},
                "policy_head": int(intent.policy_head),
                "context_revision": int(intent.context_revision),
                "target_revision": int(intent.target_revision),
                "context_dependencies": [list(x) for x in intent.context_dependencies],
            },
            "allowed_actions": list(getattr(policy, "actions", []) or []),
            "chosen_action": intent.desired_value,
            "model_desired": rt.get("baseline_prediction", intent.desired_value),
            "teaching_id": intent.teaching_id or None,
            "teaching_desired": intent.desired_value if intent.teaching_id else None,
            "experiment_id": experiment_id,
            "episode_id": None,
            "action_probability": None,
        }

    def submit(intent, features=None, action_index=None):
        features = features or {}
        with engine.lock:
            hot_agent = dict(getattr(engine, "agent_configs", {}).get(intent.agent_id) or {})
        defer_shadow = hot_agent.get("mode") == "shadow"
        payload = decision_payload(intent, features)

        # Control and uncertain routing remain synchronous because provenance must exist
        # before any possible physical dispatch. Only positively identified Shadow uses
        # the deferred audit path.
        if not defer_shadow:
            journal.record_decision(**payload)

        old_decision = getattr(_TLS, "decision_id", None)
        old_command = getattr(_TLS, "command_id", None)
        _TLS.decision_id, _TLS.command_id = intent.intent_id, None
        try:
            result = original_submit(intent, features, action_index)
        finally:
            leftover = getattr(_TLS, "command_id", None)
            if leftover:
                journal.fail_command(leftover)
            _TLS.decision_id, _TLS.command_id = old_decision, old_command

        if defer_shadow:
            payload["dispatch_status"] = result.get("status")
            payload["dispatch_reason"] = result.get("reason")
            queue_deferred(payload)
        else:
            journal.mark_decision_status(
                intent.intent_id, result.get("status"), result.get("reason")
            )

        pending = (engine.runtime.get(intent.agent_id) or {}).get("pending")
        if result.get("status") == "ACCEPTED" and pending:
            pending["decision_id"] = intent.intent_id
            pending["episode_id"] = intent.intent_id
            pending["experiment_id"] = payload.get("experiment_id")
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
        event_origin = UNKNOWN
        if agent.get("target_entity") in set(changed_entities or ()):
            row = engine._provenance_latest_events.get(agent["target_entity"])
            if row:
                event_id = row[1]
                event_origin = str(row[2] if len(row) > 2 else UNKNOWN)
        old_event = getattr(_TLS, "event_id", None)
        old_origin = getattr(_TLS, "event_origin", None)
        if event_id:
            _TLS.event_id, _TLS.event_origin = event_id, event_origin
        try:
            result = original_process_agent(agent, state_map, changed_entities)
        finally:
            _TLS.event_id, _TLS.event_origin = old_event, old_origin
        after_rt = engine.runtime.get(aid) or {}
        after_pending = after_rt.get("pending")
        if (before_pending is after_pending and before_pending and before_ack is None
                and before_pending.get("acknowledged_ts") is not None):
            journal.mark_ack(
                before_pending.get("decision_id"), event_id=event_id,
                ack_time=before_pending.get("acknowledged_ts"),
            )
        if event_id and event_origin in {"user", "user_intent"}:
            # Existing manual-learning code remains authoritative. This row only
            # supplies durable provenance and an idempotency identity for audit/replay.
            # This rare explicit-user branch may write audit data, but the common target
            # event path no longer performs a provenance SELECT before or after inference.
            for row in store.list_feedback(aid, limit=4):
                if "manual demonstration" not in str(row.get("reason") or ""):
                    continue
                journal.record_experience(
                    experience_key=f"manual:{event_id}:{aid}:{row['id']}",
                    agent_id=aid, source="manual_demonstration", origin=event_origin,
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
    original_add_historical_batch = getattr(store, "add_historical_experiences_batch", None)

    def _provenance_for_history_id(target_history_id):
        with store.conn() as c:
            history = c.execute(
                "SELECT entity_id,ts FROM entity_history WHERE id=?", (int(target_history_id),)
            ).fetchone()
        return journal.history_provenance(history["entity_id"], history["ts"]) if history else {
            "origin": UNKNOWN, "source": UNKNOWN, "event_id": None,
        }

    def historical_replay_provenance(start_ts, end_ts, entity_ids):
        """Load linked provenance for replay target rows in one indexed query.

        Rows without a v1 provenance link intentionally remain absent and therefore map
        to UNKNOWN in the caller.  This removes two SQLite reads from every completed
        dwell while preserving the own-command exclusion boundary.
        """
        ids = sorted(set(str(x) for x in (entity_ids or []) if x))
        if not ids:
            return {}
        # Replay is a cold/background persistence boundary. Flush current RAM provenance
        # once here so the indexed SQL join sees events acknowledged moments earlier,
        # without reintroducing per-event writes into the live path.
        if journal.pending_event_count():
            flush_event_provenance()
        placeholders = ",".join("?" for _ in ids)
        params = [float(start_ts), float(end_ts)] + ids
        with store.conn() as c:
            rows = c.execute(
                f"""SELECT h.id AS target_history_id,e.event_id,e.origin,e.source
                    FROM entity_history h
                    JOIN provenance_history_links l
                      ON l.entity_id=h.entity_id AND l.event_time=h.ts
                    JOIN provenance_events e ON e.event_id=l.event_id
                    WHERE h.ts>=? AND h.ts<=?
                      AND h.entity_id IN ({placeholders})""",
                params,
            ).fetchall()
        return {
            int(row["target_history_id"]): {
                "event_id": row["event_id"],
                "origin": str(row["origin"] or UNKNOWN),
                "source": str(row["source"] or UNKNOWN),
            }
            for row in rows
        }

    def _audit_row(row, provenance, *, excluded=False):
        origin = str((provenance or {}).get("origin") or UNKNOWN)
        target_history_id = int(row["target_history_id"])
        if excluded:
            return {
                "experience_key": f"history-excluded:{row['agent_id']}:{target_history_id}",
                "agent_id": row["agent_id"],
                "source": "historical_replay_excluded",
                "origin": origin,
                "source_event_id": (provenance or {}).get("event_id"),
                "action_index": row.get("action_index"),
                "action_value": row.get("action_value"),
                "reward": None,
                "features": row.get("features"),
                "metadata": {
                    "target_history_id": target_history_id,
                    "reason": "own_command_ack",
                },
            }
        return {
            "experience_key": f"history:{row['agent_id']}:{target_history_id}",
            "agent_id": row["agent_id"],
            "source": "historical_replay",
            "origin": origin,
            "source_event_id": (provenance or {}).get("event_id"),
            "action_index": row.get("action_index"),
            "action_value": row.get("action_value"),
            "reward": row.get("reward"),
            "features": row.get("features"),
            "metadata": {
                "target_history_id": target_history_id,
                "dwell_seconds": float(row.get("dwell_seconds") or 0.0),
            },
        }

    def add_historical_experience(agent_id, target_history_id, action_index, action_value,
                                  reward, dwell_seconds, features, user_id=None):
        provenance = _provenance_for_history_id(target_history_id)
        origin = str(provenance.get("origin") or UNKNOWN)
        if origin == "own_command":
            # Preserve raw history for chronology but refuse to turn the system's own ACK
            # into an independent user demonstration during rebuild.
            journal.record_experience(**_audit_row({
                "agent_id": agent_id,
                "target_history_id": target_history_id,
                "action_index": action_index,
                "action_value": action_value,
                "reward": reward,
                "dwell_seconds": dwell_seconds,
                "features": features,
            }, provenance, excluded=True))
            return False
        inserted = original_add_historical(
            agent_id, target_history_id, action_index, action_value,
            reward, dwell_seconds, features,
            # Legacy context_user_id is not upgraded to an origin. Only a v1 event that
            # was explicitly classified as user/user_intent carries user identity.
            user_id if origin in {"user", "user_intent"} else None,
        )
        if inserted:
            journal.record_experience(**_audit_row({
                "agent_id": agent_id,
                "target_history_id": target_history_id,
                "action_index": action_index,
                "action_value": action_value,
                "reward": reward,
                "dwell_seconds": dwell_seconds,
                "features": features,
            }, provenance))
        return inserted

    def add_historical_experiences_batch(rows):
        """Batch persistence with the same provenance/exclusion semantics as one-row replay."""
        rows = list(rows or [])
        if not rows:
            return 0
        accepted, audit = [], []
        for raw in rows:
            row = dict(raw)
            provenance = dict(row.pop("_provenance", None) or _provenance_for_history_id(row["target_history_id"]))
            origin = str(provenance.get("origin") or UNKNOWN)
            if origin == "own_command":
                audit.append(_audit_row(row, provenance, excluded=True))
                continue
            row["user_id"] = row.get("user_id") if origin in {"user", "user_intent"} else None
            accepted.append(row)
            audit.append(_audit_row(row, provenance, excluded=False))

        inserted = original_add_historical_batch(accepted) if (accepted and callable(original_add_historical_batch)) else 0
        # One bounded provenance transaction per replay batch. INSERT OR IGNORE keeps
        # restart/retry idempotency exactly like the single-row journal path.
        if audit:
            journal.record_experiences_batch(audit)
        return int(inserted)

    store.historical_replay_provenance = historical_replay_provenance
    store.add_historical_experience = add_historical_experience
    if callable(original_add_historical_batch):
        store.add_historical_experiences_batch = add_historical_experiences_batch

    # Legacy benchmark code guessed manual/automation provenance. Replace only the
    # diagnostic count at persistence boundaries with journal-derived values. Old rows
    # with no v1 link count as unknown and are never rewritten.
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
