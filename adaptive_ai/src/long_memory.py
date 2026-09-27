"""Deterministic bounded sparse long-term replay selection.

Stage 5 keeps the full-resolution recent training window unchanged. Older history is
scanned only for target transitions; feature/context reconstruction is deferred until
after this module has selected a bounded set of representative completed dwells.
No clustering or historical feature vectors are stored here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import math
import sqlite3

from context import archived_state, target_value


CONTRACT = "sparse_long_memory_v1"


def _finite(value, default=0.0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    return value if math.isfinite(value) else float(default)


def _priority(*parts):
    raw = "|".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


def _age_bucket(sample_ts, reference_ts):
    age_days = max(0.0, (float(reference_ts) - float(sample_ts)) / 86400.0)
    if age_days <= 14.0:
        return "08-14d"
    if age_days <= 21.0:
        return "15-21d"
    if age_days <= 28.0:
        return "22-28d"
    return "29-35d"


def _daypart(sample_ts):
    hour = datetime.fromtimestamp(float(sample_ts), tz=timezone.utc).hour
    if hour < 6:
        return "night"
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


def _day_kind(sample_ts):
    weekday = datetime.fromtimestamp(float(sample_ts), tz=timezone.utc).weekday()
    return "weekend" if weekday >= 5 else "weekday"


def _dwell_bucket(seconds):
    seconds = max(0.0, _finite(seconds))
    if seconds < 300.0:
        return "short"
    if seconds < 3600.0:
        return "medium"
    return "long"


def sparse_stratum(action_idx, sample_ts, dwell_seconds, reference_ts):
    meta = {
        "action": str(int(action_idx)),
        "day_kind": _day_kind(sample_ts),
        "daypart": _daypart(sample_ts),
        "age_bucket": _age_bucket(sample_ts, reference_ts),
        "dwell_bucket": _dwell_bucket(dwell_seconds),
    }
    meta["stratum"] = "|".join(
        meta[key]
        for key in ("action", "day_kind", "daypart", "age_bucket", "dwell_bucket")
    )
    return meta


class SparseLongMemorySelector:
    """Keep one stable representative per stratum, then bound/balance the result."""

    def __init__(self, max_samples, reference_ts, candidate_cap=None):
        self.max_samples = max(0, int(max_samples))
        self.reference_ts = float(reference_ts)
        default_cap = max(64, min(4096, max(1, self.max_samples) * 8))
        self.candidate_cap = max(1, int(candidate_cap or default_cap))
        self.best = {}
        self.considered = 0
        self.replacements = 0
        self.evictions = 0

    def consider(self, candidate):
        if self.max_samples <= 0:
            return
        candidate = dict(candidate)
        self.considered += 1
        meta = sparse_stratum(
            candidate["action_idx"],
            candidate["start_ts"],
            candidate["dwell_seconds"],
            self.reference_ts,
        )
        key = meta["stratum"]
        priority = _priority(
            candidate.get("agent_id"),
            candidate.get("history_id"),
            candidate.get("start_ts"),
            key,
        )
        candidate["stratum_meta"] = meta
        candidate["selection_priority"] = int(priority)
        previous = self.best.get(key)
        if previous is None or priority < int(previous["selection_priority"]):
            if previous is not None:
                self.replacements += 1
            self.best[key] = candidate

        if len(self.best) > self.candidate_cap:
            # Deterministic hard cap independent of target-history length.
            evict_key = max(
                self.best,
                key=lambda item: (
                    int(self.best[item]["selection_priority"]),
                    str(item),
                ),
            )
            self.best.pop(evict_key, None)
            self.evictions += 1

    def selected(self):
        if self.max_samples <= 0 or not self.best:
            return []
        by_action = {}
        for row in self.best.values():
            by_action.setdefault(int(row["action_idx"]), []).append(row)
        for rows in by_action.values():
            rows.sort(
                key=lambda row: (
                    int(row["selection_priority"]),
                    float(row["start_ts"]),
                    int(row["history_id"]),
                )
            )

        # Round-robin actions so a common OFF state cannot consume the sparse budget.
        chosen = []
        positions = {action: 0 for action in by_action}
        actions = sorted(by_action)
        while len(chosen) < self.max_samples:
            progressed = False
            for action in actions:
                pos = positions[action]
                rows = by_action[action]
                if pos >= len(rows):
                    continue
                chosen.append(rows[pos])
                positions[action] = pos + 1
                progressed = True
                if len(chosen) >= self.max_samples:
                    break
            if not progressed:
                break
        # Replay chronologically to minimize temporal-tracker rewinds.
        return sorted(
            chosen,
            key=lambda row: (float(row["start_ts"]), int(row["history_id"])),
        )

    def diagnostics(self, selected):
        selected = list(selected or ())
        action_counts = {}
        age_buckets = {}
        dayparts = {}
        day_kinds = {}
        dwell_buckets = {}
        strata = set()
        for row in selected:
            meta = dict(row.get("stratum_meta") or {})
            action = str(row.get("action_idx"))
            action_counts[action] = action_counts.get(action, 0) + 1
            for target, key in (
                (age_buckets, "age_bucket"),
                (dayparts, "daypart"),
                (day_kinds, "day_kind"),
                (dwell_buckets, "dwell_bucket"),
            ):
                value = str(meta.get(key) or "unknown")
                target[value] = target.get(value, 0) + 1
            if meta.get("stratum"):
                strata.add(str(meta["stratum"]))
        return {
            "contract": CONTRACT,
            "considered_dwells": int(self.considered),
            "retained_candidate_strata": len(self.best),
            "candidate_cap": int(self.candidate_cap),
            "selected_dwells": len(selected),
            "max_samples": int(self.max_samples),
            "replacements": int(self.replacements),
            "evictions": int(self.evictions),
            "action_distribution": action_counts,
            "age_distribution": age_buckets,
            "daypart_distribution": dayparts,
            "day_kind_distribution": day_kinds,
            "dwell_distribution": dwell_buckets,
            "strata_coverage": len(strata),
        }


def _scan_completed_dwells(
    store, agent, actions, start_ts, end_ts, *, visitor=None, checkpoint=None
):
    actions = [float(value) for value in actions]
    if not actions or float(end_ts) <= float(start_ts):
        return {"target_rows_scanned": 0, "completed_dwells": 0}
    target = str(agent["target_entity"])
    deadband = max(0.01, float(agent.get("deadband") or 0.0) * 0.05)
    previous_value = None
    pending = None
    rows_scanned = 0
    completed = 0
    for row in store.archive_iter(
        float(start_ts), float(end_ts), [target], chunk_size=256
    ):
        rows_scanned += 1
        value = target_value(archived_state(row), agent["target_property"])
        if value is None:
            continue
        if (
            previous_value is not None
            and abs(float(value) - float(previous_value)) <= deadband
        ):
            continue
        if pending is not None:
            action_idx = min(
                range(len(actions)),
                key=lambda idx: abs(actions[idx] - float(pending["value"])),
            )
            dwell_seconds = max(
                0.0, float(row["ts"]) - float(pending["row"]["ts"])
            )
            candidate = {
                "agent_id": str(agent["id"]),
                "history_id": int(pending["row"]["id"]),
                "row": dict(pending["row"]),
                "start_ts": float(pending["row"]["ts"]),
                "end_ts": float(row["ts"]),
                "dwell_seconds": dwell_seconds,
                "action_idx": int(action_idx),
                "action_value": float(actions[action_idx]),
                "user_id": pending["row"].get("context_user_id"),
                "next_user_id": row.get("context_user_id"),
            }
            completed += 1
            if callable(visitor):
                visitor(candidate)
        pending = {"row": dict(row), "value": float(value)}
        previous_value = float(value)
        if callable(checkpoint) and rows_scanned % 256 == 0:
            checkpoint("long_memory_target_scan")
    return {
        "target_rows_scanned": int(rows_scanned),
        "completed_dwells": int(completed),
    }


def collect_sparse_dwells(
    store, agent, actions, start_ts, end_ts, reference_ts, max_samples,
    *, checkpoint=None,
):
    selector = SparseLongMemorySelector(max_samples, reference_ts)
    scan = _scan_completed_dwells(
        store, agent, actions, start_ts, end_ts,
        visitor=selector.consider, checkpoint=checkpoint,
    )
    selected = selector.selected()
    return selected, {**scan, **selector.diagnostics(selected)}


def count_completed_dwells(
    store, agent, actions, start_ts, end_ts, *, checkpoint=None
):
    return _scan_completed_dwells(
        store, agent, actions, start_ts, end_ts,
        visitor=None, checkpoint=checkpoint,
    )


def filter_unseen_candidates(candidates, seen_history_ids):
    seen = {int(value) for value in (seen_history_ids or ())}
    return [
        row for row in (candidates or ())
        if int(row.get("history_id")) not in seen
    ]


def selected_history_provenance(store, history_ids):
    """Read provenance only for the already-selected bounded target rows."""
    ids = sorted({int(value) for value in (history_ids or ())})
    if not ids:
        return {}
    result = {
        history_id: {
            "event_id": None,
            "origin": "unknown",
            "source": "unknown",
        }
        for history_id in ids
    }
    try:
        with store.conn() as conn:
            for offset in range(0, len(ids), 200):
                chunk = ids[offset:offset + 200]
                marks = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""SELECT h.id AS target_history_id,e.event_id,e.origin,e.source
                        FROM entity_history h
                        LEFT JOIN provenance_history_links l
                          ON l.entity_id=h.entity_id AND l.event_time=h.ts
                        LEFT JOIN provenance_events e ON e.event_id=l.event_id
                        WHERE h.id IN ({marks})""",
                    chunk,
                ).fetchall()
                for row in rows:
                    history_id = int(row["target_history_id"])
                    result[history_id] = {
                        "event_id": row["event_id"],
                        "origin": str(row["origin"] or "unknown"),
                        "source": str(row["source"] or "unknown"),
                    }
    except (sqlite3.Error, AttributeError, TypeError):
        # Older/minimal fixtures may not have the provenance v1 tables. Unknown remains
        # explicit; production own-command protection is preserved when the tables exist.
        pass
    return result
