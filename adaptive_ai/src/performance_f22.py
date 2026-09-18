"""Stage 17 / F22: bounded history, diagnostics and training costs.

This module changes *how* established metrics are evaluated, never what counts as evidence.
Raw Candidate pairs, decisions, Teach labels and immutable episode outcomes remain the audit
source of truth.  Runtime status paths consume versioned sufficient statistics/cursors and
bounded batched as-of queries instead of repeatedly scanning all history.

The extension is deliberately installed after the characterized Candidate/Teach stack.  It
keeps the public API unchanged and adds only diagnostics/backpressure metadata.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import deque
from contextlib import nullcontext
from types import MethodType
import hashlib
import json
import math
import threading
import time

import agent_candidate_preference_metrics as preference
import agent_candidate_shadow_runtime as shadow_runtime
import teaching_rl as teaching_rl
from context import archived_state, context_scalar, is_fast_reactive_agent
from settings import OPTIONS


CONTRACT_VERSION = 2
FAST_STATE_VERSION = 1
SUMMARY_CURSOR_VERSION = 1
FAST_PAIR_CHUNK = 256
TEACH_CANDIDATE_CHUNK = 24
ANCHOR_ACTIVE_LIMIT = 64
DIAGNOSTIC_SAMPLES = 256


def _lock(store):
    return getattr(store, "lock", nullcontext())


def _table_exists(c, name):
    return bool(c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (str(name),)
    ).fetchone())


def _json(raw, default):
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw or "")
    except Exception:
        return default


def _finite(value, default=0.0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def ensure_tables(store):
    """Add only accelerators/cursors.  Evidence tables are never rewritten."""
    with _lock(store), store.conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS candidate_generation_summary_cursors (
                parent_generation_id TEXT NOT NULL,
                child_generation_id TEXT NOT NULL,
                cursor_version INTEGER NOT NULL,
                last_pair_rowid INTEGER NOT NULL DEFAULT 0,
                summary_json TEXT NOT NULL DEFAULT '{}',
                updated_ts REAL NOT NULL,
                PRIMARY KEY(parent_generation_id,child_generation_id)
            );
            CREATE TABLE IF NOT EXISTS candidate_fast_metric_state (
                parent_generation_id TEXT NOT NULL,
                child_generation_id TEXT NOT NULL,
                state_version INTEGER NOT NULL,
                option_fingerprint TEXT NOT NULL,
                last_pair_rowid INTEGER NOT NULL DEFAULT 0,
                state_json TEXT NOT NULL DEFAULT '{}',
                updated_ts REAL NOT NULL,
                PRIMARY KEY(parent_generation_id,child_generation_id)
            );
            CREATE INDEX IF NOT EXISTS idx_candidate_pairs_edge_outcome_f22
                ON candidate_generation_pairs(parent_generation_id,child_generation_id,outcome_ts);
            CREATE INDEX IF NOT EXISTS idx_candidate_decisions_generation_ts_f22
                ON candidate_generation_decisions(generation_id,ts);
            -- entity_history already has idx_entity_history_entity_ts(entity_id,ts)
            -- and because id is INTEGER PRIMARY KEY / rowid, SQLite stores rowid as the
            -- implicit final key of the secondary index. Creating a second
            -- (entity_id,ts,id) index here adds no useful ordering for replay but can
            -- spend minutes rebuilding hundreds of thousands of rows on a Raspberry Pi
            -- during the first start after upgrade.
            """
        )
        if _table_exists(c, "adaptation_regression_anchors"):
            cols = {str(row[1]) for row in c.execute(
                "PRAGMA table_info(adaptation_regression_anchors)"
            ).fetchall()}
            if "active" not in cols:
                c.execute(
                    "ALTER TABLE adaptation_regression_anchors ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
                )
            if "retired_ts" not in cols:
                c.execute(
                    "ALTER TABLE adaptation_regression_anchors ADD COLUMN retired_ts REAL"
                )
            c.execute(
                """CREATE INDEX IF NOT EXISTS idx_adaptation_anchor_active_f22
                   ON adaptation_regression_anchors(agent_id,active,retained_ts DESC)"""
            )


class PerformanceDiagnostics:
    """Tiny bounded latency ledger; status never scans historical tables."""
    def __init__(self):
        self.lock = threading.RLock()
        self.write_ms = deque(maxlen=DIAGNOSTIC_SAMPLES)
        self.batch_rows = deque(maxlen=DIAGNOSTIC_SAMPLES)
        self.backpressure_rejections = 0
        self.summary_bootstraps = 0
        self.summary_incremental_updates = 0
        self.fast_metric_bootstraps = 0
        self.fast_metric_incremental_updates = 0
        self.teach_batches = 0
        self.confidence_selection_scans = 0
        self.confidence_selection_cache_hits = 0
        self.confidence_final_scans = 0
        self.confidence_final_cache_hits = 0
        self.confidence_probability_scans = 0
        self.confidence_probability_cache_hits = 0

    def record_write(self, started):
        elapsed = max(0.0, (time.perf_counter() - float(started)) * 1000.0)
        with self.lock:
            self.write_ms.append(elapsed)
        return elapsed

    def record_batch(self, rows):
        with self.lock:
            self.batch_rows.append(max(0, int(rows or 0)))

    @staticmethod
    def _percentile(values, q):
        values = sorted(float(x) for x in values)
        if not values:
            return None
        idx = min(len(values) - 1, max(0, int(math.ceil(q * len(values))) - 1))
        return values[idx]

    def snapshot(self, training_queue=None):
        with self.lock:
            writes = list(self.write_ms)
            batches = list(self.batch_rows)
            out = {
                "contract_version": CONTRACT_VERSION,
                "write_commit_latency_ms_p95": self._percentile(writes, .95),
                "write_commit_latency_ms_max": max(writes) if writes else None,
                "max_rows_materialized_per_batch": max(batches) if batches else 0,
                "backpressure_rejections": int(self.backpressure_rejections),
                "summary_bootstraps": int(self.summary_bootstraps),
                "summary_incremental_updates": int(self.summary_incremental_updates),
                "fast_metric_bootstraps": int(self.fast_metric_bootstraps),
                "fast_metric_incremental_updates": int(self.fast_metric_incremental_updates),
                "teach_batches": int(self.teach_batches),
                "confidence_selection_scans": int(self.confidence_selection_scans),
                "confidence_selection_cache_hits": int(self.confidence_selection_cache_hits),
                "confidence_final_scans": int(self.confidence_final_scans),
                "confidence_final_cache_hits": int(self.confidence_final_cache_hits),
                "confidence_probability_scans": int(self.confidence_probability_scans),
                "confidence_probability_cache_hits": int(self.confidence_probability_cache_hits),
            }
        if training_queue is not None:
            cv = getattr(training_queue, "cv", nullcontext())
            with cv:
                queued = list(getattr(training_queue, "jobs", ()) or ())
                oldest = min((float(x.get("queued_at") or time.time()) for x in queued), default=None)
                out.update({
                    "training_queue_depth": len(queued),
                    "training_queue_capacity": int(getattr(training_queue, "_f22_capacity", 0) or 0),
                    "training_queue_oldest_wait_seconds": None if oldest is None else max(0.0, time.time() - oldest),
                })
        return out


def _apply_pair(summary, pair):
    p_ok = bool(pair["parent_correct"])
    c_ok = bool(pair["child_correct"])
    outcome = float(pair["outcome"])
    summary["samples"] += 1
    summary["live_correct"] += int(p_ok)
    summary["candidate_correct"] += int(c_ok)
    summary["parent_correct"] += int(p_ok)
    summary["child_correct"] += int(c_ok)
    if c_ok and not p_ok:
        summary["candidate_wins"] += 1
        summary["child_wins"] += 1
    elif p_ok and not c_ok:
        summary["live_wins"] += 1
        summary["parent_wins"] += 1
    elif p_ok and c_ok:
        summary["both_correct"] += 1
    else:
        summary["both_wrong"] += 1
    key = str(float(outcome))
    slot = summary["per_action"].setdefault(
        key, {"samples": 0, "live_correct": 0, "candidate_correct": 0}
    )
    slot["samples"] += 1
    slot["live_correct"] += int(p_ok)
    slot["candidate_correct"] += int(c_ok)
    if pair.get("parent_lead_seconds") is not None or pair.get("child_lead_seconds") is not None:
        if outcome >= .5:
            summary["on_events"] += 1
            summary["live_on_lead_sum"] += float(pair.get("parent_lead_seconds") or 0.0)
            summary["candidate_on_lead_sum"] += float(pair.get("child_lead_seconds") or 0.0)
        else:
            summary["off_events"] += 1
            summary["live_off_lead_sum"] += float(pair.get("parent_lead_seconds") or 0.0)
            summary["candidate_off_lead_sum"] += float(pair.get("child_lead_seconds") or 0.0)
    return summary


def _summary_cursor(store, parent_gid, child_gid):
    with store.conn() as c:
        row = c.execute(
            """SELECT * FROM candidate_generation_summary_cursors
               WHERE parent_generation_id=? AND child_generation_id=?""",
            (str(parent_gid), str(child_gid)),
        ).fetchone()
    return dict(row) if row else None


def _save_summary_and_cursor(manager, edge, summary, last_rowid, diagnostics):
    """Persist comparison and its cursor atomically so restart cannot double-apply a pair."""
    parent_gid = str(edge["parent_generation_id"])
    child_gid = str(edge["child_generation_id"])
    generation = shadow_runtime._generation(manager.store, generation_id=child_gid)
    if not generation:
        return None
    root_id = str(generation["root_agent_id"])
    summary["updated_ts"] = time.time()
    raw = json.dumps(summary, separators=(",", ":"))
    row = manager._candidate_row(edge["parent_agent_id"]) or edge
    derived = manager._comparison_summary({**row, "comparison_json": raw})
    state = "ready" if derived.get("promotable") else "comparing"
    started = time.perf_counter()
    with _lock(manager.store), manager.store.conn() as c:
        c.execute(
            """INSERT INTO candidate_generation_summary_cursors
               (parent_generation_id,child_generation_id,cursor_version,last_pair_rowid,summary_json,updated_ts)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(parent_generation_id,child_generation_id) DO UPDATE SET
                 cursor_version=excluded.cursor_version,last_pair_rowid=excluded.last_pair_rowid,
                 summary_json=excluded.summary_json,updated_ts=excluded.updated_ts""",
            (parent_gid, child_gid, SUMMARY_CURSOR_VERSION, int(last_rowid), raw, time.time()),
        )
        c.execute(
            """INSERT INTO candidate_generation_comparisons
               (parent_generation_id,child_generation_id,root_agent_id,summary_json,updated_ts)
               VALUES(?,?,?,?,?)
               ON CONFLICT(parent_generation_id,child_generation_id) DO UPDATE SET
                 summary_json=excluded.summary_json,updated_ts=excluded.updated_ts""",
            (parent_gid, child_gid, root_id, raw, time.time()),
        )
        c.execute(
            """UPDATE agent_candidates SET comparison_json=?,state=?,updated_ts=?
               WHERE parent_agent_id=? AND candidate_id=?""",
            (raw, state, time.time(), edge["parent_agent_id"], edge["candidate_id"]),
        )
        c.execute(
            """UPDATE agent_candidate_generations SET comparison_json=?,lifecycle_state=?,updated_ts=?
               WHERE generation_id=?""",
            (raw, state, time.time(), child_gid),
        )
    diagnostics.record_write(started)
    return derived


def _install_incremental_summary(manager, diagnostics):
    legacy = shadow_runtime._rebuild_summary
    if getattr(shadow_runtime, "_f22_incremental_summary_installed", False):
        return

    def incremental_rebuild(manager_obj, edge):
        parent_gid = str(edge["parent_generation_id"])
        child_gid = str(edge["child_generation_id"])
        cursor = _summary_cursor(manager_obj.store, parent_gid, child_gid)
        existing = shadow_runtime._comparison_row(manager_obj.store, parent_gid, child_gid)
        current = shadow_runtime._json(
            (existing or {}).get("summary_json"), shadow_runtime._blank_summary()
        )
        if cursor and int(cursor.get("cursor_version") or 0) == SUMMARY_CURSOR_VERSION:
            summary = {**shadow_runtime._blank_summary(), **_json(cursor.get("summary_json"), {})}
            last_rowid = int(cursor.get("last_pair_rowid") or 0)
            diagnostics.summary_incremental_updates += 1
        else:
            summary = shadow_runtime._blank_summary()
            last_rowid = 0
            diagnostics.summary_bootstraps += 1
        # False-early counters are event counters outside candidate_generation_pairs.
        summary["live_false_early"] = int(current.get("live_false_early") or 0)
        summary["candidate_false_early"] = int(current.get("candidate_false_early") or 0)

        processed = 0
        while True:
            with manager_obj.store.conn() as c:
                rows = [dict(r) for r in c.execute(
                    """SELECT rowid AS _f22_rowid,* FROM candidate_generation_pairs
                       WHERE parent_generation_id=? AND child_generation_id=? AND rowid>?
                       ORDER BY rowid LIMIT ?""",
                    (parent_gid, child_gid, int(last_rowid), FAST_PAIR_CHUNK),
                ).fetchall()]
            diagnostics.record_batch(len(rows))
            if not rows:
                break
            for pair in rows:
                _apply_pair(summary, pair)
                last_rowid = max(last_rowid, int(pair["_f22_rowid"]))
                processed += 1
            if len(rows) < FAST_PAIR_CHUNK:
                break
        if not processed and cursor:
            # Preserve legacy behaviour: the caller may rely on a freshly projected gate.
            return shadow_runtime._persist_summary(manager_obj, edge, summary)
        return _save_summary_and_cursor(manager_obj, edge, summary, last_rowid, diagnostics)

    shadow_runtime._f22_legacy_rebuild_summary = legacy
    shadow_runtime._rebuild_summary = incremental_rebuild
    shadow_runtime._f22_incremental_summary_installed = True


def _fast_option_fingerprint():
    payload = {
        "half_life": preference._option_float(
            "candidate_preference_half_life_opportunities",
            preference.DEFAULT_HALF_LIFE_OPPORTUNITIES, 1.0,
        ),
        "on_window": preference._window_for(1.0),
        "off_window": preference._window_for(0.0),
        "correction_penalty": preference._option_float(
            "candidate_preference_correction_penalty", preference.DEFAULT_CORRECTION_PENALTY, 0.0
        ),
        "decision_stale": shadow_runtime.DECISION_STALE_SECONDS,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]


def _blank_fast_state():
    return {
        "pairs": 0,
        "parent_correct": 0,
        "child_correct": 0,
        "per_action": {"0.0": 0, "1.0": 0},
        "parent_utility_sum": 0.0,
        "child_utility_sum": 0.0,
        "success_weight": 0.0,
        "failure_weight_base": 0.0,
        "on_count": 0,
        "off_count": 0,
        "on_parent_lead_sum": 0.0,
        "on_child_lead_sum": 0.0,
        "off_parent_lead_sum": 0.0,
        "off_child_lead_sum": 0.0,
        "corrections": {},
    }


def _load_fast_state(store, parent_gid, child_gid, fingerprint):
    with store.conn() as c:
        row = c.execute(
            """SELECT * FROM candidate_fast_metric_state
               WHERE parent_generation_id=? AND child_generation_id=?""",
            (str(parent_gid), str(child_gid)),
        ).fetchone()
    if not row or int(row["state_version"] or 0) != FAST_STATE_VERSION or str(row["option_fingerprint"]) != fingerprint:
        return _blank_fast_state(), 0, False
    return {**_blank_fast_state(), **_json(row["state_json"], {})}, int(row["last_pair_rowid"] or 0), True


def _save_fast_state(store, parent_gid, child_gid, fingerprint, last_rowid, state, diagnostics):
    started = time.perf_counter()
    with _lock(store), store.conn() as c:
        c.execute(
            """INSERT INTO candidate_fast_metric_state
               (parent_generation_id,child_generation_id,state_version,option_fingerprint,last_pair_rowid,state_json,updated_ts)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(parent_generation_id,child_generation_id) DO UPDATE SET
                 state_version=excluded.state_version,option_fingerprint=excluded.option_fingerprint,
                 last_pair_rowid=excluded.last_pair_rowid,state_json=excluded.state_json,updated_ts=excluded.updated_ts""",
            (str(parent_gid), str(child_gid), FAST_STATE_VERSION, fingerprint, int(last_rowid),
             json.dumps(state, separators=(",", ":"), sort_keys=True), time.time()),
        )
    diagnostics.record_write(started)


def _batch_decisions(store, parent_gid, child_gid, pairs):
    if not pairs:
        return {str(parent_gid): ([], []), str(child_gid): ([], [])}
    max_window = max(preference._window_for(0.0), preference._window_for(1.0), shadow_runtime.DECISION_STALE_SECONDS)
    lo = min(float(p["outcome_ts"]) for p in pairs) - max_window
    hi = max(float(p["outcome_ts"]) for p in pairs)
    with store.conn() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT generation_id,ts,desired FROM candidate_generation_decisions
               WHERE generation_id IN (?,?) AND ts>=? AND ts<=?
               ORDER BY generation_id,ts""",
            (str(parent_gid), str(child_gid), lo, hi),
        ).fetchall()]
    out = {str(parent_gid): ([], []), str(child_gid): ([], [])}
    for row in rows:
        gid = str(row["generation_id"])
        times, values = out.setdefault(gid, ([], []))
        times.append(float(row["ts"])); values.append(row["desired"])
    return out


def _lead_from_batch(series, outcome, outcome_ts, window_seconds):
    times, values = series
    if not times:
        return None
    outcome_ts = float(outcome_ts)
    idx = bisect_right(times, outcome_ts) - 1
    if idx < 0 or preference._binary(values[idx]) != preference._binary(outcome):
        return None
    earliest = times[idx]
    newer = outcome_ts
    while idx >= 0:
        ts = times[idx]
        if newer - ts > shadow_runtime.DECISION_STALE_SECONDS:
            break
        if preference._binary(values[idx]) != preference._binary(outcome):
            break
        earliest = ts
        newer = ts
        idx -= 1
    return max(0.0, min(max(1.0, float(window_seconds)), outcome_ts - earliest))


def _correction_key(row):
    return f"{round(float(row['sample_ts']),3)}:{round(float(row['desired']),6)}"


def _reconcile_corrections(store, state, generation, created_ts, parent_gid, child_gid, half_life, last_rowid):
    rows = preference._manual_corrections(store, generation, created_ts)
    active = {_correction_key(row): row for row in rows}
    saved = dict(state.get("corrections") or {})
    # Undo removes influence but not the underlying audit row.
    saved = {key: value for key, value in saved.items() if key in active}
    penalty = preference._option_float(
        "candidate_preference_correction_penalty", preference.DEFAULT_CORRECTION_PENALTY, 0.0
    )
    for key, row in active.items():
        if key in saved:
            continue
        created = float(row["created_ts"])
        later = 0
        if last_rowid > 0:
            with store.conn() as c:
                later = int(c.execute(
                    """SELECT COUNT(*) FROM candidate_generation_pairs
                       WHERE parent_generation_id=? AND child_generation_id=?
                         AND rowid<=? AND outcome_ts>?""",
                    (str(parent_gid), str(child_gid), int(last_rowid), created),
                ).fetchone()[0])
        saved[key] = {
            "created_ts": created,
            "weight": penalty * (0.5 ** (float(later) / max(1.0, half_life))),
        }
    changed = saved != dict(state.get("corrections") or {})
    state["corrections"] = saved
    return rows, changed


def _install_fast_metrics(manager, diagnostics):
    if getattr(preference, "_f22_fast_metrics_installed", False):
        return
    legacy = preference._fast_metrics

    def fast_metrics(manager_obj, row, parent, candidate, base_summary, candidate_status=None):
        generation = preference._generation_for_candidate(manager_obj.store, row.get("candidate_id"))
        if not generation or not generation.get("parent_generation_id"):
            return None
        parent_gid = str(generation["parent_generation_id"])
        child_gid = str(generation["generation_id"])
        fingerprint = _fast_option_fingerprint()
        state, last_rowid, warm = _load_fast_state(manager_obj.store, parent_gid, child_gid, fingerprint)
        diagnostics.fast_metric_incremental_updates += int(warm)
        diagnostics.fast_metric_bootstraps += int(not warm)
        half_life = preference._option_float(
            "candidate_preference_half_life_opportunities",
            preference.DEFAULT_HALF_LIFE_OPPORTUNITIES, 1.0,
        )
        created_ts = float(generation.get("created_ts") or row.get("queued_ts") or 0.0)
        corrections, correction_changed = _reconcile_corrections(
            manager_obj.store, state, generation, created_ts, parent_gid, child_gid, half_life, last_rowid
        )
        decay = 0.5 ** (1.0 / max(1.0, half_life))
        processed = 0
        while True:
            with manager_obj.store.conn() as c:
                pairs = [dict(r) for r in c.execute(
                    """SELECT rowid AS _f22_rowid,* FROM candidate_generation_pairs
                       WHERE parent_generation_id=? AND child_generation_id=? AND rowid>?
                       ORDER BY rowid LIMIT ?""",
                    (parent_gid, child_gid, int(last_rowid), FAST_PAIR_CHUNK),
                ).fetchall()]
            diagnostics.record_batch(len(pairs))
            if not pairs:
                break
            decisions = _batch_decisions(manager_obj.store, parent_gid, child_gid, pairs)
            for pair in pairs:
                outcome = preference._binary(pair.get("outcome"))
                p_ok = bool(pair.get("parent_correct")); c_ok = bool(pair.get("child_correct"))
                window = preference._window_for(outcome)
                p_lead = _lead_from_batch(decisions.get(parent_gid, ([], [])), outcome, pair["outcome_ts"], window)
                c_lead = _lead_from_batch(decisions.get(child_gid, ([], [])), outcome, pair["outcome_ts"], window)
                if p_lead is None:
                    p_lead = pair.get("parent_lead_seconds")
                if c_lead is None:
                    c_lead = pair.get("child_lead_seconds")
                p_lead = max(0.0, _finite(p_lead)); c_lead = max(0.0, _finite(c_lead))

                state["success_weight"] = float(state.get("success_weight") or 0.0) * decay
                state["failure_weight_base"] = float(state.get("failure_weight_base") or 0.0) * decay
                if c_ok:
                    state["success_weight"] += 1.0
                else:
                    state["failure_weight_base"] += 1.0
                for item in (state.get("corrections") or {}).values():
                    if float(pair["outcome_ts"]) > float(item.get("created_ts") or 0.0):
                        item["weight"] = float(item.get("weight") or 0.0) * decay

                state["pairs"] = int(state.get("pairs") or 0) + 1
                state["parent_correct"] = int(state.get("parent_correct") or 0) + int(p_ok)
                state["child_correct"] = int(state.get("child_correct") or 0) + int(c_ok)
                key = str(outcome)
                state.setdefault("per_action", {}).setdefault(key, 0)
                state["per_action"][key] = int(state["per_action"][key]) + 1
                state["parent_utility_sum"] = float(state.get("parent_utility_sum") or 0.0) + preference._timing_utility(p_ok, p_lead, window)
                state["child_utility_sum"] = float(state.get("child_utility_sum") or 0.0) + preference._timing_utility(c_ok, c_lead, window)
                prefix = "on" if outcome >= .5 else "off"
                state[f"{prefix}_count"] = int(state.get(f"{prefix}_count") or 0) + 1
                state[f"{prefix}_parent_lead_sum"] = float(state.get(f"{prefix}_parent_lead_sum") or 0.0) + p_lead
                state[f"{prefix}_child_lead_sum"] = float(state.get(f"{prefix}_child_lead_sum") or 0.0) + c_lead
                last_rowid = max(last_rowid, int(pair["_f22_rowid"]))
                processed += 1
            if len(pairs) < FAST_PAIR_CHUNK:
                break
        if processed or correction_changed or not warm:
            _save_fast_state(
                manager_obj.store, parent_gid, child_gid, fingerprint, last_rowid, state, diagnostics
            )

        count = int(state.get("pairs") or 0)
        parent_accuracy = float(state.get("parent_correct") or 0) / count if count else None
        child_accuracy = float(state.get("child_correct") or 0) / count if count else None
        parent_utility = float(state.get("parent_utility_sum") or 0.0) / count if count else None
        child_utility = float(state.get("child_utility_sum") or 0.0) / count if count else None
        timing_gain = None if parent_utility is None or child_utility is None else child_utility - parent_utility
        correction_weight = sum(float(x.get("weight") or 0.0) for x in (state.get("corrections") or {}).values())
        success_weight = float(state.get("success_weight") or 0.0)
        failure_weight = float(state.get("failure_weight_base") or 0.0) + correction_weight
        pref_confidence = preference._preference_confidence(success_weight, failure_weight)
        anchor = preference._teach_anchor_status(candidate_status or {})
        min_samples = preference._option_int("candidate_fast_min_opportunities", preference.DEFAULT_MIN_OPPORTUNITIES, 1)
        min_per_action = preference._option_int("candidate_fast_min_per_action", preference.DEFAULT_MIN_PER_ACTION, 1)
        min_preference = preference._option_float(
            "candidate_fast_min_preference_confidence", preference.DEFAULT_MIN_PREFERENCE_CONFIDENCE, 0.0
        )
        max_regression = preference._option_float(
            "agent_candidate_max_accuracy_regression", preference.DEFAULT_MAX_ACCURACY_REGRESSION, 0.0
        )
        accuracy_safety = bool(
            child_accuracy is not None and parent_accuracy is not None
            and child_accuracy + max_regression >= parent_accuracy
        )
        timing_safety = bool(timing_gain is not None and timing_gain >= -0.02)
        per_action = {"0.0": int((state.get("per_action") or {}).get("0.0", 0)),
                      "1.0": int((state.get("per_action") or {}).get("1.0", 0))}
        on_count = int(state.get("on_count") or 0); off_count = int(state.get("off_count") or 0)
        return {
            "comparison_metric": "fast_timing_preference",
            "meaningful_opportunities": count,
            "required_future_samples": min_samples,
            "future_sample_count_ready": count >= min_samples,
            "required_future_samples_per_action": min_per_action,
            "per_action_ready": all(per_action[str(v)] >= min_per_action for v in (0.0, 1.0)),
            "fast_per_action_samples": per_action,
            "parent_transition_accuracy": parent_accuracy,
            "candidate_transition_accuracy": child_accuracy,
            "timing_parent_utility": parent_utility,
            "timing_candidate_utility": child_utility,
            "timing_objective_gain": timing_gain,
            "preference_confidence": pref_confidence,
            "preference_confidence_threshold": min_preference,
            "preference_success_weight": success_weight,
            "preference_failure_weight": failure_weight,
            "manual_corrections_since_generation": len(corrections),
            "manual_correction_weight": correction_weight,
            "corrections_per_100_opportunities": (100.0 * len(corrections) / count) if count else None,
            "teach_anchor_fit": anchor["fit"],
            "teach_anchor_total": anchor["total"],
            "teach_anchor_passed": anchor["passed"],
            "accuracy_safety_passed": accuracy_safety,
            "timing_safety_passed": timing_safety,
            "no_new_corrections": len(corrections) == 0,
            "fast_on_parent_lead_seconds": float(state.get("on_parent_lead_sum") or 0.0) / on_count if on_count else None,
            "fast_on_candidate_lead_seconds": float(state.get("on_child_lead_sum") or 0.0) / on_count if on_count else None,
            "fast_off_parent_lead_seconds": float(state.get("off_parent_lead_sum") or 0.0) / off_count if off_count else None,
            "fast_off_candidate_lead_seconds": float(state.get("off_child_lead_sum") or 0.0) / off_count if off_count else None,
        }

    preference._f22_legacy_fast_metrics = legacy
    preference._fast_metrics = fast_metrics
    preference._f22_fast_metrics_installed = True


def _batched_supervised_scores(self, agent):
    labels = [r for r in self.labels(agent["id"]) if r["fingerprint"] == teaching_rl.fingerprint(agent)]
    candidates = self.eligible_entities(agent)
    eligible, evidence = teaching_rl._feature_evidence(agent, labels)
    stats = {"labels": len(labels), "candidates": len(candidates), **evidence,
             "history_query_mode": "batched_asof", "candidate_batch_size": TEACH_CANDIDATE_CHUNK}
    if not eligible:
        return {}, stats

    times = [float(r["sample_ts"]) for r in labels]
    desired = [float(r["desired"]) for r in labels]
    half_life_days = max(1.0, float(OPTIONS.get("policy_half_life_days", 30)))
    now = time.time()
    weights = [math.exp(-math.log(2.0) * max(0.0, now-t) / (half_life_days*86400.0)) for t in times]
    fast = is_fast_reactive_agent(agent)
    recency_tau = 12.0 if fast else max(60.0, float(OPTIONS.get("temporal_short_seconds", 60)))
    scores = {}
    diagnostics = getattr(self, "_f22_diagnostics", None)

    label_values = ",".join(f"({i},?,?,?)" for i in range(len(labels)))
    label_params = []
    for ts, target, weight in zip(times, desired, weights):
        label_params.extend((ts, target, weight))
    for offset in range(0, len(candidates), TEACH_CANDIDATE_CHUNK):
        batch = list(candidates[offset:offset+TEACH_CANDIDATE_CHUNK])
        if not batch:
            continue
        wanted_values = ",".join("(?)" for _ in batch)
        sql = f"""
            WITH wanted(entity_id) AS (VALUES {wanted_values}),
                 labels(label_idx,sample_ts,desired,weight) AS (VALUES {label_values}),
                 picked AS (
                    SELECT w.entity_id AS candidate_entity,l.label_idx,l.sample_ts,l.desired,l.weight,
                           (SELECT h.id FROM entity_history h
                            WHERE h.entity_id=w.entity_id AND h.ts<=l.sample_ts
                            ORDER BY h.ts DESC,h.id DESC LIMIT 1) AS history_id
                    FROM wanted w CROSS JOIN labels l
                 )
            SELECT p.candidate_entity,p.label_idx,p.sample_ts,p.desired,p.weight,
                   h.id,h.entity_id,h.ts,h.state,h.attributes_json,h.context_user_id,h.source
            FROM picked p LEFT JOIN entity_history h ON h.id=p.history_id
            ORDER BY p.candidate_entity,p.label_idx
        """
        with self.store.conn() as c:
            rows = [dict(r) for r in c.execute(sql, batch + label_params).fetchall()]
        if diagnostics is not None:
            diagnostics.teach_batches += 1
            diagnostics.record_batch(len(rows))
        grouped = {eid: [] for eid in batch}
        for row in rows:
            grouped.setdefault(str(row["candidate_entity"]), []).append(row)
        for eid in batch:
            xs, ys, ws, recencies, paired_labels = [], [], [], [], []
            for row in grouped.get(eid, []):
                if row.get("id") is None or row.get("ts") is None:
                    continue
                st = archived_state(row)
                val = context_scalar(eid, st, agent)
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(val):
                    continue
                xs.append(val); ys.append(float(row["desired"])); ws.append(float(row["weight"]))
                recencies.append(max(0.0, float(row["sample_ts"]) - float(row["ts"])))
                paired_labels.append({"sample_ts": float(row["sample_ts"]), "desired": float(row["desired"])})
            candidate_eligible, _ = teaching_rl._feature_evidence(agent, paired_labels)
            if not candidate_eligible or not xs:
                continue
            if max(xs)-min(xs) <= 1e-8 or max(ys)-min(ys) <= 1e-8:
                continue
            corr = abs(teaching_rl._weighted_corr(xs, ys, ws))
            coverage = min(1.0, len(xs) / max(2.0, float(len(labels))))
            recency = sum(math.exp(-age/recency_tau) for age in recencies) / max(1, len(recencies))
            evidence_factor = teaching_rl._feature_evidence_factor(len(xs))
            score = min(1.0, corr * coverage * (0.82 + 0.18*recency) * evidence_factor)
            if score >= 0.05:
                scores[eid] = round(score, 6)
    return scores, stats


def _install_teach_batches(engine, diagnostics):
    service = getattr(engine, "rl_teaching", None)
    if service is None or getattr(service, "_f22_batched_scores_installed", False):
        return
    service._f22_legacy_supervised_scores = service.supervised_scores
    service._f22_diagnostics = diagnostics
    service.supervised_scores = MethodType(_batched_supervised_scores, service)
    service._f22_batched_scores_installed = True


def _install_confidence_bounded_views(manager, diagnostics):
    """Attach Stage-17 diagnostics to Stage-13 journals.

    The journals own durable revision/cache semantics; Stage 17 only observes their
    bounded behavior so status diagnostics and the benchmark can verify it.
    """
    epochs = getattr(manager, "confidence_evaluation_epochs", None)
    probabilities = getattr(manager, "confidence_probability_journal", None)
    if epochs is not None:
        epochs._performance_diagnostics = diagnostics
    if probabilities is not None:
        probabilities._performance_diagnostics = diagnostics


def _install_anchor_retention(manager):
    service = getattr(manager, "adaptation_service", None)
    if service is None or getattr(service, "_f22_anchor_retention_installed", False):
        return
    original_retain = service.retain_regression_anchors

    def retain(self, agent_id, episode_ids, reason="pre_drift_baseline"):
        result = original_retain(agent_id, episode_ids, reason=reason)
        now = time.time()
        with _lock(self.store), self.store.conn() as c:
            if _table_exists(c, "adaptation_regression_anchors"):
                rows = c.execute(
                    """SELECT episode_id FROM adaptation_regression_anchors
                       WHERE agent_id=? AND active=1 ORDER BY retained_ts DESC,episode_id DESC""",
                    (str(agent_id),),
                ).fetchall()
                retire = [str(row[0]) for row in rows[ANCHOR_ACTIVE_LIMIT:]]
                if retire:
                    placeholders = ",".join("?" for _ in retire)
                    c.execute(
                        f"""UPDATE adaptation_regression_anchors SET active=0,retired_ts=?
                            WHERE agent_id=? AND episode_id IN ({placeholders})""",
                        [now, str(agent_id)] + retire,
                    )
        return result

    def anchors(self, agent_id):
        with self.store.conn() as c:
            return [dict(row) for row in c.execute(
                """SELECT * FROM adaptation_regression_anchors
                   WHERE agent_id=? AND active=1
                   ORDER BY retained_ts,episode_id LIMIT ?""",
                (str(agent_id), ANCHOR_ACTIVE_LIMIT),
            ).fetchall()]

    def audit(self, agent_id, limit=256):
        limit = max(1, min(1000, int(limit)))
        with self.store.conn() as c:
            return [dict(row) for row in c.execute(
                """SELECT * FROM adaptation_regression_anchors
                   WHERE agent_id=? ORDER BY retained_ts DESC,episode_id DESC LIMIT ?""",
                (str(agent_id), limit),
            ).fetchall()]

    service.retain_regression_anchors = MethodType(retain, service)
    service.regression_anchors = MethodType(anchors, service)
    service.regression_anchor_audit = MethodType(audit, service)
    service._f22_anchor_retention_installed = True


def _install_queue_backpressure(core, diagnostics):
    queue = getattr(core, "TRAINING_QUEUE", None)
    if queue is None or getattr(queue, "_f22_backpressure_installed", False):
        return
    capacity = max(2, int(OPTIONS.get("training_queue_max_pending", 16)))
    queue._f22_capacity = capacity
    original_enqueue = queue.enqueue
    original_snapshot = queue.snapshot

    def enqueue(self, agent_id, rebuild=False, reason="training"):
        with self.cv:
            existing = agent_id in self.pending or bool(self.active and self.active.get("agent_id") == agent_id)
            depth = len(self.jobs)
        if not existing and depth >= self._f22_capacity:
            diagnostics.backpressure_rejections += 1
            try:
                self.store.event(
                    agent_id, "warning", "training_queue_backpressure",
                    "Training request deferred because the bounded heavy-work queue is full",
                    {"capacity": self._f22_capacity, "queued": depth, "retryable": True},
                )
            except Exception:
                pass
            return {"state": "backpressure", "retryable": True, "position": None,
                    "ahead": depth + (1 if self.active else 0), "capacity": self._f22_capacity,
                    "agent_id": agent_id, "rebuild": bool(rebuild), "reason": str(reason)}
        return original_enqueue(agent_id, rebuild=rebuild, reason=reason)

    def snapshot(self):
        result = original_snapshot()
        result["performance"] = diagnostics.snapshot(self)
        return result

    queue.enqueue = MethodType(enqueue, queue)
    queue.snapshot = MethodType(snapshot, queue)
    queue._f22_backpressure_installed = True


def install(manager, *, core=None):
    """Install F22 optimizations after Stage 11-16 composition has been established."""
    if getattr(manager, "_performance_f22_installed", False):
        return manager
    ensure_tables(manager.store)
    diagnostics = PerformanceDiagnostics()
    _install_incremental_summary(manager, diagnostics)
    _install_fast_metrics(manager, diagnostics)
    _install_teach_batches(manager.engine, diagnostics)
    _install_confidence_bounded_views(manager, diagnostics)
    _install_anchor_retention(manager)
    if core is not None:
        _install_queue_backpressure(core, diagnostics)
    manager.performance_f22 = diagnostics
    manager.engine.performance_f22 = diagnostics
    manager.performance_f22_contract = {
        "version": CONTRACT_VERSION,
        "candidate_summary": "durable_incremental_cursor_raw_pairs_retained",
        "fast_metrics": "bounded_pair_batches_batched_decision_history_durable_sufficient_statistics",
        "teach_scores": "batched_asof_cte_max_24_candidates_x_256_labels",
        "regression_anchors": f"{ANCHOR_ACTIVE_LIMIT}_active_references_full_audit_rows_retained",
        "confidence_selection": "sqlite_streamed_exact_readiness_one_python_row_plus_revision_cache",
        "confidence_final": "calibration_only_future_rows_change_revision_cache_fixed_end_reuse",
        "probability_calibration": "sqlite_streamed_exact_bins_plus_scope_revision_cache",
        "training_queue": "bounded_deduplicated_backpressure",
        "status": "cached_or_incremental_no_full_history_scan_after_bootstrap",
    }
    return manager
