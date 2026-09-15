"""Safety-critical Candidate promotion and Shadow/Control lifecycle.

Promotion is a *generation swap* of the logical Root Live agent.  Candidate surrogates stay
physically Shadow-only until the swap commits.  By default the new generation inherits the
Root Live mode; the UI may store an explicit target mode (Shadow or Control) without ever
turning the Candidate surrogate into a controller.

For Control -> Control the target Executor lock is the dispatch freeze.  The existing
Control lease is neither released nor reacquired, so Home Assistant automations are never
restored/re-disabled in the middle of a successful promotion.  Model, benchmark, mode and
generation bookkeeping are committed in one SQLite transaction.  Until that transaction
commits the old generation remains the Live policy.
"""
from __future__ import annotations

import json
import time
from urllib.parse import urlsplit

from agent_candidate_config_guard import config_signature
from agent_candidate_lineage import _retention, _row as lineage_row
from settings import iso_now


VALID_TARGET_MODES = {"shadow", "control"}


def _ensure_schema(store):
    """Add only the small preference field; no existing Candidate data is rewritten."""
    with store.lock, store.conn() as c:
        cols = {str(r[1]) for r in c.execute("PRAGMA table_info(agent_candidates)").fetchall()}
        if "promotion_target_mode" not in cols:
            c.execute("ALTER TABLE agent_candidates ADD COLUMN promotion_target_mode TEXT")


def _table_exists(c, name):
    return bool(c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (str(name),)
    ).fetchone())


def _lease_signature(lease):
    if not lease:
        return None
    return (
        str(lease.get("agent_id") or ""),
        str(lease.get("target_entity") or ""),
        tuple(sorted(str(x) for x in (lease.get("disabled_automations") or []))),
        str(lease.get("created_at") or ""),
    )


def _automation_item(info, state_map, disabled_by_homemind):
    info = dict(info or {})
    eid = str(info.get("entity_id") or "")
    state = (state_map.get(eid) or {}).get("state")
    return {
        "entity_id": eid,
        "name": info.get("name") or eid,
        "state": state,
        "enabled": str(state).lower() == "on" if state is not None else bool(info.get("enabled")),
        "disabled_by_homemind": eid in disabled_by_homemind,
        "config_status": info.get("config_status"),
        "last_triggered": info.get("last_triggered"),
    }


def automation_ownership(manager, root):
    """Expose controller provenance without guessing who disabled an automation.

    The lease is the only authority for "disabled by HomeMind".  An automation which was
    already OFF before takeover is therefore never relabelled as HomeMind-owned.
    """
    if not root:
        return {
            "currently_controlling": [], "disabled_by_homemind": [],
            "previously_linked": [], "lease": None, "ownership_valid": True,
        }
    executor = getattr(manager.engine, "executor", None)
    handoff = getattr(executor, "handoff", None)
    journal = getattr(handoff, "journal", None)
    lease = journal.get(root["target_entity"]) if journal is not None else None
    disabled = set(str(x) for x in ((lease or {}).get("disabled_automations") or []))
    with getattr(manager.engine, "lock", _NullLock()):
        state_map = dict(getattr(manager.engine, "state_map", {}) or {})
    infos = []
    knowledge = getattr(handoff, "knowledge", None)
    if knowledge is not None and callable(getattr(knowledge, "hints_for_target", None)):
        try:
            _, infos = knowledge.hints_for_target(root["target_entity"])
        except Exception:
            infos = []
    by_id = {str(x.get("entity_id")): dict(x) for x in (infos or []) if x.get("entity_id")}
    for eid in disabled:
        by_id.setdefault(eid, {"entity_id": eid, "name": eid, "config_status": "lease"})
    linked = [_automation_item(by_id[eid], state_map, disabled) for eid in sorted(by_id)]
    current = [x for x in linked if x.get("enabled")]
    owned = [x for x in linked if x.get("entity_id") in disabled]
    ownership_valid = not lease or (
        str(lease.get("agent_id") or "") == str(root.get("id") or "")
        and str(lease.get("target_entity") or "") == str(root.get("target_entity") or "")
    )
    return {
        "currently_controlling": current,
        "disabled_by_homemind": owned,
        # AutomationKnowledge retains the last known target mapping when a config refresh
        # is unavailable, so this is deliberately the broad known/previously-linked set.
        "previously_linked": linked,
        "lease": lease,
        "ownership_valid": bool(ownership_valid),
    }


class _NullLock:
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        return False


def _edge_for_ref(manager, ref):
    row = manager._candidate_row(str(ref))
    if row:
        return row
    generation = lineage_row(manager.store, generation_id=str(ref)) or lineage_row(
        manager.store, agent_id=str(ref)
    )
    if not generation:
        return None
    parent_gid = generation.get("parent_generation_id")
    parent = lineage_row(manager.store, generation_id=parent_gid) if parent_gid else None
    if not parent or not parent.get("agent_id"):
        return None
    return manager._candidate_row(str(parent["agent_id"]))


def _root_for_edge(manager, row):
    child = lineage_row(manager.store, agent_id=row.get("candidate_id")) if row else None
    root_id = str((child or {}).get("root_agent_id") or (row or {}).get("parent_agent_id") or "")
    root = manager.store.get_agent_config(root_id) if root_id else None
    return root_id, root, child


def _preference(row, live_mode):
    value = str((row or {}).get("promotion_target_mode") or "").lower()
    if value in VALID_TARGET_MODES:
        return value
    return str(live_mode) if str(live_mode) in VALID_TARGET_MODES else "shadow"


def _restore_exact_lease(manager, root, lease_before):
    """Best-effort failure recovery after a Control -> Shadow release.

    Only automations recorded in the pre-swap HomeMind lease are re-disabled.  This never
    claims or toggles an automation that was user-disabled before takeover.
    """
    if not lease_before:
        return
    handoff = manager.engine.executor.handoff
    owned = [str(x) for x in (lease_before.get("disabled_automations") or [])]
    try:
        handoff.refresh()
    except Exception:
        pass
    state_map = handoff.state_map()
    changed = []
    for eid in owned:
        if (state_map.get(eid) or {}).get("state") != "off":
            handoff.disable_one(eid)
            changed.append(eid)
    if changed:
        try:
            handoff.refresh()
        except Exception:
            pass
    state_map = handoff.state_map()
    not_off = [eid for eid in owned if (state_map.get(eid) or {}).get("state") != "off"]
    if not_off:
        raise RuntimeError("Could not restore previous Control ownership for: " + ", ".join(not_off))
    # Restore the exact provenance snapshot (including original created_at) instead of
    # discovering a new ownership set after a failed promotion.
    journal = handoff.journal
    target = root["target_entity"]
    manager.store.meta_set(
        journal.key(target), json.dumps(lease_before, separators=(",", ":"), ensure_ascii=False)
    )
    index = journal.index()
    if target not in index:
        index.append(target)
        journal.write_index(index)


def install(manager):
    if getattr(manager, "_candidate_atomic_promote_installed", False):
        return manager

    _ensure_schema(manager.store)
    original_status = manager.status
    original_list_status = manager.list_status
    original_lineage_status = getattr(manager, "lineage_status", None)
    original_runtime_for = getattr(manager.engine, "runtime_for", None)
    handler = manager.core.Handler
    original_post = handler.do_POST

    def decorate(result):
        if not result:
            return result
        row = _edge_for_ref(manager, result.get("parent_agent_id") or result.get("generation_id") or "")
        if row is None and result.get("candidate_id"):
            child = lineage_row(manager.store, agent_id=result.get("candidate_id"))
            if child and child.get("parent_generation_id"):
                parent = lineage_row(manager.store, generation_id=child["parent_generation_id"])
                if parent and parent.get("agent_id"):
                    row = manager._candidate_row(parent["agent_id"])
        root_id, root, _ = _root_for_edge(manager, row) if row else (
            str(result.get("root_agent_id") or ""), None, None
        )
        if root is None and root_id:
            root = manager.store.get_agent_config(root_id)
        live_mode = str((root or {}).get("mode") or "shadow")
        result["live_mode"] = live_mode
        result["promotion_target_mode"] = _preference(row, live_mode)
        result["candidate_physical_mode"] = "shadow"
        result["candidate_can_dispatch"] = False
        result["automation_ownership"] = automation_ownership(manager, root)
        return result

    def status(parent_id):
        return decorate(original_status(parent_id))

    def list_status():
        return [decorate(dict(item)) for item in (original_list_status() or []) if item]

    def lineage_status(ref):
        return decorate(original_lineage_status(ref)) if original_lineage_status is not None else None

    def set_promotion_target_mode(parent_ref, target_mode):
        mode = str(target_mode or "").strip().lower()
        if mode not in VALID_TARGET_MODES:
            raise ValueError("Promotion target mode must be Shadow or Control")
        row = _edge_for_ref(manager, parent_ref)
        if not row:
            raise ValueError("Candidate not found")
        # This is only a promotion preference.  Never mutate the hidden Candidate agent's
        # physical mode and never acquire/release Control here.
        with manager.store.lock, manager.store.conn() as c:
            c.execute(
                "UPDATE agent_candidates SET promotion_target_mode=?,updated_ts=? WHERE parent_agent_id=?",
                (mode, time.time(), str(row["parent_agent_id"])),
            )
        manager.store.event(
            str((_root_for_edge(manager, row)[0]) or row["parent_agent_id"]),
            "info", "candidate_promotion_target_mode",
            f"Candidate promotion target set to {mode}; Candidate remains physical Shadow",
            {"candidate_id": row["candidate_id"], "target_mode": mode,
             "candidate_physical_mode": "shadow"},
        )
        return status(row["parent_agent_id"])

    def promote(parent_id, target_mode=None):
        # Resolve only enough to find the physical target lock.  Every safety/eligibility
        # fact is refreshed again *inside* that lock before any side effect.
        initial = _edge_for_ref(manager, parent_id)
        if not initial:
            raise ValueError("Candidate not found")
        root_id, root, _ = _root_for_edge(manager, initial)
        if not root:
            raise ValueError("Root Live agent unavailable")
        target_entity = str(root["target_entity"])
        executor = manager.engine.executor

        # Executor.submit uses this same RLock, therefore no new dispatch for the target can
        # cross the generation swap.  An old queued intent waits, then fails its model
        # revision check against the newly-loaded generation.
        with executor.target_lock(target_entity):
            with manager.lock:
                row = _edge_for_ref(manager, parent_id)
                if not row:
                    raise ValueError("Candidate disappeared before Promote")
                root_id, root, child_gen = _root_for_edge(manager, row)
                if not root or str(root.get("target_entity")) != target_entity:
                    raise ValueError("Root Live target changed before Promote")
                fresh_status = original_status(row["parent_agent_id"])
                if not fresh_status or not fresh_status.get("promotable"):
                    raise ValueError("Candidate needs more future paired evidence before Promote")
                candidate = manager.store.get_agent_config(str(row["candidate_id"]))
                candidate_model = manager.store.get_model(str(row["candidate_id"]))
                old_model = manager.store.get_model(root_id)
                if not candidate or not candidate_model:
                    raise ValueError("Candidate model unavailable")
                if candidate.get("training_state") != "qualified":
                    raise ValueError("Candidate is not qualified")
                if config_signature(root) != config_signature(candidate):
                    raise ValueError("Root Live configuration changed; rebuild Candidate before Promote")

                requested = str(target_mode or row.get("promotion_target_mode") or "").lower()
                mode = requested if requested in VALID_TARGET_MODES else _preference(row, root.get("mode"))
                if mode not in VALID_TARGET_MODES:
                    raise ValueError("Promotion target mode must be Shadow or Control")
                current_mode = str(root.get("mode") or "shadow")
                if current_mode not in VALID_TARGET_MODES:
                    current_mode = "shadow"

                comparison = fresh_status.get("comparison") or {}
                journal = executor.handoff.journal
                lease_before = journal.get(target_entity)
                lease_sig_before = _lease_signature(lease_before)
                if current_mode == "control" and mode == "control":
                    if not lease_before or str(lease_before.get("agent_id")) != root_id:
                        raise RuntimeError("Control Promote requires the existing Root ownership lease")

                rt = manager.engine.runtime.setdefault(root_id, {})
                rt["generation_swap_frozen"] = True
                rt["generation_swap_target_mode"] = mode
                acquired_for_transition = False
                released_for_transition = False
                committed = False
                try:
                    # Mode-changing promotions use the normal ownership boundary.  The
                    # safety-critical Control -> Control path deliberately does neither.
                    if current_mode != "control" and mode == "control":
                        executor.take_control(root, refresh=True)
                        acquired_for_transition = True
                    elif current_mode == "control" and mode != "control":
                        executor.release_control(root, reason="candidate_promote_to_shadow")
                        released_for_transition = True

                    now = time.time()
                    stamp = iso_now()
                    generation_number = int(
                        (child_gen or {}).get("generation_number")
                        or row.get("generation") or (manager._generation(root_id) + 1)
                    )
                    comparison_json = json.dumps(comparison, separators=(",", ":"), default=str)
                    old_model_json = (
                        json.dumps(old_model, separators=(",", ":"), default=str)
                        if old_model is not None else None
                    )
                    candidate_model_json = json.dumps(
                        candidate_model, separators=(",", ":"), default=str
                    )
                    benchmark_detail_json = json.dumps(
                        candidate.get("benchmark_detail") or {}, separators=(",", ":"), default=str
                    )

                    # One DB transaction is the commit point.  No model cache is invalidated
                    # before this succeeds, so an exception leaves the previous generation
                    # immediately runnable.
                    with manager.store.lock, manager.store.conn() as c:
                        c.execute(
                            """INSERT INTO agent_generation_backups
                               (agent_id,generation,created_ts,expires_ts,model_json,agent_json,comparison_json)
                               VALUES(?,?,?,?,?,?,?)""",
                            (
                                root_id, manager._generation(root_id), now, now + 86400.0,
                                old_model_json,
                                json.dumps(root, separators=(",", ":"), default=str),
                                comparison_json,
                            ),
                        )
                        c.execute(
                            """INSERT INTO rl_models(agent_id,model_json,updated_at) VALUES(?,?,?)
                               ON CONFLICT(agent_id) DO UPDATE SET
                                 model_json=excluded.model_json,updated_at=excluded.updated_at""",
                            (root_id, candidate_model_json, stamp),
                        )
                        cur = c.execute(
                            """UPDATE agents SET mode=?,training_state='qualified',benchmark_score=?,
                               benchmark_samples=?,benchmark_source=?,benchmark_detail_json=?,
                               benchmark_updated_at=?,training_cursor_ts=?,training_window_start_ts=?,
                               training_window_end_ts=?,training_progress=1.0,training_updated_at=?
                               WHERE id=?""",
                            (
                                mode, candidate.get("benchmark_score"),
                                int(candidate.get("benchmark_samples") or 0),
                                candidate.get("benchmark_source"), benchmark_detail_json, stamp,
                                candidate.get("training_cursor_ts"), candidate.get("training_window_start_ts"),
                                candidate.get("training_window_end_ts"), stamp, root_id,
                            ),
                        )
                        if int(cur.rowcount or 0) != 1:
                            raise RuntimeError("Root Live row disappeared during Promote")
                        c.execute(
                            """INSERT INTO agent_generation_state(agent_id,generation,updated_ts)
                               VALUES(?,?,?) ON CONFLICT(agent_id) DO UPDATE SET
                               generation=excluded.generation,updated_ts=excluded.updated_ts""",
                            (root_id, generation_number, now),
                        )
                        if child_gen and _table_exists(c, "agent_candidate_generations"):
                            c.execute(
                                """UPDATE agent_candidate_generations SET lifecycle_state='promoted',
                                   comparison_json=?,retired_ts=?,updated_ts=? WHERE generation_id=?""",
                                (comparison_json, now, now, str(child_gen["generation_id"])),
                            )
                            c.execute(
                                """DELETE FROM agent_candidates WHERE parent_agent_id IN
                                   (SELECT agent_id FROM agent_candidate_generations
                                    WHERE root_agent_id=? AND agent_id IS NOT NULL)""",
                                (root_id,),
                            )
                        c.execute("DELETE FROM agent_candidates WHERE parent_agent_id=?", (root_id,))

                        # Test/fault-injection hook intentionally runs before transaction
                        # exit so an exception proves the whole generation swap rolls back.
                        hook = getattr(manager, "_atomic_promote_before_commit", None)
                        if callable(hook):
                            hook({
                                "root_agent_id": root_id,
                                "candidate_id": candidate["id"],
                                "generation": generation_number,
                                "target_mode": mode,
                            })
                    committed = True

                    if current_mode == "control" and mode == "control":
                        lease_after = journal.get(target_entity)
                        if _lease_signature(lease_after) != lease_sig_before:
                            # No code in this path is allowed to touch the lease.  Treat a
                            # concurrent/provenance mutation as a safety fault.  Target lock
                            # prevents normal local lifecycle operations from racing here.
                            raise RuntimeError("Control ownership lease changed during atomic Promote")

                    # Cache invalidation happens only after the DB commit.  Preserve pending
                    # physical acknowledgement/manual-hold state; only policy-derived values
                    # are invalidated so Executor resumes with the new generation cleanly.
                    manager.engine.models.pop(root_id, None)
                    rt.pop("last_prediction", None)
                    rt.pop("last_confidence", None)
                    rt.pop("intent", None)
                    rt["generation_swap_ts"] = now
                    rt["generation_swap_generation"] = generation_number
                    rt["generation_swap_mode"] = mode
                    rt["generation_swap_committed"] = True
                except Exception:
                    if not committed:
                        recovery_error = None
                        try:
                            if acquired_for_transition:
                                executor.release_control(root, reason="candidate_promote_rollback")
                            elif released_for_transition:
                                _restore_exact_lease(manager, root, lease_before)
                        except Exception as recovery_exc:
                            recovery_error = recovery_exc
                        if recovery_error is not None:
                            raise RuntimeError(
                                "Promote failed and previous Control ownership recovery failed: "
                                + str(recovery_error)
                            ) from recovery_error
                    raise
                finally:
                    rt["generation_swap_frozen"] = False

        manager.store.event(
            root_id, "info", "agent_candidate_atomic_promoted",
            "Candidate generation atomically replaced Root Live",
            {
                "generation": generation_number,
                "target_mode": mode,
                "previous_mode": current_mode,
                "control_lease_preserved": bool(current_mode == "control" and mode == "control"),
                "candidate_physical_mode_before_promote": "shadow",
                "comparison": comparison,
                "rollback_snapshot_hours": 24,
            },
        )
        manager.engine.wake_event.set()
        try:
            _retention(manager, root_id)
        except Exception as exc:
            manager.store.event(
                root_id, "warning", "candidate_retention_after_promote_failed",
                "Promotion committed; post-commit Candidate retention cleanup was skipped",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
        return {
            "ok": True, "agent_id": root_id, "generation": generation_number, "mode": mode,
            "previous_mode": current_mode,
            "control_lease_preserved": bool(current_mode == "control" and mode == "control"),
        }

    if callable(original_runtime_for):
        def runtime_for(agent):
            payload = original_runtime_for(agent)
            payload["automation_ownership"] = automation_ownership(manager, agent)
            return payload
        manager.engine.runtime_for = runtime_for

    def do_post(http):
        path, _, _ = http.path.partition("?")
        if path.startswith("/api/agents/") and path.endswith("/candidate/target-mode"):
            if not http.require_trusted_client() or not http.require_runtime():
                return
            parent_id = path.split("/")[3]
            try:
                body = http.read_json()
                return http.send_json(200, set_promotion_target_mode(
                    parent_id, (body or {}).get("target_mode")
                ))
            except ValueError as exc:
                return http.send_json(409, {"error": str(exc), "candidate": status(parent_id)})
        if path.startswith("/api/agents/") and path.endswith("/candidate/promote"):
            if not http.require_trusted_client() or not http.require_runtime():
                return
            parent_id = path.split("/")[3]
            try:
                body = http.read_json()
                requested = (body or {}).get("target_mode") if isinstance(body, dict) else None
                if requested is not None:
                    set_promotion_target_mode(parent_id, requested)
                return http.send_json(200, promote(parent_id, requested))
            except (ValueError, RuntimeError) as exc:
                return http.send_json(409, {"error": str(exc), "candidate": status(parent_id)})
        return original_post(http)

    manager.status = status
    manager.list_status = list_status
    if original_lineage_status is not None:
        manager.lineage_status = lineage_status
    manager.set_promotion_target_mode = set_promotion_target_mode
    manager.promote = promote
    handler.do_POST = do_post
    manager._candidate_atomic_promote_installed = True
    manager.candidate_promotion_contract = "atomic_generation_swap_preserve_live_mode_or_explicit_target"
    manager.candidate_control_promote_contract = "target_lock_preserve_ownership_lease_no_release_reacquire"
    manager.candidate_physical_mode_contract = "candidate_always_shadow_until_committed_promote"
    return manager
