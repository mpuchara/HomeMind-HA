"""Persistent audit trail for Sensor Tournament schema changes.

Every automatic feature-schema mutation gets an immutable history row containing the
before/after schema and the evidence that justified the change.  The row status may later
move through the small lifecycle required by rollback/probation work:

    promoted -> accepted
    promoted -> rolled_back

This module is bookkeeping only.  It never changes policy schema, emits ActionIntent,
calls Executor, or invokes Home Assistant services.
"""
import json
import math
import time


VALID_SCHEMA_HISTORY_STATUSES = {"promoted", "accepted", "rolled_back"}


def _finite_or_none(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json_schema(value):
    return json.dumps([str(x) for x in (value or [])], separators=(",", ":"))


def _decode_schema(raw):
    try:
        value = json.loads(raw or "[]")
    except Exception:
        return []
    return [str(x) for x in value] if isinstance(value, list) else []


def _row_dict(row):
    if not row:
        return None
    return {
        "id": int(row["id"]),
        "agent_id": str(row["agent_id"]),
        "created_ts": float(row["created_ts"]),
        "old_schema": _decode_schema(row["old_schema_json"]),
        "new_schema": _decode_schema(row["new_schema_json"]),
        "reason": str(row["reason"]),
        "baseline_score": None if row["baseline_score"] is None else float(row["baseline_score"]),
        "challenger_score": None if row["challenger_score"] is None else float(row["challenger_score"]),
        "evaluation_samples": int(row["evaluation_samples"] or 0),
        "promoted_entity": row["promoted_entity"],
        "removed_entity": row["removed_entity"],
        "status": str(row["status"]),
    }


def install_schema_history(service):
    """Create the audit table and attach a small persistence API to Tournament service."""
    if getattr(service, "_schema_history_installed", False):
        return service

    with service.store.lock, service.store.conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS context_schema_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                created_ts REAL NOT NULL,
                old_schema_json TEXT NOT NULL,
                new_schema_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                baseline_score REAL,
                challenger_score REAL,
                evaluation_samples INTEGER NOT NULL DEFAULT 0,
                promoted_entity TEXT,
                removed_entity TEXT,
                status TEXT NOT NULL CHECK(status IN ('promoted','accepted','rolled_back'))
            );
            CREATE INDEX IF NOT EXISTS idx_context_schema_history_agent_ts
                ON context_schema_history(agent_id, created_ts DESC, id DESC);
            """
        )

    def record_schema_history(*, agent_id, old_schema, new_schema, reason,
                              baseline_score=None, challenger_score=None,
                              evaluation_samples=0, promoted_entity=None,
                              removed_entity=None, status="promoted", created_ts=None):
        status = str(status)
        if status not in VALID_SCHEMA_HISTORY_STATUSES:
            raise ValueError(f"invalid schema history status: {status}")
        created = float(time.time() if created_ts is None else created_ts)
        baseline = _finite_or_none(baseline_score)
        challenger = _finite_or_none(challenger_score)
        samples = max(0, int(evaluation_samples or 0))
        with service.store.lock, service.store.conn() as c:
            cur = c.execute(
                """INSERT INTO context_schema_history
                   (agent_id,created_ts,old_schema_json,new_schema_json,reason,
                    baseline_score,challenger_score,evaluation_samples,promoted_entity,
                    removed_entity,status)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(agent_id), created, _json_schema(old_schema), _json_schema(new_schema),
                    str(reason), baseline, challenger, samples,
                    None if promoted_entity is None else str(promoted_entity),
                    None if removed_entity is None else str(removed_entity), status,
                ),
            )
            return int(cur.lastrowid)

    def set_schema_history_status(history_id, status):
        status = str(status)
        if status not in VALID_SCHEMA_HISTORY_STATUSES:
            raise ValueError(f"invalid schema history status: {status}")
        with service.store.lock, service.store.conn() as c:
            cur = c.execute(
                "UPDATE context_schema_history SET status=? WHERE id=?",
                (status, int(history_id)),
            )
            return int(cur.rowcount or 0) > 0

    def schema_history(agent_id, limit=50):
        limit = max(1, min(500, int(limit or 50)))
        with service.store.conn() as c:
            rows = c.execute(
                """SELECT * FROM context_schema_history
                   WHERE agent_id=? ORDER BY created_ts DESC,id DESC LIMIT ?""",
                (str(agent_id), limit),
            ).fetchall()
        return [_row_dict(row) for row in rows]

    def schema_history_by_id(history_id):
        with service.store.conn() as c:
            row = c.execute(
                "SELECT * FROM context_schema_history WHERE id=?", (int(history_id),)
            ).fetchone()
        return _row_dict(row)

    service.record_schema_history = record_schema_history
    service.set_schema_history_status = set_schema_history_status
    service.schema_history = schema_history
    service.schema_history_by_id = schema_history_by_id
    service._schema_history_installed = True
    return service
