from concurrent.futures import ThreadPoolExecutor
import json
import math
from control import same_value
import threading
import time
from control import timing_for
import traceback
from settings import (APP_VERSION, HA_BASE_URL, HA_TOKEN, NUMERIC_ATTRS, OPTIONS, clamp, now_ts, parse_ts, ws_connect)
from storage import STORE
from ha import HA, AUTOMATION_KNOWLEDGE
from context import (TemporalHistory, action_values, parse_horizons, sensor_recommendations, target_options_for_state, target_value)
from policy import MultiHorizonPolicy
from teaching import Teaching
from context_engine import ContextEngine
from executor import Executor
from intent import ActionIntent
from experiments import Experiments
from telemetry import TELEMETRY, HEAVY_JOBS
from training_budget import TRAINING_BUDGET

class HAEventStream(threading.Thread):
    """Near-real-time state_changed stream plus Entity Registry metadata.

    REST polling remains as a fallback/resync path, but inference is normally woken by
    the WebSocket event bus so control latency is not tied to the full /states poll.
    """
    daemon = True

    def __init__(self, engine):
        super().__init__(name="adaptive-ai-ha-events")
        self.engine = engine
        self.stop_event = threading.Event()

    def run(self):
        if ws_connect is None:
            self.engine.ws_error = "websockets package unavailable; REST fallback active"
            return
        backoff = 2.0
        while not self.stop_event.is_set():
            try:
                with ws_connect("ws://supervisor/core/websocket", open_timeout=10, close_timeout=5) as ws:
                    hello = json.loads(ws.recv())
                    if hello.get("type") == "auth_required":
                        ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
                        auth = json.loads(ws.recv())
                        if auth.get("type") != "auth_ok":
                            raise RuntimeError(auth.get("message") or "WebSocket auth failed")
                    elif hello.get("type") != "auth_ok":
                        raise RuntimeError(f"Unexpected WebSocket greeting: {hello.get('type')}")
                    self.engine.ws_connected = True
                    self.engine.ws_error = None
                    HA.last_ok = now_ts(); HA.last_error = None
                    # Registry metadata lets discovery exclude configuration/diagnostic entities.
                    ws.send(json.dumps({"id": 1, "type": "config/entity_registry/list"}))
                    ws.send(json.dumps({"id": 2, "type": "subscribe_events", "event_type": "state_changed"}))
                    ws.send(json.dumps({"id": 3, "type": "config/device_registry/list"}))
                    ws.send(json.dumps({"id": 4, "type": "config/area_registry/list"}))
                    for ident, event_type in ((5, 'entity_registry_updated'), (6, 'device_registry_updated'), (7, 'area_registry_updated')):
                        ws.send(json.dumps({'id': ident, 'type': 'subscribe_events', 'event_type': event_type}))
                    next_id = 10
                    registry_requests = {1: self.engine.update_entity_registry, 3: self.engine.update_device_registry, 4: self.engine.update_area_registry}
                    backoff = 2.0
                    while not self.stop_event.is_set():
                        try:
                            raw = ws.recv(timeout=5)
                        except TimeoutError:
                            continue
                        msg = json.loads(raw)
                        if msg.get('type') == 'result' and msg.get('id') in registry_requests:
                            callback = registry_requests.pop(msg['id'])
                            if msg.get('success'):
                                callback(msg.get('result') or [])
                            continue
                        if msg.get('type') == 'event' and msg.get('id') in (5,6,7):
                            names = {5: ('entity', self.engine.update_entity_registry), 6: ('device', self.engine.update_device_registry), 7: ('area', self.engine.update_area_registry)}
                            name, callback = names[msg['id']]
                            ws.send(json.dumps({'id': next_id, 'type': f'config/{name}_registry/list'}))
                            registry_requests[next_id] = callback
                            next_id += 1
                            continue
                        if msg.get("type") != "event" or msg.get("id") != 2:
                            continue
                        event = msg.get("event") or {}
                        data = event.get("data") or {}
                        self.engine.on_state_changed(data)
            except Exception as exc:
                self.engine.ws_connected = False
                self.engine.ws_error = f"{type(exc).__name__}: {exc}"
                if not self.stop_event.is_set():
                    time.sleep(backoff)
                    backoff = min(30.0, backoff * 1.7)
        self.engine.ws_connected = False


class Engine(threading.Thread):
    daemon = True

    def __init__(self):
        super().__init__(name="adaptive-ai-engine")
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.runtime = {}
        self.models = {}
        self.context_relevance = {}
        self.last_state_count = 0
        self.last_poll = None
        self.error = None
        self.state_map = {}
        self.entity_registry = {}
        self.context = ContextEngine(OPTIONS, STORE)
        self.executor = Executor(self)
        self.experiments = Experiments(STORE)
        self.history_manager = None
        self.home_bootstrap = None
        self.archive_seen = {}
        self.archive_last_ts = {}
        self.pending_archive = []
        self.command_echoes = {}
        self.command_contexts = {}
        self.state_revision = 0
        self.entity_revisions = {}
        self.dirty_entities = set()
        self.last_trigger_entity = None
        self.control_workers = ThreadPoolExecutor(max_workers=8, thread_name_prefix="device-control")
        self.poll_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ha-poll")
        self.in_flight = {}
        self.resubmit_targets = set()
        self.poll_future = None
        self.last_full_poll = 0.0
        self.last_ws_event = None
        self.ws_connected = False
        self.ws_error = None
        self.temporal_history = TemporalHistory(maxlen=24)
        self.lock = threading.RLock()
        self.teaching = Teaching(STORE)
        # Optional final-entrypoint services. The core Engine owns the decision-composition
        # contract; runtimes that do not install Stage 07 retain legacy behaviour exactly.
        self.preference_model = None
        self.decision_composer = None

    def prime_temporal_from_archive(self, start_ts, end_ts):
        # Startup must not scan/replay the archive; live states warm temporal context.
        return 0

    def status(self):
        # Never hold the engine lock while waiting on SQLite. This keeps Ingress/UI
        # responsive during large history imports and avoids lock-order stalls.
        with self.lock:
            runtime_conf = {aid: rt.get("last_confidence") for aid, rt in self.runtime.items()}
            engine_error = self.error
            last_poll = self.last_poll
            state_count = self.last_state_count
            ws_connected = self.ws_connected
            ws_error = self.ws_error
            registry_count = len(self.entity_registry)
            last_ws_event = self.last_ws_event
        agents = STORE.list_agents()
        confidences = [runtime_conf.get(a["id"]) for a in agents]
        confidences = [x for x in confidences if x is not None]
        history = self.history_manager.status() if self.history_manager is not None else {"phase": "starting", "archive": {"n": 0, "days": 0, "entities": 0}}
        return {
            "version": APP_VERSION,
            "ha_connected": HA.last_error is None and HA.last_ok is not None,
            "ha_last_ok": HA.last_ok,
            "ha_error": HA.last_error,
            "engine_error": engine_error,
            "last_poll": last_poll,
            "state_count": state_count,
            "options": OPTIONS,
            "ha_base_url": HA_BASE_URL,
            "agent_count": len(agents),
            "average_confidence": sum(confidences) / len(confidences) if confidences else 0.0,
            "feedback_count": sum(int(a.get("feedback_count") or 0) for a in agents),
            "historical_experience_count": sum(int(a.get("historical_count") or 0) for a in agents),
            "realtime": {"connected": ws_connected, "error": ws_error, "registry_entries": registry_count, "last_event": last_ws_event},
            "automation_knowledge": AUTOMATION_KNOWLEDGE.status(),
            "history": history,
            "home_intelligence": self.context.diagnostics(),
            "home_bootstrap": dict(self.home_bootstrap.status) if self.home_bootstrap else {},
            "telemetry": TELEMETRY.snapshot(),
            "heavy_job": HEAVY_JOBS.owner,
        }

    def update_entity_registry(self, entries):
        registry = {e.get("entity_id"): e for e in entries if isinstance(e, dict) and e.get("entity_id")}
        with self.lock:
            changed = registry != self.entity_registry
            self.context.configure(self.state_map, entities=registry)
            self.entity_registry = self.context.resolved_registry()
            if changed:
                # Context membership depends on device_id. Recreate in-memory policies so
                # a newly detected controllable device cannot leave sibling entities in
                # an old schema. Stored weights remain available for the history rebuild.
                self.models.clear()
        STORE.event(None, "info", "entity_registry", f"Loaded {len(registry)} Entity Registry entries for cleaner agent discovery", None)

    def update_device_registry(self, entries):
        with self.lock:
            self.context.configure(self.state_map, devices=entries)
            self.entity_registry = self.context.resolved_registry()
            self.models.clear()

    def update_area_registry(self, entries):
        with self.lock:
            self.context.configure(self.state_map, areas=entries)
            self.models.clear()

    def registry_entry(self, entity_id):
        with self.lock:
            return self.entity_registry.get(entity_id)

    def on_state_changed(self, data):
        entity_id = data.get("entity_id")
        new_state = data.get("new_state")
        if not entity_id:
            return
        with self.lock:
            old_state = self.state_map.get(entity_id)
            if old_state == new_state:
                return
            incoming_ts = parse_ts((new_state or {}).get("last_updated"))
            old_ts = parse_ts((old_state or {}).get("last_updated"))
            if incoming_ts and old_ts and incoming_ts < old_ts:
                return
            self.state_revision += 1
            self.entity_revisions[entity_id] = self.state_revision
            if new_state is None:
                self.state_map.pop(entity_id, None)
            else:
                self.state_map[entity_id] = new_state
            self.last_state_count = len(self.state_map)
            self.last_ws_event = now_ts()
            self.last_trigger_entity = entity_id
            self.dirty_entities.add(entity_id)
            self.last_event_received = time.perf_counter()
            # Historical replay shares this process. Give fresh HA state changes a short
            # strict-priority window so Shadow/Control inference is never queued behind
            # cooperative offline work for seconds.
            TRAINING_BUDGET.request_interactive_window(
                0.75, reason="ha_state_changed"
            )
            received_ts = now_ts()
            event_ts = parse_ts((new_state or {}).get("last_updated") or
                                (new_state or {}).get("last_changed")) or received_ts
            self.context.observe(
                entity_id, new_state, received_ts,
                event_ts=event_ts, received_ts=received_ts,
            )
        HA.last_ok = now_ts(); HA.last_error = None
        if new_state is not None:
            received_ts = now_ts()
            ts = parse_ts(new_state.get("last_updated") or new_state.get("last_changed")) or received_ts
            self.temporal_history.add(entity_id, ts, self._temporal_state(new_state))
            self._queue_archive_state(new_state, received_ts=received_ts)
        # A state transition can be the precursor to an action; wake inference now instead
        # of waiting for a whole-state REST poll.
        self.wake_event.set()

    def _compact_attrs(self, st):
        attrs = st.get("attributes") or {}
        compact = {}
        for k, v in attrs.items():
            if k in NUMERIC_ATTRS or k in (
                "device_class", "unit_of_measurement", "friendly_name", "supported_color_modes",
                "min", "max", "step", "options", "supported_features",
            ):
                compact[k] = v
        return compact

    def _temporal_state(self, st):
        if st is None:
            return None
        return {
            "entity_id": st.get("entity_id"), "state": st.get("state"),
            "attributes": self._compact_attrs(st),
            "last_changed": st.get("last_changed"), "last_updated": st.get("last_updated"),
        }

    def _queue_archive_state(self, st, force=False, received_ts=None):
        entity_id = st.get("entity_id")
        if not entity_id:
            return
        now = float(received_ts if received_ts is not None else now_ts())
        compact_attrs = self._compact_attrs(st)
        fingerprint = json.dumps([st.get("state"), compact_attrs], sort_keys=True, separators=(",", ":"), default=str)
        with self.lock:
            if fingerprint == self.archive_seen.get(entity_id):
                return
            controllable = bool(target_options_for_state(st))
            discrete = entity_id.split('.')[0] in ("binary_sensor", "person", "device_tracker", "input_boolean")
            if not force and not controllable and not discrete and now - self.archive_last_ts.get(entity_id, 0.0) < float(OPTIONS["archive_context_interval_seconds"]):
                return
            ts = parse_ts(st.get("last_updated") or st.get("last_changed")) or now
            user_id = (st.get("context") or {}).get("user_id")
            self.pending_archive.append((entity_id, ts, st.get("state"), compact_attrs, user_id, "live", now))
            self.archive_seen[entity_id] = fingerprint
            self.archive_last_ts[entity_id] = now

    def flush_archive(self):
        with self.lock:
            rows = self.pending_archive
            self.pending_archive = []
        if rows:
            STORE.archive_batch(rows)

    def refresh_states(self):
        with self.lock:
            poll_revision = self.state_revision
        states = HA.states()
        state_map = {s["entity_id"]: s for s in (states or [])}
        with self.lock:
            # A REST request may finish after newer websocket events. Never rewind them.
            for eid, old in self.state_map.items():
                new = state_map.get(eid)
                newer_event = self.entity_revisions.get(eid, 0) > poll_revision
                old_ts = parse_ts(old.get("last_updated")) or 0
                new_ts = parse_ts((new or {}).get("last_updated")) or 0
                if newer_event or (new is not None and old_ts > new_ts):
                    state_map[eid] = old
            for eid, revision in self.entity_revisions.items():
                if revision > poll_revision and eid not in self.state_map:
                    state_map.pop(eid, None)
            initial = not self.state_map
            self.context.configure(state_map)
            poll_received_ts = now_ts()
            previous_state_map = dict(self.state_map)
            for eid in set(previous_state_map) | set(state_map):
                changed = previous_state_map.get(eid) != state_map.get(eid)
                current_state = state_map.get(eid)
                event_ts = parse_ts((current_state or {}).get("last_updated") or
                                    (current_state or {}).get("last_changed")) or poll_received_ts
                if changed:
                    self.state_revision += 1
                    self.entity_revisions[eid] = self.state_revision
                    self.context.observe(
                        eid, current_state, poll_received_ts, learn=not initial,
                        event_ts=event_ts, received_ts=poll_received_ts,
                    )
                    self.dirty_entities.add(eid)
            self.state_map = state_map
            if initial:
                self.context.home.arrivals.clear()
                self.context.home.pending = None
            self.last_state_count = len(state_map)
            self.last_poll = now_ts()
            self.last_full_poll = self.last_poll
            self.error = None
        poll_received_ts = now_ts()
        for st in state_map.values():
            ts = parse_ts(st.get("last_updated") or st.get("last_changed")) or poll_received_ts
            self.temporal_history.add(st.get("entity_id"), ts, self._temporal_state(st))
            self._queue_archive_state(st, received_ts=poll_received_ts)
        self.flush_archive()
        self.wake_event.set()
        return state_map

    def run(self):
        print(f"Adaptive AI {APP_VERSION} starting; HA={HA_BASE_URL}", flush=True)
        STORE.event(None, "info", "startup", f"Adaptive AI {APP_VERSION} started", None)
        try:
            self.refresh_states()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        tick = max(0.5, float(OPTIONS.get("proactive_tick_seconds", 1)))
        debounce = max(0.0, float(OPTIONS.get("realtime_inference_debounce_ms", 150)) / 1000.0)
        while not self.stop_event.is_set():
            event_wakeup = self.wake_event.wait(tick)
            self.wake_event.clear()
            if event_wakeup and debounce:
                # Coalesce bursts (motion + lux + light state etc.) into one inference pass.
                self.stop_event.wait(debounce)
            try:
                if now_ts() - self.last_full_poll >= float(OPTIONS["poll_seconds"]) and (self.poll_future is None or self.poll_future.done()):
                    self.last_full_poll = now_ts()
                    self.poll_future = self.poll_worker.submit(self.refresh_states)
                self.flush_archive()
                self.teaching.flush()
                self.context.home.expire(now_ts())
                self.context.save()
                with self.lock:
                    state_map = dict(self.state_map)
                    changed_entities = set(self.dirty_entities) if event_wakeup else set()
                    if event_wakeup:
                        self.dirty_entities.clear()
                if state_map:
                    self.process(state_map, changed_entities if event_wakeup else None)
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                with self.lock:
                    self.error = msg
                print(f"[engine] {msg}", flush=True)

    def archive_live_states(self, state_map):
        # Kept for compatibility with tests/older call sites; event-driven runtime queues
        # changes and commits them in batches.
        for st in state_map.values():
            self._queue_archive_state(st)
        self.flush_archive()

    def policy(self, agent):
        aid = agent["id"]
        actions = action_values(agent)
        cached = self.models.get(aid)
        if cached and cached.actions == actions and cached.dims == int(OPTIONS["feature_dimensions"]):
            return cached
        hint_entities, _ = AUTOMATION_KNOWLEDGE.hints_for_target(agent["target_entity"])
        with self.lock:
            state_map = dict(self.state_map)
            registry = dict(self.entity_registry)
        model = MultiHorizonPolicy(agent, state_map, registry, hint_entities, STORE.get_model(aid), self.context_relevance.get(aid), context_engine=self.context)
        self.models[aid] = model
        return model

    def take_control(self, agent, refresh=False):
        return self.executor.take_control(agent, refresh)

    def process(self, state_map, changed_entities=None):
        changed = set(changed_entities or ())
        if changed:
            # Extend the event's strict-priority window from the actual inference pass,
            # after websocket debounce. This prevents a slow multi-target pass from
            # handing CPU back to replay halfway through the decisions it was woken to make.
            TRAINING_BUDGET.request_interactive_window(
                1.0, reason="realtime_inference"
            )
        groups = {}
        for agent in STORE.list_agent_configs():
            if not agent["enabled"] or agent["mode"] == "paused" or agent.get("training_state") != "qualified":
                self.experiments.cancel(agent['id'], 'mode, training or availability changed')
                continue
            if changed:
                cached = self.models.get(agent["id"])
                if cached is not None and agent["target_entity"] not in changed and not (changed & (set(cached.schema.entities) | self.context.admitted | self.experiments.watches(agent['id']))):
                    continue
            groups.setdefault(agent["target_entity"], []).append(agent)
        for target, agents in groups.items():
            active = self.in_flight.get(target)
            if active is not None and not active.done():
                if changed and target not in self.resubmit_targets:
                    self.resubmit_targets.add(target)
                    def retry_completed(_future, entity=target):
                        with self.lock:
                            self.resubmit_targets.discard(entity)
                            self.dirty_entities.add(entity)
                        self.wake_event.set()
                    active.add_done_callback(retry_completed)
                continue
            self.in_flight[target] = self.control_workers.submit(self.process_target, agents, changed)
        for target in list(self.in_flight):
            if target not in groups and self.in_flight[target].done():
                del self.in_flight[target]

    def process_target(self, agents, changed_entities=None):
        with self.lock:
            revision = self.state_revision
            states = dict(self.state_map)
        for agent in agents:
            if self.stop_event.is_set():
                return
            try:
                latest = STORE.get_agent_config(agent["id"])
                if latest and latest["enabled"] and latest["mode"] != "paused" and latest.get("training_state") == "qualified":
                    if changed_entities:
                        self.process_agent(latest, states, changed_entities)
                    else:
                        self.process_agent(latest, states)
            except Exception as exc:
                STORE.event(agent["id"], "error", "agent_error", str(exc), {"trace": traceback.format_exc(limit=4)})
        if self.state_revision != revision:
            self.wake_event.set()

    def release_manual_hold(self, agent_id):
        STORE.meta_set("manual_hold:" + agent_id, "0")
        STORE.meta_set("manual_hold_source:" + agent_id, "")
        rt = self.runtime.get(agent_id)
        if rt:
            rt["manual_override_until"] = 0.0
        self.wake_event.set()

    def _reward_pending(self, agent, rt, reward, reason, user_id=None, experience=None):
        pending = experience if experience is not None else rt.get("pending")
        if not pending:
            return
        if pending.get('experiment') or pending.get('teaching_id'):
            # Trial feedback belongs to the separate online model. Do not inject a
            # counterfactual action as a demonstrated historical preference.
            if rt.get('pending') is pending:
                rt['pending'] = None
            return
        policy = self.policy(agent)
        policy.update(int(pending.get("policy_head") or min(policy.horizons)), pending["action_index"], pending["features"], reward)
        STORE.save_model(agent["id"], policy.serialize())
        STORE.add_feedback(
            agent["id"], pending["action_index"], pending["action_value"], reward, reason,
            pending["features"], user_id,
        )
        rt["last_reward_components"] = rt.pop("reward_components_pending", {})
        rt["last_reward"] = reward
        rt["last_reward_reason"] = reason
        if rt.get('pending') is pending:
            rt["pending"] = None
        STORE.event(
            agent["id"], "info" if reward >= 0 else "warning", "rl_reward",
            f"RL reward {reward:+.2f}: {reason}",
            {"reward": reward, "reason": reason, "action_value": pending["action_value"], "user_id": user_id},
        )

    def record_command(self, agent, value, response=None):
        """Remember intent and returned contexts; REST HA calls can have a user_id."""
        timestamp = now_ts()
        expiry = timestamp + max(30.0, timing_for(agent).acknowledgement * 2)
        with self.lock:
            self.command_contexts = {k: v for k, v in self.command_contexts.items() if v > timestamp}
            eid = agent["target_entity"]
            records = self.command_echoes.setdefault(eid, [])
            records[:] = [r for r in records if r["expires"] > timestamp][-7:]
            records.append({"property": agent["target_property"], "value": value, "expires": expiry})
            for state in response if isinstance(response, list) else []:
                if not isinstance(state, dict):
                    continue
                context_id = (state.get("context") or {}).get("id")
                if context_id:
                    self.command_contexts[context_id] = expiry

    def own_command_echo(self, agent, state, current):
        timestamp = now_ts()
        context = (state or {}).get("context") or {}
        with self.lock:
            if any(self.command_contexts.get(context.get(k), 0) > timestamp for k in ("id", "parent_id")):
                return True
            return any(r["expires"] > timestamp and r["property"] == agent["target_property"]
                       and same_value(current, r["value"], agent["deadband"])
                       for r in self.command_echoes.get(agent["target_entity"], [])[-1:])

    def set_manual_hold(self, agent, rt, timestamp):
        rt["manual_override_until"] = timestamp + timing_for(agent).manual_hold
        STORE.meta_set("manual_hold:" + agent["id"], str(rt["manual_override_until"]))
        STORE.meta_set("manual_hold_source:" + agent["id"], "explicit_user_v8")

    def process_agent(self, agent, state_map, changed_entities=None):
        aid = agent["id"]
        defaults = {
            "previous_target": None, "last_ai_ts": 0.0, "last_ai_value": None,
            "last_inference_ts": 0.0, "manual_override_until": 0.0,
            "last_prediction": None, "last_confidence": 0.0, "pending": None,
            "last_reward": None, "last_reward_reason": None, "context_meta": {},
            "decision_state": "idle", "decision_reason": "Waiting for inference",
            "last_service_ts": None, "last_service": None, "last_service_ok": None,
            "last_service_error": None, "last_service_data": None,
        }
        with self.lock:
            rt = self.runtime.setdefault(aid, {})
        for key, value in defaults.items():
            rt.setdefault(key, value)
        unresolved = []
        for old in rt.get('outcomes', []):
            area = old.get('area_id')
            forecast = self.context.home.forecast(area, now_ts())
            arrival = self.context.home.values.get(area, {}).get('arrival')
            delay = (arrival-old['started_ts']) if arrival is not None and old['started_ts'] < arrival <= old['ended_ts'] else None
            if delay is None and now_ts() < old['started_ts']+max(1,old['horizon'])+1:
                unresolved.append(old)
                continue
            result = self.executor.reward_engine.evaluate(anticipated=True, arrival_delay=delay,
                horizon=max(1,old['horizon']), observation_complete=True,
                observation_known=bool(forecast.get('known')), chatter=bool(old.get('chatter')))
            rt['reward_components_pending'] = result.components
            self._reward_pending(agent,rt,result.value,'completed earlier anticipation',experience=old)
        rt['outcomes'] = unresolved
        timing = timing_for(agent)
        if not rt.get("restored"):
            # Old versions persisted holds for anonymous changes and even their own echoes.
            trusted = STORE.meta_get("manual_hold_source:" + aid, "") == "explicit_user_v8"
            rt["manual_override_until"] = float(STORE.meta_get("manual_hold:" + aid, "0")) if trusted else 0.0
            rt["restored"] = True
        target_state = state_map.get(agent["target_entity"])
        current = target_value(target_state, agent["target_property"])
        if current is None or not math.isfinite(current):
            self.experiments.cancel(aid, 'target unavailable')
            rt["pending"] = None  # missing outcome is not acceptance
            rt["previous_target"] = None
            rt["decision_state"] = "blocked"
            rt["decision_reason"] = "Target state/value unavailable"
            return
        if agent["target_property"] == "position" and target_state.get("state") in ("opening", "closing"):
            rt["decision_state"] = "waiting"
            rt["decision_reason"] = "Cover is moving; wait for its final position"
            return

        # Resolve acknowledgement separately from delayed preference feedback.
        timestamp = now_ts()
        pending = rt.get("pending")
        previous = rt.get("previous_target")
        changed = previous is not None and abs(current - previous) > max(.01, agent["deadband"] * .05)
        context = (target_state or {}).get("context") or {}
        user_id = context.get("user_id") if not context.get("parent_id") else None
        own_echo = self.own_command_echo(agent, target_state, current)
        expected_ack = pending and same_value(current, pending["action_value"], agent["deadband"])
        manual = bool(changed and user_id and not own_echo and not expected_ack)
        self.experiments.observe(agent, state_map, current, manual=manual)
        if changed:
            rt["last_change_origin"] = "own_command" if own_echo or expected_ack else "manual_user" if user_id else "external"
        if changed and user_id and not own_echo and not expected_ack:
            self.teaching.physical_correction(self, agent, state_map, current, timestamp)
            if pending:
                if not same_value(current, pending["action_value"], agent["deadband"]):
                    result = self.executor.reward_engine.evaluate(manual_correction=True, chatter=bool(pending.get('chatter')))
                    rt['reward_components_pending'] = result.components
                    self._reward_pending(agent, rt, result.value, "manual correction", user_id)
                else:
                    rt["pending"] = None
            # A demonstrated preference is useful even in Shadow and without an AI action.
            policy = self.policy(agent)
            features, _, _ = policy.features(state_map, self.temporal_history, at_ts=timestamp)
            idx = min(range(len(policy.actions)), key=lambda i: abs(policy.actions[i] - current))
            for h in policy.horizons:
                policy.update(h, idx, features, 1.0)
            STORE.save_model(aid, policy.serialize())
            STORE.add_feedback(aid, idx, policy.actions[idx], 1.0, "manual demonstration", features, user_id)
            self.set_manual_hold(agent, rt, timestamp)
        elif pending:
            matches = same_value(current, pending["action_value"], agent["deadband"])
            age = timestamp - pending["started_ts"]
            if matches and pending.get("acknowledged_ts") is None:
                pending["acknowledged_ts"] = timestamp
                rt["ack_latency_seconds"] = age
            elif pending.get("acknowledged_ts") is None and age >= timing.acknowledgement:
                rt["pending"] = None
                rt["retry_after"] = timestamp + max(2.0, timing.settling)
                STORE.event(aid, "warning", "ack_timeout", "Device did not confirm the requested value", {"seconds": age})
            elif changed and pending.get("acknowledged_ts") is not None and not matches:
                # Unknown hardware changes and automations are ambiguous, not human labels.
                rt["pending"] = None
                # Unknown/linked actuator changes are not human instructions.
                rt["last_reward_reason"] = "external target change (not a manual override)"
            elif matches and pending.get("acknowledged_ts") is not None:
                window = max(timing.settling, float(OPTIONS["reward_window_seconds"]))
                if timestamp - pending["acknowledged_ts"] >= window:
                    result = self.executor.reward_engine.evaluate(accepted=True, chatter=bool(pending.get('chatter')))
                    rt['reward_components_pending'] = result.components
                    self._reward_pending(agent, rt, result.value, "weak acceptance after settling")
        elif changed and not own_echo:
            rt["last_reward_reason"] = "external target change (not a manual override)"
        pending = rt.get('pending')
        if pending and pending.get('anticipated') and pending.get('acknowledged_ts') is not None:
            area = pending.get('area_id')
            forecast = self.context.home.forecast(area, timestamp)
            arrival = self.context.home.values.get(area, {}).get('arrival')
            delay = arrival - pending['started_ts'] if arrival is not None and arrival > pending['started_ts'] else None
            horizon = max(1, pending['horizon'])
            if delay is not None or timestamp - pending['started_ts'] > horizon + timing.settling:
                result = self.executor.reward_engine.evaluate(anticipated=True, arrival_delay=delay, horizon=horizon,
                    observation_complete=True, observation_known=bool(forecast.get('known')),
                    chatter=bool(pending.get('chatter')))
                rt['reward_components_pending'] = result.components
                self._reward_pending(agent, rt, result.value, 'anticipation outcome')
        rt["previous_target"] = current

        if agent["mode"] not in ("shadow", "control"):
            rt["decision_state"] = "paused"
            rt["decision_reason"] = "Agent is paused"
            return
        inference_started = time.perf_counter()
        with self.lock:
            context_revision = self.context.home.revision
            state_map = dict(self.state_map)
            target_revision = self.entity_revisions.get(agent['target_entity'], 0)
            input_revisions = dict(self.entity_revisions)
        min_inference_gap = max(0.05, float(OPTIONS.get("realtime_inference_debounce_ms", 75)) / 1000.0)
        if not changed_entities and now_ts() - rt["last_inference_ts"] < min_inference_gap:
            return
        rt["last_inference_ts"] = now_ts()

        hint_entities, automation_infos = AUTOMATION_KNOWLEDGE.hints_for_target(agent["target_entity"])
        policy = self.policy(agent)
        features, labels, context_meta = policy.features(state_map, self.temporal_history, at_ts=now_ts())
        context_meta.update(policy.selection_meta or {})
        context_meta["automation_hint_entities"] = len(hint_entities)
        context_meta["whole_home_entities"] = len(state_map)
        context_meta["trigger_entities"] = sorted(set(changed_entities or ()))[:8]
        context_meta["primary_local_sensors"] = list((policy.selection_meta or {}).get("primary_local_sensors") or [])
        context_meta["primary_local_sensor"] = (policy.selection_meta or {}).get("primary_local_sensor")
        context_meta["primary_occupancy_sensor"] = (policy.selection_meta or {}).get("primary_occupancy_sensor")
        context_meta["causal_presence_scores"] = dict((policy.selection_meta or {}).get("causal_presence_scores") or {})
        context_meta["upstream_sensors"] = list((policy.selection_meta or {}).get("upstream_sensors") or [])
        rt["context_meta"] = context_meta
        rt["automation_priors"] = [
            {"entity_id": x.get("entity_id"), "name": x.get("name"), "enabled": bool(x.get("enabled")),
             "context_count": len(x.get("context_entities") or [])}
            for x in automation_infos[:8]
        ]

        teaching_revision = self.teaching.revision(aid)
        chosen, confidence, arms, horizon, support, novelty = policy.predict(features)
        composer = self.decision_composer
        preference = None
        instruction = None
        decision_source = "historical_policy_bootstrap"
        if composer is not None:
            composed = composer.compose(
                agent=agent, policy=policy, state_map=state_map, temporal=self.temporal_history,
                timestamp=now_ts(), features=features, labels=labels, chosen=chosen,
                confidence=confidence, arms=arms, horizon=horizon, support=support,
                novelty=novelty, runtime=rt, registry=self.context.resolved_registry,
            )
            chosen = composed["chosen"]
            confidence = composed["confidence"]
            arms = composed["arms"]
            horizon = composed["horizon"]
            support = composed["support"]
            novelty = composed["novelty"]
            baseline_value = composed["baseline_value"]
            teaching = composed["teaching"]
            instruction = composed.get("instruction")
            preference = composed.get("preference")
            trial = composed["trial"]
            decision_source = composed["source"]
        else:
            # Compatibility path for non-final entrypoints. Stage 07 changes only the
            # shipped preference_queue_main composition and does not reinterpret old data.
            baseline_value = chosen['value']
            teaching = self.teaching.match(agent, policy, state_map, self.temporal_history, now_ts())
            if teaching:
                chosen = dict(chosen, value=teaching['desired'], index=min(range(len(policy.actions)), key=lambda i: abs(policy.actions[i]-teaching['desired'])))
                decision_source = "legacy_teaching"
            trial = None if teaching else self.experiments.propose(agent, policy, state_map, self.context.resolved_registry,
                features, labels, chosen, confidence, arms, horizon, rt)
            if trial:
                chosen = dict(chosen, value=trial['value'], index=trial['index'])
                support, novelty = trial['support'], trial['novelty']
                decision_source = "experiment"
        rt['teaching_id'] = teaching['id'] if teaching else None
        rt['decision_source'] = decision_source
        rt['preference_model'] = preference
        rt['instruction_scope'] = (instruction or {}).get('scope') if instruction else None
        micro_explore = bool(trial)
        rt['baseline_prediction'] = baseline_value
        rt["last_prediction"] = chosen["value"]
        self.teaching.record(aid, current, chosen['value'], now_ts())
        rt["last_confidence"] = confidence
        rt["structural_confidence"] = chosen.get("structural_confidence", confidence)
        rt["validation_accuracy"] = chosen.get("validation_accuracy", 0.0)
        rt["validation_lower_bound"] = chosen.get("validation_lower_bound", 0.0)
        rt["validation_samples"] = chosen.get("validation_samples", 0)
        rt["last_uncertainty"] = chosen["uncertainty"]
        rt["last_expected_reward"] = chosen["mean"]
        rt["historical_support"] = support
        rt["context_novelty"] = novelty
        rt["prediction_horizon"] = horizon
        rt["micro_exploration_candidate"] = micro_explore
        rt["top_context"] = self.top_context(policy, horizon, chosen["index"], features, labels)

        intent_horizon = horizon
        forecast = context_meta.get('home_forecast', {})
        if chosen['value'] >= .5 and agent['target_property'] == 'power' and forecast.get('occupancy_now', 0) < .5:
            intent_horizon = next((h for h in (1,3,5) if forecast.get(f'occupancy_in_{h}s', 0) >= .5), horizon)
        preference_count = int((preference or {}).get('independent_evidence_count') or 0)
        intent = ActionIntent.create(
            agent_id=aid, target_entity=agent['target_entity'], target_property=agent['target_property'],
            desired_value=chosen['value'], confidence=confidence, support=support, novelty=novelty,
            prediction_horizon=intent_horizon, policy_head=horizon, created_at=now_ts(), ttl=float(OPTIONS.get('intent_ttl_seconds', 2)),
            policy_version=policy.VERSION, model_revision=policy.model_revision,
            context_revision=context_revision, target_revision=target_revision,
            teaching_id=teaching['id'] if teaching else 0,
            teaching_revision=teaching_revision,
            decision_source=decision_source,
            reason=(f"User instruction #{teaching['id']} ({rt.get('instruction_scope') or 'legacy'}): Desired {chosen['value']}" if teaching else
                f"Explicit preference model: Desired {chosen['value']} from {preference_count} independent feedback fact(s)" if preference and preference.get('applied') else
                f"Context experiment ({trial['focus']}): {baseline_value} → {chosen['value']}; baseline confidence {confidence:.0%}" if trial else
                f"Historical policy bootstrap desires {chosen['value']}; confidence {confidence:.0%}, support {support:.0%}, novelty {novelty:.0%}"),
            experiment_token=trial['token'] if trial else '',
            contributors=tuple((x['feature'], x['contribution']) for x in rt['top_context']),
            context_dependencies=tuple((eid, input_revisions.get(eid, 0)) for eid in sorted(set(policy.schema.entities) | set(trial['snapshot'] if trial else ()))))
        rt['last_intent'] = intent.export()
        rt['behavior_summary'] = self.behavior_summary(agent, rt)
        TELEMETRY.observe('inference', (time.perf_counter()-inference_started)*1000)
        received = getattr(self, 'last_event_received', None)
        if changed_entities and received:
            TELEMETRY.observe('event_to_intent', (time.perf_counter()-received)*1000)
        return self.executor.submit(intent, features, chosen['index'])

    def behavior_summary(self, agent, rt):
        forecast = rt.get('context_meta', {}).get('home_forecast', {})
        drivers = ', '.join(x['feature'] for x in rt.get('top_context', [])[:3]) or 'no stable contributors yet'
        return (f"{agent['target_entity']}: desired {rt.get('last_prediction')}; "
                f"source {rt.get('decision_source') or 'historical_policy_bootstrap'}; "
                f"target area {forecast.get('area_id') or 'unmapped'}, "
                f"occupancy within 3 s {forecast.get('occupancy_in_3s', 0):.0%}. "
                f"Largest linear contributions: {drivers}. This describes learned evidence, not a rule.")

    def top_context(self, policy, horizon, action_idx, features, labels):
        head = policy.heads[int(horizon)]
        aa, bb = head.a[action_idx], head.b[action_idx]
        ranked = []
        for idx, x in features.items():
            if idx >= policy.dims:
                continue
            theta = bb[idx] / max(aa[idx], 1e-9)
            contribution = theta * x
            if abs(contribution) < 1e-4:
                continue
            ranked.append((abs(contribution), contribution, labels.get(idx, [f"feature:{idx}"])[:2]))
        ranked.sort(reverse=True)
        return [{"feature": " / ".join(lbls), "contribution": contrib} for _, contrib, lbls in ranked[:7]]

    def runtime_for(self, agent):
        rt = self.runtime.get(agent["id"]) or {}
        experiment_status = self.experiments.status(agent['id'])
        confidence = float(rt.get("last_confidence") or 0.0)
        policy = self.models.get(agent["id"])
        selected_entities = list(policy.schema.entities) if policy else None
        with self.lock:
            states = dict(self.state_map)
            target_state = self.state_map.get(agent["target_entity"])
        recs, present = sensor_recommendations(agent, states, confidence, selected_entities)
        prediction_label = None
        if agent["target_property"] == "option_index" and rt.get("last_prediction") is not None:
            options = list(((target_state or {}).get("attributes") or {}).get("options") or [])
            idx = int(clamp(round(float(rt["last_prediction"])), 0, max(0, len(options) - 1))) if options else 0
            prediction_label = options[idx] if options else None
        return {
            "experiments": experiment_status,
            "baseline_prediction": rt.get('baseline_prediction'),
            "last_prediction": rt.get("last_prediction"),
            "teaching_id": rt.get("teaching_id"),
            "decision_source": rt.get("decision_source") or "historical_policy_bootstrap",
            "preference_model": rt.get("preference_model"),
            "instruction_scope": rt.get("instruction_scope"),
            "current_value": target_value(target_state, agent["target_property"]) if target_state else None,
            "last_prediction_label": prediction_label,
            "last_confidence": confidence,
            "structural_confidence": float(rt.get("structural_confidence") or 0.0),
            "validation_accuracy": float(rt.get("validation_accuracy") or 0.0),
            "validation_lower_bound": float(rt.get("validation_lower_bound") or 0.0),
            "validation_samples": int(rt.get("validation_samples") or 0),
            "last_uncertainty": rt.get("last_uncertainty"),
            "last_expected_reward": rt.get("last_expected_reward"),
            "last_ai_ts": rt.get("last_ai_ts"),
            "manual_override_until": rt.get("manual_override_until", 0.0),
            "last_change_origin": rt.get("last_change_origin"),
            "last_service_latency_ms": rt.get("last_service_latency_ms"),
            "timing": vars(timing_for(agent)),
            "ack_latency_seconds": rt.get("ack_latency_seconds"),
            "pending_feedback": bool(rt.get("pending")),
            "last_reward": rt.get("last_reward"),
            "last_reward_reason": rt.get("last_reward_reason"),
            "context_meta": rt.get("context_meta") or (dict(policy.selection_meta) if policy else {}),
            "top_context": rt.get("top_context") or [],
            "sensor_recommendations": recs,
            "present_capabilities": present,
            "automation_priors": rt.get("automation_priors") or [],
            "enabled_automation_conflicts": sum(1 for x in (rt.get("automation_priors") or []) if x.get("enabled")),
            "decision_state": rt.get("decision_state") or "idle",
            "decision_reason": rt.get("decision_reason") or "Waiting for inference",
            "automation_scan_warning": AUTOMATION_KNOWLEDGE.error,
            "last_service_ts": rt.get("last_service_ts"),
            "last_service": rt.get("last_service"),
            "last_service_ok": rt.get("last_service_ok"),
            "last_service_error": rt.get("last_service_error"),
            "last_service_data": rt.get("last_service_data"),
            "prediction_lead_seconds": int(rt.get("prediction_horizon") or 0),
            "prediction_horizon": int(rt.get("prediction_horizon") or 0),
            "historical_support": float(rt.get("historical_support") or 0.0),
            "context_novelty": float(rt.get("context_novelty") if rt.get("context_novelty") is not None else 1.0),
            "realtime_connected": bool(self.ws_connected),
            "policy_updates": int(policy.total_updates) if policy else int(agent.get("feedback_count") or 0) + int(agent.get("historical_count") or 0),
            "historical_experiences": int(agent.get("historical_count") or 0),
            "micro_exploration": experiment_status['config']['enabled'],
            "selected_context_entities": list(policy.schema.entities) if policy else [],
            "prediction_horizons": list(policy.horizons) if policy else parse_horizons(agent),
            "training_state": agent.get("training_state") or "training",
            "benchmark_score": agent.get("benchmark_score"),
            "benchmark_samples": int(agent.get("benchmark_samples") or 0),
            "benchmark_source": agent.get("benchmark_source"),
            "benchmark_detail": agent.get("benchmark_detail") or {},
            "model": "Shared home state + scoped instruction + explicit light preference + diagonal LinUCB bootstrap → ActionIntent → Executor",
            "intent": rt.get('intent'), "last_intent": rt.get('last_intent'),
            "home_forecast": self.context.forecast(agent['target_entity'], now_ts()),
            "behavior_summary": rt.get('behavior_summary'),
            "reward_components": rt.get('last_reward_components', {}),
            "policy_diagnostics": policy.diagnostics() if policy else {},
        }
