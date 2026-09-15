"""Route Candidate manual Rebuild through the normal full historical rebuild path.

Manual Rebuild is not Correct/Teach.  The hidden Candidate must enter the same
TrainingQueue/HistoryManager rebuild used by a normal agent, while the Live parent keeps
serving.  This shim is installed immediately after the base Candidate manager and before
config/lifecycle wrappers, so those wrappers still synchronize and validate the Candidate
before this final queue hand-off.
"""
import json
import time


def _blank_comparison():
    return {
        "samples": 0,
        "live_correct": 0,
        "candidate_correct": 0,
        "candidate_wins": 0,
        "live_wins": 0,
        "both_wrong": 0,
        "per_action": {},
        "on_events": 0,
        "off_events": 0,
        "live_on_lead_sum": 0.0,
        "candidate_on_lead_sum": 0.0,
        "live_off_lead_sum": 0.0,
        "candidate_off_lead_sum": 0.0,
        "live_false_early": 0,
        "candidate_false_early": 0,
        "updated_ts": None,
    }


def install(manager):
    if getattr(manager, "_candidate_manual_rebuild_fix", False):
        return manager

    original_start = manager._start_build

    def start_build(row):
        reason = str(row.get("reason") or "feedback")
        if reason != "manual_rebuild":
            return original_start(row)

        queue = manager._queue()
        if queue is None:
            return False
        candidate = manager.store.get_agent(row["candidate_id"])
        parent = manager.store.get_agent_config(row["parent_agent_id"])
        if not candidate or not parent:
            manager._fail(row, "candidate or live agent disappeared")
            return True
        if queue.status_for(candidate["id"]):
            return False

        try:
            # Crucial distinction from Correct/Teach: do not create a Teach-RL job and
            # do not run feature-selection preflight.  TrainingQueue owns the ordinary
            # full historical rebuild and HistoryManager exposes its real progress.
            queued = queue.enqueue(candidate["id"], rebuild=True, reason="full_rebuild")
            now = time.time()
            with manager.store.lock, manager.store.conn() as c:
                c.execute(
                    """UPDATE agent_candidates SET state='building',build_revision=feedback_revision,dirty=0,
                       build_started_ts=?,build_finished_ts=NULL,comparison_started_ts=NULL,comparison_json=?,
                       last_error=NULL,updated_ts=? WHERE parent_agent_id=?""",
                    (now, json.dumps(_blank_comparison()), now, str(row["parent_agent_id"])),
                )
            manager.store.event(
                row["parent_agent_id"], "info", "agent_candidate_full_rebuild_started",
                "Candidate full historical rebuild started while the Live agent keeps serving",
                {"candidate_id": candidate["id"], "generation": row.get("generation"),
                 "queue": queued, "reason": "manual_rebuild"},
            )
            return True
        except Exception as exc:
            manager._fail(row, f"{type(exc).__name__}: {exc}")
            return True

    manager._start_build = start_build
    manager._candidate_manual_rebuild_fix = True
    manager.candidate_manual_rebuild_contract = "training_queue_full_rebuild_without_teach_preflight"
    return manager
