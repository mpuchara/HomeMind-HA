"""FIFO admission queue for expensive per-agent historical training jobs.

The HistoryManager intentionally permits only one heavy replay at a time so Home
Assistant keeps CPU/RAM priority. This queue turns that resource limit into normal
product behaviour: Train/Resume/Rebuild requests are accepted, deduplicated and run
in order as soon as the shared heavy-job gate becomes available.

Explicit user training has priority over the periodic low-memory discovery refresh.
Discovery is safe to defer because per-agent Rebuild performs its own authoritative
Recorder backfill before replay.  The queue therefore asks an in-progress discovery
to stop issuing new Recorder requests, then reschedules a complete discovery pass once
the explicit queue becomes idle.  User-requested Home bootstrap is never preempted.
"""
from collections import deque
import json
import threading
import time

from telemetry import HEAVY_JOBS


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

    def _install_discovery_priority_bridge(self):
        """Cooperatively shorten background discovery when Train is explicitly pressed.

        HistoryManager is intentionally kept independent from the admission queue.  The
        queue therefore installs two instance-local adapters after HistoryManager exists:
        a discovery call can be skipped if training is already pending, and an in-flight
        discovery stops making additional Recorder requests as soon as a user queues work.
        No raw history is deleted; the next idle discovery pass is forced to rebuild its
        refresh window, while the selected agent performs its own full backfill.
        """
        original_cycle = getattr(self.history, "_manual_lightweight_cycle", None)
        if callable(original_cycle) and not getattr(self.history, "_training_priority_cycle_bridge", False):
            def priority_cycle(current, controllable, end_ts):
                if self._training_priority.is_set():
                    self._mark_discovery_preempted()
                    setter = getattr(self.history, "set_status", None)
                    if callable(setter):
                        setter(
                            "manual_ready", message="Agent training requested; background discovery is deferred",
                            phase_detail="Explicit Train has priority over Recorder discovery",
                        )
                    return None
                return original_cycle(current, controllable, end_ts)
            self.history._manual_lightweight_cycle = priority_cycle
            self.history._training_priority_cycle_bridge = True

        original_fetch = getattr(self.history, "_fetch_history_resilient", None)
        if callable(original_fetch) and not getattr(self.history, "_training_priority_fetch_bridge", False):
            def priority_fetch(*args, **kwargs):
                if self._training_priority.is_set() and HEAVY_JOBS.owner == "discovery":
                    self._mark_discovery_preempted()
                    return 0
                return original_fetch(*args, **kwargs)
            self.history._fetch_history_resilient = priority_fetch
            self.history._training_priority_fetch_bridge = True

    def _request_training_priority(self):
        self._training_priority.set()
        with self.cv:
            self.cv.notify_all()

    def _release_training_priority_if_idle(self):
        reschedule = False
        with self.cv:
            if self.jobs or self.active:
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

    def enqueue(self, agent_id, rebuild=False, reason="training"):
        agent = self.store.get_agent(agent_id)
        if not agent:
            raise ValueError("agent not found")

        with self.cv:
            if self.active and self.active["agent_id"] == agent_id:
                self._request_training_priority()
                return self.status_for(agent_id)
            if agent_id in self._history_active_ids():
                self._request_training_priority()
                return {"state": "active", "position": 0, "ahead": 0,
                        "rebuild": bool(rebuild), "agent_id": agent_id}
            existing = self.pending.get(agent_id)
            if existing:
                if str(reason) == "teach_rl":
                    existing["rebuild"] = True
                    existing["reason"] = "teach_rl"
                    self.store.event(agent_id, "info", "training_queue_upgraded",
                                     "Queued training upgraded to Teach RL rebuild", None)
                elif rebuild and not existing["rebuild"]:
                    existing["rebuild"] = True
                    if existing.get("reason") != "teach_rl":
                        existing["reason"] = "full_rebuild"
                    self.store.event(agent_id, "info", "training_queue_upgraded",
                                     "Queued training upgraded to a full rebuild", None)
                self._request_training_priority()
                return self.status_for(agent_id)

        # Do this outside the queue lock: restoring legacy automations can call HA.
        self._release_control_before_queue(agent, reason)
        self._preserve_waiting_state(agent)

        job = {
            "agent_id": agent_id,
            "rebuild": bool(rebuild),
            "reason": str(reason),
            "queued_at": time.time(),
        }
        with self.cv:
            # A second HTTP request may have queued the same agent while Control was
            # being released. Keep exactly one pending entry.
            existing = self.pending.get(agent_id)
            if existing:
                if str(reason) == "teach_rl":
                    existing["rebuild"] = True
                    existing["reason"] = "teach_rl"
                elif rebuild:
                    existing["rebuild"] = True
                    if existing.get("reason") != "teach_rl":
                        existing["reason"] = "full_rebuild"
                self._request_training_priority()
                return self.status_for(agent_id)
            self.jobs.append(job)
            self.pending[agent_id] = job
            position = len(self.jobs)
            self.store.event(agent_id, "info", "training_queued",
                             f"Training queued at position {position}",
                             {"position": position, "rebuild": bool(rebuild), "reason": str(reason)})
            self._request_training_priority()
            self.cv.notify_all()
            return self.status_for(agent_id)

    def cancel(self, agent_id):
        """Remove a not-yet-started job. Active HistoryManager work is not killed."""
        with self.cv:
            job = self.pending.pop(agent_id, None)
            if not job:
                return False
            self.jobs = deque(x for x in self.jobs if x["agent_id"] != agent_id)
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

    def status_for(self, agent_id):
        with self.cv:
            if self.active and self.active["agent_id"] == agent_id:
                return {
                    "state": "active", "position": 0, "ahead": 0,
                    "rebuild": bool(self.active.get("rebuild")),
                    "reason": self.active.get("reason"),
                    "queued_at": self.active.get("queued_at"),
                    "started_at": self.active.get("started_at"),
                    "agent_id": agent_id,
                }
            active_ids = self._history_active_ids()
            if agent_id in active_ids:
                return {"state": "active", "position": 0, "ahead": 0,
                        "rebuild": False, "agent_id": agent_id}
            for index, job in enumerate(self.jobs):
                if job["agent_id"] == agent_id:
                    return {
                        "state": "queued",
                        "position": index + 1,
                        "ahead": index + (1 if self.active or active_ids else 0),
                        "rebuild": bool(job.get("rebuild")),
                        "reason": job.get("reason"),
                        "queued_at": job.get("queued_at"),
                        "blocked_by": HEAVY_JOBS.owner,
                        "agent_id": agent_id,
                    }
            return None

    def snapshot(self):
        with self.cv:
            active = None
            if self.active:
                active = {**self.active, "name": self._agent_label(self.active["agent_id"])}
            queued = []
            for index, job in enumerate(self.jobs):
                queued.append({
                    **job,
                    "name": self._agent_label(job["agent_id"]),
                    "position": index + 1,
                    "ahead": index + (1 if self.active else 0),
                    "blocked_by": HEAVY_JOBS.owner,
                })
            return {
                "active": active, "queued": queued, "queued_count": len(queued),
                "heavy_job": HEAVY_JOBS.owner,
                "explicit_training_priority": self._training_priority.is_set(),
            }

    def _drop_head(self, event_code, message, detail=None):
        with self.cv:
            if not self.jobs:
                return None
            job = self.jobs.popleft()
            self.pending.pop(job["agent_id"], None)
            self.store.event(job["agent_id"], "warning", event_code, message, detail)
            self.cv.notify_all()
        self._release_training_priority_if_idle()
        return job

    def _try_start_head(self):
        with self.cv:
            if self.active or not self.jobs:
                return False
            job = self.jobs[0]

        agent = self.store.get_agent(job["agent_id"])
        if not agent:
            self._drop_head("training_queue_dropped", "Queued training dropped because the agent no longer exists")
            return True

        # Teach RL has a queue-owned preflight before the destructive Rebuild. Do not
        # query Recorder while another agent/bootstrap owns the shared heavy slot. Once
        # selection succeeds, its state becomes `selected`, so a rare acquire race does
        # not repeat the Recorder scan on the next queue poll.
        service = self._teach_service(job)
        if self._history_active_ids() or HEAVY_JOBS.owner is not None:
            return False
        try:
            if job.get("reason") == "teach_rl" and service is None:
                raise RuntimeError("Teach RL service unavailable")
            if service is not None and service.needs_context_selection(job["agent_id"]):
                service.prepare_context_selection(agent)
                agent = self.store.get_agent(job["agent_id"]) or agent
            started = (self.history.request_agent_rebuild(job["agent_id"])
                       if job.get("rebuild") else
                       self.history.request_agent_resume(job["agent_id"]))
        except Exception as exc:
            if service is not None:
                try:
                    self._abort_teach(service, job["agent_id"], exc, state="failed")
                except Exception as cleanup_exc:
                    self.store.event(job["agent_id"], "warning", "teach_rl_prepare_cleanup_failed",
                                     str(cleanup_exc), {"error": f"{type(cleanup_exc).__name__}: {cleanup_exc}"})
            dropped = self._drop_head("training_queue_failed", str(exc), {"error": str(exc)})
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
            # Another job won the small race after the preflight. Keep this request at
            # the head; Teach context is already selected and will not be rescanned.
            return False

        with self.cv:
            job = self.jobs.popleft()
            self.pending.pop(job["agent_id"], None)
            job = {**job, "started_at": time.time()}
            self.active = job
            if service is not None:
                service.mark_training(job["agent_id"])
            self.store.event(job["agent_id"], "info", "training_queue_started",
                             "Queued training started automatically",
                             {"wait_seconds": max(0.0, job["started_at"] - job["queued_at"]),
                              "rebuild": bool(job.get("rebuild")), "reason": job.get("reason")})
            self.cv.notify_all()
        return True

    def _finish_active_if_done(self):
        with self.cv:
            job = self.active
        if not job:
            return False
        if job["agent_id"] in self._history_active_ids():
            return False

        # A Teach RL job is two-stage after preflight: normal deterministic historical
        # rebuild first, then supervised fine-tuning on the active Teach labels.
        service = self._teach_service(job)
        if service is not None:
            try:
                service.finalize_retrain(job["agent_id"])
            except Exception as exc:
                self.store.event(job["agent_id"], "error", "teach_rl_finalize_failed",
                                 str(exc), {"error": f"{type(exc).__name__}: {exc}"})

        agent = self.store.get_agent(job["agent_id"])
        with self.cv:
            if self.active and self.active["agent_id"] == job["agent_id"]:
                self.active = None
                self.cv.notify_all()
        self.store.event(job["agent_id"], "info", "training_queue_finished",
                         "Training slot released; next queued job may start",
                         {"training_state": (agent or {}).get("training_state"), "reason": job.get("reason")})
        self._release_training_priority_if_idle()
        return True

    def run(self):
        while not self.stop_event.is_set():
            self._finish_active_if_done()
            progressed = self._try_start_head()
            with self.cv:
                if self.stop_event.is_set():
                    break
                if not self.jobs and not self.active:
                    self.cv.wait(timeout=1.0)
                elif not progressed:
                    self.cv.wait(timeout=self.poll_seconds)

    def stop(self):
        self.stop_event.set()
        self._training_priority.clear()
        with self.cv:
            self.cv.notify_all()
