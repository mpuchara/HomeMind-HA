"""Bound full-model retention for Candidate lineage while preserving durable metadata."""
import time

from settings import OPTIONS


def install(manager):
    import agent_candidate_lineage as lineage

    def bounded_retention(owner, root_id):
        keep = max(2, int(OPTIONS.get("agent_candidate_model_retention", 3)))
        tip = lineage._active_tip(owner.store, root_id)
        protected = set()
        if tip:
            protected.add(tip["generation_id"])
            if tip.get("parent_generation_id"):
                protected.add(tip["parent_generation_id"])
        with owner.store.conn() as c:
            rows = [dict(r) for r in c.execute(
                """SELECT * FROM agent_candidate_generations
                   WHERE root_agent_id=? AND generation_type='candidate' AND model_retained=1
                   ORDER BY generation_number DESC,created_ts DESC""",
                (str(root_id),),
            ).fetchall()]
        retained = 0
        for generation in rows:
            if retained < keep or generation["generation_id"] in protected:
                retained += 1
                continue
            agent_id = generation.get("agent_id")
            if not agent_id:
                continue
            # Historical edge rows are no longer required once neither endpoint is the
            # active direct-parent comparison edge. Their comparison snapshot already
            # lives in the lineage ledger.
            with owner.store.lock, owner.store.conn() as c:
                c.execute(
                    "DELETE FROM agent_candidates WHERE candidate_id=? AND state='parent'",
                    (str(agent_id),),
                )
                c.execute(
                    "DELETE FROM agent_candidates WHERE parent_agent_id=? AND state='parent'",
                    (str(agent_id),),
                )
            try:
                owner.engine.models.pop(str(agent_id), None)
                owner.engine.runtime.pop(str(agent_id), None)
                owner.store.delete_agent(str(agent_id))
            except Exception:
                continue
            now = time.time()
            with owner.store.lock, owner.store.conn() as c:
                c.execute(
                    """UPDATE agent_candidate_generations
                       SET agent_id=NULL,model_retained=0,lifecycle_state='pruned',retired_ts=?,updated_ts=?
                       WHERE generation_id=?""",
                    (now, now, generation["generation_id"]),
                )

    lineage._retention = bounded_retention
    manager.candidate_model_retention = max(2, int(OPTIONS.get("agent_candidate_model_retention", 3)))
    for root_id in lineage.list_lineage_roots(manager.store):
        bounded_retention(manager, root_id)
    manager._candidate_lineage_retention_installed = True
    return manager
