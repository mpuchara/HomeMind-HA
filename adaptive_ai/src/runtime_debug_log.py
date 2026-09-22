"""Opt-in bounded runtime diagnostics log exposed from the Diagnostics panel."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import threading

from settings import APP_VERSION
from telemetry import HEAVY_JOBS, RUNTIME_DEBUG, TELEMETRY


CONTRACT_VERSION = 1


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
        status = getattr(queue, "status", None)
        if not callable(status):
            return None
        try:
            return status()
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
        engine = getattr(self.core, "ENGINE", None)
        scheduler = None
        engine_error = None
        realtime = None
        if engine is not None:
            lock = getattr(engine, "lock", None)
            if lock is not None:
                with lock:
                    scheduler = dict(getattr(engine, "inference_scheduler", {}) or {})
                    engine_error = getattr(engine, "error", None)
                    realtime = {
                        "ws_connected": bool(getattr(engine, "ws_connected", False)),
                        "ws_error": getattr(engine, "ws_error", None),
                        "last_event": getattr(engine, "last_ws_event", None),
                    }
            else:
                scheduler = dict(getattr(engine, "inference_scheduler", {}) or {})
                engine_error = getattr(engine, "error", None)

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
            },
            "threads": _thread_snapshot(),
            "notes": {
                "event_to_intent_recent_p95_window_seconds": 60,
                "trace_storage": "bounded_ram_only",
                "normal_runtime_overhead_when_disabled": "boolean instrumentation checks only",
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
