#!/usr/bin/env python3
"""Run the Stage-12 policy-backend benchmark without changing the live backend.

The command is opt-in.  It reads explicit/manual demonstrations and Stage-11 TrialRecords,
performs chronological train/validation/future-test splitting, persists the diagnostic
result, and prints JSON.  It never dispatches HA services or changes rl_models.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from policy_backend_benchmark import run_benchmark, trial_records_to_episodes
from storage import Store
from trial_knowledge import ensure_trial_tables


def _ts(value, fallback):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return float(fallback)


def manual_demonstrations(store, agent_id, start_index=0):
    with store.conn() as c:
        rows = c.execute(
            """SELECT id,created_at,action_index,reward,reason,features_json
               FROM rl_feedback WHERE agent_id=? ORDER BY id""",
            (str(agent_id),),
        ).fetchall()
    episodes = []
    for offset, row in enumerate(rows):
        row = dict(row)
        reason = str(row.get("reason") or "").lower()
        if "manual" not in reason or float(row.get("reward") or 0.0) <= 0.0:
            continue
        try:
            features = {int(k): float(v) for k, v in json.loads(row.get("features_json") or "{}").items()}
            action = int(row["action_index"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        episodes.append({
            "id": f"feedback:{row['id']}",
            "timestamp": _ts(row.get("created_at"), start_index + offset),
            "features": features,
            "allowed_actions": None,
            "kind": "demonstration",
            "demonstration_action": action,
            "demonstration_source": "manual",
            "executed_action": None,
            "reward": None,
            "action_propensities": {},
        })
    return episodes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/data/adaptive_ai.db")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--enable-shadow", action="store_true",
                        help="required opt-in; benchmark remains diagnostic-only")
    parser.add_argument("--max-features", type=int, default=24)
    args = parser.parse_args()

    env_enabled = os.environ.get("HOMEMIND_POLICY_BACKEND_SHADOW", "").strip().lower() in {"1", "true", "yes", "on"}
    if not args.enable_shadow and not env_enabled:
        print(json.dumps({
            "status": "disabled",
            "reason": "set --enable-shadow or HOMEMIND_POLICY_BACKEND_SHADOW=1",
            "default_backend": "diagonal_linucb",
            "automatic_backend_switch": False,
        }, sort_keys=True))
        return 0

    store = Store(Path(args.db))
    ensure_trial_tables(store)
    episodes = manual_demonstrations(store, args.agent)
    episodes.extend(trial_records_to_episodes(store, args.agent))
    episodes.sort(key=lambda row: (float(row.get("timestamp") or 0.0), str(row.get("id") or "")))
    if len(episodes) < 5:
        print(json.dumps({
            "status": "insufficient_evidence",
            "episodes": len(episodes),
            "default_backend": "diagonal_linucb",
            "automatic_backend_switch": False,
        }, sort_keys=True))
        return 0

    indices = []
    for row in episodes:
        if row.get("demonstration_action") is not None:
            indices.append(int(row["demonstration_action"]))
        if row.get("executed_action") is not None:
            indices.append(int(row["executed_action"]))
        indices.extend(int(x) for x in (row.get("allowed_actions") or []))
    action_count = max(indices or [1]) + 1
    actions = [float(i) for i in range(action_count)]
    for row in episodes:
        if not row.get("allowed_actions"):
            row["allowed_actions"] = list(range(action_count))

    result = run_benchmark(
        episodes, actions, max_features=max(4, args.max_features),
        store=store, agent_id=args.agent,
    )
    result["status"] = "complete"
    result["evidence"] = {
        "manual_demonstrations": sum(1 for row in episodes if row.get("kind") == "demonstration"),
        "trial_records": sum(1 for row in episodes if row.get("kind") == "bandit"),
    }
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
