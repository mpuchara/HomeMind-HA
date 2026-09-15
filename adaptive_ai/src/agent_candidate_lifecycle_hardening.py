"""Final lifecycle guards for zero-downtime Candidate generations.

This module intentionally installs after the core Candidate manager and its config/status
extensions.  It closes lifecycle edge cases without changing policy math or Executor:

* Promote always leaves the new Live generation in Shadow, even when the previous Live
  generation was paused or in Control.  A Control lease is released before the mode flip.
* Manual Rebuild is a build request, not explicit feedback, so it cannot fabricate a new
  feedback revision.
* Conflicting explicit labels for the same historical instant use latest-instruction-wins
  semantics before Candidate training.
* A requested discard survives service restart and is completed without requiring a
  usable Candidate model or another expensive rebuild.
"""
import time


def install(manager):
    if getattr(manager, "_candidate_lifecycle_hardening", False):
        return manager

    store = manager.store
    original_sync_feedback = manager._sync_feedback
    original_finish_build = manager._finish_build_if_ready
    original_enqueue = manager.enqueue
    original_promote = manager.promote

    def sync_feedback(parent, candidate):
        result = original_sync_feedback(parent, candidate)
        # The core merger copies Teach and Wrong-decision rows into the Candidate Teach
        # table.  If two explicit instructions target the same historical instant, keep
        # only the newest one.  This makes the data contract match the UI expectation:
        # the latest explicit instruction is authoritative.
        removed = 0
        with store.lock, store.conn() as c:
            rows = c.execute(
                """SELECT id,created_ts,sample_ts FROM teaching_rl_labels
                   WHERE agent_id=? AND undone_ts IS NULL
                   ORDER BY sample_ts,created_ts,id""",
                (candidate["id"],),
            ).fetchall()
            latest = {}
            for row in rows:
                key = round(float(row["sample_ts"]), 6)
                marker = (float(row["created_ts"]), int(row["id"]))
                previous = latest.get(key)
                if previous is None or marker > previous[0]:
                    latest[key] = (marker, int(row["id"]))
            keep = {item[1] for item in latest.values()}
            stale = [int(row["id"]) for row in rows if int(row["id"]) not in keep]
            if stale:
                c.executemany("DELETE FROM teaching_rl_labels WHERE id=?", [(row_id,) for row_id in stale])
                removed = len(stale)
        if removed:
            store.event(
                parent["id"], "info", "agent_candidate_feedback_conflict_resolved",
                "Older conflicting Candidate feedback was retired; latest explicit instruction wins",
                {"candidate_id": candidate["id"], "retired": removed},
            )
        active_examples = 0
        with store.conn() as c:
            active_examples = int(c.execute(
                "SELECT COUNT(*) FROM teaching_rl_labels WHERE agent_id=? AND undone_ts IS NULL",
                (candidate["id"],),
            ).fetchone()[0])
        result = dict(result or {})
        result["examples"] = active_examples
        result["conflicts_retired"] = removed
        return result

    def enqueue(parent_id, reason="feedback"):
        # Rebuild changes the model generation, not the user's teaching evidence.  Route
        # it through the idempotent build-request path added by agent_candidate_teach_status.
        if str(reason) == "manual_rebuild" and callable(getattr(manager, "request_build", None)):
            return manager.request_build(parent_id, "manual_rebuild")
        return original_enqueue(parent_id, reason)

    def finish_build_if_ready(row):
        # Discard is authoritative.  After restart the in-memory training queue is gone,
        # so do not require a model or start another build merely to delete the surrogate.
        fresh = manager._candidate_row(row["parent_agent_id"]) or row
        if int(fresh.get("discard_requested") or 0):
            queue = manager._queue()
            if queue is not None and queue.status_for(fresh["candidate_id"]):
                return False
            history = getattr(manager.core, "HISTORY", None)
            if history is not None and fresh["candidate_id"] in getattr(history, "agent_jobs", set()):
                return False
            manager._delete_candidate(fresh)
            store.event(
                fresh["parent_agent_id"], "info", "agent_candidate_discarded",
                "Candidate discard completed; Live agent was unchanged",
                {"candidate_id": fresh["candidate_id"]},
            )
            return True
        return original_finish_build(fresh)

    def promote(parent_id):
        # Validate before releasing an existing Control lease. The wrapped promoter will
        # validate again while holding the same manager lock, preserving config guards.
        status = manager.status(parent_id)
        if not status or not status.get("promotable"):
            return original_promote(parent_id)
        with manager.lock:
            parent = store.get_agent_config(str(parent_id))
            if not parent:
                raise ValueError("Live agent not found")
            previous_mode = str(parent.get("mode") or "paused")
            if previous_mode == "control":
                manager.engine.executor.release_control(parent, reason="candidate_promote")
            # Promotion is deliberately non-controlling.  Put the logical Live agent in
            # Shadow before the model swap and before the promoter wakes inference, so
            # there is no window where the new model can reacquire Control automatically.
            if previous_mode != "shadow":
                store.update_agent(parent["id"], {"mode": "shadow"})
            result = original_promote(parent_id)
            result = dict(result or {})
            result["mode"] = "shadow"
            return result

    manager._sync_feedback = sync_feedback
    manager._finish_build_if_ready = finish_build_if_ready
    manager.enqueue = enqueue
    manager.promote = promote
    manager._candidate_lifecycle_hardening = True

    # Older builds recovered an interrupted `discarding` row as `queued`. Preserve the
    # user's discard intent on upgrade and let the worker delete it immediately once no
    # in-memory/history job owns the surrogate.
    now = time.time()
    with store.lock, store.conn() as c:
        c.execute(
            """UPDATE agent_candidates SET state='discarding',dirty=0,updated_ts=?
               WHERE discard_requested=1 AND state!='discarding'""",
            (now,),
        )
    manager.wake_event.set()
    return manager
