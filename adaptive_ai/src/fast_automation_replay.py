"""Historical automation thresholds for fast binary agents.

Only paired, enabled, direct numeric_state ON/OFF trigger rules become a
deterministic replay clock. The rules do not override policy decisions, teach
supervision, or grant device control. Unknown/ambiguous rules fall back to
observed target transitions.
"""
from __future__ import annotations

import math

from context import archived_state, parse_state_value
from training_budget import TRAINING_BUDGET


def paired_numeric_baseline(selection_meta):
    """Find an unambiguous numeric-state ON/OFF pair for the same HA target."""
    by_sensor = {}
    for automation in (selection_meta or {}).get("automation_baseline_automations") or ():
        if automation.get("enabled") is False:
            continue
        services = {str(v).lower() for v in automation.get("action_services") or ()}
        if any(v.endswith(".turn_on") for v in services):
            arm = "on"
            key = "above"
        elif any(v.endswith(".turn_off") for v in services):
            arm = "off"
            key = "below"
        else:
            continue
        for rule in automation.get("baseline_rules") or ():
            if rule.get("source") != "trigger" or rule.get("kind") != "numeric_state":
                continue
            eid = str(rule.get("entity_id") or "")
            if not eid or rule.get(key) is None:
                continue
            # Avoid treating compound numeric ranges as simple hysteresis.
            if rule.get("below" if arm == "on" else "above") is not None:
                continue
            try:
                threshold = float(rule[key])
                hold = float(rule.get("for_seconds") or 0.0)
            except (ValueError, TypeError):
                continue
            if not (math.isfinite(threshold) and math.isfinite(hold)) or hold < 0:
                continue
            by_sensor.setdefault(eid, {}).setdefault(arm, set()).add((threshold, hold))
    pairs = []
    for eid, rules in by_sensor.items():
        if len(rules.get("on", ())) != 1 or len(rules.get("off", ())) != 1:
            continue
        on_threshold, on_hold = next(iter(rules["on"]))
        off_threshold, off_hold = next(iter(rules["off"]))
        if on_threshold <= off_threshold:
            continue
        pairs.append({
            "sensor": eid, "on_threshold": on_threshold,
            "off_threshold": off_threshold,
            "on_hold": on_hold, "off_hold": off_hold,
        })
    return pairs[0] if len(pairs) == 1 else None


def numeric_threshold_edges(rows, threshold, *, above, hold_seconds=0.0, end_ts=None):
    """Return *completed* threshold crossings, not arbitrary nonzero values.

    First row is a seed, not proof of a crossing. A crossing with a time
    requirement is emitted only after the condition remains true for the
    requested duration. Unknown readings invalidate a pending interval.
    """
    previous = None
    pending = None
    result = []
    for index, row in enumerate(rows):
        if index and index % 128 == 0:
            TRAINING_BUDGET.checkpoint("numeric_baseline_edge_scan")
        raw = parse_state_value(archived_state(row))
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = math.nan
        ts = float(row["ts"])
        if not math.isfinite(value):
            previous = None
            pending = None
            continue
        active = value > threshold if above else value < threshold
        if pending is not None:
            if not active:
                pending = None
            elif ts - pending >= hold_seconds:
                result.append(pending + hold_seconds)
                pending = None
        if previous is not None and not previous and active:
            if hold_seconds <= 0:
                result.append(ts)
            else:
                pending = ts
        previous = active
    if pending is not None and end_ts is not None and float(end_ts) >= pending + hold_seconds:
        result.append(pending + hold_seconds)
    return result


def threshold_event(tracker, sensor, start, end, threshold, *,
                    above, hold_seconds=0.0, latest=False):
    """Read a bounded causal slice using the existing RAM/SQLite replay adapters."""
    start = float(start)
    end = float(end)
    if end <= start:
        return None
    seed = tracker._before(sensor, start, 1)
    events = numeric_threshold_edges(
        list(seed[-1:]) + tracker._base_interval_rows(
            [sensor], start, end, per_entity_limit=None
        ),
        threshold,
        above=above,
        hold_seconds=hold_seconds,
        end_ts=end,
    )
    return (events[-1] if latest else events[0]) if events else None
