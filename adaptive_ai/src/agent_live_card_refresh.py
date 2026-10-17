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
    # /api/live runs frequently. Reuse the revision-invalidated complete config cache;
    # never hit SQLite merely to refresh Current / Desired / Confidence tiles.
    core.ENGINE._refresh_agent_index()
    now = time.time()
    updates = getattr(core.ENGINE, 'live_card_updates', None)
    # Capture before the snapshot: a newer notification can cause a harmless
    # duplicate read, but can never be acknowledged without its decision data.
    revision = updates.token() if updates is not None else None

    # Snapshot configs and both maps under the same short lock. Building labels and JSON
    # happens afterwards so websocket state_changed handling is not delayed by HTTP clients.
    with core.ENGINE.lock:
        agents = [
            dict(agent)
            for agent in core.ENGINE.all_agent_configs.values()
        ]
        states = {
            str(agent["id"]): core.ENGINE.state_map.get(agent["target_entity"])
            for agent in agents
        }
        runtimes = {
            str(agent["id"]): dict((core.ENGINE.runtime.get(agent["id"]) or {}).get('_live_decision')
                                  or core.ENGINE.runtime.get(agent["id"]) or {})
            for agent in agents
        }
        target_revisions = {str(agent['id']): getattr(core.ENGINE, 'entity_revisions', {}).get(
            agent['target_entity'], 0) for agent in agents}

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
                "decision_ready_ts": _finite(runtime.get('decision_ready_ts')),
                "decision_source": runtime.get('decision_source'),
                "current_newer_than_decision": (
                    runtime.get('decision_target_revision') is not None
                    and target_revisions[aid] != runtime['decision_target_revision']
                ),
                "fast_off_confirmation_active": bool(runtime.get('fast_off_confirmation_active')),
                "fast_off_confirmation_elapsed": _finite(runtime.get('fast_off_confirmation_elapsed')),
                "fast_off_confirmation_required": _finite(runtime.get('fast_off_confirmation_required')),
            }
        )

    payload = {"ts": now, "agents": values}
    if revision is not None:
        payload['revision'] = revision
    if include_configs:
        payload["configs"] = agents
    return payload


def live_request_payload(core, query):
    from telemetry import RUNTIME_DEBUG
    updates = getattr(getattr(core, 'ENGINE', None), 'live_card_updates', None)
    started = time.perf_counter()
    admitted = True
    if updates is not None and query.get('after'):
        try:
            seconds = float((query.get('wait_ms') or ['1500'])[0]) / 1000.
        except (ValueError, TypeError):
            seconds = 0.
        # No engine/Store lock or SQLite connection is held while waiting.
        admitted = updates.wait(str(query['after'][0]), seconds)
    ready = time.perf_counter()
    include_configs = query.get('bootstrap') == ['1']
    payload = core.live_agent_payload(include_configs=include_configs)
    if not admitted:
        payload['reactive'] = False
    if RUNTIME_DEBUG.enabled:
        RUNTIME_DEBUG.instant('live_card_snapshot',
            wait_ms=round((ready-started)*1000, 3),
            snapshot_ms=round((time.perf_counter()-ready)*1000, 3),
            decisions=[{key: row.get(key) for key in (
                'id', 'current_value', 'last_prediction', 'decision_ready_ts',
                'current_newer_than_decision', 'fast_off_confirmation_active',
            )} for row in (payload.get('agents') or [])[:16]])
    return payload


def install(core):
    """Install the fast read-only route without changing the core server module."""
    if getattr(core, "_agent_live_card_refresh_installed", False):
        return core

    legacy_http = not bool(getattr(core, "_final_explicit_http_only", False))
    handler = core.Handler if legacy_http else None
    original_get = handler.do_GET if legacy_http else None

    def do_get(http):
        parsed = urlsplit(http.path)
        if parsed.path == "/api/live":
            if not http.require_trusted_client() or not http.require_runtime():
                return
            return http.send_json(
                200,
                live_request_payload(core, parse_qs(parsed.query)),
            )
        return original_get(http)

    if legacy_http:
        handler.do_GET = do_get
    core._agent_live_card_refresh_installed = True
    core.live_agent_payload = lambda include_configs=False: live_agent_payload(
        core, include_configs=include_configs
    )
    return core
