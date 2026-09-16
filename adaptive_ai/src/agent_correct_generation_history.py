"""Direct-parent observed history for the multi-generation Correct UI.

The chart is evidence-only: it renders decisions that were actually observed from the
selected generation and, for Candidate generations, its direct parent. It never replays
the current policy into historical state.
"""
import time
from urllib.parse import parse_qs, unquote, urlsplit

from agent_candidate_lineage import _row as lineage_row
from agent_workflow_actions import _resolve_generation
from context import archived_state, target_value
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


def _physical_current_points(manager, agent, start, end):
    """Return the factual target-state history, independent of Candidate inference cadence.

    Candidate decision rows are sparse observations made when a generation actually runs.
    They are valid evidence for Desired, but they are not the source of truth for physical
    Current. Current must come from the archived Home Assistant target entity itself so the
    chart and the point inspector report the same real device state at every timestamp.
    """
    entity_id = str(agent["target_entity"])
    prop = str(agent["target_property"])
    start, end = float(start), float(end)
    rows = []
    with manager.store.conn() as c:
        seed = c.execute(
            "SELECT * FROM entity_history WHERE entity_id=? AND ts<=? ORDER BY ts DESC,id DESC LIMIT 1",
            (entity_id, start),
        ).fetchone()
        if seed:
            rows.append(dict(seed))
    rows.extend(manager.store.archive_iter(start, end, [entity_id], chunk_size=512))

    out = []
    last = None
    have_last = False
    for row in rows:
        try:
            value = target_value(archived_state(dict(row)), prop)
            if value is None:
                continue
            value = float(value)
            ts = max(start, min(end, float(row["ts"])))
        except (KeyError, TypeError, ValueError):
            continue
        if have_last and abs(float(last) - value) <= 1e-9:
            continue
        out.append({"ts": ts, "value": value})
        last = value
        have_last = True

    if out and out[-1]["ts"] < end:
        out.append({"ts": end, "value": out[-1]["value"]})
    return out


def _active_labels(manager, agent):
    return [
        row for row in manager.engine.rl_teaching.labels(agent["id"])
        if str(row.get("fingerprint")) == str(rl_fingerprint(agent)) and row.get("undone_ts") is None
    ]


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
    generation, agent = _resolve_generation(manager, ref)
    start, end = float(start), float(end)
    if end <= start or end - start > 31 * 86400:
        raise ValueError("Choose a history range from 1 second to 31 days")
    payload = _base_payload(generation, agent, start, end)
    labels = _active_labels(manager, agent)

    if generation.get("generation_type") == "live":
        observed = legacy_history(ref, start, end)
        points = list(observed.get("points") or [])
        payload.update({
            "chart_mode": "live",
            "parent_generation_id": None,
            "series_order": ["current", "live_desired", "correct"],
            "series": {
                "current": {"label": "Current", "points": _physical_current_points(manager, agent, start, end)},
                "live_desired": {"label": "Live Desired", "points": _values(points, "desired")},
            },
            "labels": labels,
            "points": points,
            "gaps": list(observed.get("gaps") or []),
            "current_source": "home_assistant_entity_history",
            "current_semantics": "factual physical target state recorded from Home Assistant history",
            "desired_source": observed.get("desired_source") or "observed_live_generation_runtime",
            "desired_semantics": "Live Desired actually observed from this generation",
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
    payload.update({
        "chart_mode": "candidate_vs_parent",
        "parent_generation_id": parent["generation_id"],
        "parent_generation_number": int(parent["generation_number"]),
        "parent_generation_type": parent["generation_type"],
        "series_order": ["current", "parent_desired", "candidate_desired", "correct"],
        "series": {
            "current": {"label": "Current", "points": _physical_current_points(manager, agent, start, end)},
            "parent_desired": {"label": _generation_label(parent), "points": _values(parent_points, "desired")},
            "candidate_desired": {"label": _generation_label(generation), "points": _values(child_points, "desired")},
        },
        "labels": labels,
        # Keep the selected generation's observed rows available for backwards-compatible
        # consumers. No values below are synthesized from a policy replay.
        "points": child_points,
        "gaps": list(child_history.get("gaps") or []),
        "parent_gaps": list(parent_history.get("gaps") or []),
        "current_source": "home_assistant_entity_history",
        "current_semantics": "factual physical target state recorded from Home Assistant history",
        "desired_source": "observed_candidate_generation_shadow_runtime",
        "parent_desired_source": parent_history.get("desired_source") or "observed_generation_runtime",
        "desired_semantics": "Candidate Desired actually observed from the selected generation",
        "parent_desired_semantics": "Parent Desired actually observed from the direct parent generation",
    })
    return payload


def build_correct_point(manager, ref, timestamp, legacy_point):
    generation, _ = _resolve_generation(manager, ref)
    point = dict(legacy_point(ref, float(timestamp)))
    point["chart_contract"] = CHART_CONTRACT
    point["policy_replay_used_for_desired"] = False
    point["generation_number"] = int(generation["generation_number"])
    point["generation_type"] = generation["generation_type"]

    if generation.get("generation_type") == "live":
        point["live_desired"] = point.get("desired")
        point["live_desired_label"] = "Live Desired"
        point["parent_generation_id"] = None
        return point

    parent_id = generation.get("parent_generation_id")
    parent = lineage_row(manager.store, generation_id=parent_id) if parent_id else None
    if not parent:
        raise ValueError("Candidate direct parent generation not found")
    observed_parent = manager.generation_decision_at(parent["generation_id"], float(timestamp))
    point["parent_generation_id"] = parent["generation_id"]
    point["parent_generation_number"] = int(parent["generation_number"])
    point["parent_generation_type"] = parent["generation_type"]
    point["parent_desired"] = None if observed_parent is None else observed_parent.get("desired")
    point["parent_confidence"] = None if observed_parent is None else observed_parent.get("confidence")
    point["parent_desired_label"] = _generation_label(parent)
    point["candidate_desired"] = point.get("desired")
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
