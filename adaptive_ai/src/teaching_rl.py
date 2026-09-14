"""Historical Teach as supervised data for deterministic RL rebuilds.

This module is intentionally separate from ``Teaching`` / Wrong decision. Wrong decision
keeps its existing immediate contextual/manual behaviour. Teach-RL stores historical
supervised examples, refreshes a bounded Recorder slice around those examples, re-screens
the full eligible HA context, and then sends the selected schema through the normal offline
RL rebuild before supervised fine-tuning.

The label log is the source of truth. Undo removes one label; the next rebuild starts from
raw entity_history again, so no inverse-matrix bookkeeping is required.
"""
from bisect import bisect_right
import hashlib
import json
import math
import time

from context import (
    HistoricalTemporalTracker,
    action_values,
    archived_state,
    context_scalar,
    controllable_context_exclusions,
    electrical_context_exclusions,
    is_context_candidate_entity,
    is_fast_reactive_agent,
    select_context_entities,
    target_value,
)
from manual_feedback import _manual_value
from settings import OPTIONS, iso_from_ts, parse_ts


DIAGNOSTIC_DEVICE_CLASSES = {
    "battery", "voltage", "current", "power", "energy", "signal_strength",
}
DIAGNOSTIC_TERMS = (
    "firmware", "software version", "hardware version", "sw version", "fw version",
    "battery level", "battery voltage", "rssi", "link quality", "linkquality",
    "signal strength", "uptime", "restart count", "reboot count", "ip address",
    "mac address", "diagnostic", "commissioning",
)


def fingerprint(agent):
    """Stable target identity that deliberately excludes the mutable input schema."""
    fields = ("target_entity", "target_property", "min_value", "max_value")
    raw = {k: agent.get(k) for k in fields}
    return hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()


def _diagnostic_context(entity_id, state):
    attrs = (state or {}).get("attributes") or {}
    dc = str(attrs.get("device_class") or "").strip().lower()
    if dc in DIAGNOSTIC_DEVICE_CLASSES:
        return True
    friendly = str(attrs.get("friendly_name") or "")
    text = f"{entity_id} {friendly}".lower().replace("_", " ")
    return any(term in text for term in DIAGNOSTIC_TERMS)


def _weighted_corr(xs, ys, ws):
    total = sum(ws)
    if total <= 1e-12:
        return 0.0
    mx = sum(w*x for w, x in zip(ws, xs)) / total
    my = sum(w*y for w, y in zip(ws, ys)) / total
    vx = sum(w*(x-mx)*(x-mx) for w, x in zip(ws, xs)) / total
    vy = sum(w*(y-my)*(y-my) for w, y in zip(ws, ys)) / total
    if vx <= 1e-10 or vy <= 1e-10:
        return 0.0
    cov = sum(w*(x-mx)*(y-my) for w, x, y in zip(ws, xs, ys)) / total
    return max(-1.0, min(1.0, cov / math.sqrt(vx*vy)))


def _selection_limit(agent):
    dims = int(OPTIONS.get("feature_dimensions", 128))
    limit = min(int(OPTIONS.get("max_context_entities", 28)), max(4, (dims - 9) // 4))
    if is_fast_reactive_agent(agent):
        limit = min(limit, max(2, int(OPTIONS.get("fast_max_context_entities", 8))))
    return limit


def _feature_evidence(agent, labels):
    """Return whether Teach labels are strong enough to reconsider the feature schema.

    Action learning is deliberately *not* gated here. These requirements apply only to
    feature selection. Teach labels remain valid supervised examples even when there is
    not yet enough evidence to add/remove sensors.
    """
    min_labels = max(2, int(OPTIONS.get("teach_rl_feature_min_labels", 12)))
    min_per_binary = max(1, int(OPTIONS.get("teach_rl_feature_min_per_binary_class", 5)))
    min_days = max(0.0, float(OPTIONS.get("teach_rl_feature_min_observation_days", 2)))
    evidence_samples = max(2, int(OPTIONS.get("teach_rl_feature_evidence_samples", 24)))

    clean = []
    for row in labels or []:
        try:
            ts = float(row["sample_ts"])
            desired = float(row["desired"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(ts) and math.isfinite(desired):
            clean.append({"sample_ts": ts, "desired": desired})

    actions = [float(x) for x in action_values(agent)]
    times = [r["sample_ts"] for r in clean]
    span_days = ((max(times) - min(times)) / 86400.0) if len(times) >= 2 else 0.0
    action_indices = []
    if actions:
        for row in clean:
            action_indices.append(min(range(len(actions)), key=lambda i: abs(actions[i]-row["desired"])))

    stats = {
        "labels": len(clean),
        "min_labels": min_labels,
        "min_per_binary_class": min_per_binary,
        "min_observation_days": min_days,
        "observation_days": round(span_days, 6),
        "feature_evidence_samples": evidence_samples,
        "target_kind": "binary" if len(actions) == 2 else "continuous",
        "feature_selection_eligible": False,
    }

    if len(clean) < min_labels:
        stats["reason"] = f"need at least {min_labels} Teach labels for feature selection"
        return False, stats
    if span_days + 1e-9 < min_days:
        stats["reason"] = f"need at least {min_days:g} observation days for feature selection"
        return False, stats
    if not actions:
        stats["reason"] = "target has no action bins"
        return False, stats

    if len(actions) == 2:
        counts = [action_indices.count(i) for i in range(2)]
        stats["binary_class_counts"] = {
            str(actions[i]): int(counts[i]) for i in range(2)
        }
        if any(count < min_per_binary for count in counts):
            stats["reason"] = (
                f"need at least {min_per_binary} Teach labels in each binary class"
            )
            return False, stats
    else:
        distinct_bins = len(set(action_indices))
        stats["desired_action_bins"] = distinct_bins
        if distinct_bins < 3:
            stats["reason"] = "need at least 3 distinct Desired action bins for feature selection"
            return False, stats

    stats["feature_selection_eligible"] = True
    stats["reason"] = None
    return True, stats


def _feature_evidence_factor(sample_count):
    """Shrink feature evidence until a sensor has the configured sample support."""
    n = max(0, int(sample_count or 0))
    target = max(1, int(OPTIONS.get("teach_rl_feature_evidence_samples", 24)))
    if n <= 0:
        return 0.0
    return min(1.0, math.sqrt(float(n) / float(target)))


class RLTeaching:
    MAX_LABELS = 256
    MAX_HISTORY_ROWS = 40000
    ACTIVE_STATES = {"prepared", "selecting", "selected", "training", "finalizing"}

    def __init__(self, store, engine):
        self.store = store
        self.engine = engine
        with store.lock, store.conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS teaching_rl_labels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    created_ts REAL NOT NULL,
                    sample_ts REAL NOT NULL,
                    desired REAL NOT NULL,
                    previous_desired REAL,
                    fingerprint TEXT NOT NULL,
                    undone_ts REAL
                );
                CREATE INDEX IF NOT EXISTS idx_teaching_rl_agent
                    ON teaching_rl_labels(agent_id,id DESC);
                CREATE TABLE IF NOT EXISTS teaching_rl_jobs (
                    agent_id TEXT PRIMARY KEY,
                    requested_ts REAL NOT NULL,
                    state TEXT NOT NULL,
                    original_inputs_json TEXT NOT NULL,
                    pre_schema_json TEXT NOT NULL,
                    selected_inputs_json TEXT NOT NULL,
                    report_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
        self._recover_interrupted_jobs()

    def _set_inputs_direct(self, agent_id, inputs):
        """Temporary internal selector change without triggering user-facing config reset."""
        raw = json.dumps(list(inputs or ["*"]), separators=(",", ":"))
        with self.store.lock, self.store.conn() as c:
            c.execute("UPDATE agents SET input_entities=? WHERE id=?", (raw, str(agent_id)))

    def _recover_interrupted_jobs(self):
        """Queue state is in-memory; restore temporary selectors after a restart."""
        with self.store.conn() as c:
            rows = c.execute(
                "SELECT agent_id,original_inputs_json,state FROM teaching_rl_jobs "
                "WHERE state IN ('selecting','selected','training','finalizing')"
            ).fetchall()
        for row in rows:
            try:
                original = json.loads(row["original_inputs_json"] or '["*"]')
                self._set_inputs_direct(row["agent_id"], original)
                with self.store.lock, self.store.conn() as c:
                    c.execute("UPDATE teaching_rl_jobs SET state='interrupted' WHERE agent_id=?", (row["agent_id"],))
                self.store.event(row["agent_id"], "warning", "teach_rl_interrupted",
                                 "Interrupted Teach RL job restored the normal input selector", None)
            except Exception:
                pass

    def labels(self, agent_id, include_undone=False):
        where = "agent_id=?" + ("" if include_undone else " AND undone_ts IS NULL")
        with self.store.conn() as c:
            rows = c.execute(
                f"SELECT * FROM teaching_rl_labels WHERE {where} ORDER BY sample_ts,id",
                (str(agent_id),),
            ).fetchall()
        return [dict(r) for r in rows]

    def add_label(self, agent, desired, sample_ts):
        timestamp = float(sample_ts)
        if not math.isfinite(timestamp) or timestamp <= 0 or timestamp > time.time() + 2:
            raise ValueError("Nieprawidłowy czas próbki")
        if agent.get("training_state") == "training":
            raise ValueError("Poczekaj na zakończenie treningu")
        point = self.point(agent, timestamp)
        if point["current"] is None:
            raise ValueError("Brak stanu urządzenia w wybranej chwili")
        states, _, _ = self.engine.teaching.point_context(self.engine, agent, timestamp)
        desired = _manual_value(agent, states[agent["target_entity"]], desired)
        with self.store.lock, self.store.conn() as c:
            count = c.execute(
                "SELECT COUNT(*) FROM teaching_rl_labels WHERE agent_id=? AND undone_ts IS NULL",
                (agent["id"],),
            ).fetchone()[0]
            if count >= self.MAX_LABELS:
                raise ValueError("Limit 256 aktywnych punktów Teach")
            row = c.execute(
                "INSERT INTO teaching_rl_labels(agent_id,created_ts,sample_ts,desired,previous_desired,fingerprint) "
                "VALUES(?,?,?,?,?,?)",
                (agent["id"], time.time(), timestamp, float(desired), point["desired"], fingerprint(agent)),
            )
            label_id = int(row.lastrowid)
        self.store.event(agent["id"], "info", "teach_rl_label_added",
                         f"Teach RL label added: Desired {desired}",
                         {"label_id": label_id, "sample_ts": timestamp, "desired": desired})
        return {"ok": True, "label_id": label_id, "sample_ts": timestamp,
                "desired_value": float(desired), "previous_desired": point["desired"]}

    def undo(self, agent):
        with self.store.lock, self.store.conn() as c:
            row = c.execute(
                "SELECT id FROM teaching_rl_labels WHERE agent_id=? AND undone_ts IS NULL ORDER BY id DESC LIMIT 1",
                (agent["id"],),
            ).fetchone()
            if not row:
                raise ValueError("Brak punktów Teach RL do cofnięcia")
            label_id = int(row[0])
            c.execute("UPDATE teaching_rl_labels SET undone_ts=? WHERE id=?", (time.time(), label_id))
        self.store.event(agent["id"], "info", "teach_rl_label_undone",
                         "Teach RL label removed; retrain to rebuild without it", {"label_id": label_id})
        return {"ok": True, "undone_id": label_id}

    def is_retrain_active(self, agent_id):
        with self.store.conn() as c:
            row = c.execute("SELECT state FROM teaching_rl_jobs WHERE agent_id=?", (str(agent_id),)).fetchone()
        return bool(row and row["state"] in self.ACTIVE_STATES)

    def eligible_entities(self, agent):
        """Full Teach candidate universe, minus actuators/electrical/diagnostic telemetry."""
        with self.engine.lock:
            states = dict(self.engine.state_map)
            registry = dict(self.engine.entity_registry)
        excluded_control, _ = controllable_context_exclusions(states, registry)
        excluded_electrical, _ = electrical_context_exclusions(states, registry)
        excluded = excluded_control | excluded_electrical | {agent["target_entity"]}
        return [
            eid for eid, st in states.items()
            if eid not in excluded
            and is_context_candidate_entity(eid, st, excluded)
            and not _diagnostic_context(eid, st)
        ]

    def _job_report(self, agent_id):
        with self.store.conn() as c:
            row = c.execute("SELECT report_json FROM teaching_rl_jobs WHERE agent_id=?", (agent_id,)).fetchone()
        try:
            return json.loads((row["report_json"] if row else None) or '{}')
        except Exception:
            return {}

    def _set_job_stage(self, agent_id, state=None, **updates):
        report = self._job_report(agent_id)
        report.update(updates)
        with self.store.lock, self.store.conn() as c:
            if state is None:
                c.execute("UPDATE teaching_rl_jobs SET report_json=? WHERE agent_id=?", (json.dumps(report), agent_id))
            else:
                c.execute("UPDATE teaching_rl_jobs SET state=?,report_json=? WHERE agent_id=?",
                          (state, json.dumps(report), agent_id))
        return report

    @staticmethod
    def _merge_windows(times, lookback):
        windows = []
        for ts in sorted(set(float(x) for x in times)):
            start, end = ts-lookback, ts+1.0
            if windows and start <= windows[-1][1] + 1.0:
                windows[-1] = (windows[-1][0], max(windows[-1][1], end))
            else:
                windows.append((start, end))
        return windows

    def refresh_label_context(self, agent):
        """Backfill only small Recorder windows around Teach labels, on the queue thread.

        HA Recorder includes the state at a requested period boundary, so each short
        window supplies the as-of baseline plus any transitions around the label. This
        avoids importing days of unrelated whole-home telemetry merely to choose features.
        The normal Rebuild that follows still performs its usual deterministic backfill.
        """
        labels = [r for r in self.labels(agent["id"]) if r["fingerprint"] == fingerprint(agent)]
        if not labels:
            return {"chunks": 0, "rows": 0, "windows": 0}
        candidates = self.eligible_entities(agent)
        if not candidates:
            return {"chunks": 0, "rows": 0, "windows": 0}
        from ha import HA
        lookback = max(30.0, float(OPTIONS.get("fast_temporal_long_seconds", 12)) + 5.0)
        if not is_fast_reactive_agent(agent):
            lookback = max(120.0, float(OPTIONS.get("temporal_long_seconds", 300)) + 5.0)
        windows = self._merge_windows([r["sample_ts"] for r in labels], lookback)
        batches = [candidates[i:i+30] for i in range(0, len(candidates), 30)]
        total = max(1, len(windows)*len(batches))
        done = inserted = 0
        self._set_job_stage(agent["id"], state="selecting", stage="collecting_teach_context",
                            context_candidates=len(candidates), context_chunks_total=total,
                            context_chunks_done=0)
        with self.engine.lock:
            live = dict(self.engine.state_map)
        for start, end in windows:
            for batch in batches:
                data = HA.history(batch, iso_from_ts(start), iso_from_ts(end), minimal=True,
                                  no_attributes=True, significant=True, timeout=20) or []
                rows = []
                for group in data:
                    if not group:
                        continue
                    group_entity = group[0].get("entity_id")
                    for item in group:
                        eid = item.get("entity_id") or group_entity
                        ts = parse_ts(item.get("last_changed") or item.get("last_updated"))
                        if not eid or ts is None:
                            continue
                        # no_attributes keeps Recorder I/O small; current attributes retain
                        # only semantic metadata and never substitute the historical value.
                        attrs = dict((live.get(eid) or {}).get("attributes") or {})
                        rows.append((eid, ts, item.get("state"), attrs,
                                     (item.get("context") or {}).get("user_id"), "teach_rl_context"))
                if rows:
                    inserted += self.store.archive_batch(rows)
                done += 1
                self._set_job_stage(agent["id"], stage="collecting_teach_context",
                                    context_candidates=len(candidates), context_chunks_total=total,
                                    context_chunks_done=done, context_rows_imported=inserted)
                time.sleep(max(0.0, float(OPTIONS.get("history_background_pause_ms", 250))) / 1000.0)
        return {"chunks": done, "rows": inserted, "windows": len(windows), "candidates": len(candidates)}

    def supervised_scores(self, agent):
        """Score eligible HA sensors only after Teach evidence passes schema gates."""
        labels = [r for r in self.labels(agent["id"]) if r["fingerprint"] == fingerprint(agent)]
        candidates = self.eligible_entities(agent)
        eligible, evidence = _feature_evidence(agent, labels)
        stats = {"labels": len(labels), "candidates": len(candidates), **evidence}
        if not eligible:
            return {}, stats

        times = [float(r["sample_ts"]) for r in labels]
        desired = [float(r["desired"]) for r in labels]
        start, end = min(times), max(times)
        half_life_days = max(1.0, float(OPTIONS.get("policy_half_life_days", 30)))
        now = time.time()
        weights = [math.exp(-math.log(2.0) * max(0.0, now-t) / (half_life_days*86400.0)) for t in times]
        fast = is_fast_reactive_agent(agent)
        recency_tau = 12.0 if fast else max(60.0, float(OPTIONS.get("temporal_short_seconds", 60)))
        scores = {}
        with self.store.conn() as c:
            for eid in candidates:
                seed = c.execute(
                    "SELECT * FROM entity_history WHERE entity_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                    (eid, start),
                ).fetchone()
                rows = ([dict(seed)] if seed else []) + [
                    dict(r) for r in c.execute(
                        "SELECT * FROM entity_history WHERE entity_id=? AND ts>? AND ts<=? ORDER BY ts,id",
                        (eid, start, end),
                    ).fetchall()
                ]
                if not rows:
                    continue
                row_times = [float(r["ts"]) for r in rows]
                xs, ys, ws, recencies, paired_labels = [], [], [], [], []
                for ts, target, w in zip(times, desired, weights):
                    idx = bisect_right(row_times, ts) - 1
                    if idx < 0:
                        continue
                    st = archived_state(rows[idx])
                    val = context_scalar(eid, st, agent)
                    if val is None:
                        continue
                    try:
                        val = float(val)
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(val):
                        continue
                    xs.append(val); ys.append(target); ws.append(w)
                    recencies.append(max(0.0, ts-row_times[idx]))
                    paired_labels.append({"sample_ts": ts, "desired": target})
                candidate_eligible, _ = _feature_evidence(agent, paired_labels)
                if not candidate_eligible:
                    continue
                if max(xs)-min(xs) <= 1e-8 or max(ys)-min(ys) <= 1e-8:
                    continue
                corr = abs(_weighted_corr(xs, ys, ws))
                coverage = min(1.0, len(xs) / max(2.0, float(len(labels))))
                recency = sum(math.exp(-age/recency_tau) for age in recencies) / max(1, len(recencies))
                evidence_factor = _feature_evidence_factor(len(xs))
                score = min(1.0, corr * coverage * (0.82 + 0.18*recency) * evidence_factor)
                if score >= 0.05:
                    scores[eid] = round(score, 6)
        return scores, stats

    def select_features(self, agent, historical=None):
        scores, stats = self.supervised_scores(agent)
        limit = _selection_limit(agent)

        # Teach labels always remain available for supervised action fine-tuning, but
        # until the evidence gate passes they are not allowed to rotate the live schema.
        if not stats.get("feature_selection_eligible"):
            raw = self.store.get_model(agent["id"]) or {}
            selected = list(((raw.get("schema") or {}).get("entities") or []))
            if not selected:
                try:
                    selected = list(self.engine.policy(agent).schema.entities)
                except Exception:
                    selected = []
            selected = selected[:limit]
            meta = {
                "teach_rl_scores": {},
                "teach_rl_labels": stats.get("labels", 0),
                "teach_rl_candidates": stats.get("candidates", 0),
                "teach_rl_feature_selection_eligible": False,
                "teach_rl_feature_selection_reason": stats.get("reason"),
                "teach_rl_observation_days": stats.get("observation_days", 0.0),
                "teach_rl_binary_class_counts": stats.get("binary_class_counts") or {},
                "teach_rl_desired_action_bins": stats.get("desired_action_bins"),
            }
            return selected, meta, scores

        with self.engine.lock:
            all_states = dict(self.engine.state_map)
            registry = dict(self.engine.entity_registry)
        eligible_entities = set(self.eligible_entities(agent))
        state_map = {eid: st for eid, st in all_states.items()
                     if eid == agent["target_entity"] or eid in eligible_entities}
        try:
            from ha import AUTOMATION_KNOWLEDGE
            hints, _ = AUTOMATION_KNOWLEDGE.hints_for_target(agent["target_entity"])
        except Exception:
            hints = set()
        historical = dict((self.engine.context_relevance.get(agent["id"]) or {}) if historical is None else historical)
        combined = dict(historical)
        for eid, score in scores.items():
            combined[eid] = max(float(combined.get(eid, 0.0)), float(score))
        broad_agent = dict(agent)
        broad_agent["input_entities"] = ["*"]
        selected, meta = select_context_entities(
            broad_agent, state_map, registry, hints, relevance_scores=combined,
        )
        selected = list(selected[:limit])

        threshold = float(OPTIONS.get("teach_rl_feature_score", 0.60))
        strong = [(eid, float(score)) for eid, score in scores.items() if float(score) >= threshold]
        strong.sort(key=lambda x: (-x[1], x[0]))
        for eid, score in strong:
            if eid in selected:
                continue
            if len(selected) < limit:
                selected.append(eid)
                continue
            replace = min(
                range(len(selected)),
                key=lambda i: (float(scores.get(selected[i], 0.0)), float(historical.get(selected[i], 0.0)), i),
            )
            old = selected[replace]
            if float(scores.get(old, 0.0)) <= score:
                selected[replace] = eid
        meta = dict(meta or {})
        meta["teach_rl_scores"] = {k: round(v, 4) for k, v in sorted(scores.items(), key=lambda kv: -kv[1])[:20]}
        meta["teach_rl_labels"] = stats.get("labels", 0)
        meta["teach_rl_candidates"] = stats.get("candidates", 0)
        meta["teach_rl_feature_selection_eligible"] = True
        meta["teach_rl_feature_selection_reason"] = None
        meta["teach_rl_observation_days"] = stats.get("observation_days", 0.0)
        meta["teach_rl_binary_class_counts"] = stats.get("binary_class_counts") or {}
        meta["teach_rl_desired_action_bins"] = stats.get("desired_action_bins")
        return selected[:limit], meta, scores

    def prepare_retrain(self, agent):
        """Persist a lightweight job. Context backfill/selection runs on the queue thread."""
        existing = None
        with self.store.conn() as c:
            existing = c.execute("SELECT * FROM teaching_rl_jobs WHERE agent_id=?", (agent["id"],)).fetchone()
        original_inputs = list(agent.get("input_entities") or ["*"])
        pre_schema = ((self.store.get_model(agent["id"]) or {}).get("schema") or {}).get("entities") or []
        if existing and existing["state"] in self.ACTIVE_STATES:
            try:
                original_inputs = json.loads(existing["original_inputs_json"] or '["*"]')
                pre_schema = json.loads(existing["pre_schema_json"] or '[]')
            except Exception:
                pass
        report = {
            "labels": len(self.labels(agent["id"])), "selected": [], "added": [], "removed": [],
            "scores": {}, "candidates": len(self.eligible_entities(agent)), "stage": "queued",
        }
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO teaching_rl_jobs(agent_id,requested_ts,state,original_inputs_json,pre_schema_json,selected_inputs_json,report_json)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(agent_id) DO UPDATE SET requested_ts=excluded.requested_ts,state=excluded.state,
                     original_inputs_json=excluded.original_inputs_json,pre_schema_json=excluded.pre_schema_json,
                     selected_inputs_json=excluded.selected_inputs_json,report_json=excluded.report_json""",
                (agent["id"], time.time(), "prepared", json.dumps(original_inputs), json.dumps(pre_schema),
                 "[]", json.dumps(report)),
            )
        self.store.event(agent["id"], "info", "teach_rl_prepared",
                         "Teach RL queued for context refresh and feature re-selection", report)
        return report

    def prepare_context_selection(self, agent):
        """Queue-thread preflight: gate evidence, then optionally refresh/select context."""
        labels = [r for r in self.labels(agent["id"]) if r["fingerprint"] == fingerprint(agent)]
        evidence_ok, evidence = _feature_evidence(agent, labels)
        if evidence_ok:
            refreshed = self.refresh_label_context(agent)
        else:
            refreshed = {
                "chunks": 0, "rows": 0, "windows": 0, "candidates": len(self.eligible_entities(agent)),
                "skipped": True, "reason": evidence.get("reason"),
            }
        selected, meta, _ = self.select_features(agent)
        if not selected:
            raise ValueError("Teach RL nie znalazł dopuszczonych features do treningu")
        with self.store.conn() as c:
            row = c.execute("SELECT pre_schema_json,report_json FROM teaching_rl_jobs WHERE agent_id=?",
                            (agent["id"],)).fetchone()
        pre_schema = json.loads((row["pre_schema_json"] if row else None) or '[]')
        report = self._job_report(agent["id"])
        report.update({
            "selected": selected,
            "added": [x for x in selected if x not in pre_schema],
            "removed": [x for x in pre_schema if x not in selected],
            "scores": meta.get("teach_rl_scores") or {},
            "candidates": meta.get("teach_rl_candidates", 0),
            "context_refresh": refreshed,
            "feature_selection_eligible": bool(meta.get("teach_rl_feature_selection_eligible")),
            "feature_selection_reason": meta.get("teach_rl_feature_selection_reason"),
            "observation_days": meta.get("teach_rl_observation_days", 0.0),
            "binary_class_counts": meta.get("teach_rl_binary_class_counts") or {},
            "desired_action_bins": meta.get("teach_rl_desired_action_bins"),
            "stage": "features_selected",
        })
        # The selector is temporary configuration for the normal deterministic Rebuild.
        # If evidence is insufficient, `selected` is the existing schema, so Teach still
        # fine-tunes actions without changing which sensors the policy uses.
        self._set_inputs_direct(agent["id"], selected)
        with self.store.lock, self.store.conn() as c:
            c.execute("UPDATE teaching_rl_jobs SET state='selected',selected_inputs_json=?,report_json=? WHERE agent_id=?",
                      (json.dumps(selected), json.dumps(report), agent["id"]))
        message = (f"Teach RL selected {len(selected)} features for the offline rebuild"
                   if report["feature_selection_eligible"] else
                   f"Teach RL kept the existing {len(selected)}-feature schema; labels will still fine-tune actions")
        self.store.event(agent["id"], "info", "teach_rl_features_selected", message, report)
        return report

    def needs_context_selection(self, agent_id):
        with self.store.conn() as c:
            row = c.execute("SELECT state FROM teaching_rl_jobs WHERE agent_id=?", (agent_id,)).fetchone()
        return bool(row and row["state"] in ("prepared", "selecting"))

    def mark_training(self, agent_id):
        self._set_job_stage(agent_id, state="training", stage="training_rl")

    def _label_context(self, agent, policy, sample_ts):
        states, temporal, _ = self.engine.teaching.point_context(
            self.engine, agent, float(sample_ts), policy=policy,
        )
        current = target_value(states.get(agent["target_entity"]), agent["target_property"])
        if current is None:
            return None
        features, _, _ = policy.features(states, temporal, at_ts=float(sample_ts))
        return features

    def finalize_retrain(self, agent_id):
        """Apply active supervised examples to the freshly rebuilt base RL model."""
        with self.store.conn() as c:
            row = c.execute("SELECT * FROM teaching_rl_jobs WHERE agent_id=?", (agent_id,)).fetchone()
        if not row:
            return None
        with self.store.lock, self.store.conn() as c:
            c.execute("UPDATE teaching_rl_jobs SET state='finalizing' WHERE agent_id=?", (agent_id,))
        original_inputs = json.loads(row["original_inputs_json"] or '["*"]')
        pre_schema = json.loads(row["pre_schema_json"] or '[]')
        report = json.loads(row["report_json"] or '{}')
        try:
            agent = self.store.get_agent_config(agent_id)
            raw = self.store.get_model(agent_id)
            if not agent or not raw:
                raise RuntimeError("Offline rebuild did not produce a policy model")
            self.engine.models.pop(agent_id, None)
            policy = self.engine.policy(agent)
            labels = [r for r in self.labels(agent_id) if r["fingerprint"] == fingerprint(agent)]
            positive_weight = max(1, int(OPTIONS.get("teach_rl_positive_weight", 6)))
            negative_weight = max(0, int(OPTIONS.get("teach_rl_negative_weight", 3)))
            before_correct = 0
            usable = []
            for label in labels:
                features = self._label_context(agent, policy, label["sample_ts"])
                if features is None:
                    continue
                chosen = policy.predict(features)[0]["value"]
                desired_idx = min(range(len(policy.actions)), key=lambda i: abs(policy.actions[i]-float(label["desired"])))
                before_correct += int(abs(float(chosen)-float(policy.actions[desired_idx])) <= max(.01, float(agent.get("deadband") or .01)))
                usable.append((label, features, desired_idx))
            for label, features, desired_idx in usable:
                previous = label.get("previous_desired")
                previous_idx = None if previous is None else min(
                    range(len(policy.actions)), key=lambda i: abs(policy.actions[i]-float(previous))
                )
                for horizon in policy.horizons:
                    if previous_idx is not None and previous_idx != desired_idx:
                        for _ in range(negative_weight):
                            policy.update(horizon, previous_idx, features, -1.0)
                    for _ in range(positive_weight):
                        policy.update(horizon, desired_idx, features, 1.0)
                self.store.add_feedback(agent_id, desired_idx, policy.actions[desired_idx], 1.0,
                                        "Teach RL supervised example", features, "teach-ui", source="teach_rl")
            after_correct = 0
            for label, features, desired_idx in usable:
                chosen = policy.predict(features)[0]["value"]
                after_correct += int(abs(float(chosen)-float(policy.actions[desired_idx])) <= max(.01, float(agent.get("deadband") or .01)))
            self.store.save_model(agent_id, policy.serialize())
            self._set_inputs_direct(agent_id, original_inputs)
            restored = self.store.get_agent_config(agent_id)
            policy.agent = restored or agent
            new_schema = list(policy.schema.entities)
            report.update({
                "labels_applied": len(usable), "selected": new_schema,
                "added": [x for x in new_schema if x not in pre_schema],
                "removed": [x for x in pre_schema if x not in new_schema],
                "teach_fit_before": (before_correct/len(usable) if usable else None),
                "teach_fit_after": (after_correct/len(usable) if usable else None),
                "benchmark_score": (restored or {}).get("benchmark_score"), "stage": "done",
            })
            with self.store.lock, self.store.conn() as c:
                c.execute("UPDATE teaching_rl_jobs SET state='done',report_json=? WHERE agent_id=?",
                          (json.dumps(report), agent_id))
            rt = self.engine.runtime.setdefault(agent_id, {})
            rt["last_inference_ts"] = 0
            self.engine.wake_event.set()
            self.store.event(agent_id, "info", "teach_rl_complete",
                             f"Teach RL rebuild complete; applied {len(usable)} supervised example(s)", report)
            return report
        except Exception as exc:
            self._set_inputs_direct(agent_id, original_inputs)
            with self.store.lock, self.store.conn() as c:
                c.execute("UPDATE teaching_rl_jobs SET state='failed',report_json=? WHERE agent_id=?",
                          (json.dumps({**report, "error": f"{type(exc).__name__}: {exc}"}), agent_id))
            self.store.event(agent_id, "error", "teach_rl_failed", str(exc), report)
            raise

    def fail_preparation(self, agent_id, exc):
        with self.store.conn() as c:
            row = c.execute("SELECT original_inputs_json,report_json FROM teaching_rl_jobs WHERE agent_id=?", (agent_id,)).fetchone()
        original = json.loads((row["original_inputs_json"] if row else None) or '["*"]')
        self._set_inputs_direct(agent_id, original)
        report = self._job_report(agent_id)
        report.update({"stage": "failed", "error": f"{type(exc).__name__}: {exc}"})
        with self.store.lock, self.store.conn() as c:
            c.execute("UPDATE teaching_rl_jobs SET state='failed',report_json=? WHERE agent_id=?",
                      (json.dumps(report), agent_id))

    def status(self, agent_id):
        with self.store.conn() as c:
            row = c.execute("SELECT state,requested_ts,report_json FROM teaching_rl_jobs WHERE agent_id=?", (agent_id,)).fetchone()
        if not row:
            return {"state": "idle", "labels": len(self.labels(agent_id)), "report": {}}
        try:
            report = json.loads(row["report_json"] or '{}')
        except Exception:
            report = {}
        return {"state": row["state"], "requested_ts": row["requested_ts"],
                "labels": len(self.labels(agent_id)), "report": report}

    def point(self, agent, timestamp):
        timestamp = float(timestamp)
        policy = self.engine.teaching.clone(self.engine, agent)
        states, temporal, _ = self.engine.teaching.point_context(self.engine, agent, timestamp, policy=policy)
        current = target_value(states.get(agent["target_entity"]), agent["target_property"])
        if current is None:
            return {"ts": timestamp, "current": None, "desired": None, "context_complete": False}
        features, _, _ = policy.features(states, temporal, at_ts=timestamp)
        desired = policy.predict(features)[0]["value"]
        complete = all(states.get(eid) and states[eid].get("state") not in ("unknown", "unavailable")
                       for eid in policy.schema.entities)
        return {"ts": timestamp, "current": current, "desired": desired, "context_complete": complete}

    def history(self, agent, start, end):
        start, end = float(start), float(end)
        if not all(math.isfinite(x) for x in (start, end)) or end <= start or end-start > 31*86400:
            raise ValueError("Wybierz zakres od 1 sekundy do 31 dni")
        policy = self.engine.teaching.clone(self.engine, agent)
        ids = set(policy.schema.entities) | {agent["target_entity"]}
        lookback = max(self.engine.teaching.lags())
        rows = []
        dense = False
        for row in self.store.archive_iter(start-lookback, end, ids, chunk_size=512):
            rows.append(row)
            if len(rows) > self.MAX_HISTORY_ROWS:
                dense = True
                rows = []
                break
        times = {start, end}
        points = []
        if dense:
            times.update(start+(end-start)*i/240 for i in range(241))
            for ts in sorted(times):
                point = self.point(agent, ts)
                points.append({"ts": ts, "current": point["current"], "desired": point["desired"]})
        else:
            tracker = HistoricalTemporalTracker(sorted(rows, key=lambda r: (r["ts"], r["id"])))
            times.update(r["ts"] for r in rows if r["ts"] >= start)
            times.update(start+(end-start)*i/240 for i in range(241))
            ordered = sorted(times)
            if len(ordered) > 1000:
                ordered = [ordered[round(i*(len(ordered)-1)/999)] for i in range(1000)]
            for ts in ordered:
                tracker.advance(ts)
                states, temporal = tracker.state_map, tracker.history
                current = target_value(states.get(agent["target_entity"]), agent["target_property"])
                if current is None:
                    desired = None
                else:
                    features, _, _ = policy.features(states, temporal, at_ts=ts)
                    desired = policy.predict(features)[0]["value"]
                points.append({"ts": ts, "current": current, "desired": desired})
        labels = [
            {k: row[k] for k in ("id", "sample_ts", "desired")}
            for row in self.labels(agent["id"])
            if start <= float(row["sample_ts"]) <= end
        ]
        return {"points": points, "start": start, "end": end, "reduced": dense or len(points) >= 1000,
                "desired_source": "base_rl_policy_replay", "labels": labels}
