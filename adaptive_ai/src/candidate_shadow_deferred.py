"""Bounded deferred execution for Candidate Shadow inference.

Live inference enqueues one exact causal job per root agent and returns. Candidate policy
work is drained by the existing AgentCandidateManager worker, so Raspberry Pi does not gain
another always-on thread. The queue is bounded and coalesces to the newest job per root.

The job retains references to the immutable process_target state snapshot plus the exact
Root inference timestamp/revisions and observed parent decision. Newer HA events may extend
TemporalHistory before the job runs; all Candidate feature builders still query at the
captured event timestamp. Home Context is frozen to the forecast observed by Root Live so
async scheduling cannot leak a newer room/home belief into the paired Candidate decision.
"""
from __future__ import annotations

from collections import OrderedDict
import threading
import time

from inference_hot_path_metrics import increment_counter, observe_elapsed


_MISSING = object()


class _FrozenHomeProvider:
    def __init__(self, base, forecast):
        self._base = base
        self._forecast = dict(forecast or {})

    def forecast(self, target_entity, timestamp):
        return dict(self._forecast)

    def __getattr__(self, name):
        if self._base is None:
            raise AttributeError(name)
        return getattr(self._base, name)


class _TemporalView:
    def __init__(self, base, home_provider):
        self._base = base
        self.home_context = home_provider

    def __getattr__(self, name):
        if self._base is None:
            raise AttributeError(name)
        return getattr(self._base, name)


class DeferredCandidateShadowQueue:
    CONTRACT_VERSION = 1

    def __init__(self, manager, *, limit=32):
        self.manager = manager
        self.engine = manager.engine
        self.limit = max(4, min(128, int(limit)))
        self.lock = threading.RLock()
        self.pending = OrderedDict()
        self.local = threading.local()
        self.stats = {
            "queued": 0,
            "coalesced": 0,
            "deduplicated": 0,
            "dropped": 0,
            "processed": 0,
            "stale_generation_dropped": 0,
            "errors": 0,
            "max_depth": 0,
            "last_error": None,
            "last_root_agent_id": None,
            "last_queue_lag_ms": None,
        }

    @staticmethod
    def _same_event(left, right):
        if not left or not right:
            return False
        return (
            str(left.get("root_agent_id") or "") == str(right.get("root_agent_id") or "")
            and left.get("state_revision") == right.get("state_revision")
            and left.get("inference_ts") == right.get("inference_ts")
            and left.get("context_ts") == right.get("context_ts")
        )

    def enqueue(self, job):
        job = dict(job or {})
        root_id = str(job.get("root_agent_id") or "")
        if not root_id or job.get("state_map") is None:
            return {"deferred": False, "reason": "invalid_candidate_shadow_job"}
        job["root_agent_id"] = root_id
        job["queued_monotonic"] = time.monotonic()

        with self.lock:
            previous = self.pending.get(root_id)
            if self._same_event(previous, job):
                self.stats["deduplicated"] += 1
                increment_counter(self.engine, "candidate_shadow_deduplicated")
                return {
                    "deferred": True,
                    "deduplicated": True,
                    "root_agent_id": root_id,
                    "queue_depth": len(self.pending),
                }
            if previous is not None:
                self.stats["coalesced"] += 1
                increment_counter(self.engine, "candidate_shadow_coalesced")
            else:
                self.stats["queued"] += 1
                increment_counter(self.engine, "candidate_shadow_queued")
            self.pending[root_id] = job
            self.pending.move_to_end(root_id)
            while len(self.pending) > self.limit:
                self.pending.popitem(last=False)
                self.stats["dropped"] += 1
                increment_counter(self.engine, "candidate_shadow_dropped")
            self.stats["max_depth"] = max(self.stats["max_depth"], len(self.pending))
            depth = len(self.pending)

        # Reuse the already-running Candidate lifecycle worker. Waking it is O(1) and
        # Live never waits for the deferred policy evaluation itself.
        wake = getattr(self.manager, "wake_event", None)
        if wake is not None and callable(getattr(wake, "set", None)):
            wake.set()
        return {
            "deferred": True,
            "deduplicated": False,
            "root_agent_id": root_id,
            "queue_depth": depth,
        }

    def _job_temporal(self, job):
        base = getattr(self.engine, "temporal_history", None)
        if not job.get("home_forecast_captured"):
            return base, None
        base_home = getattr(base, "home_context", None) if base is not None else None
        frozen = _FrozenHomeProvider(base_home, job.get("home_forecast") or {})
        return _TemporalView(base, frozen), frozen

    def current_temporal(self):
        return getattr(self.local, "temporal", None) or getattr(
            self.engine, "temporal_history", None
        )

    def current_home_provider(self):
        return getattr(self.local, "home_provider", None)

    def current_job(self):
        return getattr(self.local, "job", None)

    def _bind_engine_tls(self, job):
        tls = getattr(self.engine, "_inference_tls", None)
        if tls is None:
            return None
        saved = {}
        for name in ("state_revision", "entity_revisions", "context_revision"):
            saved[name] = getattr(tls, name, _MISSING)
            value = job.get(name)
            if value is not None:
                setattr(tls, name, value)
            else:
                try:
                    delattr(tls, name)
                except AttributeError:
                    pass
        return tls, saved

    @staticmethod
    def _restore_engine_tls(binding):
        if not binding:
            return
        tls, saved = binding
        for name, value in saved.items():
            if value is _MISSING:
                try:
                    delattr(tls, name)
                except AttributeError:
                    pass
            else:
                setattr(tls, name, value)

    def _execute(self, job):
        current_generation_revision = int(
            getattr(self.manager.store, "_provenance_generation_revision", 0) or 0
        )
        captured_generation_revision = job.get("generation_revision")
        if (
            captured_generation_revision is not None
            and int(captured_generation_revision) != current_generation_revision
        ):
            self.stats["stale_generation_dropped"] += 1
            increment_counter(self.engine, "candidate_shadow_stale_generation_dropped")
            return None

        temporal, home_provider = self._job_temporal(job)
        self.local.job = job
        self.local.temporal = temporal
        self.local.home_provider = home_provider
        binding = self._bind_engine_tls(job)
        try:
            executor = getattr(self.manager, "execute_candidate_shadow_job", None)
            if not callable(executor):
                raise RuntimeError("Candidate Shadow exact-context executor is unavailable")
            return executor(job)
        finally:
            self._restore_engine_tls(binding)
            for name in ("job", "temporal", "home_provider"):
                try:
                    delattr(self.local, name)
                except AttributeError:
                    pass

    def drain(self, *, max_roots=4):
        jobs = []
        with self.lock:
            for _ in range(max(1, int(max_roots))):
                if not self.pending:
                    break
                _, job = self.pending.popitem(last=False)
                jobs.append(job)
        completed = 0
        for job in jobs:
            queued = float(job.get("queued_monotonic") or time.monotonic())
            lag_ms = max(0.0, (time.monotonic() - queued) * 1000.0)
            self.stats["last_queue_lag_ms"] = lag_ms
            observe_elapsed(
                self.engine, "candidate_shadow_queue_lag",
                time.perf_counter_ns() - int(lag_ms * 1_000_000.0),
            )
            started = time.perf_counter_ns()
            try:
                self._execute(job)
                self.stats["processed"] += 1
                increment_counter(self.engine, "candidate_shadow_processed")
                completed += 1
                self.stats["last_error"] = None
            except Exception as exc:
                self.stats["errors"] += 1
                increment_counter(self.engine, "candidate_shadow_errors")
                self.stats["last_error"] = f"{type(exc).__name__}: {exc}"[:400]
                try:
                    self.manager.store.event(
                        job.get("root_agent_id"), "warning",
                        "candidate_shadow_deferred_error",
                        "Deferred Candidate Shadow inference failed; the observation remains a gap",
                        {"error": self.stats["last_error"]},
                    )
                except Exception:
                    pass
            finally:
                observe_elapsed(self.engine, "candidate_shadow_total", started)
                self.stats["last_root_agent_id"] = job.get("root_agent_id")
        return completed

    def diagnostics(self):
        with self.lock:
            depth = len(self.pending)
            roots = list(self.pending.keys())[:8]
            stats = dict(self.stats)
        return {
            "contract_version": self.CONTRACT_VERSION,
            "execution": "existing_candidate_worker",
            "queue_policy": "bounded_latest_per_root",
            "queue_limit": self.limit,
            "queue_depth": depth,
            "pending_roots": roots,
            **stats,
        }
