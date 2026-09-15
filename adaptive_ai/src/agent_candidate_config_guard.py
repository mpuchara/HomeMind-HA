"""Keep Candidate generations bound to the Live agent configuration they are testing.

A Candidate may train for a long time while the user edits the Live agent.  Comparing or
promoting a model built for an old action range/context would make the A/B evidence
invalid.  This guard serializes Live config edits with promotion, marks an existing
Candidate dirty when policy-relevant settings change, and synchronizes the hidden
surrogate immediately before its next rebuild.
"""
import json
import time

from agent_candidates import _blank_comparison, is_candidate


_POLICY_FIELDS = (
    "min_value",
    "max_value",
    "confidence_threshold",
    "deadband",
    "action_interval",
    "exploration_step",
    "exploration_interval",
    "input_entities",
)
_TARGET_FIELDS = ("target_entity", "target_property")


def _normalized(agent):
    if not agent:
        return None
    out = {}
    for key in _TARGET_FIELDS + _POLICY_FIELDS:
        value = agent.get(key)
        if key == "input_entities":
            value = tuple(str(x) for x in (value or ["*"]))
        elif key not in _TARGET_FIELDS and value is not None:
            value = float(value)
        elif value is not None:
            value = str(value)
        out[key] = value
    return out


def config_signature(agent):
    normalized = _normalized(agent)
    if normalized is None:
        return None
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def _sync_payload(parent, candidate):
    payload = {}
    for key in _POLICY_FIELDS:
        left = _normalized(parent).get(key)
        right = _normalized(candidate).get(key)
        if left != right:
            payload[key] = list(parent.get(key) or ["*"]) if key == "input_entities" else parent.get(key)
    return payload


def install(manager):
    if getattr(manager, "_candidate_config_guard", False):
        return manager

    store = manager.store
    original_update = store.update_agent
    original_start = manager._start_build
    original_status = manager.status
    original_promote = manager.promote

    def update_agent(agent_id, payload):
        # Candidate-internal updates are performed by the original Store method and must
        # never recursively dirty their own parent generation.
        if is_candidate(store, agent_id):
            return original_update(agent_id, payload)
        with manager.lock:
            before = store.get_agent_config(str(agent_id))
            result = original_update(agent_id, payload)
            after = store.get_agent_config(str(agent_id))
            if config_signature(before) == config_signature(after):
                return result
            row = manager._candidate_row(agent_id)
            if not row or str(row.get("state") or "") == "discarding":
                return result
            now = time.time()
            next_state = "building" if str(row.get("state")) == "building" else "queued"
            with store.lock, store.conn() as c:
                c.execute(
                    """UPDATE agent_candidates SET state=?,reason='config_change',dirty=1,queued_ts=?,
                       comparison_json=?,last_error=NULL,updated_ts=? WHERE parent_agent_id=?""",
                    (next_state, now, json.dumps(_blank_comparison()), now, str(agent_id)),
                )
            manager.runtime.pop(str(agent_id), None)
            store.event(agent_id, "info", "agent_candidate_config_changed",
                        "Live agent configuration changed; Candidate evidence was invalidated and a fresh rebuild is required",
                        {"candidate_id": row.get("candidate_id")})
            manager.wake_event.set()
            return result

    def start_build(row):
        parent = store.get_agent_config(row["parent_agent_id"])
        candidate = store.get_agent_config(row["candidate_id"])
        if parent and candidate:
            parent_norm = _normalized(parent)
            candidate_norm = _normalized(candidate)
            if any(parent_norm.get(k) != candidate_norm.get(k) for k in _TARGET_FIELDS):
                manager._fail(row, "Live target changed; discard this Candidate and create a fresh generation")
                return True
            payload = _sync_payload(parent, candidate)
            if payload:
                original_update(candidate["id"], payload)
                manager.engine.models.pop(candidate["id"], None)
                manager.engine.runtime.pop(candidate["id"], None)
                store.event(row["parent_agent_id"], "info", "agent_candidate_config_synced",
                            "Candidate configuration synchronized with Live before rebuild",
                            {"candidate_id": candidate["id"], "fields": sorted(payload)})
        return original_start(manager._candidate_row(row["parent_agent_id"]) or row)

    def status(parent_id):
        result = original_status(parent_id)
        if not result:
            return result
        parent = store.get_agent_config(result["parent_agent_id"])
        candidate = store.get_agent_config(result["candidate_id"])
        matches = config_signature(parent) == config_signature(candidate)
        result["candidate_config_matches_live"] = bool(matches)
        result["config_stale"] = not bool(matches)
        if not matches:
            result["promotable"] = False
            if isinstance(result.get("comparison"), dict):
                result["comparison"]["promotable"] = False
        return result

    def promote(parent_id):
        # The same lock is used by Store.update_agent above, so a Live config edit cannot
        # slip between this check and the atomic model swap.
        with manager.lock:
            row = manager._candidate_row(parent_id)
            if not row:
                return original_promote(parent_id)
            parent = store.get_agent_config(row["parent_agent_id"])
            candidate = store.get_agent_config(row["candidate_id"])
            if config_signature(parent) != config_signature(candidate):
                raise ValueError("Live agent configuration changed; rebuild Candidate before Promote")
            return original_promote(parent_id)

    store.update_agent = update_agent
    manager._start_build = start_build
    manager.status = status
    manager.promote = promote
    manager._candidate_config_guard = True
    manager.candidate_config_contract = "policy_config_must_match_live_at_build_and_promote"
    return manager
