"""Lightweight snapshots for the normal Live-agent decision tiles.

The full ``/api/agents`` response intentionally contains diagnostics, qualification,
history counts and other comparatively expensive data.  The three values rendered at
the top of every normal agent card are different: they are runtime state and should
track the websocket-backed control loop without waiting for that heavy refresh.

This extension replaces the legacy ``/api/live`` implementation at the outer handler
layer while keeping its bootstrap contract.  It is read-only and never invokes policy
replay, training or Executor dispatch.
"""
from __future__ import annotations

import math
import time
from urllib.parse import parse_qs, urlsplit

from context import target_value
from settings import clamp


def _finite(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def live_agent_payload(core, *, include_configs=False):
    """Return only the state needed by Current / Desired / Confidence tiles."""
    agents = core.STORE.list_agent_configs()
    now = time.time()

    # Snapshot both maps under the same short lock.  Building labels and JSON happens
    # afterwards so websocket state_changed handling is not delayed by HTTP clients.
    with core.ENGINE.lock:
        states = {
            str(agent["id"]): core.ENGINE.state_map.get(agent["target_entity"])
            for agent in agents
        }
        runtimes = {
            str(agent["id"]): dict(core.ENGINE.runtime.get(agent["id"]) or {})
            for agent in agents
        }

    values = []
    for agent in agents:
        aid = str(agent["id"])
        state = states.get(aid)
        runtime = runtimes.get(aid) or {}
        prediction = _finite(runtime.get("last_prediction"))
        confidence = _finite(runtime.get("last_confidence"))
        current = _finite(target_value(state, agent["target_property"])) if state else None

        prediction_label = None
        if agent["target_property"] == "option_index" and prediction is not None:
            options = list(((state or {}).get("attributes") or {}).get("options") or [])
            if options:
                idx = int(clamp(round(prediction), 0, len(options) - 1))
                prediction_label = options[idx]

        values.append(
            {
                "id": agent["id"],
                "current_value": current,
                "last_prediction": prediction,
                "last_prediction_label": prediction_label,
                "last_confidence": confidence,
                "last_inference_ts": _finite(runtime.get("last_inference_ts")),
                "teaching_id": runtime.get("teaching_id"),
                "live_snapshot_ts": now,
            }
        )

    payload = {"ts": now, "agents": values}
    if include_configs:
        payload["configs"] = agents
    return payload


def install(core):
    """Install the fast read-only route without changing the core server module."""
    if getattr(core, "_agent_live_card_refresh_installed", False):
        return core

    handler = core.Handler
    original_get = handler.do_GET

    def do_get(http):
        parsed = urlsplit(http.path)
        if parsed.path == "/api/live":
            if not http.require_trusted_client() or not http.require_runtime():
                return
            include_configs = parse_qs(parsed.query).get("bootstrap") == ["1"]
            return http.send_json(
                200,
                live_agent_payload(core, include_configs=include_configs),
            )
        return original_get(http)

    handler.do_GET = do_get
    core._agent_live_card_refresh_installed = True
    core.live_agent_payload = lambda include_configs=False: live_agent_payload(
        core, include_configs=include_configs
    )
    return core
