"""U04c optional ACTION-PROJECTION sidecar; absolutely no dispatch capability.

Triggered only after the U04b perception observer has processed actual accepted
HA changes. Uses the production routing index and DeviceAgent legal_action_mask
on a separate budgeted thread. Never calls process_agent, policy, ActionIntent,
Executor.submit, HA services, or training. Outputs are raw UNTRAINED numbers,
not recommendations or control decisions.
"""
from __future__ import annotations

from collections import OrderedDict
from types import SimpleNamespace
import math
import threading
import time

MAX_PENDING_ENTITIES = 256
MAX_AGENTS_PER_PASS = 4
MAX_PROPOSALS_PER_AGENT = 8
MIN_AGENT_INTERVAL_SECONDS = 2.0
MAX_STATUS_AGENTS = 32


def _sample_proposals(values, limit):
    """Always retain endpoints; cap arbitrary dynamic action cardinality."""
    result = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(number) and number not in result:
            result.append(number)
    if len(result) <= limit:
        return result
    # Even spacing across the existing action_values ordering; retain both ends.
    indices = [round(i * (len(result)-1) / (limit-1)) for i in range(limit)]
    return [result[i] for i in indices]


def _bounded_agent_values(agent, state):
    """Avoid constructing an arbitrarily large option_index action list.

    The production action_values() allocates one Python item per option.
    For an observer on an open-source home this must remain bounded even
    when a third-party integration exposes tens of thousands of options.
    """
    if str(agent.get("target_property") or "") != "option_index":
        from context import action_values
        return _sample_proposals(action_values(agent), MAX_PROPOSALS_PER_AGENT)
    attrs = state.get("attributes") or {}
    options = attrs.get("options") or []
    if not isinstance(options, list) or not options:
        return []
    try:
        lo = max(0, math.ceil(float(agent["min_value"])))
        hi = min(len(options)-1, math.floor(float(agent["max_value"])))
    except (TypeError, ValueError, OverflowError):
        return []
    if lo > hi:
        return []
    if hi - lo + 1 <= MAX_PROPOSALS_PER_AGENT:
        return [float(v) for v in range(lo, hi+1)]
    return [float(lo + round(i * (hi-lo) / (MAX_PROPOSALS_PER_AGENT-1)))
            for i in range(MAX_PROPOSALS_PER_AGENT)]


class _NeuralSnapshot:
    """Read-only adapter used only on the action worker, never shared back."""
    def __init__(self, data):
        import numpy as np
        self.encoder = SimpleNamespace(room_width=len(data["home"]))
        self._home = np.asarray(data["home"], dtype=np.float32)
        self._rooms = {k: np.asarray(v, dtype=np.float32) for k, v in data["rooms"].items()}

    def home(self):
        return self._home.copy()

    def room(self, area):
        import numpy as np
        value = self._rooms.get(str(area))
        return value.copy() if value is not None else np.zeros(self.encoder.room_width, dtype=np.float32)


class UniversalActionObserver(threading.Thread):
    """Bounded observer-only action projection over already-routed targets."""

    daemon = True

    def __init__(self, engine, perception, *, max_pending=MAX_PENDING_ENTITIES,
                 max_agents=MAX_AGENTS_PER_PASS, min_interval=MIN_AGENT_INTERVAL_SECONDS):
        super().__init__(name="homemind-universal-actions")
        self.engine = engine
        self.perception = perception
        self.max_pending = int(max_pending)
        self.max_agents = int(max_agents)
        self.min_interval = float(min_interval)
        if not 1 <= self.max_pending <= 65536 or not 1 <= self.max_agents <= 32 or not 0 <= self.min_interval <= 3600:
            raise ValueError("invalid action-observer resource limits")
        self._cv = threading.Condition()
        self._stop_signal = threading.Event()
        self._pending = OrderedDict()
        self._last_by_agent = {}
        self._last_examples = OrderedDict()
        self._stats = {
            "notifications": 0, "coalesced": 0, "pending_overflow": 0,
            "batches": 0, "agents_evaluated": 0, "actions_evaluated": 0,
            "not_routed": 0, "no_neural_context": 0, "blocked": 0,
            "skipped_cooldown": 0, "errors": 0, "last_error": None,
            "max_batch_ms": 0.0,
        }
        self._scorer = None  # Imported and initialized only on this worker.

    def submit_changed(self, changed):
        """Nonblocking, at most O(batch size) on the perception worker."""
        with self._cv:
            if self._stop_signal.is_set():
                return False
            for entity_id in changed:
                eid = str(entity_id or "")
                if not eid:
                    continue
                self._stats["notifications"] += 1
                if eid in self._pending:
                    self._stats["coalesced"] += 1
                elif len(self._pending) >= self.max_pending:
                    # Never block a real sensor or WS event on diagnostic scoring.
                    self._stats["pending_overflow"] += 1
                    continue
                self._pending[eid] = None
            if self._pending:
                self._cv.notify()
        return True

    def stop(self, timeout=2.0):
        self._stop_signal.set()
        with self._cv:
            self._pending.clear()
            self._cv.notify_all()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout)

    def diagnostics(self):
        with self._cv:
            return {
                "enabled": True,
                "mode": "observer_only_untrained_action_projections",
                "worker_alive": self.is_alive(),
                "queue_pending": len(self._pending),
                "queue_limit": self.max_pending,
                "max_agents_per_pass": self.max_agents,
                "max_actions_per_agent": MAX_PROPOSALS_PER_AGENT,
                "trained": False,
                "control_eligible": False,
                "physical_actions": 0,
                "stats": dict(self._stats),
                "last_examples": list(self._last_examples.values()),
            }

    def _routed(self, changed):
        """Use the existing *cached* production dependency index, no SQLite here."""
        with self.engine.lock:
            impacted = set()
            for eid in changed:
                impacted.update(self.engine.dependency_agents.get(eid, ()))
            agents = []
            for aid in sorted(impacted):
                agent = self.engine.agent_configs.get(aid)
                if agent is None:
                    continue
                entity_id = str(agent.get("target_entity") or "")
                state = self.engine.state_map.get(entity_id)
                if state is None:
                    continue
                agents.append((dict(agent), dict(state)))
        return agents

    def _evaluate_one(self, agent, state):
        from universal_actions import from_device_agent_mask
        from universal_action_scorer import UniversalActionScorer

        service = getattr(getattr(self.engine, "executor", None), "device_agents", None)
        if service is None:
            raise ValueError("production DeviceAgent service missing")
        # DeviceAgent may do SQLite reads: this is the dedicated low-priority
        # worker, NEVER HA ingress, engine.process or the perception worker.
        desc = service.descriptor(agent)
        mask_clock = time.time()
        values = _bounded_agent_values(agent, state)
        if not values:
            return {"agent_id": agent["id"], "evaluated": 0, "blocked": True}
        mask = service.legal_action_mask(agent, state, values, now=mask_clock)
        action_set = from_device_agent_mask(
            agent, desc, mask, state, mask_timestamp=mask_clock
        )
        if not action_set.candidates:
            return {"agent_id": agent["id"], "evaluated": 0, "blocked": True}
        features = self.perception.snapshot_areas(
            {x.area_id for x in action_set.candidates if x.area_id is not None}
        )
        if features is None:
            with self._cv:
                self._stats["no_neural_context"] += 1
            return None
        if self._scorer is None:
            self._scorer = UniversalActionScorer(room_width=len(features["home"]))
        result = self._scorer.evaluate(action_set, _NeuralSnapshot(features))
        if result["trained"] or result["control_eligible"] or any(
            p.get("recommendation") is not None for p in result["projections"]
        ):
            raise RuntimeError("untrained action observer attempted to recommend a control action")
        return {
            "agent_id": agent["id"],
            "target": agent.get("target_entity"),
            "evaluated": result["evaluated"],
            "blocked": False,
            "projection_min": min(x["raw_projection"] for x in result["projections"]),
            "projection_max": max(x["raw_projection"] for x in result["projections"]),
            "at": mask_clock,
            "trained": False,
            "control_eligible": False,
        }

    def run(self):
        while not self._stop_signal.is_set():
            with self._cv:
                if not self._pending and not self._stop_signal.is_set():
                    self._cv.wait(timeout=0.5)
                if self._stop_signal.is_set():
                    break
                changed = tuple(self._pending)
                self._pending.clear()
            if not changed:
                continue
            started = time.perf_counter()
            try:
                agents = self._routed(changed)
                with self._cv:
                    self._stats["batches"] += 1
                    if not agents:
                        self._stats["not_routed"] += len(changed)
                for agent, state in agents[:self.max_agents]:
                    if self._stop_signal.is_set():
                        break
                    aid = str(agent["id"])
                    now_mono = time.monotonic()
                    if now_mono - self._last_by_agent.get(aid, float("-inf")) < self.min_interval:
                        with self._cv:
                            self._stats["skipped_cooldown"] += 1
                        continue
                    self._last_by_agent[aid] = now_mono
                    try:
                        result = self._evaluate_one(agent, state)
                        if result is None:
                            continue
                        with self._cv:
                            if result.get("blocked"):
                                self._stats["blocked"] += 1
                            else:
                                self._stats["agents_evaluated"] += 1
                                self._stats["actions_evaluated"] += result["evaluated"]
                                self._last_examples[aid] = result
                                self._last_examples.move_to_end(aid)
                                if len(self._last_examples) > MAX_STATUS_AGENTS:
                                    self._last_examples.popitem(last=False)
                    except Exception as exc:
                        with self._cv:
                            self._stats["errors"] += 1
                            self._stats["last_error"] = f"{type(exc).__name__}: {exc}"[:240]
                # If fanout exceeds this pass budget, report not sampled instead
                # of doing an unbounded all-target scan on Pi4.
                if len(agents) > self.max_agents:
                    with self._cv:
                        self._stats["not_routed"] += len(agents) - self.max_agents
            except Exception as exc:
                with self._cv:
                    self._stats["errors"] += 1
                    self._stats["last_error"] = f"{type(exc).__name__}: {exc}"[:240]
            finally:
                elapsed = (time.perf_counter() - started) * 1000.0
                with self._cv:
                    self._stats["max_batch_ms"] = max(self._stats["max_batch_ms"], elapsed)
            # CPU budget applies also to device-capability decoding and scorer.
            self._stop_signal.wait(max(0.05, elapsed / 1000.0 * 4.0))


def install(engine):
    """Second explicit opt-in: requires a running U04b perception observer."""
    from settings import OPTIONS
    if not bool(OPTIONS.get("universal_action_observer_enabled", False)):
        return None
    perception = getattr(engine, "universal_observer", None)
    if perception is None or not bool(OPTIONS.get("universal_observer_enabled", False)):
        return None
    previous = getattr(engine, "universal_action_observer", None)
    if previous is not None:
        return previous
    worker = UniversalActionObserver(engine, perception)
    perception.action_observer = worker
    engine.universal_action_observer = worker
    worker.start()
    return worker
