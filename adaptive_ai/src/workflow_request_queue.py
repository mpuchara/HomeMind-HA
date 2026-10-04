"""Durable, idempotent admission for interactive generation workflow requests.

Correct labels are already durable when they are added.  This module makes the final
"Apply Correct" step equally durable without doing Candidate orchestration on the HTTP
thread.  The request is committed first, acknowledged with HTTP 202, and a tiny worker
later asks the existing generation workflow to create/coalesce the child Candidate.

The worker never performs historical training itself.  Candidate training remains owned by
TrainingQueue and therefore keeps the single-heavy-job safety boundary.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from urllib.parse import unquote, urlsplit


CONTRACT_VERSION = 1
ACTION_CORRECT = "correct"
STATE_ACCEPTED = "accepted"
STATE_PROCESSING = "processing"
STATE_DONE = "done"
STATE_FAILED = "failed"


def ensure_tables(store):
    with store.lock, store.conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_workflow_requests (
                request_id TEXT PRIMARY KEY,
                generation_ref TEXT NOT NULL,
                action TEXT NOT NULL,
                state TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                created_ts REAL NOT NULL,
                started_ts REAL,
                finished_ts REAL,
                updated_ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_agent_workflow_requests_state_created
                ON agent_workflow_requests(state,created_ts);
            """
        )


def _json(raw, default=None):
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {} if default is None else default


def _request_id(value=None):
    raw = str(value or "").strip()
    if not raw:
        return uuid.uuid4().hex
    if len(raw) > 128:
        raise ValueError("request_id is too long")
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in raw):
        raise ValueError("request_id contains unsupported characters")
    return raw


class WorkflowRequestQueue(threading.Thread):
    daemon = True

    def __init__(self, manager, *, poll_seconds=0.25, start_worker=True):
        super().__init__(name="adaptive-ai-workflow-requests")
        self.manager = manager
        self.store = manager.store
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        ensure_tables(self.store)
        self._recover()
        if start_worker:
            self.start()

    def _recover(self):
        # A process can stop after claiming a request but before Candidate orchestration
        # commits.  Re-admit that request on startup; the request_id and generation
        # workflow coalescing make replay deterministic and idempotent.
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """UPDATE agent_workflow_requests
                   SET state=?,started_ts=NULL,updated_ts=?
                   WHERE state=?""",
                (STATE_ACCEPTED, time.time(), STATE_PROCESSING),
            )

    @staticmethod
    def _public(row):
        if row is None:
            return None
        row = dict(row)
        return {
            "ok": row.get("state") != STATE_FAILED,
            "request_id": row["request_id"],
            "generation_ref": row["generation_ref"],
            "action": row["action"],
            "state": row["state"],
            "created_ts": row.get("created_ts"),
            "started_ts": row.get("started_ts"),
            "finished_ts": row.get("finished_ts"),
            "result": _json(row.get("result_json"), {}),
            "error": row.get("error"),
            "durable": True,
        }

    def status(self, request_id):
        rid = _request_id(request_id)
        with self.store.conn() as c:
            row = c.execute(
                "SELECT * FROM agent_workflow_requests WHERE request_id=?",
                (rid,),
            ).fetchone()
        return self._public(row)

    def enqueue_correct(self, generation_ref, request_id=None):
        rid = _request_id(request_id)
        ref = str(generation_ref or "").strip()
        if not ref:
            raise ValueError("generation reference is required")
        now = time.time()
        payload = json.dumps({"generation_ref": ref}, separators=(",", ":"))
        with self.store.lock, self.store.conn() as c:
            existing = c.execute(
                "SELECT * FROM agent_workflow_requests WHERE request_id=?",
                (rid,),
            ).fetchone()
            if existing is not None:
                if str(existing["action"]) != ACTION_CORRECT or str(existing["generation_ref"]) != ref:
                    raise ValueError("request_id already belongs to a different workflow request")
                result = self._public(existing)
            else:
                c.execute(
                    """INSERT INTO agent_workflow_requests
                       (request_id,generation_ref,action,state,payload_json,result_json,error,
                        created_ts,started_ts,finished_ts,updated_ts)
                       VALUES(?,?,?,?,?,'{}',NULL,?,NULL,NULL,?)""",
                    (rid, ref, ACTION_CORRECT, STATE_ACCEPTED, payload, now, now),
                )
                result = {
                    "ok": True,
                    "request_id": rid,
                    "generation_ref": ref,
                    "action": ACTION_CORRECT,
                    "state": STATE_ACCEPTED,
                    "created_ts": now,
                    "started_ts": None,
                    "finished_ts": None,
                    "result": {},
                    "error": None,
                    "durable": True,
                }
        self.wake_event.set()
        return result

    def _claim_next(self):
        now = time.time()
        with self.store.lock, self.store.conn() as c:
            row = c.execute(
                """SELECT * FROM agent_workflow_requests
                   WHERE state=? ORDER BY created_ts,request_id LIMIT 1""",
                (STATE_ACCEPTED,),
            ).fetchone()
            if row is None:
                return None
            updated = c.execute(
                """UPDATE agent_workflow_requests
                   SET state=?,started_ts=COALESCE(started_ts,?),updated_ts=?
                   WHERE request_id=? AND state=?""",
                (STATE_PROCESSING, now, now, row["request_id"], STATE_ACCEPTED),
            )
            if updated.rowcount != 1:
                return None
        claimed = dict(row)
        claimed["state"] = STATE_PROCESSING
        claimed["started_ts"] = claimed.get("started_ts") or now
        return claimed

    def _finish(self, request_id, *, state, result=None, error=None):
        now = time.time()
        raw = json.dumps(result or {}, separators=(",", ":"), default=str)
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """UPDATE agent_workflow_requests
                   SET state=?,result_json=?,error=?,finished_ts=?,updated_ts=?
                   WHERE request_id=?""",
                (state, raw, None if error is None else str(error), now, now, str(request_id)),
            )

    def process_once(self):
        row = self._claim_next()
        if row is None:
            return False
        rid = row["request_id"]
        try:
            if str(row.get("action")) != ACTION_CORRECT:
                raise ValueError(f"Unsupported workflow action: {row.get('action')}")
            result = self.manager.workflow_correct_commit(
                str(row["generation_ref"]), request_id=rid
            )
            self._finish(rid, state=STATE_DONE, result=result)
            try:
                self.store.event(
                    None, "info", "workflow_request_done",
                    "Durable Correct request created/coalesced a child Candidate",
                    {
                        "request_id": rid,
                        "generation_ref": row["generation_ref"],
                        "child_generation_id": (result or {}).get("child_generation_id"),
                    },
                )
            except Exception:
                pass
        except Exception as exc:
            self._finish(rid, state=STATE_FAILED, error=f"{type(exc).__name__}: {exc}")
            try:
                self.store.event(
                    None, "error", "workflow_request_failed",
                    f"Durable Correct request failed: {type(exc).__name__}: {exc}",
                    {"request_id": rid, "generation_ref": row.get("generation_ref")},
                )
            except Exception:
                pass
        return True

    def run(self):
        while not self.stop_event.is_set():
            progressed = self.process_once()
            if progressed:
                continue
            self.wake_event.wait(self.poll_seconds)
            self.wake_event.clear()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()


def install(manager, *, start_worker=True):
    if getattr(manager, "_workflow_request_queue_installed", False):
        return manager

    queue = WorkflowRequestQueue(manager, start_worker=start_worker)
    handler = manager.core.Handler
    original_get = handler.do_GET
    original_post = handler.do_POST
    original_stop = manager.stop

    def do_get(http):
        parsed = urlsplit(http.path)
        tokens = parsed.path.strip("/").split("/")
        if len(tokens) == 3 and tokens[:2] == ["api", "agent-workflow-requests"]:
            if not http.require_trusted_client() or not http.require_runtime():
                return
            request_id = unquote(tokens[2])
            try:
                status = queue.status(request_id)
            except ValueError as exc:
                return http.send_json(400, {"error": str(exc)})
            if status is None:
                return http.send_json(404, {"error": "workflow request not found"})
            return http.send_json(200, status)
        return original_get(http)

    def do_post(http):
        parsed = urlsplit(http.path)
        tokens = parsed.path.strip("/").split("/")
        if len(tokens) == 4 and tokens[:2] == ["api", "agent-workflow"] and tokens[3] == ACTION_CORRECT:
            if not http.require_trusted_client() or not http.require_runtime():
                return
            ref = unquote(tokens[2])
            try:
                payload = http.read_json()
                payload = payload if isinstance(payload, dict) else {}
                accepted = queue.enqueue_correct(ref, payload.get("request_id"))
                return http.send_json(202, accepted)
            except ValueError as exc:
                return http.send_json(409, {"error": str(exc)})
            except Exception as exc:
                return http.send_json(
                    500,
                    {"error": f"Could not durably accept Correct request: {type(exc).__name__}: {exc}"},
                )
        return original_post(http)

    def stop_with_requests():
        queue.stop()
        return original_stop()

    handler.do_GET = do_get
    handler.do_POST = do_post
    manager.stop = stop_with_requests
    manager.workflow_requests = queue
    manager._workflow_request_queue_installed = True
    manager.workflow_request_contract = (
        "correct_commit_is_durable_idempotent_202_then_async_candidate_orchestration"
    )
    manager.workflow_request_contract_version = CONTRACT_VERSION
    return manager
