"""Unified, retractable manual-feedback contract.

Stage 06 makes explicit user feedback a durable fact before any learning path consumes it.
The journal is deliberately separate from physical execution: it never constructs an
ActionIntent, never calls Executor and never calls Home Assistant. Existing UI/device
commands remain on the established Executor boundary; this module only records intent,
provenance, context and learning/application effects.

The contract is additive. Legacy Teaching/Teach-RL rows without a journal link remain
valid and are never reinterpreted. New rows may link to those legacy stores so undo can
retire the complete label and request a clean Candidate rebuild from persisted history.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import uuid


CONTRACT_VERSION = 1
CONTEXT_VERSION = 2
ERROR_KINDS = {"state", "too_early", "too_late", "brightness"}
SCOPES = {"one_time", "episode", "similar_context", "persistent_preference"}
ACTIVE_STATUSES = {"recorded", "applied", "learning_queued", "rebuild_queued"}
CONFLICT_DISTANCE = 0.015


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _finite(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("Manual feedback action must be numeric or null")
    if not math.isfinite(number):
        raise ValueError("Manual feedback action must be finite")
    return number


def _table_exists(c, name):
    return bool(c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (str(name),)
    ).fetchone())


def _signature_digest(signature):
    """Stable audit key; matching still uses semantic distance, never the digest alone."""
    cleaned = {}
    for key, value in sorted(dict(signature or {}).items()):
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            cleaned[str(key)] = round(float(value), 6)
        elif value is None:
            cleaned[str(key)] = None
        else:
            cleaned[str(key)] = str(value)
    return hashlib.sha256(_json(cleaned).encode("utf-8")).hexdigest()


class ManualFeedbackJournal:
    """Durable source of truth for all explicit/manual correction channels."""

    def __init__(self, store, clock=time.time):
        self.store = store
        self.clock = clock
        self._listeners = []
        self._migrate()

    def add_listener(self, callback):
        if callable(callback) and callback not in self._listeners:
            self._listeners.append(callback)

    def _notify(self, *agent_ids):
        ids = tuple(str(x) for x in agent_ids if x)
        for callback in tuple(self._listeners):
            try:
                callback(*ids)
            except Exception:
                pass

    def _migrate(self):
        with self.store.lock, self.store.conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS manual_feedback_journal (
                    feedback_id TEXT PRIMARY KEY,
                    contract_version INTEGER NOT NULL,
                    context_version INTEGER NOT NULL,
                    created_ts REAL NOT NULL,
                    agent_id TEXT NOT NULL,
                    root_agent_id TEXT,
                    generation_id TEXT,
                    decision_id TEXT,
                    episode_id TEXT,
                    selected_ts REAL NOT NULL,
                    source TEXT NOT NULL,
                    rejected_action REAL,
                    correct_action REAL,
                    error_kind TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    fingerprint TEXT,
                    feature_schema_version INTEGER,
                    policy_version INTEGER,
                    context_digest TEXT NOT NULL,
                    context_signature_json TEXT NOT NULL,
                    application_status TEXT NOT NULL,
                    immediate_effect_json TEXT NOT NULL DEFAULT '{}',
                    learning_effect_json TEXT NOT NULL DEFAULT '{}',
                    conflict_json TEXT NOT NULL DEFAULT '[]',
                    undone_ts REAL,
                    undo_status TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_manual_feedback_agent_time
                    ON manual_feedback_journal(agent_id,selected_ts);
                CREATE INDEX IF NOT EXISTS idx_manual_feedback_root_time
                    ON manual_feedback_journal(root_agent_id,selected_ts);
                CREATE INDEX IF NOT EXISTS idx_manual_feedback_decision
                    ON manual_feedback_journal(decision_id);
                CREATE INDEX IF NOT EXISTS idx_manual_feedback_episode
                    ON manual_feedback_journal(episode_id);
                CREATE INDEX IF NOT EXISTS idx_manual_feedback_active
                    ON manual_feedback_journal(agent_id,application_status,undone_ts);

                CREATE TABLE IF NOT EXISTS manual_feedback_effects (
                    feedback_id TEXT NOT NULL,
                    effect_kind TEXT NOT NULL,
                    ref_type TEXT NOT NULL,
                    ref_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_ts REAL NOT NULL,
                    updated_ts REAL NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(feedback_id,effect_kind,ref_type,ref_id)
                );
                CREATE INDEX IF NOT EXISTS idx_manual_feedback_effect_ref
                    ON manual_feedback_effects(ref_type,ref_id);
                """
            )

    def _decision_link(self, agent_id, selected_ts, decision_id=None, episode_id=None):
        """Resolve exact/nearby provenance without inventing historical attribution."""
        selected_ts = float(selected_ts)
        with self.store.conn() as c:
            if not _table_exists(c, "provenance_decisions"):
                return decision_id, episode_id, None
            row = None
            if decision_id:
                row = c.execute(
                    "SELECT decision_id,episode_id,generation_id,agent_id,created_time "
                    "FROM provenance_decisions WHERE decision_id=?",
                    (str(decision_id),),
                ).fetchone()
                if row and str(row["agent_id"]) != str(agent_id):
                    raise ValueError("decision_id belongs to a different agent")
            else:
                # A delayed correction selects the original moment. Keep attribution
                # bounded: absence of a nearby decision remains unknown rather than
                # binding the correction to an arbitrary older inference.
                row = c.execute(
                    """SELECT decision_id,episode_id,generation_id,agent_id,created_time
                       FROM provenance_decisions
                       WHERE agent_id=? AND created_time<=? AND created_time>=?
                       ORDER BY created_time DESC LIMIT 1""",
                    (str(agent_id), selected_ts + 0.001, selected_ts - 95.0),
                ).fetchone()
            if not row:
                return decision_id, episode_id, None
            return (
                str(row["decision_id"]),
                episode_id or row["episode_id"],
                row["generation_id"],
            )

    def _generation_link(self, agent_id, generation_id=None, root_agent_id=None):
        with self.store.conn() as c:
            if not _table_exists(c, "agent_candidate_generations"):
                return generation_id, root_agent_id or str(agent_id)
            row = None
            if generation_id:
                row = c.execute(
                    "SELECT generation_id,root_agent_id,agent_id FROM agent_candidate_generations "
                    "WHERE generation_id=?",
                    (str(generation_id),),
                ).fetchone()
            if row is None:
                row = c.execute(
                    """SELECT generation_id,root_agent_id,agent_id FROM agent_candidate_generations
                       WHERE agent_id=? ORDER BY created_ts DESC LIMIT 1""",
                    (str(agent_id),),
                ).fetchone()
            if not row:
                return generation_id, root_agent_id or str(agent_id)
            return str(row["generation_id"]), root_agent_id or str(row["root_agent_id"])

    @staticmethod
    def _scope_can_conflict(left, right):
        if left == "one_time" or right == "one_time":
            return False
        return True

    def _find_conflicts(self, *, agent_id, root_agent_id, episode_id, fingerprint,
                        signature, correct_action, deadband, scope):
        if correct_action is None or not signature:
            return []
        with self.store.conn() as c:
            rows = c.execute(
                """SELECT * FROM manual_feedback_journal
                   WHERE COALESCE(root_agent_id,agent_id)=? AND undone_ts IS NULL
                     AND correct_action IS NOT NULL
                     AND application_status IN ('recorded','applied','learning_queued','rebuild_queued')
                   ORDER BY created_ts,id""".replace(",id", ""),
                (str(root_agent_id or agent_id),),
            ).fetchall()
        try:
            from teaching import distance as context_distance
        except Exception:
            context_distance = None
        conflicts = []
        tolerance = max(0.01, float(deadband or 0.0))
        for raw in rows:
            row = dict(raw)
            if fingerprint and row.get("fingerprint") and str(row["fingerprint"]) != str(fingerprint):
                continue
            if not self._scope_can_conflict(scope, str(row.get("scope") or "similar_context")):
                continue
            if scope == "episode" or str(row.get("scope")) == "episode":
                if not episode_id or str(row.get("episode_id") or "") != str(episode_id):
                    continue
            try:
                previous_action = float(row["correct_action"])
            except (TypeError, ValueError):
                continue
            if abs(previous_action - float(correct_action)) <= tolerance:
                continue
            try:
                old_sig = json.loads(row.get("context_signature_json") or "{}")
            except Exception:
                continue
            if callable(context_distance):
                try:
                    dist = context_distance(signature, old_sig)
                except Exception:
                    dist = None
            else:
                dist = 0.0 if _signature_digest(signature) == row.get("context_digest") else None
            if dist is not None and float(dist) <= CONFLICT_DISTANCE:
                conflicts.append(str(row["feedback_id"]))
        return conflicts

    def record(self, *, agent_id, selected_ts, source, rejected_action=None,
               correct_action=None, error_kind="state", scope="similar_context",
               decision_id=None, episode_id=None, generation_id=None, root_agent_id=None,
               fingerprint=None, context_signature=None, feature_schema_version=None,
               policy_version=None, deadband=0.0, feedback_id=None):
        error_kind = str(error_kind or "state")
        scope = str(scope or "similar_context")
        if error_kind not in ERROR_KINDS:
            raise ValueError("Unsupported manual feedback error kind")
        if scope not in SCOPES:
            raise ValueError("Unsupported manual feedback scope")
        selected_ts = float(selected_ts)
        if not math.isfinite(selected_ts) or selected_ts <= 0 or selected_ts > self.clock() + 2.0:
            raise ValueError("Invalid manual feedback selected time")
        rejected_action = _finite(rejected_action)
        correct_action = _finite(correct_action)
        signature = dict(context_signature or {})
        decision_id, episode_id, decision_generation = self._decision_link(
            agent_id, selected_ts, decision_id=decision_id, episode_id=episode_id,
        )
        generation_id = generation_id or decision_generation
        generation_id, root_agent_id = self._generation_link(
            agent_id, generation_id=generation_id, root_agent_id=root_agent_id,
        )
        feedback_id = str(feedback_id or uuid.uuid4())
        conflicts = self._find_conflicts(
            agent_id=agent_id, root_agent_id=root_agent_id, episode_id=episode_id,
            fingerprint=fingerprint, signature=signature, correct_action=correct_action,
            deadband=deadband, scope=scope,
        )
        status = "conflict" if conflicts else "recorded"
        now = float(self.clock())
        with self.store.lock, self.store.conn() as c:
            existing = c.execute(
                "SELECT * FROM manual_feedback_journal WHERE feedback_id=?", (feedback_id,)
            ).fetchone()
            if existing:
                current = dict(existing)
                immutable = {
                    "agent_id": str(agent_id), "selected_ts": selected_ts,
                    "source": str(source), "rejected_action": rejected_action,
                    "correct_action": correct_action, "error_kind": error_kind, "scope": scope,
                    "context_digest": _signature_digest(signature),
                }
                for key, value in immutable.items():
                    old = current.get(key)
                    if isinstance(value, float) and old is not None:
                        if abs(float(old) - value) > 1e-9:
                            raise ValueError("feedback_id is immutable and already has different content")
                    elif old != value:
                        raise ValueError("feedback_id is immutable and already has different content")
                return self.get(feedback_id)
            c.execute(
                """INSERT INTO manual_feedback_journal
                   (feedback_id,contract_version,context_version,created_ts,agent_id,root_agent_id,
                    generation_id,decision_id,episode_id,selected_ts,source,rejected_action,
                    correct_action,error_kind,scope,fingerprint,feature_schema_version,policy_version,
                    context_digest,context_signature_json,application_status,immediate_effect_json,
                    learning_effect_json,conflict_json,undone_ts,undo_status)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'{}','{}',?,NULL,NULL)""",
                (
                    feedback_id, CONTRACT_VERSION, CONTEXT_VERSION, now, str(agent_id),
                    root_agent_id, generation_id, decision_id, episode_id, selected_ts,
                    str(source), rejected_action, correct_action, error_kind, scope,
                    fingerprint, feature_schema_version, policy_version,
                    _signature_digest(signature), _json(signature), status, _json(conflicts),
                ),
            )
            if conflicts:
                placeholders = ",".join("?" for _ in conflicts)
                c.execute(
                    f"UPDATE manual_feedback_journal SET application_status='conflict', "
                    f"conflict_json=? WHERE feedback_id IN ({placeholders}) AND undone_ts IS NULL",
                    (_json(sorted(set(conflicts + [feedback_id]))), *conflicts),
                )
        row = self.get(feedback_id)
        if row:
            self._notify(row.get("agent_id"), row.get("root_agent_id"))
        return row

    def get(self, feedback_id):
        with self.store.conn() as c:
            row = c.execute(
                "SELECT * FROM manual_feedback_journal WHERE feedback_id=?", (str(feedback_id),)
            ).fetchone()
        if not row:
            return None
        out = dict(row)
        for key in ("context_signature_json", "immediate_effect_json", "learning_effect_json", "conflict_json"):
            name = key[:-5] if key.endswith("_json") else key
            try:
                out[name] = json.loads(out.get(key) or ("[]" if key == "conflict_json" else "{}"))
            except Exception:
                out[name] = [] if key == "conflict_json" else {}
        return out

    def latest(self, agent_id, include_undone=False):
        where = "agent_id=?" + ("" if include_undone else " AND undone_ts IS NULL")
        with self.store.conn() as c:
            row = c.execute(
                f"SELECT feedback_id FROM manual_feedback_journal WHERE {where} ORDER BY created_ts DESC LIMIT 1",
                (str(agent_id),),
            ).fetchone()
        return self.get(row["feedback_id"]) if row else None

    def set_status(self, feedback_id, status, *, immediate_effect=None, learning_effect=None,
                   undo_status=None):
        row = self.get(feedback_id)
        if not row:
            raise ValueError("Manual feedback not found")
        immediate = dict(row.get("immediate_effect") or {})
        learning = dict(row.get("learning_effect") or {})
        if immediate_effect:
            immediate.update(dict(immediate_effect))
        if learning_effect:
            learning.update(dict(learning_effect))
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """UPDATE manual_feedback_journal SET application_status=?,immediate_effect_json=?,
                   learning_effect_json=?,undo_status=COALESCE(?,undo_status) WHERE feedback_id=?""",
                (str(status), _json(immediate), _json(learning), undo_status, str(feedback_id)),
            )
        row = self.get(feedback_id)
        if row:
            self._notify(row.get("agent_id"), row.get("root_agent_id"))
        return row

    def link(self, feedback_id, effect_kind, ref_type, ref_id, *, status="applied", metadata=None):
        now = float(self.clock())
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO manual_feedback_effects
                   (feedback_id,effect_kind,ref_type,ref_id,status,created_ts,updated_ts,metadata_json)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(feedback_id,effect_kind,ref_type,ref_id) DO UPDATE SET
                     status=excluded.status,updated_ts=excluded.updated_ts,metadata_json=excluded.metadata_json""",
                (str(feedback_id), str(effect_kind), str(ref_type), str(ref_id), str(status),
                 now, now, _json(metadata or {})),
            )
        return str(ref_id)

    def filter_linked_rows(self, ref_type, rows):
        """Keep legacy-unlinked rows; suppress undone/conflicted linked feedback."""
        rows = list(rows or [])
        if not rows:
            return rows
        ids = [str(row["id"]) for row in rows if row.get("id") is not None]
        if not ids:
            return rows
        placeholders = ",".join("?" for _ in ids)
        with self.store.conn() as c:
            linked = c.execute(
                f"""SELECT e.ref_id,j.application_status,j.undone_ts,e.status
                    FROM manual_feedback_effects e
                    JOIN manual_feedback_journal j ON j.feedback_id=e.feedback_id
                    WHERE e.ref_type=? AND e.ref_id IN ({placeholders})""",
                (str(ref_type), *ids),
            ).fetchall()
        state = {str(r["ref_id"]): dict(r) for r in linked}
        result = []
        for row in rows:
            entry = state.get(str(row.get("id")))
            if not entry:
                result.append(row)
                continue
            if entry.get("undone_ts") is not None:
                continue
            if str(entry.get("application_status") or "") == "conflict":
                continue
            if str(entry.get("status") or "") == "undone":
                continue
            result.append(row)
        return result

    def _retire_linked_labels(self, feedback_id, timestamp):
        retired = []
        with self.store.conn() as c:
            effects = [dict(r) for r in c.execute(
                "SELECT * FROM manual_feedback_effects WHERE feedback_id=?",
                (str(feedback_id),),
            ).fetchall()]
        with self.store.lock, self.store.conn() as c:
            for effect in effects:
                ref_type = str(effect.get("ref_type") or "")
                ref_id = effect.get("ref_id")
                table = None
                if ref_type == "teaching_label":
                    table = "teaching_labels"
                elif ref_type == "teach_rl_label":
                    table = "teaching_rl_labels"
                if table and _table_exists(c, table):
                    c.execute(
                        f"UPDATE {table} SET undone_ts=COALESCE(undone_ts,?) WHERE id=?",
                        (timestamp, ref_id),
                    )
                    retired.append({"ref_type": ref_type, "ref_id": str(ref_id)})
                c.execute(
                    """UPDATE manual_feedback_effects SET status='undone',updated_ts=?
                       WHERE feedback_id=? AND effect_kind=? AND ref_type=? AND ref_id=?""",
                    (timestamp, str(feedback_id), effect["effect_kind"], ref_type, str(ref_id)),
                )
        return retired

    def undo(self, feedback_id, *, engine=None, candidate_manager=None):
        """Withdraw a feedback fact and rebuild forward; never apply an inverse update.

        The current Live generation is not destructively rewritten. If a correction may
        already be baked into policy weights, undo queues a full-rebuild child from the
        current root Live generation. Promotion/rollback rules remain unchanged.
        """
        row = self.get(feedback_id)
        if not row:
            raise ValueError("Manual feedback not found")
        if row.get("undone_ts") is not None:
            return row
        now = float(self.clock())
        retired = self._retire_linked_labels(feedback_id, now)
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """UPDATE manual_feedback_journal SET undone_ts=?,application_status='undone',
                   undo_status='labels_retired' WHERE feedback_id=? AND undone_ts IS NULL""",
                (now, str(feedback_id)),
            )
        if engine is not None:
            teaching = getattr(engine, "teaching", None)
            if teaching is not None:
                try:
                    teaching.cache.pop(str(row["agent_id"]), None)
                    agent = self.store.get_agent_config(str(row["agent_id"]))
                    if agent:
                        teaching.refresh(engine, agent)
                except Exception:
                    pass

        learning = dict(row.get("learning_effect") or {})
        needs_rebuild = bool(
            retired
            or learning.get("model_updated")
            or learning.get("candidate_generation_id")
            or learning.get("candidate_queued")
            or learning.get("rebuild_required")
        )
        rebuild = None
        rebuild_error = None
        root_agent_id = str(row.get("root_agent_id") or row.get("agent_id"))
        if needs_rebuild and candidate_manager is not None:
            try:
                rebuild = candidate_manager.enqueue(root_agent_id, "manual_feedback_undo_rebuild")
            except Exception as exc:
                rebuild_error = f"{type(exc).__name__}: {exc}"
        undo_status = "rebuild_queued" if rebuild else ("rebuild_pending" if needs_rebuild else "undone")
        updated = self.set_status(
            feedback_id, "undone",
            learning_effect={
                "undo_retired_labels": retired,
                "undo_rebuild": rebuild,
                "undo_rebuild_error": rebuild_error,
                "rebuild_required": needs_rebuild,
            },
            undo_status=undo_status,
        )
        return updated

    @staticmethod
    def ui_summary(row):
        row = dict(row or {})
        status = str(row.get("application_status") or "recorded")
        immediate = dict(row.get("immediate_effect") or {})
        learning = dict(row.get("learning_effect") or {})
        if status == "conflict":
            return "Feedback zapisany jako konflikt; nie uczę sprzecznej preferencji."
        if status == "undone":
            if str(row.get("undo_status") or "") == "rebuild_queued":
                return "Korekta cofnięta; czysty Candidate został przebudowany z dziennika."
            return "Korekta cofnięta; jej aktywne etykiety zostały wyłączone."
        parts = []
        if immediate.get("physical_change"):
            parts.append("urządzenie zmienione od razu")
        elif immediate.get("runtime_override"):
            parts.append("decyzja lokalna zmieniona od razu")
        elif immediate.get("manual_hold"):
            parts.append("ręczne pierwszeństwo aktywne")
        if learning.get("candidate_queued") or learning.get("candidate_generation_id"):
            parts.append("nauka idzie do Candidate")
        elif learning.get("label_recorded"):
            parts.append("etykieta zapisana do nauki")
        if row.get("correct_action") is None:
            parts.append("brak założenia, jaka akcja byłaby poprawna")
        return "; ".join(parts) if parts else "Feedback zapisany."
