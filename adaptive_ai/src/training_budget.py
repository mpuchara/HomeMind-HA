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
        self._interactive_started_at = None
        # Monotonic priority epoch. A worker yields strictly once per burst, then
        # resumes ordinary short duty-cycle slices even while the window remains open.
        # This prevents N tiny checkpoints from turning one realtime event into N sleeps.
        self._interactive_epoch = 0
        self._interactive_class = None
        # Continuous HA sensor traffic must not starve offline training forever. A burst
        # may keep strict realtime priority only for a bounded interval, followed by a
        # short cooldown in which the training worker is guaranteed a scheduling slice.
        self._interactive_max_burst_seconds = 1.25
        self._interactive_cooldown_seconds = 0.10
        # Realtime HA traffic gets a much shorter burst contract than explicit user
        # actions such as Correct.  The trainer already yields after <=35 ms work slices,
        # so a full 1.25 s blackout after every state_changed event needlessly starves
        # replay in active homes.  These values are configurable by the Pi runtime.
        self._realtime_max_burst_seconds = 0.45
        self._realtime_cooldown_seconds = 0.20
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
            "interactive_priority_epochs": 0,
            "interactive_epoch_escalations": 0,
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
        realtime_max_burst_seconds=None,
        realtime_cooldown_seconds=None,
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
            if realtime_max_burst_seconds is not None:
                self._realtime_max_burst_seconds = self._clamp(
                    float(realtime_max_burst_seconds), 0.15, 1.25
                )
            if realtime_cooldown_seconds is not None:
                self._realtime_cooldown_seconds = self._clamp(
                    float(realtime_cooldown_seconds), 0.05, 1.0
                )
        return self.snapshot()

    def _eligible(self, thread_name=None):
        name = str(thread_name or threading.current_thread().name)
        return any(name.startswith(prefix) for prefix in self._thread_prefixes)

    def request_interactive_window(self, seconds=0.75, reason="interactive"):
        """Temporarily give HTTP/realtime inference priority over training.

        Priority is represented as a bounded *burst epoch*.  A training worker performs
        one strict scheduler yield per epoch, then continues with the normal short
        duty-cycle slices.  Repeated state_changed events may extend the same epoch up to
        its cap, but they do not create one extra sleep at every tiny replay checkpoint.

        This keeps realtime responsive while guaranteeing offline work can make progress
        under sustained 2/4/10 Hz sensor traffic.
        """
        duration = self._clamp(float(seconds), 0.05, 3.0)
        reason = str(reason or "interactive")
        realtime_reason = reason in {"ha_state_changed", "realtime_inference"}
        priority_class = "realtime" if realtime_reason else "user"
        now = self._clock()
        with self._lock:
            max_burst = (
                float(self._realtime_max_burst_seconds)
                if realtime_reason else float(self._interactive_max_burst_seconds)
            )
            cooldown = (
                float(self._realtime_cooldown_seconds)
                if realtime_reason else float(self._interactive_cooldown_seconds)
            )
            if (
                now < float(self._interactive_cooldown_until or 0.0)
                and now >= float(self._interactive_until or 0.0)
            ):
                self._stats["interactive_requests_suppressed"] += 1
                return float(self._interactive_until or 0.0)

            active = now < float(self._interactive_until or 0.0)
            # Escalating from background realtime traffic to an explicit user action
            # deserves a fresh immediate yield, but ordinary event extensions stay in the
            # same epoch and therefore cannot multiply sleeps by checkpoint count.
            escalated = active and self._interactive_class == "realtime" and priority_class == "user"
            if not active or escalated:
                self._interactive_started_at = now
                self._interactive_epoch += 1
                self._interactive_class = priority_class
                self._stats["interactive_priority_epochs"] += 1
                if escalated:
                    self._stats["interactive_epoch_escalations"] += 1
                until = now + min(duration, max_burst)
            else:
                started = float(
                    self._interactive_started_at
                    if self._interactive_started_at is not None else now
                )
                cap = started + max_burst
                until = min(
                    cap,
                    max(float(self._interactive_until or 0.0), now + duration),
                )

            self._interactive_until = until
            self._interactive_cooldown_until = max(
                float(self._interactive_cooldown_until or 0.0),
                until + cooldown,
            )
            self._interactive_reason = reason
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
        """Yield at most once per priority burst, then enforce the normal work quantum.

        The old implementation slept again at every checkpoint while an interactive
        window remained open.  Fine-grained replay loops could therefore pay hundreds of
        milliseconds repeatedly for one burst.  The epoch contract below separates
        "give realtime a turn now" from ordinary wall-clock duty-cycle accounting.
        """
        if not self._eligible(thread_name):
            return 0.0

        now = self._clock()
        with self._lock:
            interactive_until = float(self._interactive_until or 0.0)
            interactive_reason = self._interactive_reason
            interactive_epoch = int(self._interactive_epoch)
            max_slice = float(self._max_slice_seconds)
            duty = float(self._duty_cycle)
            max_sleep = float(self._max_sleep_seconds)

        if now < interactive_until:
            served_epoch = int(getattr(self._local, "interactive_epoch", -1))
            if served_epoch != interactive_epoch:
                # One explicit scheduler hand-off per burst.  The same quantum used to
                # bound training work also bounds this pause, so priority cost is not a
                # function of how many micro-checkpoints the replay code contains.
                remaining = max(0.0, interactive_until - now)
                pause = min(max_slice, remaining)
                if pause > 0.0:
                    self._sleep(pause)
                with self._lock:
                    self._stats["interactive_preemptions"] += 1
                    self._stats["interactive_sleep_seconds"] += pause
                    self._stats["last_label"] = (
                        "interactive:" + str(interactive_reason or "priority")
                    )
                self._local.interactive_epoch = interactive_epoch
                self._local.slice_started = self._clock()
                self._local.active = True
                return pause
            # This burst already received its strict yield. Continue below and account
            # actual worker time normally; max_slice still bounds continuous work.

        started = getattr(self._local, "slice_started", None)
        if started is None:
            self._local.slice_started = now
            self._local.active = True
            return 0.0

        active = max(0.0, now - float(started))

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
            "interactive_priority_epochs": int(stats["interactive_priority_epochs"]),
            "interactive_epoch_escalations": int(stats["interactive_epoch_escalations"]),
            "interactive_yield_quantum_ms": round(max_slice * 1000.0, 1),
            "training_wall_duty_cycle_target": duty,
            "effective_training_wall_duty_cycle": (
                None if effective is None else round(effective, 4)
            ),
            "interactive_max_burst_seconds": float(self._interactive_max_burst_seconds),
            "interactive_cooldown_seconds": float(self._interactive_cooldown_seconds),
            "realtime_max_burst_seconds": float(self._realtime_max_burst_seconds),
            "realtime_cooldown_seconds": float(self._realtime_cooldown_seconds),
            "last_checkpoint": stats["last_label"],
        }


TRAINING_BUDGET = CooperativeTrainingBudget()
