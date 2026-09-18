"""Release guard for responsive startup and deterministic initial Train admission.

This module is installed by the final shipped entrypoint after all runtime wrappers are
composed but before ``core.main()`` binds/serves the application. It deliberately keeps
the fixes narrow:

* status stays lightweight until the runtime reports ready, so a half-built Engine can
  never make the first UI status poll block;
* the FIFO training queue is created before HistoryManager starts background discovery;
* an explicit normal Train gets an immediate admission attempt when the heavy slot is
  idle, instead of depending solely on thread scheduling;
* the queue worker survives an unexpected iteration error and reports it to events.

``core.runtime_available()`` is intentionally NOT changed here. Internal extension
installers use that predicate while the Engine exists but before startup is marked ready.
Changing its semantics would skip safety/feedback adapters during normal startup.

No persisted model, feedback, generation, label, setting or rollback state is changed.
"""
from __future__ import annotations

import time


COLD_START_STATES = {"waiting", "paused", "needs_retrain"}


def _install_initial_training_bridge(history, queue, store):
    """Queue the first real model for auto-discovered agents, never a Candidate.

    Candidate generations are meaningful only after a persisted Live/base policy exists.
    Discovery may therefore create many WAITING agents, but their first historical build
    is admitted through the existing single-heavy-job FIFO.  This keeps Raspberry Pi
    resource bounds intact while removing the cold-start dead end where only Candidate
    cards could appear.

    The bridge also repairs existing auto-created WAITING/PAUSED agents from previous
    releases. TrainingQueue deduplication makes repeated discovery/rescan calls harmless.
    """
    if getattr(history, "_initial_training_bridge_installed", False):
        return history

    original_discover = history.auto_discover_agents

    def discover_and_queue_initial(*args, **kwargs):
        created = original_discover(*args, **kwargs)
        enqueued = []
        for agent in store.list_agent_configs():
            aid = str(agent.get("id") or "")
            if (
                not aid
                or not agent.get("enabled")
                or not agent.get("auto_created")
                or str(agent.get("training_state") or "") not in COLD_START_STATES
                or store.get_model(aid) is not None
            ):
                continue
            try:
                status = queue.enqueue(aid, rebuild=True, reason="initial_training")
                if isinstance(status, dict):
                    enqueued.append({
                        "agent_id": aid,
                        "state": status.get("state"),
                        "position": status.get("position"),
                    })
            except Exception as exc:
                store.event(
                    aid, "error", "initial_training_queue_failed",
                    f"Could not queue initial agent training: {type(exc).__name__}: {exc}",
                    {"error": str(exc)},
                )
        history.initial_training_enqueued = enqueued
        if enqueued:
            store.event(
                None, "info", "initial_training_queued",
                f"Queued initial historical training for {len(enqueued)} auto-discovered agent(s)",
                {"agents": enqueued, "resource_policy": "single_heavy_job_fifo"},
            )
        return created

    history.auto_discover_agents = discover_and_queue_initial
    history._initial_training_bridge_installed = True
    history.initial_training_enqueued = []
    return history


def install(runtime):
    core = runtime.core
    if getattr(core, "_startup_train_guard_installed", False):
        return core

    import queue_main as queued_runtime
    from telemetry import HEAVY_JOBS
    from training_queue import TrainingQueue

    # ---- HTTP/status readiness boundary -------------------------------------
    previous_status_payload = core.Handler.status_payload

    def status_payload(self):
        startup = core.startup_snapshot()
        if not startup.get("ready"):
            queue = getattr(core, "TRAINING_QUEUE", None)
            return {
                "version": core.APP_VERSION,
                "ha_connected": False,
                "ha_error": None,
                "engine_error": startup.get("error"),
                "state_count": 0,
                "agent_count": 0,
                "average_confidence": 0.0,
                "historical_experience_count": 0,
                "realtime": {"connected": False, "error": None, "registry_entries": 0, "last_event": None},
                "history": {
                    "phase": "starting", "progress": 0.0,
                    "message": startup.get("message"),
                    "archive": {"n": 0, "days": 0, "entities": 0},
                },
                "home_intelligence": {}, "home_bootstrap": {}, "telemetry": {},
                "heavy_job": HEAVY_JOBS.owner,
                "training_queue": queue.snapshot() if queue is not None else {
                    "active": None, "queued": [], "queued_count": 0,
                    "heavy_job": HEAVY_JOBS.owner,
                },
                "options": core.OPTIONS,
                "startup": startup,
            }
        return previous_status_payload(self)

    core.Handler.status_payload = status_payload

    # ---- Training queue reliability ----------------------------------------
    original_enqueue = TrainingQueue.enqueue
    original_thread_start = TrainingQueue.start

    def idempotent_start(self):
        # queue_main's legacy post-initialize hook may call start() again after the
        # pre-discovery queue below has already been started. A Thread cannot normally
        # be started twice; treating the second call as a no-op keeps one worker only.
        if self.ident is not None or self.is_alive():
            return None
        return original_thread_start(self)

    def enqueue(self, agent_id, rebuild=False, reason="training"):
        result = original_enqueue(self, agent_id, rebuild=rebuild, reason=reason)
        # Teach-RL owns a context-selection preflight and remains worker-driven. Plain
        # Train/Rebuild/Resume can safely claim an idle slot immediately because the
        # HistoryManager itself performs the expensive replay in its own worker thread.
        if str(reason) != "teach_rl" and isinstance(result, dict) and result.get("state") == "queued":
            try:
                if HEAVY_JOBS.owner is None and not self._history_active_ids():
                    self._try_start_head()
                    latest = self.status_for(agent_id)
                    if latest is not None:
                        result = latest
            except Exception as exc:
                try:
                    self.store.event(
                        agent_id, "error", "training_queue_immediate_start_failed",
                        f"Immediate Train admission failed: {type(exc).__name__}: {exc}",
                        {"error": str(exc)},
                    )
                except Exception:
                    pass
        return result

    def resilient_run(self):
        while not self.stop_event.is_set():
            progressed = False
            try:
                self._finish_active_if_done()
                progressed = self._try_start_head()
            except Exception as exc:
                try:
                    self.store.event(
                        None, "error", "training_queue_worker_error",
                        f"Training queue recovered from {type(exc).__name__}: {exc}",
                        {"error": str(exc)},
                    )
                except Exception:
                    pass
                # Never terminate the only admission worker because one poll failed.
                time.sleep(self.poll_seconds)
            with self.cv:
                if self.stop_event.is_set():
                    break
                if not self.jobs and not self.active:
                    self.cv.wait(timeout=1.0)
                elif not progressed:
                    self.cv.wait(timeout=self.poll_seconds)

    TrainingQueue.start = idempotent_start
    TrainingQueue.enqueue = enqueue
    TrainingQueue.run = resilient_run

    # ---- Queue before background discovery ---------------------------------
    original_initialize_runtime = core.initialize_runtime
    original_queue_factory = queued_runtime.TrainingQueue

    def existing_or_new_queue(history, store, engine, *args, **kwargs):
        existing = queued_runtime.TRAINING_QUEUE
        if existing is not None and existing.history is history:
            return existing
        return original_queue_factory(history, store, engine, *args, **kwargs)

    queued_runtime.TrainingQueue = existing_or_new_queue

    def initialize_runtime():
        # History is intentionally imported here, in the background init thread, not at
        # entrypoint import time. HTTP therefore remains the first externally visible
        # service even with the final production entrypoint.
        import history as history_module

        original_history_start = history_module.HistoryManager.start

        def history_start_with_queue(history_self, *args, **kwargs):
            if queued_runtime.TRAINING_QUEUE is None:
                queue = TrainingQueue(history_self, core.STORE, core.ENGINE)
                queued_runtime.TRAINING_QUEUE = queue
                core.TRAINING_QUEUE = queue
                queue.start()
                core.STORE.event(
                    None, "info", "training_queue_ready",
                    "FIFO training queue ready before background discovery", None,
                )
            else:
                queue = queued_runtime.TRAINING_QUEUE
            _install_initial_training_bridge(history_self, queue, core.STORE)
            return original_history_start(history_self, *args, **kwargs)

        history_module.HistoryManager.start = history_start_with_queue
        try:
            return original_initialize_runtime()
        finally:
            history_module.HistoryManager.start = original_history_start

    core.initialize_runtime = initialize_runtime
    core._startup_train_guard_installed = True
    core.startup_train_guard_contract = {
        "status_until_ready": "lightweight_only",
        "training_queue_order": "before_history_discovery",
        "explicit_train_idle_slot": "immediate_admission_attempt",
        "initial_auto_agent_training": "first_model_in_place_via_single_heavy_job_fifo",
        "candidate_before_base_model": "forbidden_by_candidate_manager",
        "worker_failure": "recover_and_continue",
        "internal_runtime_available_semantics": "preserved_for_extension_installers",
    }
    return core
