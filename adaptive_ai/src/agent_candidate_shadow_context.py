"""Bind Candidate Shadow inference to the exact context used by Root Live.

Engine.process_agent intentionally refreshes its policy context from ``engine.state_map``
inside the inference path. A websocket event can therefore arrive after the scheduler's
outer snapshot was taken. Candidate generations must not accidentally evaluate that older
outer snapshot while their parent used the newer one.

This guard observes the actual state-map object and timestamp passed to the Root Live
policy's ``features()`` call and lets Candidate Shadow run only when that same process
invocation performed a fresh parent inference. The Shadow layer then receives that exact
state-map snapshot and its shared event is stamped with the parent's inference-context
timestamp. It remains diagnostics-only and never touches Executor or HA services.
"""
import math
import time

from agent_candidate_shadow_runtime import _generation


def install(manager):
    if getattr(manager, "_candidate_shadow_context_installed", False):
        return manager

    engine = manager.engine
    original_policy = engine.policy
    original_before = manager.before_live_process
    original_after = manager.after_live_process
    calls = {}

    def _is_root_live(agent_id):
        try:
            row = _generation(manager.store, agent_id=str(agent_id))
            return bool(row and str(row.get("generation_type") or "") == "live")
        except Exception:
            return False

    def policy(agent):
        policy_obj = original_policy(agent)
        aid = str((agent or {}).get("id") or "")
        if not aid or not _is_root_live(aid):
            return policy_obj
        if getattr(policy_obj, "_candidate_shadow_exact_context", False):
            return policy_obj

        original_features = policy_obj.features

        def features(state_map, history, at_ts=None):
            result = original_features(state_map, history, at_ts=at_ts)
            call = calls.get(aid)
            if call and call.get("active"):
                runtime = engine.runtime.get(aid) or {}
                inference_ts = runtime.get("last_inference_ts")
                try:
                    inference_ts = float(inference_ts) if inference_ts is not None else None
                except (TypeError, ValueError):
                    inference_ts = None
                try:
                    context_ts = float(at_ts) if at_ts is not None else time.time()
                except (TypeError, ValueError):
                    context_ts = time.time()
                tls = getattr(engine, "_inference_tls", None)
                meta = (
                    dict(result[2] or {})
                    if isinstance(result, tuple) and len(result) > 2 and isinstance(result[2], dict)
                    else {}
                )
                schema = getattr(policy_obj, "schema", None)
                selection = dict(getattr(policy_obj, "selection_meta", None) or {})
                call["capture"] = {
                    # Do not mutate this mapping. Engine.process_target created it as the
                    # immutable pass snapshot. Retaining the mapping avoids an O(home)
                    # copy on the Live critical path.
                    "state_map": state_map,
                    "context_ts": context_ts,
                    "inference_ts": inference_ts,
                    "state_revision": getattr(tls, "state_revision", getattr(engine, "state_revision", None)) if tls is not None else getattr(engine, "state_revision", None),
                    "entity_revisions": getattr(tls, "entity_revisions", None) if tls is not None else None,
                    "context_revision": getattr(tls, "context_revision", None) if tls is not None else None,
                    "home_forecast_captured": "home_forecast" in meta,
                    "home_forecast": dict(meta.get("home_forecast") or {}),
                    "parent_model_revision": getattr(policy_obj, "model_revision", None),
                    "parent_schema_revision": (
                        selection.get("schema_revision")
                        if selection.get("schema_revision") is not None
                        else getattr(schema, "version", None)
                    ),
                }
            return result

        policy_obj.features = features
        policy_obj._candidate_shadow_exact_context = True
        return policy_obj

    def before_live_process(agent, state_map):
        aid = str(agent["id"])
        runtime = engine.runtime.get(aid) or {}
        try:
            start_inference = float(runtime.get("last_inference_ts") or 0.0)
        except (TypeError, ValueError):
            start_inference = 0.0
        calls[aid] = {"active": True, "start_inference_ts": start_inference, "capture": None}
        # Paired outcome resolution belongs before the new parent inference and therefore
        # still receives the scheduler's physical target event snapshot.
        return original_before(agent, state_map)

    def _capture_shadow_job(agent):
        aid = str(agent["id"])
        call = calls.pop(aid, None)
        if not call:
            return None
        call["active"] = False
        capture = call.get("capture")
        if not capture:
            # Parent did not reach policy.features in this process pass: Candidate must not
            # fabricate a newer prediction while the parent kept its previous decision.
            return None
        runtime = engine.runtime.get(aid) or {}
        try:
            final_inference = float(runtime.get("last_inference_ts") or 0.0)
        except (TypeError, ValueError):
            final_inference = 0.0
        captured_inference = capture.get("inference_ts")
        if (
            final_inference <= float(call.get("start_inference_ts") or 0.0)
            or captured_inference is None
            or not math.isfinite(float(captured_inference))
            or abs(float(captured_inference) - final_inference) > 1e-6
        ):
            return None
        try:
            context_ts = float(capture["context_ts"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(context_ts):
            return None

        try:
            parent_prediction = float(runtime.get("last_prediction"))
            parent_confidence = runtime.get("last_confidence")
            parent_confidence = None if parent_confidence is None else float(parent_confidence)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parent_prediction):
            return None
        if parent_confidence is not None and not math.isfinite(parent_confidence):
            parent_confidence = None

        return {
            "root_agent_id": aid,
            "agent": dict(agent),
            # Exact Root policy snapshot; no later engine.state_map reconstruction.
            "state_map": capture["state_map"],
            "context_ts": context_ts,
            "inference_ts": final_inference,
            "state_revision": capture.get("state_revision"),
            "entity_revisions": capture.get("entity_revisions"),
            "context_revision": capture.get("context_revision"),
            "generation_revision": int(
                getattr(manager.store, "_provenance_generation_revision", 0) or 0
            ),
            "home_forecast_captured": bool(capture.get("home_forecast_captured")),
            "home_forecast": dict(capture.get("home_forecast") or {}),
            "parent_observation": {
                "desired": parent_prediction,
                "confidence": parent_confidence,
                "model_revision": (
                    str(capture.get("parent_model_revision"))
                    if capture.get("parent_model_revision") is not None else None
                ),
                "schema_revision": (
                    str(capture.get("parent_schema_revision"))
                    if capture.get("parent_schema_revision") is not None else None
                ),
            },
        }

    def execute_candidate_shadow_job(job):
        agent = dict(job.get("agent") or {})
        if not agent:
            return None
        bundle = original_after(agent, job.get("state_map"))
        if not bundle:
            return bundle

        # Shadow runtime creates the event on the deferred worker. Re-anchor it to the
        # timestamp actually passed to Root policy.features so Parent/Child remain one
        # causal event even though Candidate computation runs later.
        try:
            context_ts = float(job["context_ts"])
            old_ts = float(bundle["ts"])
        except (KeyError, TypeError, ValueError):
            return bundle
        bundle["ts"] = context_ts
        align_runtime = getattr(manager, "align_candidate_shadow_event_timestamp", None)
        if callable(align_runtime):
            align_runtime(agent["id"], bundle["event_id"], old_ts, context_ts)
        for result in (bundle.get("results") or {}).values():
            try:
                if abs(float(result.get("desired_since_ts")) - old_ts) <= 1e-6:
                    result["desired_since_ts"] = context_ts
            except (TypeError, ValueError):
                pass
        try:
            with manager.store.lock, manager.store.conn() as c:
                c.execute(
                    """UPDATE candidate_generation_decisions SET ts=?
                       WHERE root_agent_id=? AND event_id=? AND ts=?""",
                    (context_ts, str(agent["id"]), str(bundle["event_id"]), old_ts),
                )
        except Exception as exc:
            manager.store.event(
                agent["id"], "warning", "candidate_shadow_timestamp_alignment_failed",
                "Candidate Shadow kept its observed decision but could not align the persisted timestamp",
                {"event_id": bundle.get("event_id"), "error": f"{type(exc).__name__}: {exc}"},
            )
        return bundle

    def after_live_process(agent, state_map):
        job = _capture_shadow_job(agent)
        if not job:
            return None
        defer = getattr(manager, "defer_candidate_shadow", None)
        if callable(defer):
            return defer(job)
        # Compatibility for unit/legacy compositions that intentionally do not install
        # the deferred worker. Shipped runtime installs defer_candidate_shadow.
        return execute_candidate_shadow_job(job)

    manager.execute_candidate_shadow_job = execute_candidate_shadow_job
    manager.candidate_shadow_exact_context_contract = (
        "captured_state_map_timestamp_revisions_parent_decision_and_home_forecast"
    )

    engine.policy = policy
    manager.before_live_process = before_live_process
    manager.after_live_process = after_live_process
    manager._candidate_shadow_context_installed = True
    manager.candidate_shadow_context_contract = "exact_root_policy_features_snapshot_and_timestamp_same_process_inference"

    # Correct history must use the actually observed Live Desired, never replay today's
    # policy into the past. Candidate generations already have their own observed-only
    # decision table; install the equivalent Live overlay before generation actions.
    service = getattr(manager.engine, "rl_teaching", None)
    if (
        service is not None
        and callable(getattr(service, "history", None))
        and callable(getattr(service, "point", None))
    ):
        from teach_observed_history import install as install_observed_desired_history
        install_observed_desired_history(manager.store, manager.engine, service)

    # Generation-aware user actions must wrap the final Shadow/context contract. Keeping
    # this installation here guarantees they are active before the Candidate worker starts
    # without introducing a second startup path.
    from agent_workflow_actions import install as install_agent_workflow_actions
    manager = install_agent_workflow_actions(manager)

    # The Correct chart is a separate presentation/evidence contract layered on top of
    # generation workflow. It compares only the selected child with its direct parent and
    # reads observed decision history exclusively; it never replays today's policy.
    from agent_correct_generation_history import install as install_correct_generation_history
    manager = install_correct_generation_history(manager)

    # Explore is deliberately last: it coordinates the already-installed Experiments,
    # Sensor Tournament, Candidate Shadow and generation workflow without replacing any of
    # their safety/evidence paths.
    from agent_explore import install as install_agent_explore
    return install_agent_explore(manager)