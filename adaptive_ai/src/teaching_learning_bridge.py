"""Make Teach corrections first-class supervised context feedback.

Teaching now owns the semantic context/signature contract directly. This compatibility
bridge only feeds the existing full-context relevance learner after a Teaching label is
accepted. It no longer replaces ``teaching.distance``; stage 06 therefore has one context
matcher, including home-trajectory/version metadata, in the proper Teaching contract.
"""
import time


def _historical_broad_state(core, engine, timestamp):
    """Reconstruct broad HA context at one Teach-chart point without future leakage."""
    from context import HistoricalTemporalTracker

    with engine.lock:
        entity_ids = list(engine.state_map.keys())
    entity_ids = entity_ids[:768]
    rows = []
    with core.STORE.conn() as c:
        for eid in entity_ids:
            row = c.execute(
                "SELECT * FROM entity_history WHERE entity_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                (eid, float(timestamp)),
            ).fetchone()
            if row:
                rows.append(dict(row))
    tracker = HistoricalTemporalTracker(sorted(rows, key=lambda r: (r["ts"], r["id"])))
    tracker.advance(float(timestamp))
    return tracker.state_map


def install(core):
    if getattr(core, "_TEACHING_LEARNING_BRIDGE_INSTALLED", False):
        return
    if core.ENGINE is None or core.STORE is None:
        return

    import teaching as teaching_module
    import manual_context_learning as manual_context

    cls = teaching_module.Teaching
    original_teach = cls.teach

    def teach(self, engine, agent, desired=None, sample_ts=None, **feedback_meta):
        # Capture broad context independently from the compact policy schema. Historical
        # clicks use only archived as-of state; live state is never substituted for them.
        if sample_ts is None:
            with engine.lock:
                broad_states = dict(engine.state_map)
        else:
            timestamp = self.timestamp(sample_ts)
            broad_states = _historical_broad_state(core, engine, timestamp)

        result = original_teach(
            self, engine, agent, desired=desired, sample_ts=sample_ts, **feedback_meta
        )
        # Conflict rows are deliberately not turned into learning examples.
        if result.get("conflict") or result.get("label_id") is None:
            return result

        fresh = core.STORE.get_agent_config(agent["id"]) or agent
        rejected = None
        try:
            rejected = float(result.get("feedback", {}).get("rejected_action"))
        except (TypeError, ValueError):
            try:
                rejected = float(engine.runtime.get(agent["id"], {}).get("last_prediction"))
            except (TypeError, ValueError):
                pass

        learning = manual_context.observe(
            core, fresh, broad_states, float(result["desired_value"]), rejected=rejected,
            source="teach_history" if sample_ts is not None else "teach_live",
            user_id="teach-ui", refresh_policy=True,
        )
        result["context_learning"] = learning

        # A schema refresh may have happened after the label was stored. Re-evaluate using
        # the same Teaching.distance contract rather than a bridge-specific matcher.
        self.refresh(engine, fresh)
        journal = getattr(engine, "manual_feedback_journal", None)
        if journal is not None and result.get("feedback_id"):
            row = journal.set_status(
                result["feedback_id"],
                result.get("feedback", {}).get("application_status") or "applied",
                learning_effect={
                    "context_learning": True,
                    "context_schema_changed": bool(learning.get("schema_changed")),
                    "context_added": learning.get("added") or [],
                    "context_removed": learning.get("removed") or [],
                },
            )
            result["feedback"] = row
            result["ui_message"] = journal.ui_summary(row)

        core.STORE.event(
            fresh["id"], "info", "teaching_supervised_context",
            "Teach correction recorded as supervised context feedback",
            {
                "label_id": result.get("label_id"),
                "feedback_id": result.get("feedback_id"),
                "sample_ts": result.get("sample_ts"),
                "schema_changed": bool(learning.get("schema_changed")),
                "added": learning.get("added") or [],
                "removed": learning.get("removed") or [],
                "scores": learning.get("scores") or {},
            },
        )
        return result

    cls.teach = teach
    core._TEACHING_LEARNING_BRIDGE_INSTALLED = True
    core.STORE.event(
        None, "info", "teaching_learning_bridge_ready",
        "Teach corrections feed supervised context learning through the shared Teaching contract",
        {"context_matcher": "teaching.distance", "signature_contract": 2},
    )
