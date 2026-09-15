"""Balanced future-evidence gate for binary Candidate generations.

The Candidate comparison records ON/OFF transitions explicitly for timing. Use those
counters as the authoritative binary class-coverage proof, aligned with the existing
Control qualification principle that both binary actions need independent future evidence.
"""
import math

from settings import OPTIONS


def install(manager):
    if getattr(manager, "_candidate_balance_gate", False):
        return manager
    original = manager._comparison_summary

    def summary(row, parent=None, candidate=None):
        out = original(row, parent, candidate)
        parent_agent = parent or manager.store.get_agent_config(row["parent_agent_id"])
        candidate_agent = candidate or manager.store.get_agent(row["candidate_id"])
        if parent_agent and str(parent_agent.get("target_property") or "") == "power":
            per_action_min = max(1, int(OPTIONS.get("agent_candidate_future_samples_per_binary_action", 20)))
            min_samples = max(per_action_min * 2, int(OPTIONS.get("agent_candidate_future_samples", 40)))
            max_regression = max(0.0, float(OPTIONS.get("agent_candidate_max_accuracy_regression", .03)))
            balanced = (
                int(out.get("on_events") or 0) >= per_action_min
                and int(out.get("off_events") or 0) >= per_action_min
            )
            out["per_action_ready"] = balanced
            out["required_future_samples"] = min_samples
            out["required_future_samples_per_action"] = per_action_min
            samples = int(out.get("samples") or 0)
            live_acc = out.get("live_accuracy")
            cand_acc = out.get("candidate_accuracy")
            fresh = bool(out.get("fresh_feedback_revision"))
            trained = bool(candidate_agent and candidate_agent.get("training_state") == "qualified"
                           and manager.store.get_model(candidate_agent["id"]))
            false_margin = max(2, int(math.ceil(samples * .10)))
            safety = bool(
                samples >= min_samples
                and live_acc is not None
                and cand_acc is not None
                and cand_acc + max_regression >= live_acc
                and int(out.get("candidate_false_early") or 0)
                    <= int(out.get("live_false_early") or 0) + false_margin
            )
            out["max_accuracy_regression"] = max_regression
            out["promotable"] = bool(fresh and trained and balanced and safety)
        return out

    manager._comparison_summary = summary
    manager._candidate_balance_gate = True
    return manager
