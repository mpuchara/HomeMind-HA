"""FIFO admission queue for expensive per-agent historical training jobs.

The HistoryManager intentionally permits only one heavy replay at a time so Home
Assistant keeps CPU/RAM priority. This queue turns that resource limit into normal
product behaviour: Train/Resume/Rebuild requests are accepted, deduplicated and run
in order as soon as the shared heavy-job gate becomes available.
"""
from collections import deque
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

    def enqueue(self, agent_id, rebuild=False, reason="training"):
        agent = self.store.get_agent(agent_id)
        if not agent:
            raise ValueError("agent not found")

        with self.cv:
            if self.active and self.active["agent_id"] == agent_id:
                return self.status_for(agent_id)
            if agent_id in self._history_active_ids():
                return {"state": "active", "position": 0, "ahead": 0,
                        "rebuild": bool(rebuild), "agent_id": agent_id}
            existing = self.pending.get(agent_id)
            if existing:
                if rebuild and not existing["rebuild"]:
                    existing["rebuild"] = True
                    existing["reason"] = "full_rebuild"
                    self.store.event(agent_id, "info", "training_queue_upgraded",
                                     "Queued training upgraded to a full rebuild", None)
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
                if rebuild:
                    existing["rebuild"] = True
                    existing["reason"] = "full_rebuild"
                return self.status_for(agent_id)
            self.jobs.append(job)
            self.pending[agent_id] = job
            position = len(self.jobs)
            self.store.event(agent_id, "info", "training_queued",
                             f"Training queued at position {position}",
                             {"position": position, "rebuild": bool(rebuild), "reason": str(reason)})
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
                service.abort_retrain(agent_id, "Teach RL queue request cancelled", state="cancelled")
            except Exception as exc:
                self.store.event(agent_id, "warning", "teach_rl_cancel_cleanup_failed",
                                 str(exc), {"error": f"{type(exc).__name__}: {exc}"})
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
                })
            return {"active": active, "queued": queued, "queued_count": len(queued)}

    def _drop_head(self, event_code, message, detail=None):
        with self.cv:
            if not self.jobs:
                return None
            job = self.jobs.popleft()
            self.pending.pop(job["agent_id"], None)
            self.store.event(job["agent_id"], "warning", event_code, message, detail)
            self.cv.notify_all()
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

        # Teach RL has a queue-owned preflight before the destructive Rebuild.  Do not
        # query Recorder while another agent/bootstrap owns the shared heavy slot.  Once
        # selection succeeds, its state becomes `selected`, so a rare acquire race does
        # not repeat the Recorder scan on the next queue poll.
        service = self._teach_service(job)
        if self._history_active_ids() or HEAVY_JOBS.owner is not None:
            return False
        try:
            if service is not None:
                if service.needs_context_selection(job["agent_id"]):
                    service.prepare_context_selection(agent)
                    agent = self.store.get_agent(job["agent_id"]) or agent
            started = (self.history.request_agent_rebuild(job["agent_id"])
                       if job.get("rebuild") else
                       self.history.request_agent_resume(job["agent_id"]))
        except Exception as exc:
            if service is not None:
                try:
                    service.abort_retrain(job["agent_id"], exc, state="failed")
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
        with self.cv:
            self.cv.notify_all()
