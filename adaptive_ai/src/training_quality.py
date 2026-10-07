"""Bounded causal preference evidence, separate from features and dispatch.

Outcome facts may use later observations. Pattern queries may only use outcomes
already available at the query timestamp. No function creates a physical action.
"""
from collections import OrderedDict, deque
import math

from context import context_scalar, occupancy_state_bool
from training_evidence import USER_EVIDENCE_ORIGINS
from radar_context import radar_family, radar_role

CONTRACT = "conditional_light_training_quality_v1"


def sensor_snapshot(agent, entities, states, registry, *, local_radars=()):
    target_area = (registry.get(agent.get("target_entity")) or {}).get("area_id")
    signature, active, absent, reliable, radar = [], [], [], [], []
    unresolved_radar_devices = set()
    def radar_group(eid):
        reg = registry.get(eid) or {}
        if reg.get('device_id'):
            return reg['device_id']
        family = radar_family(eid)
        return ('radar_family', family, reg.get('area_id')) if family else None
    for eid in sorted(set(entities))[:64]:
        domain = eid.split(".", 1)[0]
        if eid == agent.get("target_entity") or domain not in (
            "binary_sensor", "sensor", "person", "device_tracker"
        ):
            continue
        state = states.get(eid) or {}
        raw = str(state.get("state") or "unknown").lower()
        available = raw not in ("unknown", "unavailable", "none", "")
        value = context_scalar(eid, state, agent) if available else None
        bucket = None if value is None or not math.isfinite(value) else round(value * 4) / 4
        signature.append((eid, bucket))
        same_area = bool(target_area and (registry.get(eid) or {}).get("area_id") == target_area)
        device_class = str((state.get("attributes") or {}).get("device_class") or "")
        role = radar_role(eid, state)
        source_area = (registry.get(eid) or {}).get("area_id")
        baseline_numeric = (eid in local_radars and role in ("energy", "distance")
                            and not (source_area and target_area and source_area != target_area))
        # The exact automation source may have no registry area. This preserves
        # uncertainty within the recorded ON dwell; it never asserts occupancy.
        if (same_area or baseline_numeric) and role:
            radar.append(eid)
            if role in ("energy", "distance") and available:
                try:
                    measurement = float(raw)
                except (ValueError, TypeError):
                    measurement = 0.0
                device = radar_group(eid)
                if device and math.isfinite(measurement) and measurement > 0:
                    unresolved_radar_devices.add(device)
        if same_area and domain == "binary_sensor" and (device_class in ("occupancy", "presence", "motion") or role in ("presence", "still", "moving")):
            present = occupancy_state_bool(state) if available else None
            if present is True:
                active.append(eid)
            # Motion OFF never proves vacancy. Only persistent occupancy sources can.
            if role in ("presence", "still") or (role != "moving" and device_class in ("occupancy", "presence")):
                if present is not None:
                    reliable.append(eid)
                # Still OFF alone is compatible with a moving occupant. Require
                # its same-device moving channel to be known OFF as well.
                device = radar_group(eid)
                moving_off = bool(device) and any(
                    radar_role(other, states.get(other)) == "moving"
                    and radar_group(other) == device
                    and occupancy_state_bool(states.get(other) or {}) is False
                    for other in entities
                )
                if present is False and (role != "still" or moving_off):
                    absent.append(eid)
    # The radar's binary flags share its configured thresholds; they are not
    # independent confirmation of absence while numeric channels still report a
    # signal. Positive energy can also be empty-room background, so it does not
    # create presence. Keep this unresolved and learn the observed state/weights.
    absent = [eid for eid in absent if
              radar_group(eid) not in unresolved_radar_devices]
    return {"signature": signature, "active": active, "absent": absent, "reliable": reliable, "radar": radar}


def light_dwell_reward(action, reward, before, after, positive_during, *,
                       explicit_user=False, observation_complete=True):
    """Return utility and attribution; unknown sensing never manufactures a penalty."""
    if explicit_user or reward <= 0:
        return reward, "explicit_or_rejected"
    reliable = set(before.get("reliable") or ())
    if (reliable and reliable <= set(before.get("absent") or ())
            and before.get("active") and not positive_during):
        return 0.0, "conflicting_presence_unknown"
    if float(action) < .5 and before.get("active"):
        return -1.0, "premature_off_confirmed_presence"
    complete_absence = bool(
        observation_complete and reliable and reliable == set(after.get("reliable") or ())
        and reliable <= set(before.get("absent") or ())
        and reliable <= set(after.get("absent") or ())
        and not before.get("active") and not after.get("active")
        and not positive_during
    )
    if float(action) >= .5 and complete_absence:
        return -.6, "false_on_verified_vacancy"
    if before.get("active") or positive_during:
        return reward, "confirmed_use" if float(action) >= .5 else "initial_off"
    return reward, "unknown_outcome"


class ConditionalPatternMemory:
    """Small checkpointed table with at most one vote per action/day/context.

    Query includes only completed outcomes available strictly before prediction.
    Minorities are softened, never discarded globally; manual exceptions retain weight.
    """
    def __init__(self, action_count, state=None, *, max_contexts=256, max_events=64, min_days=3):
        self.action_count = int(action_count)
        self.max_contexts = max(1, min(1024, int(max_contexts)))
        self.max_events = max(1, min(128, int(max_events)))
        self.min_days = max(1, min(14, int(min_days)))
        self.rows = OrderedDict()
        self.queries = self.downweighted = self.manual_preserved = 0
        state = state if isinstance(state, dict) else {}
        if state.get("contract") == CONTRACT and state.get("actions") == self.action_count:
            rows = state.get("rows") or []
            if isinstance(rows, list):
                for row in rows[-self.max_contexts:]:
                    if not isinstance(row, (list, tuple)) or len(row) != 2:
                        continue
                    key, events = row
                    if not isinstance(key, str) or len(key) > 8192 or not isinstance(events, list):
                        continue
                    clean = []
                    for event in events[-self.max_events:]:
                        try:
                            available, day, idx = event
                            available, day, idx = float(available), int(day), int(idx)
                            if math.isfinite(available) and 0 <= idx < self.action_count:
                                clean.append([available, day, idx])
                        except (TypeError, ValueError, OverflowError):
                            continue
                    self.rows[key] = deque(clean, maxlen=self.max_events)

    @staticmethod
    def key(signature):
        import json
        return json.dumps(signature, separators=(",", ":")) if signature and any(
            value is not None for _, value in signature
        ) else None

    def factor(self, signature, action, ts, origin):
        self.queries += 1
        if origin in USER_EVIDENCE_ORIGINS:
            self.manual_preserved += 1
            return 1.0
        key = self.key(signature)
        votes = {(int(day), int(idx)) for available, day, idx in self.rows.get(key, ()) if float(available) < float(ts)}
        days = {day for day, _ in votes}
        if len(days) < self.min_days:
            return 1.0
        matching = sum(idx == int(action) for _, idx in votes)
        probability = (matching + 1) / (len(votes) + self.action_count)
        factor = max(.2, min(1.0, probability * self.action_count))
        self.downweighted += int(factor < 1)
        return factor

    def observe(self, signature, action, sample_ts, available_ts, reward):
        key = self.key(signature)
        if not key or reward <= 0:
            return
        events = self.rows.setdefault(key, deque(maxlen=self.max_events))
        day = int(float(sample_ts) // 86400)
        # Retain the earliest available completed outcome for this daily vote.
        if not any(int(d) == day and int(a) == int(action) for _, d, a in events):
            events.append([float(available_ts), day, int(action)])
        self.rows.move_to_end(key)
        while len(self.rows) > self.max_contexts:
            self.rows.popitem(last=False)

    def export(self):
        return {"contract": CONTRACT, "actions": self.action_count,
                "rows": [[key, list(events)] for key, events in self.rows.items()]}

    def status(self):
        return {"contract": CONTRACT, "contexts": len(self.rows), "queries": self.queries,
                "downweighted": self.downweighted, "manual_preserved": self.manual_preserved,
                "minimum_independent_days": self.min_days, "minority_weight_floor": .2}
