"""Cooperative wall-clock budget for explicit historical training.

The explicit agent trainer shares one Python process with HTTP/Ingress, realtime HA state
handling and local inference. Average duty-cycle throttling alone is not enough: one
expensive replay row or a post-replay finalization step can otherwise hold the process for
seconds before the next sleep point.

This module provides small cooperative checkpoints for adaptive-ai-index-* worker
threads. It never changes model semantics, ordering, labels, persisted history or physical
control. It only yields CPU between deterministic pieces of training work.
"""
from __future__ import annotations

import threading
import time


class CooperativeTrainingBudget:
    """Bound continuous worker slices and maintain an approximate duty cycle.

    The budget is intentionally cooperative rather than a cgroup/OS quota. Callers place
    checkpoints inside expensive loops. When a slice reaches max_slice_seconds the
    worker sleeps long enough to approximate duty_cycle and gives HTTP/realtime
    threads an immediate scheduling opportunity.
    """

    def __init__(self, *, clock=None, sleeper=None):
        self._clock = clock or time.perf_counter
        self._sleep = sleeper or time.sleep
        self._lock = threading.RLock()
        self._local = threading.local()
        self._duty_cycle = 0.25
        self._max_slice_seconds = 0.075
        self._max_sleep_seconds = 2.0
        self._thread_prefixes = ("adaptive-ai-index-",)
        self._interactive_until = 0.0
        self._interactive_started_at = 0.0
        # Continuous HA sensor traffic must not starve offline training forever. A burst
        # may keep strict realtime priority only for a bounded interval, followed by a
        # short cooldown in which the training worker is guaranteed a scheduling slice.
        self._interactive_max_burst_seconds = 1.25
        self._interactive_cooldown_seconds = 0.10
        self._interactive_cooldown_until = 0.0
        self._interactive_reason = None
        self._stats = {
            "checkpoints": 0,
            "throttle_sleeps": 0,
            "active_seconds": 0.0,
            "sleep_seconds": 0.0,
            "max_observed_slice_seconds": 0.0,
            "slice_overruns": 0,
            "last_label": None,
            "interactive_preemptions": 0,
            "interactive_sleep_seconds": 0.0,
            "interactive_requests_suppressed": 0,
        }

    @staticmethod
    def _clamp(value, low, high):
        return max(low, min(high, value))

    def configure(
        self,
        *,
        duty_cycle=0.25,
        max_slice_seconds=0.075,
        max_sleep_seconds=2.0,
        thread_prefixes=("adaptive-ai-index-",),
        clock=None,
        sleeper=None,
    ):
        with self._lock:
            if clock is not None:
                self._clock = clock
            if sleeper is not None:
                self._sleep = sleeper
            self._duty_cycle = self._clamp(float(duty_cycle), 0.10, 0.70)
            self._max_slice_seconds = self._clamp(
                float(max_slice_seconds), 0.025, 0.500
            )
            self._max_sleep_seconds = self._clamp(
                float(max_sleep_seconds), 0.050, 5.0
            )
            self._thread_prefixes = tuple(str(x) for x in thread_prefixes)
        return self.snapshot()

    def _eligible(self, thread_name=None):
        name = str(thread_name or threading.current_thread().name)
        return any(name.startswith(prefix) for prefix in self._thread_prefixes)

    def request_interactive_window(self, seconds=0.75, reason="interactive"):
        """Temporarily give HTTP/realtime inference strict priority over training.

        Realtime priority is deliberately burst-bounded. Hundreds of Home Assistant
        entities can produce state changes more often than once per second; extending the
        deadline on every event used to keep an adaptive-ai-index worker asleep forever.
        A burst can extend only up to the configured burst cap and is followed by a short
        cooldown in which new priority requests are ignored. The training worker is still
        capped to its normal short cooperative slice, so realtime latency remains protected
        without starving the FIFO.
        """
        duration = self._clamp(float(seconds), 0.05, 3.0)
        now = self._clock()
        with self._lock:
            if (
                now < float(self._interactive_cooldown_until or 0.0)
                and now >= float(self._interactive_until or 0.0)
            ):
                self._stats["interactive_requests_suppressed"] += 1
                return float(self._interactive_until or 0.0)

            active = now < float(self._interactive_until or 0.0)
            if active:
                started = float(self._interactive_started_at or now)
                cap = started + float(self._interactive_max_burst_seconds)
                until = min(
                    cap,
                    max(float(self._interactive_until or 0.0), now + duration),
                )
            else:
                self._interactive_started_at = now
                until = now + min(duration, float(self._interactive_max_burst_seconds))

            self._interactive_until = until
            self._interactive_cooldown_until = max(
                float(self._interactive_cooldown_until or 0.0),
                until + float(self._interactive_cooldown_seconds),
            )
            self._interactive_reason = str(reason or "interactive")
            return until

    def begin(self, *, thread_name=None):
        if not self._eligible(thread_name):
            return False
        self._local.slice_started = self._clock()
        self._local.active = True
        return True

    def end(self):
        self._local.active = False
        self._local.slice_started = None

    def checkpoint(self, label=None, *, force=False, thread_name=None):
        """Yield CPU if the current training slice exhausted its wall-clock allowance.

        Returns the sleep duration. The first checkpoint on a worker starts accounting so
        callers are safe even if begin() was not called explicitly.
        """
        if not self._eligible(thread_name):
            return 0.0

        now = self._clock()
        with self._lock:
            interactive_until = float(self._interactive_until or 0.0)
            interactive_reason = self._interactive_reason
        if now < interactive_until:
            pause = min(0.25, max(0.001, interactive_until - now))
            self._sleep(pause)
            with self._lock:
                self._stats["interactive_preemptions"] += 1
                self._stats["interactive_sleep_seconds"] += pause
                self._stats["last_label"] = (
                    "interactive:" + str(interactive_reason or "priority")
                )
            self._local.slice_started = self._clock()
            self._local.active = True
            return pause

        started = getattr(self._local, "slice_started", None)
        if started is None:
            self._local.slice_started = now
            self._local.active = True
            return 0.0

        active = max(0.0, now - float(started))
        with self._lock:
            max_slice = self._max_slice_seconds
            duty = self._duty_cycle
            max_sleep = self._max_sleep_seconds

        if not force and active < max_slice:
            return 0.0
        if active <= 0.0:
            pause = 0.001 if force else 0.0
        else:
            pause = active * (1.0 - duty) / max(duty, 1e-9)
            pause = self._clamp(pause, 0.001, max_sleep)

        with self._lock:
            self._stats["checkpoints"] += 1
            self._stats["active_seconds"] += active
            self._stats["max_observed_slice_seconds"] = max(
                float(self._stats["max_observed_slice_seconds"]), active
            )
            if active > max_slice:
                self._stats["slice_overruns"] += 1
            self._stats["last_label"] = None if label is None else str(label)

        if pause > 0.0:
            self._sleep(pause)
            with self._lock:
                self._stats["throttle_sleeps"] += 1
                self._stats["sleep_seconds"] += pause

        self._local.slice_started = self._clock()
        self._local.active = True
        return pause

    def snapshot(self):
        with self._lock:
            stats = dict(self._stats)
            duty = self._duty_cycle
            max_slice = self._max_slice_seconds
            max_sleep = self._max_sleep_seconds
        total = float(stats["active_seconds"]) + float(stats["sleep_seconds"])
        effective = (
            float(stats["active_seconds"]) / total
            if total > 0.0
            else None
        )
        return {
            "training_cpu_duty_cycle": duty,
            "max_continuous_work_ms": round(max_slice * 1000.0, 1),
            "max_throttle_sleep_seconds": max_sleep,
            "slice_checkpoints": int(stats["checkpoints"]),
            "throttle_batches": int(stats["throttle_sleeps"]),
            "throttle_active_seconds": round(float(stats["active_seconds"]), 3),
            "throttle_sleep_seconds": round(float(stats["sleep_seconds"]), 3),
            "effective_training_duty_cycle": (
                None if effective is None else round(effective, 4)
            ),
            "max_observed_slice_ms": round(
                float(stats["max_observed_slice_seconds"]) * 1000.0, 1
            ),
            "slice_overruns": int(stats["slice_overruns"]),
            "interactive_preemptions": int(stats["interactive_preemptions"]),
            "interactive_sleep_seconds": round(float(stats["interactive_sleep_seconds"]), 3),
            "interactive_requests_suppressed": int(stats["interactive_requests_suppressed"]),
            "interactive_max_burst_seconds": float(self._interactive_max_burst_seconds),
            "interactive_cooldown_seconds": float(self._interactive_cooldown_seconds),
            "last_checkpoint": stats["last_label"],
        }


TRAINING_BUDGET = CooperativeTrainingBudget()
