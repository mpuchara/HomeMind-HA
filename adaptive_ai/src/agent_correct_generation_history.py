"""Direct-parent observed history for the multi-generation Correct UI.

The chart is evidence-only: it renders decisions that were actually observed from the
selected generation and, for Candidate generations, its direct parent. It never replays
the current policy into historical state.
"""
from bisect import bisect_right
import time
from urllib.parse import parse_qs, unquote, urlsplit

from agent_candidate_lineage import _row as lineage_row
from agent_workflow_actions import _resolve_generation
from context import archived_state, target_value
from teach_observed_history import DESIRED_STALE_SECONDS, _desired_at, _recorded_rows, _ensure_table
from training_budget import TRAINING_BUDGET
from teaching_rl import fingerprint as rl_fingerprint


CHART_CONTRACT = "observed_direct_parent_vs_child_no_policy_replay"


def _generation_label(generation):
    number = int(generation.get("generation_number") or 0)
    if generation.get("generation_type") == "live":
        return f"Live G{number} Desired"
    return f"Candidate G{number} Desired"


def _values(rows, field):
    out = []
    for row in rows or []:
        value = row.get(field)
        if value is None:
            continue
        try:
            out.append({"ts": float(row["ts"]), "value": float(value)})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _active_labels(manager, agent):
    return [
        row for row in manager.engine.rl_teaching.labels(agent["id"])
        if str(row.get("fingerprint")) == str(rl_fingerprint(agent)) and row.get("undone_ts") is None
    ]


def _current_rows(manager, agent, start, end):
    """Observed physical target-state curve projected across the selected chart range.

    Recorder stores state changes, not a sample for every second. A seed row can therefore
    be older than the visible range even though its value is still the real Current at
    `start`. Project that seed onto the left edge and extend the last known value to the
    right edge so a stable OFF/ON interval remains visible instead of disappearing.
    """
    entity_id = str(agent["target_entity"])
    start = float(start); end = float(end)
    with manager.store.conn() as c:
        seed = c.execute(
            "SELECT * FROM entity_history WHERE entity_id=? AND ts<=? "
            "ORDER BY ts DESC,id DESC LIMIT 1",
            (entity_id, start),
        ).fetchone()
        rows = [
            dict(row) for row in c.execute(
                "SELECT * FROM entity_history WHERE entity_id=? AND ts>? AND ts<=? "
                "ORDER BY ts,id",
                (entity_id, start, end),
            ).fetchall()
        ]

    out = []
    last_value = None
    if seed:
        last_value = target_value(archived_state(dict(seed)), agent["target_property"])
        if last_value is not None:
            out.append({"ts": start, "current": last_value, "projected": True})
    for row in rows:
        value = target_value(archived_state(row), agent["target_property"])
        if value is None:
            continue
        last_value = value
        point = {"ts": float(row["ts"]), "current": value}
        if out and abs(float(out[-1]["ts"]) - float(point["ts"])) < 1e-6:
            out[-1] = point
        else:
            out.append(point)
    if last_value is not None and end >= start:
        if not out or abs(float(out[-1]["ts"]) - end) > 1e-6:
            out.append({"ts": end, "current": last_value, "projected": True})
    return out


def _current_at(rows, timestamp):
    if not rows:
        return None
    times = [float(row["ts"]) for row in rows]
    idx = bisect_right(times, float(timestamp)) - 1
    return None if idx < 0 else rows[idx].get("current")


def _live_observed_history(manager, generation, agent, start, end):
    """Build Correct history from two narrow indexed streams, with zero policy replay."""
    _ensure_table(manager.store)
    decisions = _recorded_rows(
        manager.store, manager.engine, agent["id"], float(start), float(end)
    )
    currents = _current_rows(manager, agent, start, end)

    times = {float(start), float(end)}
    times.update(float(row["ts"]) for row in decisions)
    times.update(float(row["ts"]) for row in currents)
    for row in decisions:
        cutoff = float(row["ts"]) + float(DESIRED_STALE_SECONDS) + 1e-4
        if float(start) <= cutoff <= float(end):
            times.add(cutoff)

    points = []
    gaps = []
    gap_start = None
    for ts in sorted(times):
        desired = _desired_at(decisions, ts)
        current = _current_at(currents, ts)
        points.append({"ts": ts, "current": current, "desired": desired})
        if desired is None and gap_start is None:
            gap_start = ts
        elif desired is not None and gap_start is not None:
            gaps.append({"start": gap_start, "end": ts})
            gap_start = None
    if gap_start is not None:
        gaps.append({"start": gap_start, "end": float(end)})

    return {
        "points": points,
        "gaps": gaps,
        "desired_source": "observed_runtime_decision_history",
        "desired_semantics": "Live Desired actually observed from this generation",
        "stale_after_seconds": DESIRED_STALE_SECONDS,
        "policy_replay_used": False,
    }


def _observed_generation_at(manager, generation, timestamp):
    if callable(getattr(manager, "generation_decision_at", None)):
        row = manager.generation_decision_at(generation["generation_id"], float(timestamp))
        if row:
            return dict(row)
    if generation.get("generation_type") == "live":
        _ensure_table(manager.store)
        rows = _recorded_rows(
            manager.store, manager.engine, generation["agent_id"],
            float(timestamp), float(timestamp),
        )
        desired = _desired_at(rows, float(timestamp))
        if desired is not None:
            return {"ts": float(timestamp), "desired": desired, "confidence": None}
    return None


def _base_payload(generation, agent, start, end):
    return {
        "root_agent_id": generation["root_agent_id"],
        "generation_id": generation["generation_id"],
        "generation_number": int(generation["generation_number"]),
        "generation_type": generation["generation_type"],
        "start": float(start), "end": float(end),
        "agent": {
            "id": agent["id"], "name": agent.get("name"),
            "target_entity": agent["target_entity"], "target_property": agent["target_property"],
            "min_value": agent["min_value"], "max_value": agent["max_value"],
            "deadband": agent.get("deadband"),
        },
        "chart_contract": CHART_CONTRACT,
        "policy_replay_used": False,
        "direct_parent_only": True,
    }


def build_correct_history(manager, ref, start, end, legacy_history):
    TRAINING_BUDGET.request_interactive_window(1.0, reason="correct_history")
    generation, agent = _resolve_generation(manager, ref)
    start, end = float(start), float(end)
    if end <= start or end - start > 31 * 86400:
        raise ValueError("Choose a history range from 1 second to 31 days")
    payload = _base_payload(generation, agent, start, end)
    labels = _active_labels(manager, agent)

    if generation.get("generation_type") == "live":
        observed = _live_observed_history(manager, generation, agent, start, end)
        points = list(observed.get("points") or [])
        payload.update({
            "chart_mode": "live",
            "parent_generation_id": None,
            "series_order": ["current", "live_desired", "correct"],
            "series": {
                "current": {"label": "Current", "points": _values(points, "current")},
                "live_desired": {"label": "Live Desired", "points": _values(points, "desired")},
            },
            "labels": labels,
            "points": points,
            "gaps": list(observed.get("gaps") or []),
            "stale_after_seconds": observed.get("stale_after_seconds"),
            "desired_source": observed.get("desired_source"),
            "desired_semantics": observed.get("desired_semantics"),
        })
        return payload

    parent_id = generation.get("parent_generation_id")
    parent = lineage_row(manager.store, generation_id=parent_id) if parent_id else None
    if not parent:
        raise ValueError("Candidate direct parent generation not found")

    child_history = manager.generation_history(generation["generation_id"], start, end)
    parent_history = manager.generation_history(parent["generation_id"], start, end)
    child_points = list(child_history.get("points") or [])
    parent_points = list(parent_history.get("points") or [])
    # Current is physical target history, not Candidate observation history. Candidate
    # inference may legitimately have gaps; the real device state must remain visible
    # across those gaps so Correct can still anchor the user's correction in time.
    current_points = _current_rows(manager, agent, start, end)
    payload.update({
        "chart_mode": "candidate_vs_parent",
        "parent_generation_id": parent["generation_id"],
        "parent_generation_number": int(parent["generation_number"]),
        "parent_generation_type": parent["generation_type"],
        "series_order": ["current", "parent_desired", "candidate_desired", "correct"],
        "series": {
            "current": {"label": "Current", "points": _values(current_points, "current")},
            "parent_desired": {"label": _generation_label(parent), "points": _values(parent_points, "desired")},
            "candidate_desired": {"label": _generation_label(generation), "points": _values(child_points, "desired")},
        },
        "labels": labels,
        # Keep the selected generation's observed rows available for backwards-compatible
        # consumers. No values below are synthesized from a policy replay.
        "points": child_points,
        "gaps": list(child_history.get("gaps") or []),
        "parent_gaps": list(parent_history.get("gaps") or []),
        "desired_source": "observed_candidate_generation_shadow_runtime",
        "parent_desired_source": parent_history.get("desired_source") or "observed_generation_runtime",
        "desired_semantics": "Candidate Desired actually observed from the selected generation",
        "parent_desired_semantics": "Parent Desired actually observed from the direct parent generation",
    })
    return payload


def build_correct_point(manager, ref, timestamp, legacy_point):
    TRAINING_BUDGET.request_interactive_window(1.0, reason="correct_point")
    generation, agent = _resolve_generation(manager, ref)
    timestamp = float(timestamp)
    current_rows = _current_rows(manager, agent, timestamp, timestamp)
    current = _current_at(current_rows, timestamp)
    observed = _observed_generation_at(manager, generation, timestamp)
    desired = None if observed is None else observed.get("desired")
    confidence = None if observed is None else observed.get("confidence")

    point = {
        "ts": timestamp,
        "current": current,
        "desired": desired,
        "confidence": confidence,
        "context_complete": bool(current is not None and desired is not None),
        "gap": desired is None,
        "desired_source": "observed_generation_runtime",
        "chart_contract": CHART_CONTRACT,
        "policy_replay_used_for_desired": False,
        "generation_number": int(generation["generation_number"]),
        "generation_type": generation["generation_type"],
    }

    if generation.get("generation_type") == "live":
        point["live_desired"] = desired
        point["live_desired_label"] = "Live Desired"
        point["parent_generation_id"] = None
        return point

    parent_id = generation.get("parent_generation_id")
    parent = lineage_row(manager.store, generation_id=parent_id) if parent_id else None
    if not parent:
        raise ValueError("Candidate direct parent generation not found")
    observed_parent = _observed_generation_at(manager, parent, timestamp)
    point["parent_generation_id"] = parent["generation_id"]
    point["parent_generation_number"] = int(parent["generation_number"])
    point["parent_generation_type"] = parent["generation_type"]
    point["parent_desired"] = (
        None if observed_parent is None else observed_parent.get("desired")
    )
    point["parent_confidence"] = (
        None if observed_parent is None else observed_parent.get("confidence")
    )
    point["parent_desired_label"] = _generation_label(parent)
    point["candidate_desired"] = desired
    point["candidate_desired_label"] = _generation_label(generation)
    point["parent_policy_replay_used"] = False
    return point


def install(manager):
    if getattr(manager, "_correct_generation_history_installed", False):
        return manager
    if not callable(getattr(manager, "workflow_correct_history", None)):
        raise RuntimeError("Generation workflow must be installed before Correct history")

    original_history = manager.workflow_correct_history
    original_point = manager.workflow_correct_point
    handler = manager.core.Handler
    original_get = handler.do_GET

    def correct_history(ref, start, end):
        return build_correct_history(manager, ref, start, end, original_history)

    def correct_point(ref, timestamp):
        return build_correct_point(manager, ref, timestamp, original_point)

    def do_get(http):
        parsed = urlsplit(http.path)
        tokens = parsed.path.strip("/").split("/")
        if len(tokens) == 4 and tokens[:2] == ["api", "agent-workflow"] and tokens[3] in ("correct-history", "correct-point"):
            if not http.require_trusted_client() or not http.require_runtime():
                return
            ref = unquote(tokens[2])
            try:
                query = parse_qs(parsed.query)
                if tokens[3] == "correct-history":
                    now = time.time()
                    start = float((query.get("start") or [now - 600])[0])
                    end = float((query.get("end") or [now])[0])
                    return http.send_json(200, correct_history(ref, start, end))
                ts = float((query.get("ts") or [time.time()])[0])
                return http.send_json(200, correct_point(ref, ts))
            except (TypeError, ValueError) as exc:
                return http.send_json(404, {"error": str(exc)})
        return original_get(http)

    manager.workflow_correct_history = correct_history
    manager.workflow_correct_point = correct_point
    handler.do_GET = do_get
    manager._correct_generation_history_installed = True
    manager.correct_history_contract = CHART_CONTRACT
    return manager
