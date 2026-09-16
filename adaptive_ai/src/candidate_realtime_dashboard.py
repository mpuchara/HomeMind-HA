"""Sub-second Candidate dashboard snapshot without changing learning semantics.

Candidate evidence/status remains durable and SQLite-backed.  This module only mirrors the
last Shadow bundle in RAM and exposes a lightweight HTTP endpoint for the four decision
tiles.  It wraps ``after_live_process`` after the complete Candidate stack is installed, so
it cannot dispatch actions or alter Candidate scoring/promotion.
"""
import math
import time
from urllib.parse import urlsplit

from context import target_value


STALE_SECONDS = 95.0


def install(manager):
    if getattr(manager, "_candidate_realtime_dashboard_installed", False):
        return manager

    original_after = manager.after_live_process
    handler = manager.core.Handler
    original_get = handler.do_GET
    latest = {}

    def _generation_meta(generation_ids):
        ids = [str(x) for x in generation_ids if x]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with manager.store.conn() as c:
            rows = c.execute(
                f"""SELECT generation_id,parent_generation_id,generation_number
                    FROM agent_candidate_generations WHERE generation_id IN ({placeholders})""",
                ids,
            ).fetchall()
        return {str(row["generation_id"]): dict(row) for row in rows}

    def after_live_process(agent, state_map):
        bundle = original_after(agent, state_map)
        if not bundle:
            return bundle
        root_id = str(bundle.get("root_agent_id") or agent["id"])
        generation_ids = tuple(sorted(str(x) for x in (bundle.get("results") or {}).keys()))
        previous = latest.get(root_id) or {}
        meta = previous.get("meta") if previous.get("generation_ids") == generation_ids else None
        if meta is None:
            meta = _generation_meta(generation_ids)
        latest[root_id] = {
            "bundle": bundle,
            "generation_ids": generation_ids,
            "meta": meta,
            "target_entity": str(agent.get("target_entity") or ""),
            "target_property": str(agent.get("target_property") or ""),
        }
        return bundle

    def snapshot():
        now = time.time()
        rows = []
        # Called while Engine.lock is held by the request wrapper.  Copy references first
        # so a later event cannot make us iterate a changing dictionary.
        for root_id, state in list(latest.items()):
            bundle = state.get("bundle") or {}
            bundle_ts = float(bundle.get("ts") or 0.0)
            if not bundle_ts or now - bundle_ts > STALE_SECONDS:
                continue
            results = dict(bundle.get("results") or {})
            meta = dict(state.get("meta") or {})
            current = bundle.get("current")
            try:
                physical = target_value(
                    manager.engine.state_map.get(state.get("target_entity")),
                    state.get("target_property"),
                )
                if physical is not None and math.isfinite(float(physical)):
                    current = float(physical)
            except (TypeError, ValueError):
                pass
            for generation_id, child in results.items():
                info = meta.get(str(generation_id)) or {}
                parent_generation_id = info.get("parent_generation_id")
                if not parent_generation_id:
                    continue
                parent = results.get(str(parent_generation_id))
                if not parent:
                    continue
                rows.append({
                    "root_agent_id": root_id,
                    "generation_id": str(generation_id),
                    "parent_generation_id": str(parent_generation_id),
                    "generation_number": info.get("generation_number"),
                    "ts": bundle_ts,
                    "shadow_current": current,
                    "parent_desired": parent.get("desired"),
                    "candidate_desired": child.get("desired"),
                    "candidate_confidence": child.get("confidence"),
                    "target_property": state.get("target_property"),
                })
        return rows

    def do_get(http):
        if urlsplit(http.path).path == "/api/candidate-live":
            if not http.require_trusted_client() or not http.require_runtime():
                return
            with manager.engine.lock:
                rows = snapshot()
            return http.send_json(200, {"ts": time.time(), "candidates": rows})
        return original_get(http)

    manager.after_live_process = after_live_process
    manager.candidate_live_snapshot = snapshot
    handler.do_GET = do_get
    manager._candidate_realtime_dashboard_installed = True
    manager.candidate_realtime_dashboard_contract = "ram_shadow_bundle_http_200ms_ui_poll"
    return manager
