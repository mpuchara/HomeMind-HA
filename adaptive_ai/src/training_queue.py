"""Priority admission queue for expensive per-agent historical training jobs.

The HistoryManager intentionally permits only one heavy replay at a time so Home
Assistant keeps CPU/RAM priority. This queue turns that resource limit into normal
product behaviour: Train/Resume/Rebuild requests are accepted, deduplicated and run
in order as soon as the shared heavy-job gate becomes available.

Explicit user training has priority over the periodic low-memory discovery refresh.
Discovery is safe to defer because per-agent Rebuild performs its own authoritative
Recorder backfill before replay. The queue therefore ends an in-progress discovery
between Recorder requests, then reschedules a complete discovery pass once the explicit
queue becomes idle. User-requested Home bootstrap is never preempted.
"""
from collections import deque
import inspect
import json
import threading
import time

from telemetry import HEAVY_JOBS
from learning_lifecycle import INITIAL_MODEL_BUILD, normalize_rebuild_reason


def _queue_rebuild_reason(rebuild, reason, rebuild_reason=None):
    if not rebuild:
        return None
    if rebuild_reason:
        if str(rebuild_reason) == INITIAL_MODEL_BUILD:
            return INITIAL_MODEL_BUILD
        return normalize_rebuild_reason(rebuild_reason, explicit=str(reason) == "full_rebuild")
    if str(reason) == "initial_training":
        return INITIAL_MODEL_BUILD
    if str(reason) == "teach_rl":
        return "feature_mask_change"
    if str(reason) == "full_rebuild":
        return "explicit_manual_rebuild"
    return "incompatible_persisted_model"


PRIORITY_INTERACTIVE = 0
PRIORITY_USER = 10
PRIORITY_AUTOMATIC = 20
PRIORITY_MAINTENANCE = 30


def training_priority_for_reason(reason):
    """Lower number means earlier admission; FIFO is preserved inside each class."""
    reason = str(reason or "training")
    if reason == "teach_rl":
        return PRIORITY_INTERACTIVE
    if reason in ("training", "resume_training", "full_rebuild", "autonomous_continuation"):
        return PRIORITY_USER
    if reason == "initial_training":
        return PRIORITY_AUTOMATIC
    if reason in ("maintenance", "background", "discovery"):
        return PRIORITY_MAINTENANCE
    return PRIORITY_USER


def _call_rebuild_compat(history, agent_id, rebuild_reason):
    """Call Stage-8 rebuild API while preserving extension/test adapters from older releases."""
    method = history.request_agent_rebuild
    try:
        params = inspect.signature(method).parameters
        supports_reason = (
            "rebuild_reason" in params
            or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        )
    except (TypeError, ValueError):
        supports_reason = True
    if supports_reason:
        return method(agent_id, rebuild_reason=rebuild_reason)
    return method(agent_id)


def training_priority_class(priority):
    priority = int(priority)
    if priority <= PRIORITY_INTERACTIVE:
        return "interactive"
    if priority <= PRIORITY_USER:
        return "user"
    if priority <= PRIORITY_AUTOMATIC:
        return "automatic"
    return "maintenance"


class _YieldDiscovery(Exception):
    """Private cooperative signal used only to leave automatic discovery promptly."""


class TrainingQueue(threading.Thread):
    daemon = True

    def __init__(self, history, store, engine, poll_seconds=0.25):
        super().__init__(name="adaptive-ai-training-queue")
        self.history = history
        self.store = store
        self.engine = engine
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.stop_event = threading.Event()
        self.cv = threading.Condition(threading.RLock())
        self.jobs = deque()
        self.pending = {}
        self.active = None
        self.parallel_active = {}
        # Active jobs normally finish. Candidate Discard is the one lifecycle action
        # that must be able to stop its own isolated training worker.
        self.cancel_requested = set()
        self._queue_sequence = 0
        self.revision = 0
        self._training_priority = threading.Event()
        self._discovery_preempted = False
        self._install_discovery_priority_bridge()

    def _history_active_ids(self):
        lock = getattr(self.history, "agent_jobs_lock", None)
        jobs = getattr(self.history, "agent_jobs", set())
        if lock is None:
            return set(jobs)
        with lock:
            return set(jobs)

    def _active_jobs_locked(self):
        jobs = []
        if self.active:
            jobs.append(self.active)
        jobs.extend(self.parallel_active.values())
        return jobs

    def _active_job_locked(self, agent_id):
        agent_id = str(agent_id)
        if self.active and str(self.active.get("agent_id")) == agent_id:
            return self.active
        return self.parallel_active.get(agent_id)

    def _active_count_locked(self):
        return len(self._active_jobs_locked())

    def _effective_slots(self):
        resolver = getattr(self.history, "effective_training_slots", None)
        if callable(resolver):
            try:
                return max(1, min(2, int(resolver() or 1)))
            except Exception:
                pass
        return 1

    def _agent_label(self, agent_id):
        agent = self.store.get_agent(agent_id)
        return (agent or {}).get("name") or agent_id

    def _mark_discovery_preempted(self):
        first = False
        with self.cv:
            if not self._discovery_preempted:
                self._discovery_preempted = True
                first = True
        if first:
            try:
                self.store.event(
                    None, "info", "discovery_yielded_to_training",
                    "Background discovery yielded to explicit agent training",
                    {"heavy_job": HEAVY_JOBS.owner},
                )
            except Exception:
                pass

    def _set_discovery_deferred_status(self):
        setter = getattr(self.history, "set_status", None)
        if callable(setter):
            setter(
                "manual_ready", message="Agent training requested; background discovery is deferred",
                phase_detail="Explicit Train has priority over Recorder discovery",
            )

    def _install_discovery_priority_bridge(self):
        """Cooperatively end automatic discovery when Train is explicitly pressed.

        HistoryManager stays independent from queue admission. Instance-local adapters are
        installed after HistoryManager exists. If Train is already pending, a new discovery
        pass is skipped. If discovery is already running, its next Recorder request raises a
        private signal that is caught at the discovery-cycle boundary, so we do not walk the
        remaining chunks or pay their background pauses. No raw history is deleted.
        """
        original_cycle = getattr(self.history, "_manual_lightweight_cycle", None)
        if callable(original_cycle) and not getattr(self.history, "_training_priority_cycle_bridge", False):
            def priority_cycle(current, controllable, end_ts, *args, **kwargs):
                if self._training_priority.is_set():
                    self._mark_discovery_preempted()
                    self._set_discovery_deferred_status()
                    return None
                try:
                    return original_cycle(current, controllable, end_ts, *args, **kwargs)
                except _YieldDiscovery:
                    self._set_discovery_deferred_status()
                    return None
            self.history._manual_lightweight_cycle = priority_cycle
            self.history._training_priority_cycle_bridge = True

        original_fetch = getattr(self.history, "_fetch_history_resilient", None)
        if callable(original_fetch) and not getattr(self.history, "_training_priority_fetch_bridge", False):
            def priority_fetch(*args, **kwargs):
                if self._training_priority.is_set() and HEAVY_JOBS.owner == "discovery":
                    self._mark_discovery_preempted()
                    raise _YieldDiscovery()
                return original_fetch(*args, **kwargs)
            self.history._fetch_history_resilient = priority_fetch
            self.history._training_priority_fetch_bridge = True

    def _request_training_priority(self):
        self._training_priority.set()
        if HEAVY_JOBS.owner == "discovery":
            self._mark_discovery_preempted()
        with self.cv:
            self.cv.notify_all()

    def _release_training_priority_if_idle(self):
        reschedule = False
        with self.cv:
            if self.jobs or self._active_count_locked():
                return False
            self._training_priority.clear()
            if self._discovery_preempted:
                self._discovery_preempted = False
                reschedule = True
        if reschedule:
            try:
                # Empty value makes the next maintenance cycle use the normal initial
                # discovery window instead of treating the shortened pass as complete.
                self.store.meta_set("manual_discovery_refresh", "")
                self.store.event(
                    None, "info", "discovery_rescheduled_after_training",
                    "Background discovery will run a complete refresh after explicit training",
                    None,
                )
            except Exception:
                pass
        return True

    def _preserve_waiting_state(self, agent):
        """Block Control while queued without throwing away benchmark evidence."""
        self.store.set_training_state(
            agent["id"], "waiting",
            score=agent.get("benchmark_score"),
            samples=agent.get("benchmark_samples") or 0,
            source=agent.get("benchmark_source"),
            detail=agent.get("benchmark_detail") or {},
        )

    def _release_control_before_queue(self, agent, reason):
        if agent.get("mode") == "control":
            self.engine.executor.release_control(agent, reason=reason)

    def _teach_service(self, job):
        if not job or job.get("reason") != "teach_rl":
            return None
        return getattr(self.engine, "rl_teaching", None)

    def _abort_teach(self, service, agent_id, reason, state="failed"):
        """Restore a temporary Teach selector and close its durable job state.

        Keep a compatibility fallback here because queue lifecycle and Teach storage are
        separate extensions and upgrades can briefly mix their versions after restart.
        """
        if service is None:
            return
        abort = getattr(service, "abort_retrain", None)
        if callable(abort):
            abort(agent_id, reason, state=state)
            return
        original = ["*"]
        try:
            with service.store.conn() as c:
                row = c.execute(
                    "SELECT original_inputs_json FROM teaching_rl_jobs WHERE agent_id=?",
                    (agent_id,),
                ).fetchone()
            if row:
                original = json.loads(row["original_inputs_json"] or '["*"]')
            service._set_inputs_direct(agent_id, original)
            service._set_job_stage(
                agent_id, state=state, stage=state,
                error=f"{type(reason).__name__}: {reason}" if isinstance(reason, Exception) else str(reason),
            )
            service.store.event(
                agent_id, "warning" if state == "failed" else "info", "teach_rl_" + state,
                str(reason), {"state": state},
            )
        except Exception:
            # The caller records a dedicated cleanup failure event.
            raise

    def _bump_revision_locked(self):
        self.revision += 1
        return self.revision

    def _resort_jobs_locked(self):
        self.jobs = deque(sorted(
            self.jobs,
            key=lambda job: (
                int(job.get("priority", PRIORITY_USER)),
                int(job.get("sequence", 0)),
            ),
        ))

    def _upgrade_pending_job_locked(self, existing, *, rebuild, reason, requested_priority, rebuild_reason=None):
        old_priority = int(existing.get("priority", training_priority_for_reason(existing.get("reason"))))
        requested_rebuild_reason = _queue_rebuild_reason(rebuild, reason, rebuild_reason)
        changed = False
        if str(reason) == "teach_rl":
            if existing.get("reason") != "teach_rl" or not existing.get("rebuild"):
                changed = True
            existing["rebuild"] = True
            existing["reason"] = "teach_rl"
            existing["rebuild_reason"] = requested_rebuild_reason or "feature_mask_change"
        elif rebuild and not existing.get("rebuild"):
            existing["rebuild"] = True
            if existing.get("reason") != "teach_rl":
                existing["reason"] = "full_rebuild"
            existing["rebuild_reason"] = requested_rebuild_reason
            changed = True

        if int(requested_priority) < old_priority:
            existing["priority"] = int(requested_priority)
            if existing.get("reason") != "teach_rl":
                existing["reason"] = str(reason)
            changed = True
        else:
            existing.setdefault("priority", old_priority)
        existing["priority_class"] = training_priority_class(existing["priority"])
        self._resort_jobs_locked()
        return changed, old_priority

    def enqueue(self, agent_id, rebuild=False, reason="training", rebuild_reason=None):
        agent = self.store.get_agent(agent_id)
        if not agent:
            raise ValueError("agent not found")
        requested_priority = training_priority_for_reason(reason)
        requested_rebuild_reason = _queue_rebuild_reason(rebuild, reason, rebuild_reason)

        with self.cv:
            if self._active_job_locked(agent_id):
                self._request_training_priority()
                return self.status_for(agent_id)
            if agent_id in self._history_active_ids():
                self._request_training_priority()
                return {"state": "active", "position": 0, "ahead": 0,
                        "rebuild": bool(rebuild), "rebuild_reason": requested_rebuild_reason,
                        "agent_id": agent_id}
            existing = self.pending.get(agent_id)
            if existing:
                changed, old_priority = self._upgrade_pending_job_locked(
                    existing, rebuild=rebuild, reason=reason,
                    requested_priority=requested_priority,
                    rebuild_reason=requested_rebuild_reason,
                )
                if changed:
                    self.store.event(
                        agent_id, "info", "training_queue_upgraded",
                        "Queued training request was upgraded or reprioritized",
                        {
                            "reason": existing.get("reason"),
                            "rebuild": bool(existing.get("rebuild")),
                            "rebuild_reason": existing.get("rebuild_reason"),
                            "priority": int(existing.get("priority", requested_priority)),
                            "priority_class": existing.get("priority_class"),
                            "previous_priority": old_priority,
                        },
                    )
                self._request_training_priority()
                return self.status_for(agent_id)

        # Do this outside the queue lock: restoring legacy automations can call HA.
        self._release_control_before_queue(agent, reason)
        self._preserve_waiting_state(agent)

        with self.cv:
            self._queue_sequence += 1
            sequence = self._queue_sequence
        job = {
            "agent_id": agent_id,
            "rebuild": bool(rebuild),
            "reason": str(reason),
            "rebuild_reason": requested_rebuild_reason,
            "queued_at": time.time(),
            "priority": int(requested_priority),
            "priority_class": training_priority_class(requested_priority),
            "sequence": int(sequence),
        }
        with self.cv:
            # A second HTTP request may have queued the same agent while Control was
            # being released. Keep exactly one pending entry and preserve the strongest
            # priority requested by either caller.
            existing = self.pending.get(agent_id)
            if existing:
                changed, old_priority = self._upgrade_pending_job_locked(
                    existing, rebuild=rebuild, reason=reason,
                    requested_priority=requested_priority,
                    rebuild_reason=requested_rebuild_reason,
                )
                if changed:
                    self.store.event(
                        agent_id, "info", "training_queue_upgraded",
                        "Queued training request was upgraded or reprioritized",
                        {
                            "reason": existing.get("reason"),
                            "rebuild": bool(existing.get("rebuild")),
                            "rebuild_reason": existing.get("rebuild_reason"),
                            "priority": int(existing.get("priority", requested_priority)),
                            "priority_class": existing.get("priority_class"),
                            "previous_priority": old_priority,
                        },
                    )
                self._request_training_priority()
                return self.status_for(agent_id)
            self.jobs.append(job)
            self.pending[agent_id] = job
            self._resort_jobs_locked()
            self._bump_revision_locked()
            position = next(
                (index + 1 for index, queued in enumerate(self.jobs)
                 if queued["agent_id"] == agent_id),
                len(self.jobs),
            )
            self.store.event(
                agent_id, "info", "training_queued",
                f"Training queued at position {position}",
                {
                    "position": position,
                    "rebuild": bool(rebuild),
                    "reason": str(reason),
                    "rebuild_reason": requested_rebuild_reason,
                    "priority": int(requested_priority),
                    "priority_class": training_priority_class(requested_priority),
                },
            )
            self._request_training_priority()
            self.cv.notify_all()
            return self.status_for(agent_id)

    def cancel(self, agent_id):
        """Remove a not-yet-started job. Active work needs explicit cancel_active()."""
        with self.cv:
            job = self.pending.pop(agent_id, None)
            if not job:
                return False
            self.jobs = deque(x for x in self.jobs if x["agent_id"] != agent_id)
            self._bump_revision_locked()
            self.store.event(agent_id, "info", "training_queue_cancelled",
                             "Queued training request cancelled", None)
            self.cv.notify_all()
        service = self._teach_service(job)
        if service is not None:
            try:
                self._abort_teach(service, agent_id, "Teach RL queue request cancelled", state="cancelled")
            except Exception as exc:
                self.store.event(agent_id, "warning", "teach_rl_cancel_cleanup_failed",
                                 str(exc), {"error": f"{type(exc).__name__}: {exc}"})
        self._release_training_priority_if_idle()
        return True

    def cancel_active(self, agent_id, reason="active_training_cancelled"):
        """Cooperatively stop the currently active agent-training job.

        Historical training is process-isolated by default. HistoryManager supervises
        that subprocess and already honours job_cancel_event by terminating the worker
        with the configured grace period. The queue owns that per-job event so a
        cancellation cannot leak into the next training request.

        This method never deletes the agent. Lifecycle owners such as Candidate Discard
        wait until status_for() and HistoryManager.agent_jobs clear, then remove the
        surrogate after the worker has actually exited.
        """
        agent_id = str(agent_id)
        active_ids = {str(x) for x in self._history_active_ids()}
        with self.cv:
            queue_active = self._active_job_locked(agent_id) is not None
            history_active = agent_id in active_ids
            if not queue_active and not history_active:
                return False
            self.cancel_requested.add(agent_id)
            cancel = getattr(self.history, "cancel_agent_training", None)
            if callable(cancel):
                cancel(agent_id)
            else:
                event = getattr(self.history, "job_cancel_event", None)
                if event is None:
                    event = threading.Event()
                    self.history.job_cancel_event = event
                event.set()
            self._bump_revision_locked()
            self.store.event(
                agent_id, "info", "training_queue_active_cancel_requested",
                "Active training cancellation requested",
                {"reason": str(reason)},
            )
            self.cv.notify_all()
        return True

    def status_for(self, agent_id):
        agent_id = str(agent_id)
        with self.cv:
            active_job = self._active_job_locked(agent_id)
            if active_job:
                return {
                    "state": "active", "position": 0, "ahead": 0,
                    "rebuild": bool(active_job.get("rebuild")),
                    "reason": active_job.get("reason"),
                    "rebuild_reason": active_job.get("rebuild_reason"),
                    "priority": active_job.get("priority"),
                    "priority_class": active_job.get("priority_class"),
                    "queued_at": active_job.get("queued_at"),
                    "started_at": active_job.get("started_at"),
                    "cancel_requested": agent_id in self.cancel_requested,
                    "agent_id": agent_id,
                }
            active_ids = {str(x) for x in self._history_active_ids()}
            if agent_id in active_ids:
                return {
                    "state": "active", "position": 0, "ahead": 0,
                    "rebuild": False, "agent_id": agent_id,
                }
            active_count = self._active_count_locked()
            for index, job in enumerate(self.jobs):
                if str(job["agent_id"]) == agent_id:
                    return {
                        "state": "queued",
                        "position": index + 1,
                        "ahead": index + active_count,
                        "rebuild": bool(job.get("rebuild")),
                        "reason": job.get("reason"),
                        "rebuild_reason": job.get("rebuild_reason"),
                        "priority": job.get("priority"),
                        "priority_class": job.get("priority_class"),
                        "queued_at": job.get("queued_at"),
                        "blocked_by": HEAVY_JOBS.owner,
                        "agent_id": agent_id,
                    }
            return None

    def snapshot(self):
        with self.cv:
            active_jobs = [
                {**job, "name": self._agent_label(job["agent_id"])}
                for job in self._active_jobs_locked()
            ]
            active = active_jobs[0] if active_jobs else None
            queued = []
            active_count = len(active_jobs)
            for index, job in enumerate(self.jobs):
                queued.append({
                    **job,
                    "name": self._agent_label(job["agent_id"]),
                    "position": index + 1,
                    "ahead": index + active_count,
                    "blocked_by": HEAVY_JOBS.owner,
                })
            return {
                "active": active,
                "active_jobs": active_jobs,
                "active_count": active_count,
                "effective_slots": self._effective_slots(),
                "queued": queued,
                "queued_count": len(queued),
                "revision": int(self.revision),
                "heavy_job": HEAVY_JOBS.owner,
                "explicit_training_priority": self._training_priority.is_set(),
            }

    def _drop_head(self, event_code, message, detail=None):
        with self.cv:
            if not self.jobs:
                return None
            job = self.jobs.popleft()
            self.pending.pop(job["agent_id"], None)
            self._bump_revision_locked()
            self.store.event(job["agent_id"], "warning", event_code, message, detail)
            self.cv.notify_all()
        self._release_training_priority_if_idle()
        return job

    def _try_start_head(self):
        with self.cv:
            if not self.jobs:
                return False
            active_jobs = self._active_jobs_locked()
            slots = self._effective_slots()
            if len(active_jobs) >= slots:
                return False
            job = self.jobs[0]
            # Teach/Correct retraining stays exclusive. Likewise, no ordinary second
            # worker starts while a Teach job owns the training slot.
            if job.get("reason") == "teach_rl" and active_jobs:
                return False
            if any(active.get("reason") == "teach_rl" for active in active_jobs):
                return False

        agent = self.store.get_agent(job["agent_id"])
        if not agent:
            self._drop_head(
                "training_queue_dropped",
                "Queued training dropped because the agent no longer exists",
            )
            return True

        service = self._teach_service(job)
        owner = HEAVY_JOBS.owner
        if owner not in (None, "agent_pool"):
            return False
        # Teach context selection is allowed only before any agent worker is running.
        if service is not None and self._history_active_ids():
            return False
        try:
            if job.get("reason") == "teach_rl" and service is None:
                raise RuntimeError("Teach RL service unavailable")
            if service is not None and service.needs_context_selection(job["agent_id"]):
                service.prepare_context_selection(agent)
                agent = self.store.get_agent(job["agent_id"]) or agent
            started = (
                _call_rebuild_compat(
                    self.history, job["agent_id"], job.get("rebuild_reason")
                )
                if job.get("rebuild")
                else self.history.request_agent_resume(job["agent_id"])
            )
        except Exception as exc:
            if service is not None:
                try:
                    self._abort_teach(service, job["agent_id"], exc, state="failed")
                except Exception as cleanup_exc:
                    self.store.event(
                        job["agent_id"], "warning", "teach_rl_prepare_cleanup_failed",
                        str(cleanup_exc),
                        {"error": f"{type(cleanup_exc).__name__}: {cleanup_exc}"},
                    )
            dropped = self._drop_head(
                "training_queue_failed", str(exc), {"error": str(exc)}
            )
            if dropped:
                current = self.store.get_agent(dropped["agent_id"])
                if current:
                    self.store.set_training_state(
                        dropped["agent_id"], "paused",
                        score=current.get("benchmark_score"),
                        samples=current.get("benchmark_samples") or 0,
                        source=current.get("benchmark_source"),
                        detail={"reason": str(exc)},
                    )
            return True

        if not started:
            return False

        with self.cv:
            job = self.jobs.popleft()
            self.pending.pop(job["agent_id"], None)
            job = {**job, "started_at": time.time()}
            if self.active is None:
                self.active = job
            else:
                self.parallel_active[str(job["agent_id"])] = job
            self._bump_revision_locked()
            if service is not None:
                service.mark_training(job["agent_id"])
            self.store.event(
                job["agent_id"], "info", "training_queue_started",
                "Queued training started automatically",
                {
                    "wait_seconds": max(0.0, job["started_at"] - job["queued_at"]),
                    "rebuild": bool(job.get("rebuild")),
                    "reason": job.get("reason"),
                    "rebuild_reason": job.get("rebuild_reason"),
                    "parallel_active_count": self._active_count_locked(),
                    "effective_slots": slots,
                },
            )
            self.cv.notify_all()
        return True

    def _finish_active_if_done(self):
        active_ids = {str(x) for x in self._history_active_ids()}
        with self.cv:
            jobs = list(self._active_jobs_locked())
        job = next(
            (item for item in jobs if str(item["agent_id"]) not in active_ids),
            None,
        )
        if not job:
            return False
        agent_id = str(job["agent_id"])
        with self.cv:
            cancelled = agent_id in self.cancel_requested

        service = self._teach_service(job)
        if service is not None:
            try:
                if cancelled:
                    self._abort_teach(
                        service, agent_id,
                        "Active training cancelled by lifecycle owner",
                        state="cancelled",
                    )
                else:
                    service.finalize_retrain(agent_id)
            except Exception as exc:
                code = (
                    "teach_rl_cancel_cleanup_failed"
                    if cancelled else "teach_rl_finalize_failed"
                )
                self.store.event(
                    agent_id, "warning" if cancelled else "error", code,
                    str(exc), {"error": f"{type(exc).__name__}: {exc}"},
                )

        config_getter = getattr(self.store, "get_agent_config", None)
        agent = (
            config_getter(agent_id)
            if callable(config_getter) else self.store.get_agent(agent_id)
        )
        with self.cv:
            if self.active and str(self.active.get("agent_id")) == agent_id:
                # Promote the oldest parallel slot to the legacy primary field so old UI
                # surfaces continue to show one active job while the new API exposes all.
                self.active = None
                if self.parallel_active:
                    _, promoted = min(
                        self.parallel_active.items(),
                        key=lambda item: float(item[1].get("started_at") or 0.0),
                    )
                    self.parallel_active.pop(str(promoted["agent_id"]), None)
                    self.active = promoted
            else:
                self.parallel_active.pop(agent_id, None)
            self.cancel_requested.discard(agent_id)
            self._bump_revision_locked()
            self.cv.notify_all()

        state = str((agent or {}).get("training_state") or "")
        progress_raw = (agent or {}).get("training_progress")
        progress = (
            None if progress_raw is None
            else max(0.0, min(1.0, float(progress_raw)))
        )
        interrupted = (
            state in ("paused", "needs_retrain", "waiting")
            and progress is not None and progress < 0.999
        )
        detail = {
            "training_state": state,
            "training_progress": progress,
            "reason": job.get("reason"),
            "rebuild_reason": job.get("rebuild_reason"),
            "failure_reason": ((agent or {}).get("benchmark_detail") or {}).get("reason"),
            "cancel_requested": bool(cancelled),
        }
        if cancelled:
            self.store.event(
                agent_id, "info", "training_queue_active_cancelled",
                "Active training cancelled; slot released for the next queued job",
                detail,
            )
        elif interrupted:
            self.store.event(
                agent_id, "warning", "training_queue_interrupted",
                f"Training stopped at {progress:.0%}; slot released for the next queued job",
                detail,
            )
        else:
            self.store.event(
                agent_id, "info", "training_queue_finished",
                "Training slot released; next queued job may start", detail,
            )
        self._release_training_priority_if_idle()
        return True

    def run(self):
        while not self.stop_event.is_set():
            finished = False
            while self._finish_active_if_done():
                finished = True
            progressed = False
            while self._try_start_head():
                progressed = True
                if self._active_count_locked() >= self._effective_slots():
                    break
            with self.cv:
                if self.stop_event.is_set():
                    break
                if not self.jobs and not self._active_count_locked():
                    self.cv.wait(timeout=1.0)
                elif not progressed and not finished:
                    self.cv.wait(timeout=self.poll_seconds)

    def stop(self):
        self.stop_event.set()
        self._training_priority.clear()
        with self.cv:
            self.cv.notify_all()