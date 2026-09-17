"""F22 correctness guard for late/out-of-order Candidate outcome rows.

The optimized fast-metric cursor consumes the normal online stream incrementally.  Legacy
metric semantics order opportunities by ``outcome_ts``.  A delayed row can therefore not be
folded into an already-decayed sufficient statistic without reordering earlier evidence.

Normal edges stay on the bounded F22 path.  If an edge ever receives a late outcome, this
guard switches only that edge to a persisted legacy-result cache.  The expensive legacy
recompute then happens only when pair/correction/options/Teach-anchor inputs change; repeated
status polls return the durable cached result.  Correctness wins over pretending a late row
can be appended to an order-sensitive half-life statistic.
"""
from __future__ import annotations

import hashlib
import json
import time

import agent_candidate_preference_metrics as preference
import performance_f22


GUARD_VERSION = 1


def ensure_table(store):
    with store.lock, store.conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS candidate_fast_order_guard (
                   parent_generation_id TEXT NOT NULL,
                   child_generation_id TEXT NOT NULL,
                   guard_version INTEGER NOT NULL,
                   last_checked_rowid INTEGER NOT NULL DEFAULT 0,
                   max_outcome_ts REAL,
                   fallback_legacy INTEGER NOT NULL DEFAULT 0,
                   cache_key TEXT,
                   metrics_json TEXT,
                   updated_ts REAL NOT NULL,
                   PRIMARY KEY(parent_generation_id,child_generation_id)
               )"""
        )


def _row(store, parent_gid, child_gid):
    with store.conn() as c:
        row = c.execute(
            """SELECT * FROM candidate_fast_order_guard
               WHERE parent_generation_id=? AND child_generation_id=?""",
            (str(parent_gid), str(child_gid)),
        ).fetchone()
    return dict(row) if row else None


def _save_guard(store, parent_gid, child_gid, *, last_rowid, max_outcome_ts, fallback,
                cache_key=None, metrics=None):
    raw = None if metrics is None else json.dumps(metrics, separators=(",", ":"), sort_keys=True)
    with store.lock, store.conn() as c:
        c.execute(
            """INSERT INTO candidate_fast_order_guard
               (parent_generation_id,child_generation_id,guard_version,last_checked_rowid,
                max_outcome_ts,fallback_legacy,cache_key,metrics_json,updated_ts)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(parent_generation_id,child_generation_id) DO UPDATE SET
                 guard_version=excluded.guard_version,
                 last_checked_rowid=excluded.last_checked_rowid,
                 max_outcome_ts=excluded.max_outcome_ts,
                 fallback_legacy=excluded.fallback_legacy,
                 cache_key=excluded.cache_key,
                 metrics_json=excluded.metrics_json,
                 updated_ts=excluded.updated_ts""",
            (
                str(parent_gid), str(child_gid), GUARD_VERSION, int(last_rowid or 0),
                None if max_outcome_ts is None else float(max_outcome_ts),
                int(bool(fallback)), cache_key, raw, time.time(),
            ),
        )


def _initial_edge_state(store, parent_gid, child_gid):
    """One-time migration check: verify historical rowid order matches outcome order."""
    with store.conn() as c:
        stats = c.execute(
            """SELECT COALESCE(MAX(rowid),0),MAX(outcome_ts)
               FROM candidate_generation_pairs
               WHERE parent_generation_id=? AND child_generation_id=?""",
            (str(parent_gid), str(child_gid)),
        ).fetchone()
        inversion = c.execute(
            """SELECT 1 FROM (
                   SELECT outcome_ts,LAG(outcome_ts) OVER (ORDER BY rowid) AS previous_outcome_ts
                   FROM candidate_generation_pairs
                   WHERE parent_generation_id=? AND child_generation_id=?
               )
               WHERE previous_outcome_ts IS NOT NULL AND outcome_ts < previous_outcome_ts
               LIMIT 1""",
            (str(parent_gid), str(child_gid)),
        ).fetchone()
    return int(stats[0] or 0), (None if stats[1] is None else float(stats[1])), bool(inversion)


def _advance_guard(store, parent_gid, child_gid):
    guard = _row(store, parent_gid, child_gid)
    if not guard or int(guard.get("guard_version") or 0) != GUARD_VERSION:
        last_rowid, max_outcome, fallback = _initial_edge_state(store, parent_gid, child_gid)
        _save_guard(
            store, parent_gid, child_gid, last_rowid=last_rowid,
            max_outcome_ts=max_outcome, fallback=fallback,
        )
        return _row(store, parent_gid, child_gid)

    last_rowid = int(guard.get("last_checked_rowid") or 0)
    previous_max = guard.get("max_outcome_ts")
    with store.conn() as c:
        stats = c.execute(
            """SELECT COALESCE(MAX(rowid),0),MIN(outcome_ts),MAX(outcome_ts)
               FROM candidate_generation_pairs
               WHERE parent_generation_id=? AND child_generation_id=? AND rowid>?""",
            (str(parent_gid), str(child_gid), last_rowid),
        ).fetchone()
    new_last = int(stats[0] or 0)
    if new_last <= 0:
        return guard
    min_new = None if stats[1] is None else float(stats[1])
    max_new = None if stats[2] is None else float(stats[2])
    fallback = bool(guard.get("fallback_legacy"))
    if previous_max is not None and min_new is not None and min_new < float(previous_max):
        fallback = True
    max_outcome = max(
        [float(x) for x in (previous_max, max_new) if x is not None],
        default=None,
    )
    # Pair evidence changed, so any fallback result cache is stale.
    _save_guard(
        store, parent_gid, child_gid,
        last_rowid=max(last_rowid, new_last), max_outcome_ts=max_outcome,
        fallback=fallback,
    )
    return _row(store, parent_gid, child_gid)


def _cache_key(manager, row, generation, candidate_status):
    created_ts = float(generation.get("created_ts") or row.get("queued_ts") or 0.0)
    corrections = preference._manual_corrections(manager.store, generation, created_ts)
    correction_facts = [
        (
            round(float(item.get("sample_ts") or 0.0), 6),
            round(float(item.get("desired") or 0.0), 9),
            round(float(item.get("created_ts") or 0.0), 6),
            str(item.get("source") or ""),
        )
        for item in corrections
    ]
    payload = {
        "fast_option_fingerprint": performance_f22._fast_option_fingerprint(),
        "min_samples": preference._option_int(
            "candidate_fast_min_opportunities", preference.DEFAULT_MIN_OPPORTUNITIES, 1
        ),
        "min_per_action": preference._option_int(
            "candidate_fast_min_per_action", preference.DEFAULT_MIN_PER_ACTION, 1
        ),
        "min_preference": preference._option_float(
            "candidate_fast_min_preference_confidence",
            preference.DEFAULT_MIN_PREFERENCE_CONFIDENCE, 0.0,
        ),
        "max_regression": preference._option_float(
            "agent_candidate_max_accuracy_regression",
            preference.DEFAULT_MAX_ACCURACY_REGRESSION, 0.0,
        ),
        "teach_fit_total": (candidate_status or {}).get("teach_fit_total"),
        "teach_fit_after_count": (candidate_status or {}).get("teach_fit_after_count"),
        "corrections": correction_facts,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def install(manager):
    """Keep normal F22 incremental metrics; cache exact legacy semantics after a late row."""
    if getattr(manager, "_performance_f22_order_guard_installed", False):
        return manager
    ensure_table(manager.store)
    optimized = preference._fast_metrics
    legacy = getattr(preference, "_f22_legacy_fast_metrics", None)
    if legacy is None:
        manager._performance_f22_order_guard_installed = True
        return manager

    if getattr(preference, "_f22_order_guard_global_installed", False):
        manager._performance_f22_order_guard_installed = True
        return manager

    def guarded_fast_metrics(manager_obj, row, parent, candidate, base_summary, candidate_status=None):
        generation = preference._generation_for_candidate(manager_obj.store, row.get("candidate_id"))
        if not generation or not generation.get("parent_generation_id"):
            return optimized(manager_obj, row, parent, candidate, base_summary, candidate_status)
        parent_gid = str(generation["parent_generation_id"])
        child_gid = str(generation["generation_id"])
        guard = _advance_guard(manager_obj.store, parent_gid, child_gid)
        if not guard or not bool(guard.get("fallback_legacy")):
            return optimized(manager_obj, row, parent, candidate, base_summary, candidate_status)

        key = _cache_key(manager_obj, row, generation, candidate_status)
        if guard.get("cache_key") == key and guard.get("metrics_json"):
            try:
                return json.loads(guard["metrics_json"])
            except Exception:
                pass

        metrics = legacy(manager_obj, row, parent, candidate, base_summary, candidate_status)
        _save_guard(
            manager_obj.store, parent_gid, child_gid,
            last_rowid=int(guard.get("last_checked_rowid") or 0),
            max_outcome_ts=guard.get("max_outcome_ts"), fallback=True,
            cache_key=key, metrics=metrics,
        )
        try:
            manager_obj.store.event(
                row.get("parent_agent_id"), "warning", "candidate_fast_metric_order_fallback",
                "Late Candidate outcome detected; exact outcome-time ordering is served from a cached repair path",
                {"parent_generation_id": parent_gid, "child_generation_id": child_gid},
            )
        except Exception:
            pass
        return metrics

    preference._f22_order_guard_optimized_fast_metrics = optimized
    preference._fast_metrics = guarded_fast_metrics
    preference._f22_order_guard_global_installed = True
    manager._performance_f22_order_guard_installed = True
    manager.performance_f22_contract["late_outcome_ordering"] = (
        "detect_rowid_vs_outcome_ts_inversion_then_cache_exact_legacy_metric_until_inputs_change"
    )
    return manager
