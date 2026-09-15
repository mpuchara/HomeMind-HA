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
                call["capture"] = {
                    # Do not mutate this mapping. Engine created it as the policy snapshot.
                    "state_map": state_map,
                    "context_ts": context_ts,
                    "inference_ts": inference_ts,
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

    def after_live_process(agent, state_map):
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

        # This is the exact map supplied to the parent's policy feature builder, not a
        # later reconstruction and not the outer scheduler snapshot.
        bundle = original_after(agent, capture["state_map"])
        if not bundle:
            return bundle

        # agent_candidate_shadow_runtime creates one shared event a few milliseconds after
        # Root inference. Re-anchor that event to the timestamp that was actually passed to
        # the Root policy. This makes parent/child decision-history timestamps refer to the
        # same inference context rather than to a later bookkeeping instant.
        try:
            context_ts = float(capture["context_ts"])
            old_ts = float(bundle["ts"])
        except (KeyError, TypeError, ValueError):
            return bundle
        if not math.isfinite(context_ts):
            return bundle
        bundle["ts"] = context_ts
        for result in (bundle.get("results") or {}).values():
            try:
                if abs(float(result.get("desired_since_ts")) - old_ts) <= 1e-6:
                    result["desired_since_ts"] = context_ts
            except (TypeError, ValueError):
                pass
        # Rows may not exist when the 30 s history heartbeat did not need a write. When
        # they do exist, move only this exact shared event; no historical policy replay is
        # involved and no other generation/event is touched.
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
    return install_agent_workflow_actions(manager)
