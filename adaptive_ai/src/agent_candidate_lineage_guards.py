"""Lifecycle guards for a single mutable lineage tip.

Once a Candidate owns a child it is immutable.  Further Candidate-targeted correction
creates the next generation instead of rewriting an ancestor.  Root feedback is allowed
to update G1 while G1 is still the leaf; once descendants exist it advances from the
current leaf rather than unfreezing G1.
"""


def install(manager):
    if getattr(manager, "_candidate_lineage_guards_installed", False):
        return manager
    import agent_candidate_lineage as lineage

    original_enqueue = manager.enqueue

    def enqueue(parent_id, reason="feedback"):
        generation = lineage._row(manager.store, agent_id=parent_id)
        if generation and generation.get("generation_type") == "candidate":
            children = lineage._generation_children(manager.store, generation["generation_id"])
            if children:
                raise ValueError("Candidate generation is frozen because it already owns a child")
            return manager.spawn_child(generation["generation_id"], reason)

        # Root Live may continue mutating its first Candidate only while that first
        # Candidate is the leaf.  If G2+ exists, advance from the current tip instead of
        # rewriting the frozen G1 edge.
        root_id = str(parent_id)
        tip = lineage._active_tip(manager.store, root_id)
        if tip and int(tip.get("generation_number") or 0) >= 2:
            children = lineage._generation_children(manager.store, tip["generation_id"])
            if children:
                raise ValueError("Active Candidate tip already owns a child")
            return manager.spawn_child(tip["generation_id"], reason)
        return original_enqueue(parent_id, reason)

    manager.enqueue = enqueue
    manager._candidate_lineage_guards_installed = True
    return manager
