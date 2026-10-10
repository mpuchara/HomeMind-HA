from sqlite_background import background_sqlite
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
from inference_hot_path_metrics import InferenceHotPathMetrics, observe_elapsed
from shared_inference_context import shared_inference_temporal

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
                ) as ws, STORE.connection_session():
                    # Own one SQLite connection per socket, not per sensor event.
                    # Each Store.conn() still reads fresh data and commits/rolls back
                    # independently; no transaction or Store lock spans ws.recv().
                    hello = json.loads(ws.recv())
                    if hello.get("type") == "auth_required":
                        ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
                        auth = json.loads(ws.recv())
                        if auth.get("type") != "auth_ok":
                            raise RuntimeError(auth.get("message") or "WebSocket auth failed")
                    elif hello.get("type") != "auth_ok":
                        raise RuntimeError(f"Unexpected WebSocket greeting: {hello.get('type')}")
                    # Authentication is not enough to call realtime healthy. 0.14.115
                    # captured a session that reported ws_connected=true for minutes while
                    # no state_changed event ever reached Engine. Keep the connection
                    # unhealthy until Home Assistant explicitly confirms subscription #2.
                    with self.engine.lock:
                        self.engine.ws_connected = False
                        self.engine.ws_subscription_confirmed = False
                        self.engine.ws_error = None
                    HA.last_ok = now_ts(); HA.last_error = None
                    # Registry payloads can be large. Keep at most one request per
                    # registry in flight and coalesce bursts into one bounded follow-up.
                    registry_callbacks = {
                        "entity": self.engine.update_entity_registry,
                        "device": self.engine.update_device_registry,
                        "area": self.engine.update_area_registry,
                    }
                    registry_event_names = {5: "entity", 6: "device", 7: "area"}
                    registry_requests = {}
                    registry_inflight = {}
                    registry_dirty = set()
                    registry_last_request = {}
                    registry_min_interval = 2.0
                    next_id = 10

                    def send_registry(name, *, force=False):
                        nonlocal next_id
                        now_mono = time.monotonic()
                        if name in registry_inflight:
                            registry_dirty.add(name)
                            with self.engine.lock:
                                self.engine.registry_refresh_stats["coalesced"] += 1
                            return False
                        last = float(registry_last_request.get(name) or 0.0)
                        if not force and now_mono - last < registry_min_interval:
                            registry_dirty.add(name)
                            with self.engine.lock:
                                self.engine.registry_refresh_stats["coalesced"] += 1
                            return False
                        ident = next_id
                        next_id += 1
                        ws.send(json.dumps({"id": ident, "type": f"config/{name}_registry/list"}))
                        registry_requests[ident] = (name, registry_callbacks[name])
                        registry_inflight[name] = ident
                        registry_last_request[name] = now_mono
                        registry_dirty.discard(name)
                        with self.engine.lock:
                            self.engine.registry_refresh_stats["requests"] += 1
                        return True

                    def flush_registry_due():
                        now_mono = time.monotonic()
                        for name in tuple(registry_dirty):
                            if name in registry_inflight:
                                continue
                            if now_mono - float(registry_last_request.get(name) or 0.0) >= registry_min_interval:
                                send_registry(name)

                    # Home Assistant requires command identifiers on one websocket
                    # connection to increase monotonically. 0.14.116 accidentally sent
                    # registry list requests 10/11/12 before subscriptions 2/5/6/7,
                    # which HA rejected with id_reuse. Establish all subscriptions first,
                    # then start registry reads at id 10 so every subsequent command keeps
                    # increasing on the same socket.
                    ws.send(json.dumps({"id": 2, "type": "subscribe_events", "event_type": "state_changed"}))
                    state_subscription_sent_mono = time.monotonic()
                    for ident, event_type in (
                        (5, "entity_registry_updated"),
                        (6, "device_registry_updated"),
                        (7, "area_registry_updated"),
                    ):
                        ws.send(json.dumps({"id": ident, "type": "subscribe_events", "event_type": event_type}))
                    for name in ("entity", "device", "area"):
                        send_registry(name, force=True)

                    backoff = 2.0
                    while not self.stop_event.is_set():
                        try:
                            raw = ws.recv(timeout=5)
                        except TimeoutError:
                            if (
                                not bool(getattr(self.engine, "ws_subscription_confirmed", False))
                                and time.monotonic() - state_subscription_sent_mono > 10.0
                            ):
                                raise RuntimeError("state_changed subscription confirmation timeout")
                            flush_registry_due()
                            continue
                        msg = json.loads(raw)
                        with self.engine.lock:
                            self.engine.ws_messages_total += 1
                            self.engine.ws_last_message = now_ts()
                        if msg.get("type") == "result" and msg.get("id") == 2:
                            if not msg.get("success"):
                                detail = msg.get("error") or msg.get("message") or msg.get("result")
                                raise RuntimeError(
                                    "state_changed subscription failed"
                                    + (f": {detail}" if detail else "")
                                )
                            with self.engine.lock:
                                self.engine.ws_subscription_confirmed = True
                                self.engine.ws_connected = True
                                self.engine.ws_error = None
                            HA.last_ok = now_ts(); HA.last_error = None
                            if RUNTIME_DEBUG.enabled:
                                RUNTIME_DEBUG.instant(
                                    "ha_state_subscription",
                                    confirmed=True,
                                    messages_total=int(self.engine.ws_messages_total),
                                )
                            continue
                        if msg.get("type") == "result" and msg.get("id") in registry_requests:
                            name, callback = registry_requests.pop(msg["id"])
                            registry_inflight.pop(name, None)
                            if msg.get("success"):
                                payload = msg.get("result") or []
                                self.engine.registry_worker.submit(callback, payload)
                            flush_registry_due()
                            continue
                        if msg.get("type") == "event" and msg.get("id") in registry_event_names:
                            name = registry_event_names[msg["id"]]
                            registry_dirty.add(name)
                            send_registry(name)
                            flush_registry_due()
                            continue
                        if msg.get("type") != "event" or msg.get("id") != 2:
                            flush_registry_due()
                            continue
                        event = msg.get("event") or {}
                        data = event.get("data") or {}
                        with self.engine.lock:
                            self.engine.ws_state_events_total += 1
                            # Receiving a real event is definitive evidence even if a
                            # nonstandard proxy reordered the subscription result.
                            self.engine.ws_subscription_confirmed = True
                            self.engine.ws_connected = True
                            self.engine.ws_error = None
                        self.engine.on_state_changed(data)
                        flush_registry_due()
            except Exception as exc:
                with self.engine.lock:
                    self.engine.ws_connected = False
                    self.engine.ws_subscription_confirmed = False
                    self.engine.ws_error = f"{type(exc).__name__}: {exc}"
                # Realtime loss is the one case where a full REST snapshot should become
                # urgent. Healthy websocket operation uses the much slower safety resync.
                with self.engine.lock:
                    self.engine.last_full_poll = 0.0
                    # A websocket gap can lose the one target transition that defines
                    # the UI/Correct "Current" truth. Keep an explicit recovery latch
                    # until a successful REST /states reconciliation completes. This
                    # must survive a fast websocket reconnect and unrelated realtime
                    # traffic that would otherwise keep deferring the safety poll.
                    self.engine.state_resync_urgent = True
                    self.engine.state_resync_due_since_monotonic = time.monotonic()
                    self.engine.next_resync_retry_monotonic = 0.0
                    stats = self.engine.state_resync_stats
                    stats["urgent_requested"] = int(stats.get("urgent_requested") or 0) + 1
                self.engine.wake_event.set()
                if not self.stop_event.is_set():
                    time.sleep(backoff)
                    backoff = min(30.0, backoff * 1.7)
        with self.engine.lock:
            self.engine.ws_connected = False
            self.engine.ws_subscription_confirmed = False


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
        self.inference_hot_path_metrics = InferenceHotPathMetrics()
        self.inference_scheduler = {
            "event_passes": 0,
            "timer_passes": 0,
            "idle_skips": 0,
            "last_timer_targets": 0,
            "initial_full_passes": 0,
            "startup_warmup_total_targets": 0,
            "startup_warmup_scheduled_targets": 0,
            "startup_warmup_remaining_targets": 0,
            "startup_warmup_started_at": None,
            "startup_warmup_completed_at": None,
        }
        self.initial_inference_pending = True
        # 0.14.131: startup warm-up is no longer allowed to wait for a completely quiet
        # Home Assistant event loop. Busy homes can emit state_changed traffic continuously,
        # which previously postponed the one all-agent cold pass for minutes. Keep one
        # bounded target queue and admit work only when a control worker slot is free.
        self._startup_warmup_targets = None
        self._startup_warmup_scheduled = set()
        self.models = {}
        self.context_relevance = {}
        # Observer-derived cross-area signals are kept separate for diagnostics even
        # though Train/Rebuild may merge them into ordinary feature relevance.
        self.context_suppressor_relevance = {}
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
        self.agent_index_context_signature = None
        self._automation_context_marker = None
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
        self._entity_registry_raw = None
        self._device_registry_raw = None
        self._area_registry_raw = None
        self.registry_refresh_stats = {
            "requests": 0,
            "coalesced": 0,
            "entity_updates": 0,
            "device_updates": 0,
            "area_updates": 0,
            "duplicates": 0,
            "last_duration_ms": 0.0,
            "max_duration_ms": 0.0,
        }
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
        # Diagnostic timestamp carried with each concrete HA entity transition. Unlike
        # last_event_received this cannot be overwritten by an unrelated later event
        # before a coalesced inference pass reaches its worker.
        self.entity_event_received_perf = {}
        # Policy inference is predominantly Python CPU work. More worker threads increase
        # GIL contention and can starve Ingress on Raspberry Pi. Two workers retain limited
        # overlap for SQLite/I/O while bounding CPU contention; single-core hosts stay at 1.
        self.control_worker_count = min(2, max(1, int(os.cpu_count() or 1)))
        self.control_workers = ThreadPoolExecutor(
            max_workers=self.control_worker_count, thread_name_prefix="device-control"
        )
        self.poll_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ha-poll")
        self.registry_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ha-registry")
        self.housekeeping_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="runtime-housekeeping")
        self.housekeeping_future = None
        self.housekeeping_last_submit = 0.0
        self.housekeeping_stats = {
            "runs": 0,
            "deferred_for_realtime": 0,
            "busy_skips": 0,
            "last_duration_ms": 0.0,
            "max_duration_ms": 0.0,
            "archive_rows": 0,
            "decision_rows": 0,
            "context_saves": 0,
        }
        self.in_flight = {}
        self.resubmit_targets = set()
        # While a target worker is busy, preserve the *real* dependency entities that
        # changed. 0.14.117 resubmitted only the target entity as a synthetic marker,
        # which discarded the event batch identity and made event->decision latency use
        # an arbitrarily old target timestamp. The bounded per-target set is RAM-only and
        # is drained when the current worker completes.
        self.pending_target_changes = {}
        self.poll_future = None
        self.last_full_poll = 0.0
        self.next_resync_retry_monotonic = 0.0
        # Correctness recovery is separate from the ordinary 15-minute safety cadence.
        # Once set (currently by a websocket gap), the latch is cleared only by a
        # successful full-state reconciliation.
        self.state_resync_urgent = False
        self.state_resync_due_since_monotonic = 0.0
        self.state_resync_max_defer_seconds = 30.0
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
            "deferred_for_realtime": 0,
            "deferred_for_heavy_job": 0,
            "scheduled": 0,
            "urgent_requested": 0,
            "urgent_scheduled": 0,
            "forced_after_starvation": 0,
        }
        self.last_ws_event = None
        self.last_event_monotonic = 0.0
        self.ws_connected = False
        self.ws_subscription_confirmed = False
        self.ws_messages_total = 0
        self.ws_state_events_total = 0
        self.ws_last_message = None
        self.ws_error = None
        self.temporal_history = TemporalHistory(maxlen=24)
        self.lock = threading.RLock()
        self.teaching = Teaching(STORE)
        # Optional final-entrypoint services. The core Engine owns the decision-composition
        # contract; runtimes that do not install Stage 07 retain legacy behaviour exactly.
        self.preference_model = None
        self.decision_composer = None
        # Optional Stage-12 full-ridge observer. Never consulted for action selection.
        self.policy_backend_shadow = None

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
            ws_subscription_confirmed = self.ws_subscription_confirmed
            ws_messages_total = self.ws_messages_total
            ws_state_events_total = self.ws_state_events_total
            ws_last_message = self.ws_last_message
            ws_error = self.ws_error
            registry_count = len(self.entity_registry)
            last_ws_event = self.last_ws_event
            last_state_sync_ok = self.last_state_sync_ok
            last_state_sync_error = self.last_state_sync_error
            state_resync_stats = dict(self.state_resync_stats)
            registry_refresh_stats = dict(self.registry_refresh_stats)
            housekeeping_stats = dict(self.housekeeping_stats)
            inference_scheduler = dict(self.inference_scheduler)
        # Status is polled before/through startup and must never aggregate the complete
        # feedback/experience tables from cold microSD. Detailed per-agent counters are
        # loaded by /api/agents after readiness; status needs only lightweight configs.
        agents = STORE.list_agent_configs()
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
            "feedback_count": None,
            "historical_experience_count": None,
            "agent_metrics_deferred": True,
            "realtime": {
                "connected": ws_connected,
                "subscription_confirmed": ws_subscription_confirmed,
                "messages_total": ws_messages_total,
                "state_events_total": ws_state_events_total,
                "last_message": ws_last_message,
                "error": ws_error,
                "registry_entries": registry_count,
                "last_event": last_ws_event,
            },
            "state_resync": {
                **state_resync_stats,
                "last_ok": last_state_sync_ok,
                "error": last_state_sync_error,
            },
            "runtime_qos": {
                "registry_refresh": registry_refresh_stats,
                "housekeeping": housekeeping_stats,
                "healthy_resync_seconds": float(OPTIONS.get("realtime_resync_seconds", 900)),
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

    @staticmethod
    def _registry_payload_snapshot(entries):
        return [dict(item) if isinstance(item, dict) else item for item in (entries or [])]

    def _registry_update(self, kind, entries):
        started = time.perf_counter()
        attr = f"_{kind}_registry_raw"
        payload = self._registry_payload_snapshot(entries)
        with self.lock:
            previous = getattr(self, attr, None)
            if previous is not None and previous == payload:
                self.registry_refresh_stats["duplicates"] += 1
                return False
            setattr(self, attr, payload)

            if kind == "entity":
                registry = {
                    e.get("entity_id"): e
                    for e in payload
                    if isinstance(e, dict) and e.get("entity_id")
                }
                self.context.configure(self.state_map, entities=registry)
            elif kind == "device":
                self.context.configure(self.state_map, devices=payload)
            elif kind == "area":
                self.context.configure(self.state_map, areas=payload)
            else:
                raise ValueError(f"unsupported registry kind: {kind}")

            self.entity_registry = self.context.resolved_registry()
            self.registry_refresh_stats[f"{kind}_updates"] += 1
            # Keep trained in-memory policy weights warm; only topology-dependent event
            # routing is invalidated by registry metadata changes.
            self.agent_index_at = 0.0
            self.agent_index_revision = -1

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        with self.lock:
            self.registry_refresh_stats["last_duration_ms"] = elapsed_ms
            self.registry_refresh_stats["max_duration_ms"] = max(
                float(self.registry_refresh_stats.get("max_duration_ms") or 0.0),
                elapsed_ms,
            )
        TELEMETRY.observe(f"registry_{kind}", elapsed_ms)
        return True

    def update_entity_registry(self, entries):
        changed = self._registry_update("entity", entries)
        if changed:
            STORE.event(
                None, "info", "entity_registry",
                f"Loaded {len(self.entity_registry)} Entity Registry entries for cleaner agent discovery",
                None,
            )
        return changed

    def update_device_registry(self, entries):
        return self._registry_update("device", entries)

    def update_area_registry(self, entries):
        return self._registry_update("area", entries)

    def registry_entry(self, entity_id):
        with self.lock:
            return self.entity_registry.get(entity_id)

    def on_state_changed(self, data):
        entity_id = data.get("entity_id")
        new_state = data.get("new_state")
        if not entity_id:
            return
        received_ts = now_ts()
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
            self.last_event_monotonic = time.monotonic()
            self.last_trigger_entity = entity_id
            self.dirty_entities.add(entity_id)
            received_perf = time.perf_counter()
            self.last_event_received = received_perf
            self.entity_event_received_perf[entity_id] = received_perf
            # Historical replay shares this process. Give fresh HA state changes a short
            # strict-priority window so Shadow/Control inference is never queued behind
            # cooperative offline work for seconds.
            TRAINING_BUDGET.request_interactive_window(
                float(OPTIONS.get("training_realtime_event_priority_seconds", 0.30)),
                reason="ha_state_changed",
            )
            event_ts = parse_ts(
                (new_state or {}).get("last_updated") or
                (new_state or {}).get("last_changed")
            ) or received_ts
            self.context.observe(
                entity_id, new_state, received_ts,
                event_ts=event_ts, received_ts=received_ts,
            )
        HA.last_ok = now_ts(); HA.last_error = None
        if new_state is not None:
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
            self.pending_archive.append(
                (entity_id, ts, st.get("state"), compact_attrs, user_id, "live", now)
            )
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

            poll_received_ts = now_ts()
            for eid in changed_eids:
                self.state_revision += 1
                self.entity_revisions[eid] = self.state_revision
                current_state = state_map.get(eid)
                event_ts = parse_ts(
                    (current_state or {}).get("last_updated") or
                    (current_state or {}).get("last_changed")
                ) or poll_received_ts
                self.context.observe(
                    eid, current_state, poll_received_ts, learn=not initial,
                    event_ts=event_ts, received_ts=poll_received_ts,
                )
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
            # Only a completed snapshot may acknowledge a missed-event recovery.
            # Scheduling (or a failed REST request) must never make stale Current look
            # healthy for another 15 minutes.
            self.state_resync_urgent = False
            self.state_resync_due_since_monotonic = 0.0
            self.next_resync_retry_monotonic = 0.0
            self.error = None

        # Initial bootstrap needs every current entity once. Later safety/fallback polls
        # touch only the changed suffix instead of rebuilding temporal/archive state for
        # the whole Home Assistant installation.
        process_eids = set(state_map) if initial else changed_eids
        for eid in process_eids:
            st = state_map.get(eid)
            if st is None:
                continue
            ts = parse_ts(st.get("last_updated") or st.get("last_changed")) or poll_received_ts
            self.temporal_history.add(eid, ts, self._temporal_state(st))
            self._queue_archive_state(st, received_ts=poll_received_ts)
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
        if fast and hold_until <= now:
            forecast = (rt.get('context_meta') or {}).get('home_forecast') or {}
            if (float(forecast.get('arrival_probability') or 0.0) > 0.0
                    or float(forecast.get('departure_probability') or 0.0) > 0.0):
                # A 1/3/5 s forecast can change before any next HA event. Limit this
                # timer to an active movement forecast, not every lamp in the house.
                due = min(due, now + 1.0)

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

    def _realtime_recent(self, seconds=0.75):
        last = float(getattr(self, "last_event_monotonic", 0.0) or 0.0)
        return bool(last and time.monotonic() - last < max(0.0, float(seconds)))

    def _run_housekeeping(self):
        """Persist low-priority runtime state away from the event->intent thread."""
        started = time.perf_counter()
        archive_rows = decision_rows = 0
        context_saved = False
        try:
            with background_sqlite(STORE):
                archive_rows = int(self.flush_archive(force=False) or 0)
                decision_rows = int(self.teaching.flush(force=False) or 0)
                context_saved = bool(self.context.save())
        except Exception as exc:
            STORE.event(
                None, "warning", "runtime_housekeeping_error",
                f"{type(exc).__name__}: {exc}", None,
            )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        with self.lock:
            stats = self.housekeeping_stats
            stats["runs"] += 1
            stats["last_duration_ms"] = elapsed_ms
            stats["max_duration_ms"] = max(float(stats.get("max_duration_ms") or 0.0), elapsed_ms)
            stats["archive_rows"] += archive_rows
            stats["decision_rows"] += decision_rows
            stats["context_saves"] += int(context_saved)
        TELEMETRY.observe("runtime_housekeeping", elapsed_ms)
        return {
            "archive_rows": archive_rows,
            "decision_rows": decision_rows,
            "context_saved": context_saved,
        }

    def _schedule_housekeeping(self, *, force=False):
        now = time.monotonic()
        future = self.housekeeping_future
        if future is not None and not future.done():
            with self.lock:
                self.housekeeping_stats["busy_skips"] += 1
            return False

        since_submit = now - float(self.housekeeping_last_submit or 0.0)
        if not force and self._realtime_recent(0.40) and since_submit < 2.0:
            with self.lock:
                self.housekeeping_stats["deferred_for_realtime"] += 1
            return False
        if not force and since_submit < 0.75:
            return False

        self.housekeeping_last_submit = now
        self.housekeeping_future = self.housekeeping_worker.submit(self._run_housekeeping)
        return True

    def _maybe_schedule_state_resync(self):
        """Schedule /states reconciliation without allowing Current truth to starve.

        Normal healthy-websocket reconciliation still prefers a realtime-quiet window.
        A websocket gap is different: one missed target transition can leave state_map,
        /api/live and Correct stuck on an old Current value. That recovery is urgent and
        bypasses the quiet/heavy gates. Ordinary due work also gets a bounded starvation
        limit so a permanently busy home cannot suppress the safety snapshot forever.
        """
        now_epoch = now_ts()
        now_mono = time.monotonic()
        with self.lock:
            healthy = bool(self.ws_connected)
            urgent = bool(getattr(self, "state_resync_urgent", False))
            due_since = float(
                getattr(self, "state_resync_due_since_monotonic", 0.0) or 0.0
            )
            last_full_poll = float(self.last_full_poll or 0.0)
        resync_seconds = float(
            OPTIONS.get("realtime_resync_seconds", 900)
            if healthy
            else OPTIONS.get("realtime_fallback_poll_seconds", 10)
        )
        periodic_due = (
            now_epoch - last_full_poll >= max(5.0, resync_seconds)
        )
        if not urgent and not periodic_due:
            return False
        if self.poll_future is not None and not self.poll_future.done():
            return False
        if now_mono < float(self.next_resync_retry_monotonic or 0.0):
            return False

        if due_since <= 0.0:
            due_since = now_mono
            with self.lock:
                if not float(
                    getattr(self, "state_resync_due_since_monotonic", 0.0) or 0.0
                ):
                    self.state_resync_due_since_monotonic = due_since
                else:
                    due_since = float(self.state_resync_due_since_monotonic)

        max_defer = max(
            1.0, float(getattr(self, "state_resync_max_defer_seconds", 30.0) or 30.0)
        )
        starved = now_mono - due_since >= max_defer

        if healthy and not urgent and not starved:
            if self._realtime_recent(2.0):
                with self.lock:
                    self.state_resync_stats["deferred_for_realtime"] += 1
                self.next_resync_retry_monotonic = now_mono + 3.0
                return False
            if HEAVY_JOBS.owner is not None:
                with self.lock:
                    self.state_resync_stats["deferred_for_heavy_job"] += 1
                self.next_resync_retry_monotonic = now_mono + 5.0
                return False

        # Do not advance last_full_poll here. It is the timestamp of the last successful
        # reconciliation, not the last attempt. A failed request must remain due.
        self.next_resync_retry_monotonic = now_mono + 2.0
        with self.lock:
            self.state_resync_stats["scheduled"] += 1
            if urgent:
                self.state_resync_stats["urgent_scheduled"] += 1
            if starved and not urgent:
                self.state_resync_stats["forced_after_starvation"] += 1
        self.poll_future = self.poll_worker.submit(self.refresh_states)
        return True

    def _startup_warmup_step(self, state_map):
        """Schedule cold startup inference without waiting for a quiet HA event loop.

        This is scheduling-only: process()/process_target()/Executor keep all existing
        inference and Control semantics. At most the currently free worker slots are
        admitted, so startup cannot build an unbounded executor queue or starve Ingress.
        """
        if not self.initial_inference_pending:
            return 0
        self._refresh_agent_index()
        now = now_ts()
        with self.lock:
            if self._startup_warmup_targets is None:
                self._startup_warmup_targets = tuple(
                    sorted(str(target) for target in self.active_agents_by_target)
                )
                self.inference_scheduler["startup_warmup_total_targets"] = len(
                    self._startup_warmup_targets
                )
                self.inference_scheduler["startup_warmup_started_at"] = now

            targets = tuple(self._startup_warmup_targets or ())
            active_targets = set(self.active_agents_by_target)
            # A target already being processed because of genuine realtime traffic has
            # effectively received its startup warm-up. Do not schedule it a second time.
            for target in targets:
                future = self.in_flight.get(target)
                if future is not None and not future.done():
                    self._startup_warmup_scheduled.add(target)
                    continue
                agent_ids = self.active_agents_by_target.get(target, ())
                if any(
                    float((self.runtime.get(aid) or {}).get("last_inference_ts") or 0.0) > 0.0
                    for aid in agent_ids
                ):
                    self._startup_warmup_scheduled.add(target)

            pending = [
                target for target in targets
                if target in active_targets
                and target not in self._startup_warmup_scheduled
            ]
            outstanding = sum(
                1 for future in self.in_flight.values()
                if future is not None and not future.done()
            )
            capacity = max(0, int(self.control_worker_count) - int(outstanding))
            configured = max(
                1,
                int(OPTIONS.get("startup_warmup_targets_per_tick", 2) or 2),
            )
            selected = pending[: min(capacity, configured)]
            self.inference_scheduler["startup_warmup_scheduled_targets"] = len(
                self._startup_warmup_scheduled
            )
            self.inference_scheduler["startup_warmup_remaining_targets"] = len(pending)

        if selected:
            # Target ids are timer/scheduling hints, not HA event timestamps.
            self.process(state_map, set(selected), event_driven=False)
            with self.lock:
                self._startup_warmup_scheduled.update(selected)
                remaining = [
                    target for target in (self._startup_warmup_targets or ())
                    if target in self.active_agents_by_target
                    and target not in self._startup_warmup_scheduled
                ]
                self.inference_scheduler["startup_warmup_scheduled_targets"] = len(
                    self._startup_warmup_scheduled
                )
                self.inference_scheduler["startup_warmup_remaining_targets"] = len(remaining)
                if not remaining:
                    self.initial_inference_pending = False
                    self.inference_scheduler["initial_full_passes"] += 1
                    self.inference_scheduler["startup_warmup_completed_at"] = now_ts()
            return len(selected)

        with self.lock:
            remaining = [
                target for target in (self._startup_warmup_targets or ())
                if target in self.active_agents_by_target
                and target not in self._startup_warmup_scheduled
            ]
            self.inference_scheduler["startup_warmup_remaining_targets"] = len(remaining)
            if not remaining:
                self.initial_inference_pending = False
                self.inference_scheduler["initial_full_passes"] += 1
                self.inference_scheduler["startup_warmup_completed_at"] = now_ts()
        return 0

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
                # Expiration is in-memory and affects current inference semantics. Durable
                # archive/decision/context writes are scheduled after inference below.
                self.context.home.expire(now_ts())
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
                    else:
                        handled_realtime = False
                        if event_wakeup and changed_entities:
                            handled_realtime = True
                            with self.lock:
                                self.inference_scheduler["event_passes"] += 1
                            if RUNTIME_DEBUG.enabled:
                                RUNTIME_DEBUG.instant("event_pass", changed_count=len(changed_entities), changed_entities=sorted(changed_entities)[:24])
                            self.process(state_map, changed_entities)

                        # Warm-up is independent from HA quietness. Genuine event work is
                        # always admitted first; cold targets then fill only idle worker
                        # slots. This avoids the old busy-home starvation mode while
                        # preserving realtime priority and the bounded worker pool.
                        warmup_scheduled = self._startup_warmup_step(state_map)
                        if handled_realtime or self.initial_inference_pending or warmup_scheduled:
                            due_targets = set()
                        else:
                            due_targets = self._due_inference_targets()
                        if due_targets:
                            with self.lock:
                                self.inference_scheduler["timer_passes"] += 1
                                self.inference_scheduler["last_timer_targets"] = len(due_targets)
                            # Timer target IDs are scheduling hints, not HA events. Keep
                            # them out of event->decision telemetry so an old target event
                            # timestamp can never masquerade as current reaction latency.
                            self.process(state_map, due_targets, event_driven=False)
                        else:
                            with self.lock:
                                self.inference_scheduler["idle_skips"] += 1

                self._schedule_housekeeping()
                self._maybe_schedule_state_resync()
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

    def refresh_automation_context(self):
        """Resolve structural radar areas before live routing or worker snapshots."""
        marker = (getattr(AUTOMATION_KNOWLEDGE, 'last_scan', None), self.context.registry_revision)
        with self.lock, self.context.lock:
            if marker == self._automation_context_marker:
                return False
            with AUTOMATION_KNOWLEDGE.lock:
                hints = {target: [dict(info) for info in infos]
                         for target, infos in AUTOMATION_KNOWLEDGE.by_target.items()}
            old_mapping = dict(self.context.mapping)
            self.context.configure(self.state_map, automation_hints=hints)
            self.entity_registry = self.context.resolved_registry()
            changed = {eid for eid in set(old_mapping) | set(self.context.mapping)
                       if old_mapping.get(eid) != self.context.mapping.get(eid)}
            if changed:
                # Reconstruct current evidence, not a fabricated arrival at remap time.
                stamp = now_ts()
                with self.context.home.lock:
                    for eid in changed:
                        previous = self.context.home.sources.pop(eid, None)
                        if previous:
                            self.context.home.area_sources.get(previous['area'], set()).discard(eid)
                        st = self.state_map.get(eid)
                        event_ts = parse_ts((st or {}).get('last_updated') or (st or {}).get('last_changed')) or stamp
                        self.context.observe(eid, st, stamp, learn=False, event_ts=event_ts, received_ts=stamp)
                    self.context.home.reset_movement_state()
            self._automation_context_marker = (getattr(AUTOMATION_KNOWLEDGE, 'last_scan', None), self.context.registry_revision)
            return bool(changed)

    def _refresh_agent_index(self, force=False):
        """Refresh live agent configs and entity->agent routing outside the hot event path."""
        now = time.monotonic()
        self.refresh_automation_context()
        context_signature = (self.context.registry_revision, self.context.home.routing_revision)
        store_revision = int(getattr(STORE, "_agent_index_revision", 0))
        with self.lock:
            if (
                not force
                and float(self.agent_index_at or 0.0) > 0.0
                and int(self.agent_index_revision) == store_revision
                and self.agent_index_context_signature == context_signature
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
            self.agent_index_context_signature = context_signature
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
        additive home features depend on local occupancy and learned trajectory routes,
        including competing branches. Unrelated rooms do not cause whole-house fanout.
        """
        deps = {str(agent.get("target_entity") or "")}
        configured_inputs = agent.get("input_entities") or ()
        from additional_signal import entities as additional_entities
        deps.update(additional_entities(agent))
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
            # Explicit boundaries and learned paths are sparse. Their events must reach
            # the policy even when automation-first selects only the local raw radar.
            deps.update(
                str(eid) for eid in self.context.boundary_sources_for(target_entity)
            )
            deps.update(self.context.trajectory_sources_for(target_entity))
        try:
            deps.update(str(eid) for eid in self.experiments.watches(agent["id"]))
        except Exception:
            pass
        deps.discard("")
        return deps

    def process(self, state_map, changed_entities=None, *, event_driven=True):
        changed = set(changed_entities or ())
        if changed and event_driven:
            TRAINING_BUDGET.request_interactive_window(
                float(OPTIONS.get("training_realtime_inference_priority_seconds", 0.40)),
                reason="realtime_inference",
            )

        agents = self._active_agents_for_changes(changed)
        groups = {}
        for agent in agents:
            groups.setdefault(agent["target_entity"], []).append(agent)

        # One atomically consistent state+revision snapshot per coalesced pass. Every
        # worker shares it; Executor rejects a Control intent if any dependency changes
        # later. Event timestamps are included only for genuine HA-event passes.
        with self.lock:
            pass_states = dict(self.state_map) if self.state_map else dict(state_map or {})
            pass_revision = self.state_revision
            revision_snapshot = dict(self.entity_revisions)
            context_revision = self.context.home.revision
            dependency_snapshot = {
                str(eid): set(agent_ids)
                for eid, agent_ids in self.dependency_agents.items()
            }
            event_received_all = ({
                eid: self.entity_event_received_perf.get(eid)
                for eid in changed
                if self.entity_event_received_perf.get(eid) is not None
            } if event_driven else {})
        snapshot = (pass_states, pass_revision, revision_snapshot, context_revision)

        for target, target_agents in groups.items():
            # Keep only the concrete entities that can affect this target. A burst may
            # contain unrelated HA changes; those must not inflate this target's latency.
            target_agent_ids = {str(agent.get("id") or "") for agent in target_agents}
            target_changed = {
                str(eid) for eid in changed
                if target_agent_ids.intersection(dependency_snapshot.get(str(eid), ()))
            }
            # Synthetic timer scheduling uses the target id solely to select the group.
            # Preserve that hint for process_agent semantics but carry no event timestamp.
            if not target_changed and changed and not event_driven:
                target_changed = set(changed)
            target_event_received = {
                eid: event_received_all[eid]
                for eid in target_changed
                if eid in event_received_all
            }

            active = self.in_flight.get(target)
            if active is not None and not active.done():
                if event_driven and target_changed:
                    install_callback = False
                    with self.lock:
                        pending = self.pending_target_changes.setdefault(target, set())
                        pending.update(target_changed)
                        if target not in self.resubmit_targets:
                            self.resubmit_targets.add(target)
                            install_callback = True
                    if install_callback:
                        def retry_completed(_future, entity=target):
                            with self.lock:
                                pending_changes = set(
                                    self.pending_target_changes.pop(entity, set())
                                )
                                self.resubmit_targets.discard(entity)
                                self.dirty_entities.update(pending_changes)
                            if pending_changes:
                                self.wake_event.set()
                        active.add_done_callback(retry_completed)
                continue
            self.in_flight[target] = self.control_workers.submit(
                self.process_target,
                target_agents,
                target_changed,
                snapshot,
                target_event_received,
            )
        for target in list(self.in_flight):
            if target not in groups and self.in_flight[target].done():
                del self.in_flight[target]

    def process_target(self, agents, changed_entities=None, snapshot=None, event_received_perf=None):
        received_values = [
            float(value) for value in (event_received_perf or {}).values()
            if value is not None
        ]
        oldest_event_age_ms = (
            max(0.0, (time.perf_counter() - min(received_values)) * 1000.0)
            if received_values else None
        )
        target_trace = (RUNTIME_DEBUG.begin(
            "inference_target",
            target_entity=str((agents[0] if agents else {}).get("target_entity") or "unknown"),
            agent_count=len(agents or ()),
            changed_count=len(changed_entities or ()),
            event_timestamp_count=len(received_values),
            oldest_event_age_ms=None if oldest_event_age_ms is None else round(oldest_event_age_ms, 3),
        ) if RUNTIME_DEBUG.enabled else None)
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
        self._inference_tls.event_received_perf = dict(event_received_perf or {})
        try:
            for agent in agents:
                if self.stop_event.is_set():
                    return
                agent_trace = (RUNTIME_DEBUG.begin("inference_agent", agent_id=str(agent.get("id") or ""), target_entity=str(agent.get("target_entity") or ""), mode=str(agent.get("mode") or "")) if RUNTIME_DEBUG.enabled else None)
                try:
                    # Agent snapshots come from the routing cache. Control safety is still
                    # revalidated from durable config inside Executor before any HA call.
                    wrapped_started = time.perf_counter()
                    self.process_agent(agent, states, changed_entities if changed_entities else None)
                    TELEMETRY.observe("wrapped_inference", (time.perf_counter() - wrapped_started) * 1000)
                    RUNTIME_DEBUG.end(agent_trace, status="ok")
                except Exception as exc:
                    RUNTIME_DEBUG.end(agent_trace, status="error", error=f"{type(exc).__name__}: {exc}")
                    STORE.event(agent["id"], "error", "agent_error", str(exc), {"trace": traceback.format_exc(limit=4)})
        finally:
            for name in ("entity_revisions", "context_revision", "state_revision", "event_received_perf"):
                try:
                    delattr(self._inference_tls, name)
                except AttributeError:
                    pass
            RUNTIME_DEBUG.end(target_trace, status="ok")
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
        shadow = getattr(self, "policy_backend_shadow", None)
        if shadow is not None and bool(getattr(shadow, "enabled", False)):
            try:
                shadow.observe_reward(agent, pending, reward, reason)
            except Exception as exc:
                STORE.event(
                    agent["id"], "warning", "policy_backend_shadow_reward_gap",
                    "Shadow backend could not observe an executed reward",
                    {"error": f"{type(exc).__name__}: {exc}"},
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
        pre_inference_started = time.perf_counter()

        def trace_stage(stage, started, **fields):
            if not RUNTIME_DEBUG.enabled:
                return
            RUNTIME_DEBUG.instant(
                "inference_stage",
                agent_id=str(aid),
                target_entity=str(agent.get("target_entity") or ""),
                stage=str(stage),
                duration_ms=round(max(0.0, time.perf_counter() - float(started)) * 1000.0, 3),
                **fields,
            )

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
            trace_stage("pre_inference", pre_inference_started, outcome="target_unavailable")
            return
        if agent["target_property"] == "position" and target_state.get("state") in ("opening", "closing"):
            rt["decision_state"] = "waiting"
            rt["decision_reason"] = "Cover is moving; wait for its final position"
            trace_stage("pre_inference", pre_inference_started, outcome="cover_moving")
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
            trace_stage("pre_inference", pre_inference_started, outcome="paused")
            return
        trace_stage("pre_inference", pre_inference_started, outcome="continue")
        inference_started = time.perf_counter()
        inference_started_ns = time.perf_counter_ns()
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
        inference_ts = now_ts()
        if not changed_entities and inference_ts - rt["last_inference_ts"] < min_inference_gap:
            trace_stage("inference_debounce_skip", inference_started, outcome="skip")
            return
        rt["last_inference_ts"] = inference_ts

        policy_context_started = time.perf_counter()
        policy = self.policy(agent)
        shared_temporal = shared_inference_temporal(
            self.temporal_history,
            inference_ts,
            home_provider=(
                getattr(self.temporal_history, "home_context", None)
                or getattr(policy, "context_engine", None)
            ),
        )
        trace_stage("policy_context", policy_context_started)
        feature_started = time.perf_counter()
        stage_started_ns = time.perf_counter_ns()
        features, labels, context_meta = policy.features(
            state_map, shared_temporal, at_ts=inference_ts
        )
        observe_elapsed(self, "ridge_feature_construction", stage_started_ns)
        trace_stage("feature_construction", feature_started, feature_count=len(features))
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
        predict_started = time.perf_counter()
        stage_started_ns = time.perf_counter_ns()
        chosen, confidence, arms, horizon, support, novelty = policy.predict(features)
        observe_elapsed(self, "ridge_predict", stage_started_ns)
        trace_stage("policy_predict", predict_started, arm_count=len(arms))
        post_predict_started = time.perf_counter()

        shadow = getattr(self, "policy_backend_shadow", None)
        if shadow is not None and bool(getattr(shadow, "enabled", False)):
            try:
                rt["policy_backend_shadow"] = shadow.observe_decision(
                    agent, policy, features, labels,
                    allowed_indices=list(range(len(policy.actions))),
                    timestamp=inference_ts,
                )
            except Exception as exc:
                rt["policy_backend_shadow"] = {"error": f"{type(exc).__name__}: {exc}"}
                STORE.event(
                    agent["id"], "warning", "policy_backend_shadow_decision_gap",
                    "Shadow backend could not observe the live Ridge decision",
                    {"error": f"{type(exc).__name__}: {exc}"},
                )

        # Ridge is always evaluated first and remains the safety authority. A selected
        # Tiny MLP may replace only the proposed action index; confidence, support,
        # novelty and horizon are recomputed/retained from Ridge for that exact action.
        ridge_chosen = dict(chosen)
        ridge_confidence = float(confidence)
        ridge_support = float(support)
        ridge_novelty = float(novelty)
        ridge_horizon = int(horizon)
        rt["ridge_policy_prediction"] = float(ridge_chosen["value"])
        hybrid_dependencies = ()
        base_decision_source = "historical_policy_bootstrap"
        hybrid = getattr(self, "hybrid_policy", None)
        if hybrid is not None:
            hybrid_started_ns = time.perf_counter_ns()
            try:
                hybrid_result = hybrid.evaluate(
                    agent,
                    policy,
                    state_map,
                    shared_temporal,
                    timestamp=inference_ts,
                    ridge_chosen=ridge_chosen,
                    ridge_confidence=ridge_confidence,
                    ridge_arms=arms,
                    ridge_horizon=ridge_horizon,
                    ridge_support=ridge_support,
                    ridge_novelty=ridge_novelty,
                    home_provider=shared_temporal.home_context,
                )
            except Exception as exc:
                hybrid_result = {
                    "evaluated": True,
                    "applied": False,
                    "backend": "tiny_mlp_action+ridge_guard",
                    "reason": "hybrid_service_error",
                    "error": f"{type(exc).__name__}: {exc}"[:400],
                }
            observe_elapsed(self, "hybrid_policy_total", hybrid_started_ns)
            hybrid_public = {
                key: value for key, value in dict(hybrid_result or {}).items()
                if key != "chosen"
            }
            hybrid_public["evaluated_ts"] = float(rt["last_inference_ts"])
            rt["hybrid_policy"] = hybrid_public
            if bool((hybrid_result or {}).get("applied")):
                chosen = dict(hybrid_result["chosen"])
                confidence = float(hybrid_result["confidence"])
                support = float(hybrid_result["support"])
                novelty = float(hybrid_result["novelty"])
                horizon = int(hybrid_result["horizon"])
                hybrid_dependencies = tuple(
                    str(x) for x in (hybrid_result.get("dependencies") or ())
                )
                base_decision_source = str(
                    hybrid_result.get("decision_source")
                    or "hybrid_tiny_mlp_ridge_guard"
                )
        else:
            rt["hybrid_policy"] = {
                "evaluated": False,
                "applied": False,
                "reason": "hybrid_service_unavailable",
            }

        rt["shared_inference_context"] = shared_temporal.diagnostics()

        composer = self.decision_composer
        preference = None
        instruction = None
        decision_source = base_decision_source
        if composer is not None:
            composer_started_ns = time.perf_counter_ns()
            composed = composer.compose(
                agent=agent, policy=policy, state_map=state_map, temporal=self.temporal_history,
                timestamp=now_ts(), features=features, labels=labels, chosen=chosen,
                confidence=confidence, arms=arms, horizon=horizon, support=support,
                novelty=novelty, runtime=rt, registry=self.context.resolved_registry,
            )
            observe_elapsed(self, "decision_composer", composer_started_ns)
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
            if decision_source == "historical_policy_bootstrap":
                decision_source = base_decision_source
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

        # Explicit instruction/preference/experiment has higher authority than the base
        # hybrid selector. Once such an override wins, MLP-only feature freshness must
        # not block the resulting intent.
        if decision_source != "hybrid_tiny_mlp_ridge_guard":
            hybrid_dependencies = ()

        raw_prediction = float(chosen["value"])
        forecast = context_meta.get('home_forecast', {})
        from lighting_conditions import lighting_context
        lighting = lighting_context(policy.selection_meta, state_map, now_ts(), self.temporal_history)
        rt["lighting_context"] = lighting
        context_meta["lighting_context"] = lighting
        assist_idx = fast_light_on_assist_action(
            agent, current, raw_prediction, decision_source, arms, forecast, lighting=lighting
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

        from additional_signal import evaluate as evaluate_signal, apply as apply_signal, stabilize as stabilize_signal
        signal_evidence = stabilize_signal(evaluate_signal(agent, state_map, now_ts(), self.temporal_history),
                                           rt.get("additional_signal"))
        signal_current = rt.get("additional_signal_virtual_power", current) if agent.get("mode") == "shadow" else current
        if teaching or (preference or {}).get("applied"):
            signal_current = current
        signal_value, signal_applied = apply_signal(
            agent, signal_current, chosen["value"], signal_evidence,
            "user_instruction" if teaching or (preference or {}).get("applied") or trial else decision_source)
        rt["additional_signal"] = {**signal_evidence, "applied": signal_applied}
        context_meta["additional_signal"] = rt["additional_signal"]
        if signal_applied:
            signal_idx = min(range(len(policy.actions)), key=lambda i: abs(policy.actions[i]-signal_value))
            selected_arm = next((arm for arm in arms if int(arm["index"]) == signal_idx), None)
            if selected_arm:
                chosen = {**chosen, **selected_arm, "value": signal_value, "index": signal_idx}
                confidence = min(confidence, policy.heads[int(horizon)].structural_confidence(arms, signal_idx))
                support, novelty = selected_arm.get("support", support), selected_arm.get("novelty", novelty)
                decision_source = "additional_signal_preference"

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
        rt["additional_signal_virtual_power"] = float(chosen["value"])
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
                f"Hybrid Tiny MLP action {chosen['value']} accepted by Ridge guard; confidence {confidence:.0%}, support {support:.0%}, novelty {novelty:.0%}" if decision_source == "hybrid_tiny_mlp_ridge_guard" else
                f"Historical policy bootstrap desires {chosen['value']}; confidence {confidence:.0%}, support {support:.0%}, novelty {novelty:.0%}"),
            experiment_token=trial['token'] if trial else '',
            contributors=tuple((x['feature'], x['contribution']) for x in rt['top_context']),
            context_dependencies=self._intent_dependencies(
                policy, trial, snapshot_revisions,
                extra_entities=hybrid_dependencies,
            ))
        rt['last_intent'] = intent.export()
        rt['behavior_summary'] = self.behavior_summary(agent, rt)
        trace_stage("post_predict", post_predict_started, decision_source=str(decision_source))
        observe_elapsed(self, "live_inference_total", inference_started_ns)
        TELEMETRY.observe('inference', (time.perf_counter()-inference_started)*1000)

        pass_has_event_timestamps = hasattr(self._inference_tls, 'event_received_perf')
        received_map = getattr(self._inference_tls, 'event_received_perf', {}) or {}
        received_values = [
            float(received_map[eid])
            for eid in set(changed_entities or ())
            if received_map.get(eid) is not None
        ]
        # Normal scheduler passes always bind event_received_perf, including an empty
        # mapping for timer hints. The legacy global timestamp fallback is therefore
        # restricted to direct process_agent() callers that have no pass binding at all.
        received = min(received_values) if received_values else (
            getattr(self, 'last_event_received', None)
            if changed_entities and not pass_has_event_timestamps else None
        )
        if changed_entities and received is not None:
            event_to_decision_ms = max(0.0, (time.perf_counter()-received)*1000)
            # New semantic name: HA websocket receipt -> ActionIntent decision ready.
            # Keep the legacy key for one compatibility window; both carry the same sample.
            TELEMETRY.observe('event_to_decision', event_to_decision_ms)
            TELEMETRY.observe('event_to_intent', event_to_decision_ms)
            if RUNTIME_DEBUG.enabled:
                RUNTIME_DEBUG.instant(
                    "event_to_intent_pass",
                    agent_id=str(aid),
                    target_entity=str(agent.get("target_entity") or ""),
                    changed_count=len(changed_entities or ()),
                    timestamp_count=len(received_values),
                    metric_semantics="ha_ws_receive_to_decision_ready",
                    sample_ms=round(event_to_decision_ms, 3),
                )
        self._schedule_next_inference(agent, rt)

        # The decision is complete before Executor validation/dispatch. Measure that
        # boundary separately: Shadow should be a RAM-only validation, while Control may
        # legitimately include durable guards and an HA service call.
        trace_stage("executor_submit", time.perf_counter(), boundary="decision_ready")
        executor_started = time.perf_counter()
        result = self.executor.submit(
            intent, features, chosen['index'], agent_snapshot=agent
        )
        decision_to_executor_ms = max(
            0.0, (time.perf_counter() - executor_started) * 1000.0
        )
        TELEMETRY.observe('decision_to_executor', decision_to_executor_ms)
        trace_stage(
            "executor_result",
            executor_started,
            status=str((result or {}).get("status") or "unknown"),
        )
        return result

    def _intent_dependencies(
        self, policy, trial, snapshot_revisions=None, extra_entities=None
    ):
        entities = sorted(
            set(policy.schema.entities)
            | set((trial or {}).get("snapshot") or ())
            | set(extra_entities or ())
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
            registry = dict(self.entity_registry)
            target_state = self.state_map.get(agent["target_entity"])
        if policy is not None and getattr(policy, "observation_mask", None) is None:
            try:
                hint_entities, _ = AUTOMATION_KNOWLEDGE.hints_for_target(
                    agent["target_entity"]
                )
                policy.materialize_observation_mask(
                    states,
                    registry,
                    hint_entities,
                    relevance_scores=self.context_relevance.get(agent["id"]),
                )
            except Exception as exc:
                policy.observation_diagnostics = {
                    **dict(getattr(policy, "observation_diagnostics", {}) or {}),
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "hot_path_active": False,
                }
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
            "ridge_policy_prediction": rt.get("ridge_policy_prediction"),
            "hybrid_policy": dict(rt.get("hybrid_policy") or {}),
            "fast_off_confirmation_active": bool(rt.get("fast_off_confirmation_active")),
            "fast_off_confirmation_elapsed": float(rt.get("fast_off_confirmation_elapsed") or 0.0),
            "fast_off_confirmation_required": float(rt.get("fast_off_confirmation_required") or 0.0),
            "fast_on_assist_active": bool(rt.get("fast_on_assist_active")),
            "teaching_id": rt.get("teaching_id"),
            "decision_source": rt.get("decision_source") or "historical_policy_bootstrap",
            "preference_model": rt.get("preference_model"),
            "policy_backend_shadow": rt.get("policy_backend_shadow"),
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
            "additional_signal": rt.get("additional_signal"),
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
            "observation_space": dict(getattr(policy, "observation_diagnostics", {}) or {}) if policy else {},
            "prediction_horizons": list(policy.horizons) if policy else parse_horizons(agent),
            "training_state": agent.get("training_state") or "training",
            "benchmark_score": agent.get("benchmark_score"),
            "benchmark_samples": int(agent.get("benchmark_samples") or 0),
            "benchmark_source": agent.get("benchmark_source"),
            "benchmark_detail": agent.get("benchmark_detail") or {},
            "model": "Tiny MLP action selector (when tournament-selected) + Ridge safety/confidence fallback → scoped instruction/preference → ActionIntent → Executor",
            "intent": rt.get('intent'), "last_intent": rt.get('last_intent'),
            "home_forecast": self.context.forecast(agent['target_entity'], now_ts()),
            "trajectory_event_sources": list(self.context.trajectory_sources_for(agent['target_entity'])),
            "behavior_summary": rt.get('behavior_summary'),
            "reward_components": rt.get('last_reward_components', {}),
            "policy_diagnostics": policy.diagnostics() if policy else {},
        }
