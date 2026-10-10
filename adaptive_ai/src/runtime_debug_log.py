"""Opt-in bounded runtime diagnostics log exposed from the Diagnostics panel."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import threading

from settings import APP_VERSION
from telemetry import HEAVY_JOBS, RUNTIME_DEBUG, TELEMETRY


CONTRACT_VERSION = 4


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _thread_snapshot():
    rows = []
    for thread in threading.enumerate():
        rows.append({
            "name": thread.name,
            "ident": thread.ident,
            "daemon": bool(thread.daemon),
            "alive": bool(thread.is_alive()),
        })
    rows.sort(key=lambda row: row["name"])
    return rows


class RuntimeDebugLogService:
    def __init__(self, core, manager):
        self.core = core
        self.manager = manager

    def _queue_status(self):
        queue = getattr(self.core, "TRAINING_QUEUE", None)
        if queue is None:
            return None
        getter = getattr(queue, "snapshot", None)
        if not callable(getter):
            getter = getattr(queue, "status", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    def _candidate_worker_status(self):
        getter = getattr(self.manager, "_worker_health", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    def status(self):
        telemetry = TELEMETRY.snapshot()
        result = RUNTIME_DEBUG.summary(telemetry)
        result.update({
            "contract_version": CONTRACT_VERSION,
            "heavy_job": HEAVY_JOBS.owner,
            "training_queue": self._queue_status(),
            "candidate_worker": self._candidate_worker_status(),
        })
        return result

    def configure(self, enabled, *, clear=False):
        RUNTIME_DEBUG.set_enabled(bool(enabled), clear=bool(clear))
        RUNTIME_DEBUG.instant(
            "runtime_debug_configuration",
            enabled=bool(enabled),
            clear=bool(clear),
        )
        return self.status()

    def export_payload(self):
        from model_encoding_cache import MODEL_ENCODING_CACHE
        from shadow_model_json import snapshot as shadow_json_snapshot
        engine = getattr(self.core, "ENGINE", None)
        scheduler = None
        engine_error = None
        realtime = None
        context_persistence = None
        deferred_persistence = {}
        store = getattr(self.core, "STORE", None)
        sqlite_snapshot = getattr(store, "sqlite_snapshot", None)
        sqlite_status = sqlite_snapshot() if callable(sqlite_snapshot) else None
        if engine is not None:
            lock = getattr(engine, "lock", None)
            if lock is not None:
                with lock:
                    scheduler = dict(getattr(engine, "inference_scheduler", {}) or {})
                    engine_error = getattr(engine, "error", None)
                    realtime = {
                        "ws_connected": bool(getattr(engine, "ws_connected", False)),
                        "subscription_confirmed": bool(
                            getattr(engine, "ws_subscription_confirmed", False)
                        ),
                        "messages_total": int(getattr(engine, "ws_messages_total", 0) or 0),
                        "state_events_total": int(
                            getattr(engine, "ws_state_events_total", 0) or 0
                        ),
                        "last_message": getattr(engine, "ws_last_message", None),
                        "ws_error": getattr(engine, "ws_error", None),
                        "last_event": getattr(engine, "last_ws_event", None),
                        "state_revision": int(getattr(engine, "state_revision", 0) or 0),
                        "event_timestamp_entries": len(
                            getattr(engine, "entity_event_received_perf", {}) or {}
                        ),
                    }
            else:
                scheduler = dict(getattr(engine, "inference_scheduler", {}) or {})
                engine_error = getattr(engine, "error", None)

            tournament = getattr(engine, 'context_tournament', None)
            if tournament is not None:
                context_persistence = tournament.shadow_persistence_snapshot()
            for name, owner, method in (
                ("provenance", engine, "provenance_deferred_snapshot"),
                ("feature_journal", engine, "feature_observation_deferred_snapshot"),
                ("fast_light", tournament, "fast_light_persistence_snapshot"),
            ):
                getter = getattr(owner, method, None)
                if callable(getter):
                    try:
                        deferred_persistence[name] = getter()
                    except Exception as exc:
                        deferred_persistence[name] = {"error": f"{type(exc).__name__}: {exc}"}

        return {
            "contract_version": CONTRACT_VERSION,
            "generated_at": _now_iso(),
            "app_version": APP_VERSION,
            "runtime_debug": RUNTIME_DEBUG.export(),
            "telemetry": TELEMETRY.snapshot(),
            "heavy_job": HEAVY_JOBS.owner,
            "training_queue": self._queue_status(),
            "candidate_worker": self._candidate_worker_status(),
            "engine": {
                "error": engine_error,
                "realtime": realtime,
                "inference_scheduler": scheduler,
                "context_persistence": context_persistence,
                "deferred_persistence": deferred_persistence,
            },
            "sqlite": sqlite_status,
            "model_encoding_cache": MODEL_ENCODING_CACHE.snapshot(),
            "shadow_json_encoding_cache": shadow_json_snapshot(),
            "threads": _thread_snapshot(),
            "notes": {
                "event_to_decision_recent_p95_window_seconds": 60,
                "event_to_decision_semantics": "ha_websocket_receive_to_actionintent_decision_ready",
                "legacy_event_to_intent_alias": True,
                "decision_to_executor_metric": True,
                "wrapped_inference_metric": True,
                "context_shadow_observation_metric": True,
                "context_observed_pool_metric": True,
                "context_probation_snapshot_metric": True,
                "context_shadow_snapshot_metric": True,
                "context_promotion_snapshot_counts": [
                    "context_promotion_snapshot_changed", "context_promotion_snapshot_skipped",
                ],
                "promotion_snapshot_contract": "new_proof_or_window_change; availability_uses_existing_60s_metrics_interval",
                "inference_sql_session": "per_agent_pipeline_fresh_queries_independent_transactions",
                "nested_shadow_sql_timeout_ms": 250,
                "context_shadow_json_metric": True,
                "context_candidate_validation_metric": True,
                "context_candidate_load_metric": True,
                "context_candidate_prediction_metrics": [
                    "context_candidate_features", "context_candidate_predict",
                ],
                "context_training_phase_metrics": [
                    "binary_classifier_fit", "context_candidate_training",
                    "context_candidate_serialization", "context_candidate_score",
                    "context_pool_selection",
                ],
                "context_candidate_cache_trace": True,
                "context_shadow_persistence": "immutable_ram_snapshot_to_canonical_json_writer",
                "observed_pool_atomic_batch": True,
                "context_rows_persistence": "coalesced_complete_cumulative_snapshots_background_writer",
                "context_persistence_metrics": ["context_rows_persist", "context_shadow_persist"],
                "context_persistence_durability": "prompt_wakeup_on_target_label_periodic_5s_and_explicit_shutdown_barrier; slow_database_may_delay_commit",
                "resubmit_preserves_trigger_entities": True,
                "resubmit_scope": "only_busy_target_no_global_event_replay",
                "rest_resync_clears_superseded_ws_latency_timestamp": True,
                "shadow_validation": "observation_only_optimistic_revision_read_no_engine_writer_lock",
                "trace_storage": "bounded_ram_only",
                "normal_runtime_overhead_when_disabled": "RAM timing counters; detailed spans require debug enabled",
                "inference_stage_trace": [
                    "pre_inference",
                    "policy_context",
                    "feature_construction",
                    "policy_predict",
                    "post_predict",
                    "executor_submit",
                    "executor_result",
                ],
                "correct_history_stage_trace": True,
                "training_queue_recent_transition_limit": 50,
                "api_live_snapshot_timing": True,
                "ha_state_subscription_confirmation_required": True,
                "generation_correct_history_installed": bool(
                    getattr(self.manager, "_correct_generation_history_installed", False)
                ),
            },
        }

    def export_bytes(self):
        payload = self.export_payload()
        return json.dumps(
            payload, ensure_ascii=False, indent=2, sort_keys=True
        ).encode("utf-8")


def register_runtime_debug_routes(registry, core, manager):
    service = RuntimeDebugLogService(core, manager)

    def status(http, _params):
        return http.send_json(200, service.status())

    def configure(http, _params):
        try:
            body = http.read_json()
            body = body if isinstance(body, dict) else {}
            if "enabled" not in body:
                return http.send_json(400, {"error": "enabled is required"})
            result = service.configure(
                bool(body.get("enabled")),
                clear=bool(body.get("clear", False)),
            )
            return http.send_json(200, result)
        except Exception as exc:
            return http.send_json(500, {
                "error": f"Runtime debug configuration failed: {type(exc).__name__}: {exc}"
            })

    def download(http, _params):
        try:
            data = service.export_bytes()
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = f"adaptive-ai-runtime-debug-{stamp}.json"
            http.send_response(200)
            http.send_header("Content-Type", "application/json; charset=utf-8")
            http.send_header("Content-Length", str(len(data)))
            http.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            http.send_header("Cache-Control", "no-store")
            http.end_headers()
            http.wfile.write(data)
        except Exception as exc:
            return http.send_json(500, {
                "error": f"Runtime debug export failed: {type(exc).__name__}: {exc}"
            })

    registry.register(
        "GET",
        "debug.runtime_log.status",
        r"^/api/debug/runtime-log$",
        status,
        require_trusted=True,
        require_runtime=True,
        priority=260,
    )
    registry.register(
        "POST",
        "debug.runtime_log.configure",
        r"^/api/debug/runtime-log$",
        configure,
        require_trusted=True,
        require_runtime=True,
        priority=260,
    )
    registry.register(
        "GET",
        "debug.runtime_log.download",
        r"^/api/debug/runtime-log/download$",
        download,
        require_trusted=True,
        require_runtime=False,
        priority=260,
    )

    manager.runtime_debug_log = service
    manager.runtime_debug_log_contract = (
        "opt_in_bounded_ram_trace_event_to_intent_p95_active_runtime_spans_and_download"
    )
    return service
