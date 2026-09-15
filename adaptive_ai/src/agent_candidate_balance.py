"""Balanced future-evidence gate for binary Candidate generations.

The Candidate comparison already records ON/OFF transitions explicitly for timing.  Use
those counters as the authoritative binary class-coverage proof: promotion requires at
least 20 future ON and 20 future OFF outcomes, matching the live Control qualification
contract even when auxiliary per-action diagnostics are absent.
"""


def install(manager):
    if getattr(manager, "_candidate_balance_gate", False):
        return manager
    original = manager._comparison_summary

    def summary(row, parent=None, candidate=None):
        out = original(row, parent, candidate)
        parent_agent = parent or manager.store.get_agent_config(row["parent_agent_id"])
        candidate_agent = candidate or manager.store.get_agent(row["candidate_id"])
        if parent_agent and str(parent_agent.get("target_property") or "") == "power":
            balanced = int(out.get("on_events") or 0) >= 20 and int(out.get("off_events") or 0) >= 20
            out["per_action_ready"] = balanced
            samples = int(out.get("samples") or 0)
            live_acc = out.get("live_accuracy")
            cand_acc = out.get("candidate_accuracy")
            fresh = bool(out.get("fresh_feedback_revision"))
            trained = bool(candidate_agent and candidate_agent.get("training_state") == "qualified"
                           and manager.store.get_model(candidate_agent["id"]))
            false_margin = max(2, int(__import__("math").ceil(samples * .10)))
            safety = bool(samples >= 40 and live_acc is not None and cand_acc is not None
                          and cand_acc + .03 >= live_acc
                          and int(out.get("candidate_false_early") or 0)
                              <= int(out.get("live_false_early") or 0) + false_margin)
            out["promotable"] = bool(fresh and trained and balanced and safety)
        return out

    manager._comparison_summary = summary
    manager._candidate_balance_gate = True
    return manager
