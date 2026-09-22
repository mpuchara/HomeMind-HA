from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
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
from telemetry import TELEMETRY, HEAVY_JOBS, RUNTIME_DEBUG
from fast_runtime import fast_light_on_assist_action, is_fast_target, stabilize_fast_light_power_decision
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
                with ws_connect(
                    "ws://supervisor/core/websocket",
                    open_timeout=10,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
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
                # Realtime loss is the one case where a full REST snapshot should become
                # urgent. Healthy websocket operation uses the much slower safety resync.
                with self.engine.lock:
                    self.engine.last_full_poll = 0.0
                self.engine.wake_event.set()
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
        # Startup gate: initial HA snapshot/registry work may touch thousands of states.
        # Do not let the first all-agent inference burst compete with Ingress before the
        # runtime has finished composing and the UI readiness endpoint is available.
        # Direct process/process_agent calls remain unchanged; only the background loop
        # waits for initialize_runtime() to explicitly open the gate.
        self.inference_enabled = threading.Event()
        self.startup_inference_not_before = 0.0
        self.runtime = {}
        self.inference_scheduler = {
            "event_passes": 0,
            "timer_passes": 0,
            "idle_skips": 0,
            "last_timer_targets": 0,
            "initial_full_passes": 0,
        }
        self.initial_inference_pending = True
        self.models = {}
        self.context_relevance = {}
        # Realtime routing cache. Event dispatch must not hit SQLite or recompute every
        # agent's dependency set on each HA state_changed event.
        self.agent_index_at = 0.0
        # Normal invalidation is revision-driven. A 10 minute safety fallback catches
        # truly external/direct DB edits without turning agent-table scans into periodic I/O.
        self.agent_index_ttl_seconds = 600.0
        self.agent_index_revision = -1
        # Keep the complete configured-agent snapshot separate from the inference routing
        # subset. UI/status needs paused/waiting agents too; event dispatch must only see
        # policies that are currently eligible to infer.
        self.all_agent_configs = {}
        self.agent_configs = {}
        self.active_agents_by_target = {}
        self.dependency_agents = {}
        # A pass-level immutable revision snapshot is shared by all agents dispatched
        # from one coalesced event pass. Thread-local binding preserves the existing
        # process_agent(agent, states, changed) public signature used by extensions.
        self._inference_tls = threading.local()
        # Runtime extensions may broaden observation-only inference eligibility, but they
        # must not replace the scheduler. Control qualification remains in Executor.
        self.inference_eligible = self._default_inference_eligible
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
        self.archive_flush_interval_seconds = 5.0
        self.archive_flush_batch_rows = 128
        self.last_archive_flush = time.monotonic()
        self.command_echoes = {}
        self.command_contexts = {}
        self.state_revision = 0
        self.entity_revisions = {}
        self.dirty_entities = set()
        self.last_trigger_entity = None
        # Policy inference is predominantly Python CPU work. More worker threads increase
        # GIL contention and can starve Ingress on Raspberry Pi. Two workers retain limited
        # overlap for SQLite/I/O while bounding CPU contention; single-core hosts stay at 1.
        self.control_worker_count = min(2, max(1, int(os.cpu_count() or 1)))
        self.control_workers = ThreadPoolExecutor(
            max_workers=self.control_worker_count, thread_name_prefix="device-control"
        )
        self.poll_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ha-poll")
        self.in_flight = {}
        self.resubmit_targets = set()
        self.poll_future = None
        self.last_full_poll = 0.0
        # REST /states health is tracked separately from generic HAClient requests.
        # A failed history/automation/config request must never make the UI claim that
        # Home Assistant itself is disconnected.
        self.last_state_sync_ok = None
        self.last_state_sync_error = "No successful state snapshot yet"
        self.state_resync_stats = {
            "runs": 0,
            "failures": 0,
            "last_changed_entities": 0,
            "last_duration_ms": 0.0,
            "max_duration_ms": 0.0,
        }
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
            last_state_sync_ok = self.last_state_sync_ok
            last_state_sync_error = self.last_state_sync_error
            state_resync_stats = dict(self.state_resync_stats)
            inference_scheduler = dict(self.inference_scheduler)
        agents = STORE.list_agents()
        confidences = [runtime_conf.get(a["id"]) for a in agents]
        confidences = [x for x in confidences if x is not None]
        history = self.history_manager.status() if self.history_manager is not None else {"phase": "starting", "archive": {"n": 0, "days": 0, "entities": 0}}
        telemetry = TELEMETRY.snapshot()
        return {
            "version": APP_VERSION,
            "ha_connected": bool(
                ws_connected
                or (last_state_sync_ok is not None and last_state_sync_error is None)
            ),
            "ha_last_ok": last_state_sync_ok,
            "ha_error": (
                None
                if ws_connected or (last_state_sync_ok is not None and last_state_sync_error is None)
                else (last_state_sync_error or ws_error)
            ),
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
            "state_resync": {
                **state_resync_stats,
                "last_ok": last_state_sync_ok,
                "error": last_state_sync_error,
            },
            "inference_scheduler": {
                **inference_scheduler,
                "fast_idle_seconds": float(OPTIONS.get("fast_idle_inference_interval_seconds", 10)),
                "default_idle_seconds": float(OPTIONS.get("idle_inference_interval_seconds", 30)),
            },
            "automation_knowledge": AUTOMATION_KNOWLEDGE.status(),
            "history": history,
            "home_intelligence": self.context.diagnostics(),
            "home_bootstrap": dict(self.home_bootstrap.status) if self.home_bootstrap else {},
            "telemetry": telemetry,
            "runtime_debug": RUNTIME_DEBUG.summary(telemetry),
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
            self.context.observe(entity_id, new_state, now_ts())
        HA.last_ok = now_ts(); HA.last_error = None
        if new_state is not None:
            ts = parse_ts(new_state.get("last_updated") or new_state.get("last_changed")) or now_ts()
            self.temporal_history.add(entity_id, ts, self._temporal_state(new_state))
            self._queue_archive_state(new_state)
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

    def _queue_archive_state(self, st, force=False):
        entity_id = st.get("entity_id")
        if not entity_id:
            return
        now = now_ts()
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
            self.pending_archive.append((entity_id, ts, st.get("state"), compact_attrs, user_id, "live"))
            self.archive_seen[entity_id] = fingerprint
            self.archive_last_ts[entity_id] = now

    def flush_archive(self, force=True):
        """Persist live history in coarse batches instead of one WAL txn per tick."""
        now = time.monotonic()
        with self.lock:
            pending = len(self.pending_archive)
            if not pending:
                self.last_archive_flush = now
                return 0
            if (not force and pending < self.archive_flush_batch_rows
                    and now - self.last_archive_flush < self.archive_flush_interval_seconds):
                return 0
            rows = self.pending_archive
            self.pending_archive = []
        try:
            written = STORE.archive_batch(rows)
        except Exception:
            with self.lock:
                self.pending_archive = rows + self.pending_archive
            raise
        self.last_archive_flush = time.monotonic()
        return int(written or 0)

    def refresh_states(self):
        """Reconcile a REST snapshot without replaying the whole home on every resync.

        Websocket state_changed is authoritative for the realtime path. REST is startup,
        outage fallback and a low-frequency safety reconciliation. Periodic snapshots may
        contain hundreds of entities, so only actual differences are fed through context,
        temporal history and archive persistence after the initial bootstrap.
        """
        started = time.perf_counter()
        with self.lock:
            poll_revision = self.state_revision
        try:
            states = HA.states()
        except Exception as exc:
            with self.lock:
                self.last_state_sync_error = f"{type(exc).__name__}: {exc}"
                self.state_resync_stats["failures"] = int(
                    self.state_resync_stats.get("failures") or 0
                ) + 1
            raise

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

            previous = self.state_map
            initial = not previous
            changed_eids = {
                eid for eid in set(previous) | set(state_map)
                if previous.get(eid) != state_map.get(eid)
            }
            # Registry websocket updates already reconfigure topology. A routine /states
            # reconciliation must not rebuild the full source map unless entity membership
            # actually changed (or this is the initial bootstrap).
            topology_changed = initial or set(previous) != set(state_map)
            if topology_changed:
                self.context.configure(state_map)

            event_ts = now_ts()
            for eid in changed_eids:
                self.state_revision += 1
                self.entity_revisions[eid] = self.state_revision
                self.context.observe(eid, state_map.get(eid), event_ts, learn=not initial)
                # The initial REST snapshot is not a realtime transition. Marking
                # thousands of startup entities dirty causes an immediate all-agent
                # burst and defeats HTTP-first startup. A proactive pass after the
                # startup grace covers the same current state.
                if not initial:
                    self.dirty_entities.add(eid)

            self.state_map = state_map
            if initial:
                # The REST startup snapshot describes current state, not fresh movement.
                # Clear anonymous trajectories and explicit boundary-arrival hints together.
                self.context.home.reset_movement_state()
            sync_now = now_ts()
            self.last_state_count = len(state_map)
            self.last_poll = sync_now
            self.last_full_poll = sync_now
            self.last_state_sync_ok = sync_now
            self.last_state_sync_error = None
            self.error = None

        # Initial bootstrap needs every current entity once. Later safety/fallback polls
        # touch only the changed suffix instead of rebuilding temporal/archive state for
        # the whole Home Assistant installation.
        process_eids = set(state_map) if initial else changed_eids
        for eid in process_eids:
            st = state_map.get(eid)
            if st is None:
                continue
            ts = parse_ts(st.get("last_updated") or st.get("last_changed")) or now_ts()
            self.temporal_history.add(eid, ts, self._temporal_state(st))
            self._queue_archive_state(st)
        self.flush_archive(force=False)
        if initial or changed_eids:
            self.wake_event.set()

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        with self.lock:
            stats = self.state_resync_stats
            stats["runs"] = int(stats.get("runs") or 0) + 1
            stats["last_changed_entities"] = len(changed_eids)
            stats["last_duration_ms"] = elapsed_ms
            stats["max_duration_ms"] = max(
                float(stats.get("max_duration_ms") or 0.0), elapsed_ms
            )
        return state_map

    def _schedule_next_inference(self, agent, rt, timestamp=None):
        """Schedule only the next time-dependent inference; HA events remain immediate.

        The 1 s engine tick is a cheap timer wheel, not a global inference cadence.
        Fast targets need a modest heartbeat for time-decaying presence/context, while
        slower plant targets can refresh less frequently. Exact runtime deadlines always
        pre-empt the heartbeat.
        """
        now = float(now_ts() if timestamp is None else timestamp)
        fast = is_fast_target(agent)
        idle = float(OPTIONS.get(
            "fast_idle_inference_interval_seconds" if fast
            else "idle_inference_interval_seconds",
            10.0 if fast else 30.0,
        ))
        idle = max(2.0 if fast else 10.0, idle)
        due = now + idle

        # During an explicit manual hold the learned policy cannot act, so repeatedly
        # recomputing it is pure CPU waste. Wake at hold expiry unless another lifecycle
        # deadline below must be observed earlier.
        try:
            hold_until = float(rt.get("manual_override_until") or 0.0)
        except (TypeError, ValueError):
            hold_until = 0.0
        if hold_until > now:
            due = hold_until

        # Fast-light statistical OFF confirmation must complete close to its exact 6 s
        # deadline even if no HA entity changes in the meantime.
        if rt.get("fast_off_confirmation_active"):
            try:
                since = float(rt.get("fast_off_candidate_since"))
                required = float(rt.get("fast_off_confirmation_required") or 0.0)
                if required > 0:
                    due = min(due, since + required)
            except (TypeError, ValueError):
                pass

        pending = rt.get("pending")
        if isinstance(pending, dict):
            try:
                started = float(pending.get("started_ts") or now)
                acknowledged = pending.get("acknowledged_ts")
                timing = timing_for(agent)
                if acknowledged is None:
                    due = min(due, started + max(0.05, float(timing.acknowledgement)))
                else:
                    due = min(
                        due,
                        float(acknowledged)
                        + max(float(timing.settling), float(OPTIONS["reward_window_seconds"])),
                    )
                if pending.get("anticipated"):
                    due = min(
                        due,
                        started
                        + max(1.0, float(pending.get("horizon") or 1.0))
                        + max(0.0, float(timing.settling)),
                    )
            except (TypeError, ValueError):
                pass

        for outcome in rt.get("outcomes") or ():
            try:
                outcome_due = (
                    float(outcome["started_ts"])
                    + max(1.0, float(outcome.get("horizon") or 1.0))
                    + 1.0
                )
                due = min(due, outcome_due)
            except (KeyError, TypeError, ValueError):
                continue

        try:
            retry_after = float(rt.get("retry_after") or 0.0)
            if retry_after > now:
                due = min(due, retry_after)
        except (TypeError, ValueError):
            pass

        # Never spin on a deadline already in the past. A short floor gives the current
        # worker enough time to publish runtime state before a follow-up is admitted.
        rt["next_periodic_inference_ts"] = max(now + 0.05, float(due))
        return rt["next_periodic_inference_ts"]

    def _due_inference_targets(self, timestamp=None):
        """Return target entities whose in-memory timer deadline has arrived.

        This is intentionally SQLite-free so the 1 s scheduler tick remains negligible.
        Agents that have not run yet are handled by the initial startup pass or an HA
        event; every successful inference schedules its next heartbeat/deadline.
        """
        now = float(now_ts() if timestamp is None else timestamp)
        due = set()
        self._refresh_agent_index()
        with self.lock:
            runtime = list(self.runtime.items())
        for aid, rt in runtime:
            try:
                deadline = float(rt.get("next_periodic_inference_ts") or 0.0)
            except (TypeError, ValueError):
                deadline = 0.0
            if deadline <= 0.0 or deadline > now:
                continue
            with self.lock:
                agent = self.agent_configs.get(str(aid))
            if not agent:
                rt["next_periodic_inference_ts"] = 0.0
                continue
            due.add(str(agent.get("target_entity") or ""))
            # Claim the deadline before scheduling. process_agent will publish the real
            # next deadline after inference; this prevents a busy worker from being
            # re-enqueued on every 1 s tick.
            rt["next_periodic_inference_ts"] = now + 60.0
        due.discard("")
        return due

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
                # A healthy websocket already delivers every state_changed event. The
                # full /states snapshot is only a low-frequency safety resync in that
                # mode; when realtime is down, fall back to the ordinary REST cadence.
                resync_seconds = float(
                    OPTIONS.get("realtime_resync_seconds", 300)
                    if self.ws_connected
                    else OPTIONS.get("realtime_fallback_poll_seconds", 10)
                )
                if (now_ts() - self.last_full_poll >= max(5.0, resync_seconds)
                        and (self.poll_future is None or self.poll_future.done())):
                    self.last_full_poll = now_ts()
                    self.poll_future = self.poll_worker.submit(self.refresh_states)
                self.flush_archive(force=False)
                self.teaching.flush(force=False)
                self.context.home.expire(now_ts())
                self.context.save()
                with self.lock:
                    state_map = dict(self.state_map)
                    changed_entities = set(self.dirty_entities) if event_wakeup else set()
                    if event_wakeup:
                        self.dirty_entities.clear()
                if state_map:
                    gate_open = self.inference_enabled.is_set()
                    startup_grace = time.monotonic() < float(
                        self.startup_inference_not_before or 0.0
                    )
                    if not gate_open or startup_grace:
                        # Keep ingest/archive/context warm during construction, but avoid
                        # the expensive first all-agent prediction pass until HTTP/runtime
                        # startup is fully ready and Ingress has a short grace window.
                        # Genuine realtime changes are retained and consumed afterwards.
                        if changed_entities:
                            with self.lock:
                                self.dirty_entities.update(changed_entities)
                        continue
                    if event_wakeup and changed_entities:
                        with self.lock:
                            self.inference_scheduler["event_passes"] += 1
                        if RUNTIME_DEBUG.enabled:
                            RUNTIME_DEBUG.instant("event_pass", changed_count=len(changed_entities), changed_entities=sorted(changed_entities)[:24])
                        self.process(state_map, changed_entities)
                    elif self.initial_inference_pending:
                        # Exactly one complete inference pass warms all qualified agents
                        # after startup. Later empty wakeups (REST resync, queue/lifecycle
                        # nudges) must never regain the old "process every agent" meaning.
                        self.initial_inference_pending = False
                        with self.lock:
                            self.inference_scheduler["initial_full_passes"] += 1
                        self.process(state_map, set())
                    else:
                        due_targets = self._due_inference_targets()
                        if due_targets:
                            with self.lock:
                                self.inference_scheduler["timer_passes"] += 1
                                self.inference_scheduler["last_timer_targets"] = len(due_targets)
                            # Reuse the normal dependency-aware event scheduler by marking
                            # only due target entities. No HA state is fabricated or fed to
                            # RoomBelief; these IDs are scheduling hints only.
                            self.process(state_map, due_targets)
                        else:
                            with self.lock:
                                self.inference_scheduler["idle_skips"] += 1
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
        self.flush_archive(force=True)

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
        raw_model = STORE.get_model(aid)
        if raw_model is None and self.history_manager is not None:
            seed_getter = getattr(self.history_manager, "training_schema_seed", None)
            if callable(seed_getter):
                raw_model = seed_getter(aid, agent)
        model = MultiHorizonPolicy(agent, state_map, registry, hint_entities, raw_model, self.context_relevance.get(aid), context_engine=self.context)
        self.models[aid] = model
        # The policy schema is part of event routing. Refresh the index before the next
        # event instead of rebuilding dependencies inside the current inference.
        self.agent_index_at = 0.0
        self.agent_index_revision = -1
        return model

    def take_control(self, agent, refresh=False):
        return self.executor.take_control(agent, refresh)

    @staticmethod
    def _default_inference_eligible(agent):
        return bool(
            agent
            and agent.get("enabled")
            and str(agent.get("mode") or "paused") != "paused"
            and str(agent.get("training_state") or "") == "qualified"
        )

    def _refresh_agent_index(self, force=False):
        """Refresh live agent configs and entity->agent routing outside the hot event path."""
        now = time.monotonic()
        store_revision = int(getattr(STORE, "_agent_index_revision", 0))
        with self.lock:
            if (
                not force
                and float(self.agent_index_at or 0.0) > 0.0
                and int(self.agent_index_revision) == store_revision
                and now - float(self.agent_index_at or 0.0) < self.agent_index_ttl_seconds
            ):
                return
            previous_active = set(self.agent_configs)

        if callable(getattr(STORE, "list_agent_configs", None)):
            configs = STORE.list_agent_configs()
        else:
            with self.lock:
                known_ids = set(self.runtime) | set(self.models)
            configs = [
                row for row in (
                    STORE.get_agent_config(aid) for aid in known_ids
                ) if row
            ]
        all_configs = {}
        active = {}
        by_target = {}
        dependency_agents = {}
        for agent in configs:
            aid = str(agent.get("id") or "")
            if not aid:
                continue
            all_configs[aid] = agent
            if not self.inference_eligible(agent):
                continue
            active[aid] = agent
            target = str(agent.get("target_entity") or "")
            if target:
                by_target.setdefault(target, []).append(aid)
            policy = self.models.get(aid)
            for eid in self.event_dependencies(agent, policy):
                dependency_agents.setdefault(str(eid), set()).add(aid)

        removed = previous_active - set(active)
        with self.lock:
            self.all_agent_configs = all_configs
            self.agent_configs = active
            self.active_agents_by_target = by_target
            self.dependency_agents = dependency_agents
            self.agent_index_at = now
            self.agent_index_revision = store_revision
        for aid in removed:
            try:
                self.experiments.cancel(aid, "mode, training or availability changed")
            except Exception:
                pass

    def _active_agents_for_changes(self, changed):
        self._refresh_agent_index()
        with self.lock:
            if not changed:
                return list(self.agent_configs.values())
            ids = set()
            for eid in changed:
                ids.update(self.dependency_agents.get(str(eid), ()))
            return [
                self.agent_configs[aid]
                for aid in ids
                if aid in self.agent_configs
            ]

    def event_dependencies(self, agent, policy=None):
        """Entities whose change can materially alter this agent's next decision.

        Policy schema already carries selected local/upstream predictors. RoomBelief's
        additive home features also depend on occupancy sources in the target's own area.
        Critically, we do *not* add every admitted presence source in the whole house.
        """
        deps = {str(agent.get("target_entity") or "")}
        configured_inputs = agent.get("input_entities") or ()
        deps.update(
            str(eid) for eid in configured_inputs
            if isinstance(eid, str) and eid and eid != "*"
        )
        if policy is not None:
            deps.update(str(eid) for eid in (getattr(policy.schema, "entities", ()) or ()))
        target_entity = agent.get("target_entity")
        area = self.context.area_for(target_entity)
        if area:
            deps.update(
                str(eid)
                for eid in getattr(self.context.home, "area_sources", {}).get(area, ())
            )
            # Explicit boundary mappings are sparse and intentional. They are the only
            # cross-area RoomBelief dependencies added automatically; arbitrary remote
            # PIR/radar sources still require schema selection, avoiding whole-home fanout.
            deps.update(
                str(eid) for eid in self.context.boundary_sources_for(target_entity)
            )
        try:
            deps.update(str(eid) for eid in self.experiments.watches(agent["id"]))
        except Exception:
            pass
        deps.discard("")
        return deps

    def process(self, state_map, changed_entities=None):
        changed = set(changed_entities or ())
        if changed:
            TRAINING_BUDGET.request_interactive_window(
                1.0, reason="realtime_inference"
            )

        agents = self._active_agents_for_changes(changed)
        groups = {}
        for agent in agents:
            groups.setdefault(agent["target_entity"], []).append(agent)

        # One atomically consistent state+revision snapshot per coalesced pass. Every
        # worker shares it; Executor rejects an intent if any dependency changes later.
        with self.lock:
            pass_states = dict(self.state_map) if self.state_map else dict(state_map or {})
            pass_revision = self.state_revision
            revision_snapshot = dict(self.entity_revisions)
            context_revision = self.context.home.revision
        snapshot = (pass_states, pass_revision, revision_snapshot, context_revision)

        for target, target_agents in groups.items():
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
            self.in_flight[target] = self.control_workers.submit(
                self.process_target, target_agents, changed, snapshot
            )
        for target in list(self.in_flight):
            if target not in groups and self.in_flight[target].done():
                del self.in_flight[target]

    def process_target(self, agents, changed_entities=None, snapshot=None):
        if snapshot is None:
            with self.lock:
                states = dict(self.state_map)
                revision = self.state_revision
                revisions = dict(self.entity_revisions)
                context_revision = self.context.home.revision
        else:
            states, revision, revisions, context_revision = snapshot

        self._inference_tls.entity_revisions = revisions
        self._inference_tls.context_revision = context_revision
        self._inference_tls.state_revision = revision
        try:
            for agent in agents:
                if self.stop_event.is_set():
                    return
                try:
                    # Agent snapshots come from the routing cache. Control safety is still
                    # revalidated from durable config inside Executor before any HA call.
                    self.process_agent(agent, states, changed_entities if changed_entities else None)
                except Exception as exc:
                    STORE.event(agent["id"], "error", "agent_error", str(exc), {"trace": traceback.format_exc(limit=4)})
        finally:
            for name in ("entity_revisions", "context_revision", "state_revision"):
                try:
                    delattr(self._inference_tls, name)
                except AttributeError:
                    pass
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
            forecast_ts = now_ts()
            self.context.prepare_home_reliability(self.context.home, area, forecast_ts)
            forecast = self.context.home.forecast(area, forecast_ts)
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
            self.context.prepare_home_reliability(self.context.home, area, timestamp)
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
        snapshot_revisions = getattr(self._inference_tls, "entity_revisions", None)
        snapshot_context_revision = getattr(self._inference_tls, "context_revision", None)
        if snapshot_revisions is None:
            with self.lock:
                context_revision = self.context.home.revision
                target_revision = self.entity_revisions.get(agent['target_entity'], 0)
        else:
            context_revision = (
                self.context.home.revision
                if snapshot_context_revision is None
                else snapshot_context_revision
            )
            target_revision = snapshot_revisions.get(agent['target_entity'], 0)
        min_inference_gap = max(0.05, float(OPTIONS.get("realtime_inference_debounce_ms", 75)) / 1000.0)
        if not changed_entities and now_ts() - rt["last_inference_ts"] < min_inference_gap:
            return
        rt["last_inference_ts"] = now_ts()

        policy = self.policy(agent)
        features, labels, context_meta = policy.features(state_map, self.temporal_history, at_ts=now_ts())
        context_meta.update(policy.selection_meta or {})
        automation_scan_marker = getattr(AUTOMATION_KNOWLEDGE, "last_scan", None)
        if rt.get("_automation_scan_marker") != automation_scan_marker:
            hint_entities, automation_infos = AUTOMATION_KNOWLEDGE.hints_for_target(agent["target_entity"])
            rt["_automation_scan_marker"] = automation_scan_marker
            rt["_automation_hint_entities"] = len(hint_entities)
            rt["automation_priors"] = [
                {"entity_id": x.get("entity_id"), "name": x.get("name"), "enabled": bool(x.get("enabled")),
                 "context_count": len(x.get("context_entities") or [])}
                for x in automation_infos[:8]
            ]
        context_meta["automation_hint_entities"] = int(rt.get("_automation_hint_entities") or 0)
        context_meta["whole_home_entities"] = len(state_map)
        context_meta["trigger_entities"] = sorted(set(changed_entities or ()))[:8]
        context_meta["primary_local_sensors"] = list((policy.selection_meta or {}).get("primary_local_sensors") or [])
        context_meta["primary_local_sensor"] = (policy.selection_meta or {}).get("primary_local_sensor")
        context_meta["primary_occupancy_sensor"] = (policy.selection_meta or {}).get("primary_occupancy_sensor")
        context_meta["causal_presence_scores"] = dict((policy.selection_meta or {}).get("causal_presence_scores") or {})
        context_meta["upstream_sensors"] = list((policy.selection_meta or {}).get("upstream_sensors") or [])
        rt["context_meta"] = context_meta
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
        raw_prediction = float(chosen["value"])
        forecast = context_meta.get('home_forecast', {})
        assist_idx = fast_light_on_assist_action(
            agent, current, raw_prediction, decision_source, arms, forecast
        )
        rt["raw_policy_prediction"] = raw_prediction
        rt["fast_on_assist_active"] = assist_idx is not None
        if assist_idx is not None:
            selected_arm = next(
                (arm for arm in arms if int(arm.get("index", -1)) == int(assist_idx)),
                None,
            )
            if selected_arm is not None:
                chosen = {**chosen, **selected_arm}
                head = policy.heads[int(horizon)]
                structural = head.structural_confidence(arms, int(assist_idx))
                calibration = head.calibration(int(assist_idx))
                confidence = min(float(structural), float(calibration["ceiling"]))
                chosen["structural_confidence"] = structural
                chosen["validation_accuracy"] = calibration["accuracy"]
                chosen["validation_lower_bound"] = calibration["ceiling"]
                chosen["validation_samples"] = calibration["samples"]
                support = float(selected_arm.get("support", support))
                novelty = float(selected_arm.get("novelty", novelty))
                chosen = dict(
                    chosen,
                    value=float(policy.actions[int(assist_idx)]),
                    index=int(assist_idx),
                )

        stabilized_value, off_confirmation = stabilize_fast_light_power_decision(
            agent, rt, current, float(chosen["value"]), decision_source, now_ts()
        )
        if off_confirmation:
            current_idx = min(
                range(len(policy.actions)),
                key=lambda i: abs(float(policy.actions[i]) - float(stabilized_value)),
            )
            selected_arm = next(
                (arm for arm in arms if int(arm.get("index", -1)) == int(current_idx)),
                None,
            )
            if selected_arm is not None:
                chosen = {**chosen, **selected_arm}
            chosen = dict(chosen, value=float(stabilized_value), index=int(current_idx))

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
            context_dependencies=self._intent_dependencies(
                policy, trial, snapshot_revisions
            ))
        rt['last_intent'] = intent.export()
        rt['behavior_summary'] = self.behavior_summary(agent, rt)
        TELEMETRY.observe('inference', (time.perf_counter()-inference_started)*1000)
        received = getattr(self, 'last_event_received', None)
        if changed_entities and received:
            TELEMETRY.observe('event_to_intent', (time.perf_counter()-received)*1000)
        self._schedule_next_inference(agent, rt)
        return self.executor.submit(intent, features, chosen['index'])

    def _intent_dependencies(self, policy, trial, snapshot_revisions=None):
        entities = sorted(
            set(policy.schema.entities)
            | set((trial or {}).get("snapshot") or ())
        )
        if snapshot_revisions is not None:
            return tuple((eid, snapshot_revisions.get(eid, 0)) for eid in entities)
        with self.lock:
            return tuple((eid, self.entity_revisions.get(eid, 0)) for eid in entities)

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
            "raw_policy_prediction": rt.get("raw_policy_prediction"),
            "fast_off_confirmation_active": bool(rt.get("fast_off_confirmation_active")),
            "fast_off_confirmation_elapsed": float(rt.get("fast_off_confirmation_elapsed") or 0.0),
            "fast_off_confirmation_required": float(rt.get("fast_off_confirmation_required") or 0.0),
            "fast_on_assist_active": bool(rt.get("fast_on_assist_active")),
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
