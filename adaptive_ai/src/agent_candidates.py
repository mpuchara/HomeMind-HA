"""Isolated next-generation policies for zero-downtime learning.

A Candidate is a hidden training surrogate for one live agent.  It reuses the existing
historical/Teach pipeline, but is filtered out of normal runtime agent enumeration, so it
can never create ActionIntent or call Executor.  The live agent keeps serving while the
candidate rebuilds, then both policies are evaluated on the same future target outcomes.

Explicit feedback (Wrong decision and Teach) increments a durable feedback revision.  At
most one candidate exists per live agent; repeated feedback is coalesced.  If new feedback
arrives during a long build, that build may finish but is never promotable: the same
candidate is queued for another rebuild from the newer revision.
"""
from contextlib import contextmanager
import json
import math
import threading
import time

from telemetry import RUNTIME_DEBUG


_TLS = threading.local()
_STORE_PATCHED = False
_HISTORY_PATCHED = False


def refresh_candidate_ids_cache(store):
    """Refresh the tiny hidden-Candidate ID set after a lifecycle mutation.

    Candidate membership changes only when a Candidate is created/promoted/discarded.
    Normal agent enumeration and is_candidate() checks are much hotter, so they must not
    open SQLite merely to hide a handful of surrogate IDs.
    """
    try:
        with store.conn() as c:
            values = set()
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_candidates'").fetchone():
                values.update(str(r[0]) for r in c.execute("SELECT candidate_id FROM agent_candidates").fetchall())
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_candidate_generations'").fetchone():
                values.update(
                    str(r[0]) for r in c.execute(
                        """SELECT agent_id FROM agent_candidate_generations
                           WHERE generation_type='candidate' AND agent_id IS NOT NULL"""
                    ).fetchall()
                )
    except Exception:
        values = set(getattr(store, "_candidate_ids_ram", set()) or set())
    lock = getattr(store, "_candidate_ids_ram_lock", None)
    if lock is None:
        lock = threading.RLock()
        store._candidate_ids_ram_lock = lock
    with lock:
        store._candidate_ids_ram = set(values)
        store._candidate_ids_ram_ready = True
        store._candidate_ids_ram_revision = int(getattr(store, "_candidate_ids_ram_revision", 0)) + 1
    return set(values)


def _candidate_ids(store):
    lock = getattr(store, "_candidate_ids_ram_lock", None)
    if lock is not None and getattr(store, "_candidate_ids_ram_ready", False):
        with lock:
            return set(getattr(store, "_candidate_ids_ram", set()) or set())
    return refresh_candidate_ids_cache(store)


def is_candidate(store, agent_id):
    return str(agent_id) in _candidate_ids(store)


def ensure_tables(store):
    with store.lock, store.conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_candidates (
                parent_agent_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL UNIQUE,
                generation INTEGER NOT NULL DEFAULT 1,
                state TEXT NOT NULL DEFAULT 'queued',
                reason TEXT,
                feedback_revision INTEGER NOT NULL DEFAULT 0,
                build_revision INTEGER NOT NULL DEFAULT 0,
                dirty INTEGER NOT NULL DEFAULT 1,
                queued_ts REAL,
                build_started_ts REAL,
                build_finished_ts REAL,
                comparison_started_ts REAL,
                comparison_json TEXT NOT NULL DEFAULT '{}',
                discard_requested INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                updated_ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_agent_candidates_candidate
                ON agent_candidates(candidate_id);
            CREATE TABLE IF NOT EXISTS agent_generation_state (
                agent_id TEXT PRIMARY KEY,
                generation INTEGER NOT NULL DEFAULT 0,
                updated_ts REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_generation_backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                created_ts REAL NOT NULL,
                expires_ts REAL NOT NULL,
                model_json TEXT,
                agent_json TEXT NOT NULL,
                comparison_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_agent_generation_backup_expiry
                ON agent_generation_backups(expires_ts);
            """
        )


def install_store_overlay(store):
    """Hide training surrogates from every normal live-agent enumeration."""
    global _STORE_PATCHED
    ensure_tables(store)
    refresh_candidate_ids_cache(store)
    if _STORE_PATCHED:
        return
    cls = type(store)
    cls._candidate_base_list_agents = cls.list_agents
    cls._candidate_base_list_agent_configs = cls.list_agent_configs
    cls._candidate_base_training_agent_ids = cls.training_agent_ids

    def list_agents(self):
        rows = cls._candidate_base_list_agents(self)
        if getattr(_TLS, "include_candidates", False):
            return rows
        hidden = _candidate_ids(self)
        return [a for a in rows if str(a.get("id")) not in hidden]

    def list_agent_configs(self):
        rows = cls._candidate_base_list_agent_configs(self)
        if getattr(_TLS, "include_candidates", False):
            return rows
        hidden = _candidate_ids(self)
        return [a for a in rows if str(a.get("id")) not in hidden]

    def training_agent_ids(self, unstarted_only=False):
        ids = cls._candidate_base_training_agent_ids(self, unstarted_only=unstarted_only)
        if getattr(_TLS, "include_candidates", False):
            return ids
        hidden = _candidate_ids(self)
        return [aid for aid in ids if str(aid) not in hidden]

    def find_agent_by_target(self, entity_id, property_name):
        hidden = _candidate_ids(self)
        for agent in self.list_agent_configs():
            if str(agent.get("id")) in hidden:
                continue
            if agent.get("target_entity") == entity_id and agent.get("target_property") == property_name:
                return self.get_agent(agent["id"])
        return None

    cls.list_agents = list_agents
    cls.list_agent_configs = list_agent_configs
    cls.training_agent_ids = training_agent_ids
    cls.find_agent_by_target = find_agent_by_target
    _STORE_PATCHED = True


@contextmanager
def candidate_training_scope():
    previous = bool(getattr(_TLS, "include_candidates", False))
    _TLS.include_candidates = True
    try:
        yield
    finally:
        _TLS.include_candidates = previous


def install_history_overlay(store):
    """Expose a hidden candidate only inside its explicit historical replay thread."""
    global _HISTORY_PATCHED
    if _HISTORY_PATCHED:
        return
    import history as history_module
    cls = history_module.HistoryManager
    original = cls._train_from_archive

    def train_with_candidate(self, start_ts, end_ts, **kwargs):
        ids = set(kwargs.get("agent_ids") or ())
        if ids and any(is_candidate(store, aid) for aid in ids):
            with candidate_training_scope():
                return original(self, start_ts, end_ts, **kwargs)
        return original(self, start_ts, end_ts, **kwargs)

    cls._train_from_archive = train_with_candidate
    _HISTORY_PATCHED = True


def _json(raw, default=None):
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {} if default is None else default


def _blank_comparison():
    return {
        "samples": 0,
        "live_correct": 0,
        "candidate_correct": 0,
        "candidate_wins": 0,
        "live_wins": 0,
        "both_wrong": 0,
        "per_action": {},
        "on_events": 0,
        "off_events": 0,
        "live_on_lead_sum": 0.0,
        "candidate_on_lead_sum": 0.0,
        "live_off_lead_sum": 0.0,
        "candidate_off_lead_sum": 0.0,
        "live_false_early": 0,
        "candidate_false_early": 0,
        "updated_ts": None,
    }


class AgentCandidateManager(threading.Thread):
    daemon = True

    def __init__(self, core, *, start_worker=True, poll_seconds=0.5):
        super().__init__(name="adaptive-ai-agent-candidates")
        self.core = core
        self.store = core.STORE
        self.engine = core.ENGINE
        self.poll_seconds = max(0.1, float(poll_seconds))
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.lock = threading.RLock()
        self.runtime = {}
        self._worker_health_lock = threading.RLock()
        self._worker_heartbeat_ts = 0.0
        self._worker_last_error = None
        self._worker_error_count = 0
        self._worker_restart_count = 0
        self._worker_last_error_event_ts = 0.0
        self._worker_last_error_signature = None
        self._recovery_worker = None
        ensure_tables(self.store)
        self._recover()
        self._install_feedback_hooks()
        self._install_process_wrapper()
        self._install_http()
        if start_worker:
            self.start()

    def _worker_health(self):
        recovery = getattr(self, "_recovery_worker", None)
        alive = bool(self.is_alive() or (recovery is not None and recovery.is_alive()))
        heartbeat = float(getattr(self, "_worker_heartbeat_ts", 0.0) or 0.0)
        now = time.time()
        return {
            "alive": alive,
            "heartbeat_ts": heartbeat or None,
            "heartbeat_age_seconds": (max(0.0, now - heartbeat) if heartbeat else None),
            "last_error": getattr(self, "_worker_last_error", None),
            "error_count": int(getattr(self, "_worker_error_count", 0) or 0),
            "restart_count": int(getattr(self, "_worker_restart_count", 0) or 0),
        }

    def _record_worker_error(self, phase, exc, row=None):
        detail = {
            "phase": str(phase),
            "error": f"{type(exc).__name__}: {exc}",
        }
        if row:
            detail.update({
                "parent_agent_id": row.get("parent_agent_id"),
                "candidate_id": row.get("candidate_id"),
                "state": row.get("state"),
                "reason": row.get("reason"),
            })
        now = time.time()
        signature = f"{detail['phase']}|{detail['error']}|{detail.get('candidate_id') or ''}"
        with self._worker_health_lock:
            self._worker_last_error = detail["error"]
            self._worker_error_count += 1
            self._worker_heartbeat_ts = now
            should_emit = (
                signature != self._worker_last_error_signature
                or now - float(self._worker_last_error_event_ts or 0.0) >= 30.0
            )
            if should_emit:
                self._worker_last_error_signature = signature
                self._worker_last_error_event_ts = now
        if should_emit:
            try:
                self.store.event(
                    (row or {}).get("parent_agent_id"), "error", "agent_candidate_worker_error",
                    "Candidate lifecycle worker recovered from an internal error", detail,
                )
            except Exception:
                pass

    def _ensure_worker_alive(self):
        if self.stop_event.is_set():
            return False
        recovery = getattr(self, "_recovery_worker", None)
        if self.is_alive() or (recovery is not None and recovery.is_alive()):
            return True
        # start_worker=False is used deliberately by unit tests and composition setup.
        # Before Thread.start() has ever run, leave startup ownership with the caller.
        if self.ident is None:
            return False
        with self._worker_health_lock:
            recovery = getattr(self, "_recovery_worker", None)
            if self.is_alive() or (recovery is not None and recovery.is_alive()):
                return True
            self._worker_restart_count += 1
            recovery = threading.Thread(
                target=self._worker_loop,
                name=f"adaptive-ai-agent-candidates-recovery-{self._worker_restart_count}",
                daemon=True,
            )
            self._recovery_worker = recovery
            recovery.start()
        try:
            self.store.event(
                None, "warning", "agent_candidate_worker_restarted",
                "Candidate lifecycle worker restarted after an unexpected exit",
                {"restart_count": int(self._worker_restart_count)},
            )
        except Exception:
            pass
        return True

    def _queue(self):
        return getattr(self.core, "TRAINING_QUEUE", None)

    def _claim_candidate_training_job(
        self, candidate_id, *, rebuild, reason, rebuild_reason=None
    ):
        """Own exactly one queued Candidate job without deadlocking lifecycle state.

        A Candidate can briefly acquire a TrainingQueue entry before AgentCandidateManager
        advances its durable edge from queued -> building. Returning early merely because
        status_for() is non-null leaves that edge queued forever, so Shadow comparison never
        starts and the Candidate appears to have stopped receiving events.

        Pending jobs are safe to adopt/upgrade. Active jobs are never mutated mid-flight:
        the Candidate manager waits for them to finish, then requests the intended build on
        the next poll.
        """
        queue = self._queue()
        if queue is None:
            return None, "queue_unavailable"
        candidate_id = str(candidate_id)
        reason = str(reason)
        existing = queue.status_for(candidate_id)
        if existing and str(existing.get("state") or "") == "active":
            return None, "active_external_job"

        if existing and str(existing.get("state") or "") == "queued":
            existing_reason = str(existing.get("reason") or "")
            existing_rebuild = bool(existing.get("rebuild"))
            existing_rebuild_reason = existing.get("rebuild_reason")
            desired_rebuild = bool(rebuild)
            desired_rebuild_reason = rebuild_reason

            if reason == "teach_rl":
                # TrainingQueue has an explicit atomic pending-job upgrade for Teach RL.
                queued = queue.enqueue(
                    candidate_id, rebuild=True, reason="teach_rl",
                    rebuild_reason=rebuild_reason or "feature_mask_change",
                )
                return queued, (
                    "adopted_pending_teach_rl"
                    if existing_reason == "teach_rl" and existing_rebuild
                    else "upgraded_pending_to_teach_rl"
                )

            if (
                existing_reason == reason
                and existing_rebuild == desired_rebuild
                and (
                    not desired_rebuild
                    or desired_rebuild_reason is None
                    or existing_rebuild_reason == desired_rebuild_reason
                )
            ):
                return existing, "adopted_matching_pending_job"

            # Full rebuild / autonomous continuation semantics cannot safely be inferred
            # from another pending reason. Cancel only the not-yet-active entry and replace
            # it with the exact lifecycle request.
            queue.cancel(candidate_id)

        queued = queue.enqueue(
            candidate_id, rebuild=bool(rebuild), reason=reason,
            rebuild_reason=rebuild_reason,
        )
        return queued, "enqueued_candidate_job"

    def _candidate_row(self, parent_id):
        with self.store.conn() as c:
            row = c.execute("SELECT * FROM agent_candidates WHERE parent_agent_id=?", (str(parent_id),)).fetchone()
        return dict(row) if row else None

    def _row_by_candidate(self, candidate_id):
        with self.store.conn() as c:
            row = c.execute("SELECT * FROM agent_candidates WHERE candidate_id=?", (str(candidate_id),)).fetchone()
        return dict(row) if row else None

    def _all_rows(self):
        with self.store.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM agent_candidates ORDER BY queued_ts,updated_ts")]

    def _generation(self, parent_id):
        with self.store.conn() as c:
            row = c.execute("SELECT generation FROM agent_generation_state WHERE agent_id=?", (str(parent_id),)).fetchone()
        return int(row[0]) if row else 0

    def _set_generation(self, parent_id, generation):
        now = time.time()
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO agent_generation_state(agent_id,generation,updated_ts) VALUES(?,?,?)
                   ON CONFLICT(agent_id) DO UPDATE SET generation=excluded.generation,updated_ts=excluded.updated_ts""",
                (str(parent_id), int(generation), now),
            )

    def _recover(self):
        now = time.time()
        with self.store.lock, self.store.conn() as c:
            c.execute("DELETE FROM agent_generation_backups WHERE expires_ts<?", (now,))
            # TrainingQueue is intentionally in-memory.  An interrupted build is safely
            # restarted from raw history; the live model was never touched.
            c.execute(
                """UPDATE agent_candidates SET state='queued',dirty=1,last_error=NULL,updated_ts=?
                   WHERE state IN ('building','discarding')""",
                (now,),
            )
            # 0.14.70 could leave a Correct Candidate failed when Stage-3 discovered that
            # its persisted stable base used an older feature-schema contract.  Preserve
            # the Candidate/feedback and route it through the isolated current-schema
            # historical rebuild on the first 0.14.71 startup.
            c.execute(
                """UPDATE agent_candidates
                   SET state='queued',reason='schema_upgrade_rebuild',dirty=1,queued_ts=?,
                       last_error=NULL,updated_ts=?
                   WHERE state='failed'
                     AND last_error LIKE '%Stable correction base schema is incompatible%'""",
                (now, now),
            )

    def _create_candidate(self, parent):
        payload = {
            "name": f"{parent.get('name') or parent['id']} · Candidate",
            "target_entity": parent["target_entity"],
            "target_property": parent["target_property"],
            "min_value": parent["min_value"],
            "max_value": parent["max_value"],
            "confidence_threshold": parent.get("confidence_threshold", .78),
            "deadband": parent.get("deadband", 1.0),
            "action_interval": parent.get("action_interval", .25),
            "exploration_step": parent.get("exploration_step", 1.0),
            "exploration_interval": parent.get("exploration_interval", 21600),
            "micro_exploration": False,
            "auto_created": False,
            "input_entities": list(parent.get("input_entities") or ["*"]),
        }
        candidate = self.store.create_agent(payload)
        live_model = self.store.get_model(parent["id"])
        if live_model:
            self.store.save_model(candidate["id"], live_model)
        generation = self._generation(parent["id"]) + 1
        now = time.time()
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO agent_candidates
                   (parent_agent_id,candidate_id,generation,state,reason,feedback_revision,build_revision,dirty,
                    queued_ts,comparison_json,updated_ts)
                   VALUES(?,?,?,?,?,0,0,1,?,?,?)""",
                (parent["id"], candidate["id"], generation, "queued", "feedback",
                 now, json.dumps(_blank_comparison()), now),
            )
        refresh_candidate_ids_cache(self.store)
        self.engine.models.pop(candidate["id"], None)
        self.engine.runtime.pop(candidate["id"], None)
        return self._candidate_row(parent["id"])

    def _queue_initial_parent_training(self, parent, reason):
        """Cold start belongs to the Live/base agent, never to a Candidate generation."""
        queue = self._queue()
        if queue is None:
            raise ValueError("initial agent training queue is not ready")
        queued = queue.enqueue(parent["id"], rebuild=True, reason="initial_training")
        self.store.event(
            parent["id"], "info", "agent_initial_training_required",
            "Feedback arrived before the first base policy existed; training the Live agent in place",
            {"reason": str(reason), "training_queue": queued},
        )
        return {
            "state": "initial_training",
            "parent_agent_id": parent["id"],
            "candidate_id": None,
            "generation": self._generation(parent["id"]),
            "reason": str(reason),
            "promotable": False,
            "queue": queued,
            "training_queue": queued,
        }

    def enqueue(self, parent_id, reason="feedback"):
        parent = self.store.get_agent_config(str(parent_id))
        if not parent or is_candidate(self.store, parent_id):
            raise ValueError("live agent not found")
        # A Candidate is a proposed *next* generation. Creating one before Gen-0 has
        # produced any persisted model leaves the actual agent forever untrained and
        # moves all learning into a hidden surrogate. Keep cold start on the parent.
        if self.store.get_model(parent["id"]) is None:
            return self._queue_initial_parent_training(parent, reason)
        with self.lock:
            row = self._candidate_row(parent_id)
            if not row:
                row = self._create_candidate(parent)
            now = time.time()
            building = row.get("state") == "building"
            next_state = "building" if building else "queued"
            with self.store.lock, self.store.conn() as c:
                c.execute(
                    """UPDATE agent_candidates SET feedback_revision=feedback_revision+1,dirty=1,state=?,reason=?,
                       queued_ts=?,comparison_json=?,last_error=NULL,discard_requested=0,updated_ts=?
                       WHERE parent_agent_id=?""",
                    (next_state, str(reason), now, json.dumps(_blank_comparison()), now, str(parent_id)),
                )
            self.store.event(parent_id, "info", "agent_candidate_queued",
                             "Explicit feedback queued a new Candidate generation",
                             {"reason": reason, "candidate_id": row["candidate_id"]})
            self.wake_event.set()
        self._ensure_worker_alive()
        return self.status(parent_id)

    def _sync_feedback(self, parent, candidate):
        """Copy explicit parent feedback into the isolated Candidate Teach dataset."""
        from teaching_rl import fingerprint as teach_fingerprint
        from teaching import fingerprint as wrong_fingerprint

        teach_fp_parent = teach_fingerprint(parent)
        wrong_fp_parent = wrong_fingerprint(parent)
        candidate_fp = teach_fingerprint(candidate)
        examples = []
        with self.store.conn() as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='teaching_rl_labels'").fetchone():
                for r in c.execute(
                    """SELECT created_ts,sample_ts,desired,previous_desired,fingerprint
                       FROM teaching_rl_labels WHERE agent_id=? AND undone_ts IS NULL ORDER BY sample_ts,id""",
                    (parent["id"],),
                ).fetchall():
                    if str(r["fingerprint"]) == str(teach_fp_parent):
                        examples.append((float(r["created_ts"]), float(r["sample_ts"]), float(r["desired"]),
                                         None if r["previous_desired"] is None else float(r["previous_desired"]), "teach"))
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='teaching_labels'").fetchone():
                for r in c.execute(
                    """SELECT created_ts,sample_ts,desired,previous_desired,fingerprint
                       FROM teaching_labels WHERE agent_id=? AND undone_ts IS NULL ORDER BY sample_ts,id""",
                    (parent["id"],),
                ).fetchall():
                    if str(r["fingerprint"]) == str(wrong_fp_parent):
                        examples.append((float(r["created_ts"]), float(r["sample_ts"]), float(r["desired"]),
                                         None if r["previous_desired"] is None else float(r["previous_desired"]), "wrong_decision"))

        # Same explicit moment may exist in both logs.  Keep one authoritative desired
        # value; the latest insertion wins without multiplying its supervised weight.
        unique = {}
        for created, sample, desired, previous, source in examples:
            unique[(round(sample, 6), round(desired, 8))] = (created, sample, desired, previous, source)
        ordered = sorted(unique.values(), key=lambda x: (x[1], x[0]))
        with self.store.lock, self.store.conn() as c:
            c.execute("DELETE FROM teaching_rl_labels WHERE agent_id=?", (candidate["id"],))
            for created, sample, desired, previous, source in ordered:
                c.execute(
                    """INSERT INTO teaching_rl_labels(agent_id,created_ts,sample_ts,desired,previous_desired,fingerprint,undone_ts)
                       VALUES(?,?,?,?,?,?,NULL)""",
                    (candidate["id"], created, sample, desired, previous, candidate_fp),
                )
        return {
            "examples": len(ordered),
            "teach": sum(1 for x in ordered if x[4] == "teach"),
            "wrong_decision": sum(1 for x in ordered if x[4] == "wrong_decision"),
        }

    def _start_build(self, row):
        queue = self._queue()
        if queue is None:
            return False
        candidate = self.store.get_agent(row["candidate_id"])
        parent = self.store.get_agent_config(row["parent_agent_id"])
        if not candidate or not parent:
            self._fail(row, "candidate or live agent disappeared")
            return True

        # Never mutate an already-active unrelated queue/history job. It will disappear
        # from status_for() when complete and the next Candidate poll will claim the slot.
        existing = queue.status_for(candidate["id"])
        if existing and str(existing.get("state") or "") == "active":
            return False

        try:
            synced = self._sync_feedback(parent, candidate)
            service = getattr(self.engine, "rl_teaching", None)
            if service is None:
                raise RuntimeError("Teach RL service unavailable")
            service.prepare_retrain(candidate)
            queued, queue_claim = self._claim_candidate_training_job(
                candidate["id"], rebuild=True, reason="teach_rl"
            )
            if queued is None:
                return False
            now = time.time()
            with self.store.lock, self.store.conn() as c:
                c.execute(
                    """UPDATE agent_candidates SET state='building',build_revision=feedback_revision,dirty=0,
                       build_started_ts=?,build_finished_ts=NULL,comparison_started_ts=NULL,comparison_json=?,
                       last_error=NULL,updated_ts=? WHERE parent_agent_id=?""",
                    (now, json.dumps(_blank_comparison()), now, row["parent_agent_id"]),
                )
            self.store.event(row["parent_agent_id"], "info", "agent_candidate_build_started",
                             "Candidate rebuild started while the live agent keeps serving",
                             {"candidate_id": candidate["id"], "generation": row["generation"],
                              "feedback": synced, "queue": queued,
                              "queue_claim": queue_claim})
            return True
        except Exception as exc:
            self._fail(row, f"{type(exc).__name__}: {exc}")
            return True

    def _fail(self, row, error):
        now = time.time()
        with self.store.lock, self.store.conn() as c:
            c.execute("UPDATE agent_candidates SET state='failed',last_error=?,updated_ts=? WHERE parent_agent_id=?",
                      (str(error), now, row["parent_agent_id"]))
        self.store.event(row["parent_agent_id"], "error", "agent_candidate_failed", str(error),
                         {"candidate_id": row.get("candidate_id")})

    def _finish_build_if_ready(self, row):
        queue = self._queue()
        if queue is None or queue.status_for(row["candidate_id"]):
            return False
        history = getattr(self.core, "HISTORY", None)
        if history is not None and row["candidate_id"] in getattr(history, "agent_jobs", set()):
            return False
        candidate = self.store.get_agent(row["candidate_id"])
        if not candidate:
            self._fail(row, "candidate training surrogate disappeared")
            return True
        service = getattr(self.engine, "rl_teaching", None)
        teach_state = service.status(candidate["id"]).get("state") if service is not None else None
        if teach_state == "failed" or self.store.get_model(candidate["id"]) is None:
            self._fail(row, "Candidate rebuild did not produce a usable model")
            return True
        fresh = self._candidate_row(row["parent_agent_id"]) or row
        if int(fresh.get("discard_requested") or 0):
            self._delete_candidate(fresh)
            return True
        if int(fresh.get("feedback_revision") or 0) > int(fresh.get("build_revision") or 0) or int(fresh.get("dirty") or 0):
            now = time.time()
            with self.store.lock, self.store.conn() as c:
                c.execute("UPDATE agent_candidates SET state='queued',dirty=1,updated_ts=? WHERE parent_agent_id=?",
                          (now, fresh["parent_agent_id"]))
            self.store.event(fresh["parent_agent_id"], "info", "agent_candidate_requeued",
                             "New feedback arrived during Candidate build; rebuilding the newer revision",
                             {"candidate_id": fresh["candidate_id"],
                              "build_revision": fresh["build_revision"],
                              "feedback_revision": fresh["feedback_revision"]})
            self.wake_event.set()
            return True
        now = time.time()
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """UPDATE agent_candidates SET state='comparing',build_finished_ts=?,comparison_started_ts=?,
                   comparison_json=?,updated_ts=? WHERE parent_agent_id=?""",
                (now, now, json.dumps(_blank_comparison()), now, fresh["parent_agent_id"]),
            )
        self.runtime.pop(str(fresh["parent_agent_id"]), None)
        self.store.event(fresh["parent_agent_id"], "info", "agent_candidate_comparison_started",
                         "Candidate build finished; future paired comparison with Live started",
                         {"candidate_id": fresh["candidate_id"], "generation": fresh["generation"]})
        return True

    def _delete_candidate(self, row):
        candidate_id = str(row["candidate_id"])
        queue = self._queue()
        if queue is not None:
            queue.cancel(candidate_id)
        self.engine.models.pop(candidate_id, None)
        self.engine.runtime.pop(candidate_id, None)
        with self.store.lock, self.store.conn() as c:
            for table in (
                "teaching_rl_labels",
                "teaching_rl_jobs",
                "manual_context_feedback",
                "tiny_mlp_shadow_models",
            ):
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    c.execute(f"DELETE FROM {table} WHERE agent_id=?", (candidate_id,))
            c.execute("DELETE FROM agent_candidates WHERE candidate_id=?", (candidate_id,))
        refresh_candidate_ids_cache(self.store)
        self.store.delete_agent(candidate_id)
        self.runtime.pop(str(row["parent_agent_id"]), None)

    def discard(self, parent_id):
        row = self._candidate_row(parent_id)
        if not row:
            return {"ok": True, "discarded": False}
        queue = self._queue()
        active = queue.status_for(row["candidate_id"]) if queue is not None else None
        if active and active.get("state") == "active":
            now = time.time()
            with self.store.lock, self.store.conn() as c:
                c.execute("UPDATE agent_candidates SET state='discarding',discard_requested=1,dirty=0,updated_ts=? WHERE parent_agent_id=?",
                          (now, str(parent_id)))
            return {"ok": True, "discarded": False, "state": "discarding"}
        self._delete_candidate(row)
        self.store.event(parent_id, "info", "agent_candidate_discarded", "Candidate discarded; Live agent was unchanged", None)
        return {"ok": True, "discarded": True}

    def _comparison_summary(self, row, parent=None, candidate=None):
        raw = _json(row.get("comparison_json"), _blank_comparison())
        out = {**_blank_comparison(), **raw}
        samples = int(out.get("samples") or 0)
        live_acc = (float(out.get("live_correct") or 0) / samples) if samples else None
        cand_acc = (float(out.get("candidate_correct") or 0) / samples) if samples else None
        on_n = int(out.get("on_events") or 0)
        off_n = int(out.get("off_events") or 0)
        live_on = float(out.get("live_on_lead_sum") or 0.0) / on_n if on_n else None
        cand_on = float(out.get("candidate_on_lead_sum") or 0.0) / on_n if on_n else None
        live_off = float(out.get("live_off_lead_sum") or 0.0) / off_n if off_n else None
        cand_off = float(out.get("candidate_off_lead_sum") or 0.0) / off_n if off_n else None
        out.update({
            "live_accuracy": live_acc,
            "candidate_accuracy": cand_acc,
            "accuracy_gain": None if live_acc is None or cand_acc is None else cand_acc - live_acc,
            "live_on_lead_seconds": live_on,
            "candidate_on_lead_seconds": cand_on,
            "on_lead_gain_seconds": None if live_on is None or cand_on is None else cand_on - live_on,
            "live_off_lead_seconds": live_off,
            "candidate_off_lead_seconds": cand_off,
            "off_lead_gain_seconds": None if live_off is None or cand_off is None else cand_off - live_off,
        })
        parent = parent or self.store.get_agent_config(row["parent_agent_id"])
        candidate = candidate or self.store.get_agent_config(row["candidate_id"])
        min_samples = 40
        per_action_ok = True
        if parent and str(parent.get("target_property")) == "power":
            per = out.get("per_action") or {}
            per_action_ok = all(int((per.get(str(v)) or {}).get("samples") or 0) >= 20 for v in (0.0, 1.0))
        safety_ok = bool(samples >= min_samples and cand_acc is not None and live_acc is not None and cand_acc + 0.03 >= live_acc)
        false_margin = max(2, int(math.ceil(samples * 0.10)))
        safety_ok = safety_ok and int(out.get("candidate_false_early") or 0) <= int(out.get("live_false_early") or 0) + false_margin
        fresh = int(row.get("feedback_revision") or 0) == int(row.get("build_revision") or 0) and not int(row.get("dirty") or 0)
        trained = bool(candidate and candidate.get("training_state") == "qualified" and self.store.get_model(candidate["id"]))
        out["promotable"] = bool(fresh and trained and per_action_ok and safety_ok)
        out["required_future_samples"] = min_samples
        out["per_action_ready"] = per_action_ok
        out["fresh_feedback_revision"] = fresh
        return out

    def _status_from_row(self, row):
        """Build a Candidate card from one already-fetched edge without history aggregates."""
        if not row:
            return None
        parent = self.store.get_agent_config(row["parent_agent_id"])
        candidate = self.store.get_agent_config(row["candidate_id"])
        comparison = self._comparison_summary(row, parent, candidate)
        queue = self._queue()
        q = queue.status_for(row["candidate_id"]) if queue is not None else None
        state = str(row.get("state") or "queued")
        if comparison.get("promotable") and state == "comparing":
            state = "ready"
        return {
            "parent_agent_id": row["parent_agent_id"],
            "parent_name": (parent or {}).get("name") or row["parent_agent_id"],
            "candidate_id": row["candidate_id"],
            "name": (candidate or {}).get("name") or "Candidate",
            "generation": int(row.get("generation") or 1),
            "state": state,
            "reason": row.get("reason"),
            "feedback_revision": int(row.get("feedback_revision") or 0),
            "build_revision": int(row.get("build_revision") or 0),
            "stale": int(row.get("feedback_revision") or 0) != int(row.get("build_revision") or 0),
            "dirty": bool(row.get("dirty")),
            "last_error": row.get("last_error"),
            "queued_ts": row.get("queued_ts"),
            "build_started_ts": row.get("build_started_ts"),
            "build_finished_ts": row.get("build_finished_ts"),
            "comparison_started_ts": row.get("comparison_started_ts"),
            "training_state": (candidate or {}).get("training_state"),
            "training_progress": float((candidate or {}).get("training_progress") or 0.0),
            "queue": q,
            "comparison": comparison,
            "promotable": bool(comparison.get("promotable")),
        }

    def status(self, parent_id):
        self._ensure_worker_alive()
        result = self._status_from_row(self._candidate_row(parent_id))
        if result is not None:
            result["worker"] = self._worker_health()
        return result

    def list_status(self):
        self._ensure_worker_alive()
        out = []
        for row in self._all_rows():
            status = self._status_from_row(row)
            if status is not None:
                status["worker"] = self._worker_health()
                out.append(status)
        return out

    @staticmethod
    def _nearest_binary(value):
        return 1.0 if float(value) >= 0.5 else 0.0

    def _persist_comparison(self, row, comp):
        comp["updated_ts"] = time.time()
        summary = self._comparison_summary({**row, "comparison_json": json.dumps(comp)})
        state = "ready" if summary.get("promotable") else "comparing"
        with self.store.lock, self.store.conn() as c:
            c.execute("UPDATE agent_candidates SET comparison_json=?,state=?,updated_ts=? WHERE parent_agent_id=?",
                      (json.dumps(comp, separators=(",", ":")), state, time.time(), row["parent_agent_id"]))

    def before_live_process(self, agent, state_map):
        row = self._candidate_row(agent["id"])
        if not row or row.get("state") not in ("comparing", "ready"):
            return
        from context import target_value
        current = target_value((state_map or {}).get(agent["target_entity"]), agent["target_property"])
        if current is None:
            return
        current = float(current)
        aid = str(agent["id"])
        rt = self.runtime.setdefault(aid, {})
        previous = rt.get("current")
        rt["current"] = current
        if previous is None or abs(float(previous) - current) <= max(.01, float(agent.get("deadband") or 0.0) * .05):
            return
        target_state = (state_map or {}).get(agent["target_entity"])
        try:
            if self.engine.own_command_echo(agent, target_state, current):
                rt["transition_ts"] = time.time()
                return
        except Exception:
            pass
        live_pred = rt.get("live_prediction")
        cand_pred = rt.get("candidate_prediction")
        if live_pred is None or cand_pred is None:
            rt["transition_ts"] = time.time()
            return
        now = time.time()
        actual = self._nearest_binary(current) if str(agent.get("target_property")) == "power" else current
        live_action = self._nearest_binary(live_pred) if str(agent.get("target_property")) == "power" else float(live_pred)
        cand_action = self._nearest_binary(cand_pred) if str(agent.get("target_property")) == "power" else float(cand_pred)
        tolerance = max(float(agent.get("deadband") or 0.0), (float(agent["max_value"])-float(agent["min_value"]))*.03)
        live_ok = live_action == actual if str(agent.get("target_property")) == "power" else abs(live_action-actual) <= tolerance
        cand_ok = cand_action == actual if str(agent.get("target_property")) == "power" else abs(cand_action-actual) <= tolerance
        comp = {**_blank_comparison(), **_json(row.get("comparison_json"), _blank_comparison())}
        comp["samples"] = int(comp.get("samples") or 0) + 1
        comp["live_correct"] = int(comp.get("live_correct") or 0) + int(live_ok)
        comp["candidate_correct"] = int(comp.get("candidate_correct") or 0) + int(cand_ok)
        if cand_ok and not live_ok:
            comp["candidate_wins"] = int(comp.get("candidate_wins") or 0) + 1
        elif live_ok and not cand_ok:
            comp["live_wins"] = int(comp.get("live_wins") or 0) + 1
        elif not live_ok and not cand_ok:
            comp["both_wrong"] = int(comp.get("both_wrong") or 0) + 1
        key = str(float(actual))
        slot = (comp.get("per_action") or {}).setdefault(key, {"samples": 0, "live_correct": 0, "candidate_correct": 0})
        slot["samples"] += 1; slot["live_correct"] += int(live_ok); slot["candidate_correct"] += int(cand_ok)
        comp["per_action"] = comp.get("per_action") or {}
        cap = 30.0
        live_lead = min(cap, max(0.0, now-float(rt.get("live_since") or now))) if live_ok else 0.0
        cand_lead = min(cap, max(0.0, now-float(rt.get("candidate_since") or now))) if cand_ok else 0.0
        if str(agent.get("target_property")) == "power" and actual >= .5:
            comp["on_events"] = int(comp.get("on_events") or 0) + 1
            comp["live_on_lead_sum"] = float(comp.get("live_on_lead_sum") or 0.0) + live_lead
            comp["candidate_on_lead_sum"] = float(comp.get("candidate_on_lead_sum") or 0.0) + cand_lead
        elif str(agent.get("target_property")) == "power":
            comp["off_events"] = int(comp.get("off_events") or 0) + 1
            comp["live_off_lead_sum"] = float(comp.get("live_off_lead_sum") or 0.0) + live_lead
            comp["candidate_off_lead_sum"] = float(comp.get("candidate_off_lead_sum") or 0.0) + cand_lead
        self._persist_comparison(row, comp)
        rt["transition_ts"] = now

    def after_live_process(self, agent, state_map):
        row = self._candidate_row(agent["id"])
        if not row or row.get("state") not in ("comparing", "ready"):
            return
        candidate = self.store.get_agent_config(row["candidate_id"])
        if not candidate or self.store.get_model(candidate["id"]) is None:
            return
        try:
            policy = self.engine.models.get(candidate["id"])
            if policy is None:
                policy = self.engine.policy(candidate)
            features, _, _ = policy.features(state_map, self.engine.temporal_history, at_ts=time.time())
            cand = float(policy.predict(features)[0]["value"])
        except Exception as exc:
            self._fail(row, f"candidate inference failed: {type(exc).__name__}: {exc}")
            return
        live = (self.engine.runtime.get(agent["id"]) or {}).get("last_prediction")
        try:
            live = float(live)
        except (TypeError, ValueError):
            return
        now = time.time()
        aid = str(agent["id"])
        rt = self.runtime.setdefault(aid, {})
        current = rt.get("current")
        old_live = rt.get("live_prediction")
        old_cand = rt.get("candidate_prediction")
        if old_live is None or abs(float(old_live)-live) > 1e-9:
            # Returning to current without an intervening target transition is a false
            # early proposal.  It never controlled hardware, but is useful A/B evidence.
            if current is not None and old_live is not None and abs(float(old_live)-float(current)) > .5 and abs(live-float(current)) <= .5:
                comp = {**_blank_comparison(), **_json(row.get("comparison_json"), _blank_comparison())}
                comp["live_false_early"] = int(comp.get("live_false_early") or 0) + 1
                self._persist_comparison(row, comp)
            rt["live_since"] = now
        if old_cand is None or abs(float(old_cand)-cand) > 1e-9:
            if current is not None and old_cand is not None and abs(float(old_cand)-float(current)) > .5 and abs(cand-float(current)) <= .5:
                comp = {**_blank_comparison(), **_json((self._candidate_row(agent["id"]) or row).get("comparison_json"), _blank_comparison())}
                comp["candidate_false_early"] = int(comp.get("candidate_false_early") or 0) + 1
                self._persist_comparison(self._candidate_row(agent["id"]) or row, comp)
            rt["candidate_since"] = now
        rt["live_prediction"] = live
        rt["candidate_prediction"] = cand

    def promote(self, parent_id):
        row = self._candidate_row(parent_id)
        if not row:
            raise ValueError("Candidate not found")
        status = self.status(parent_id)
        if not status or not status.get("promotable"):
            raise ValueError("Candidate needs more future paired evidence before Promote")
        parent = self.store.get_agent(parent_id)
        candidate = self.store.get_agent(row["candidate_id"])
        model = self.store.get_model(row["candidate_id"])
        if not parent or not candidate or not model:
            raise ValueError("Candidate model unavailable")
        comparison = status.get("comparison") or {}
        now = time.time()
        with self.engine.executor.target_lock(parent["target_entity"]):
            if parent.get("mode") == "control":
                self.engine.executor.release_control(parent, reason="candidate_promote")
            old_model = self.store.get_model(parent_id)
            with self.store.lock, self.store.conn() as c:
                c.execute(
                    """INSERT INTO agent_generation_backups
                       (agent_id,generation,created_ts,expires_ts,model_json,agent_json,comparison_json)
                       VALUES(?,?,?,?,?,?,?)""",
                    (parent_id, self._generation(parent_id), now, now+86400.0,
                     json.dumps(old_model, separators=(",", ":")) if old_model else None,
                     json.dumps(parent, separators=(",", ":"), default=str),
                     json.dumps(comparison, separators=(",", ":"))),
                )
            self.store.save_model(parent_id, model)
            self.store.set_training_state(
                parent_id, "qualified",
                score=candidate.get("benchmark_score"),
                samples=candidate.get("benchmark_samples") or 0,
                source=candidate.get("benchmark_source"),
                detail=candidate.get("benchmark_detail") or {},
            )
            self.engine.models.pop(parent_id, None)
            self.engine.runtime.pop(parent_id, None)
            self._set_generation(parent_id, int(row.get("generation") or 1))
            self._delete_candidate(row)
        self.store.event(parent_id, "info", "agent_candidate_promoted",
                         "Candidate promoted to Live; previous generation retained as a 24 h rollback snapshot",
                         {"generation": int(row.get("generation") or 1), "comparison": comparison,
                          "mode": "shadow", "old_generation_backup_hours": 24})
        self.engine.wake_event.set()
        return {"ok": True, "agent_id": parent_id, "generation": int(row.get("generation") or 1), "mode": "shadow"}

    def _install_feedback_hooks(self):
        teaching = self.engine.teaching
        if not getattr(teaching, "_candidate_feedback_hook", False):
            original_teach = teaching.teach
            original_undo = teaching.undo

            def teach(engine, agent, desired=None, sample_ts=None):
                result = original_teach(engine, agent, desired=desired, sample_ts=sample_ts)
                self.enqueue(agent["id"], "wrong_decision")
                return result

            def undo(engine, agent):
                result = original_undo(engine, agent)
                self.enqueue(agent["id"], "wrong_decision_undo")
                return result

            teaching.teach = teach
            teaching.undo = undo
            teaching._candidate_feedback_hook = True

        rl = getattr(self.engine, "rl_teaching", None)
        if rl is not None and not getattr(rl, "_candidate_feedback_hook", False):
            original_add = rl.add_label
            original_undo = rl.undo

            def add_label(agent, desired, sample_ts):
                result = original_add(agent, desired, sample_ts)
                self.enqueue(agent["id"], "teach")
                return result

            def undo_label(agent):
                result = original_undo(agent)
                self.enqueue(agent["id"], "teach_undo")
                return result

            rl.add_label = add_label
            rl.undo = undo_label
            rl._candidate_feedback_hook = True

    def _install_process_wrapper(self):
        if getattr(self.engine, "_agent_candidate_process_hook", False):
            return
        original = self.engine.process_agent

        def process(agent, state_map, changed_entities=None):
            self.before_live_process(agent, state_map)
            result = original(agent, state_map, changed_entities)
            self.after_live_process(agent, state_map)
            return result

        self.engine.process_agent = process
        self.engine._agent_candidate_process_hook = True

    def _install_http(self):
        handler = self.core.Handler
        if getattr(handler, "_agent_candidates_installed", False):
            return
        original_get = handler.do_GET
        original_post = handler.do_POST
        original_delete = handler.do_DELETE

        def do_get(http):
            path, _, _ = http.path.partition("?")
            if path == "/candidate_ui.js":
                if not http.require_trusted_client():
                    return
                return http.static("candidate_ui.js", "application/javascript; charset=utf-8")
            if path == "/api/candidates":
                if not http.require_trusted_client() or not http.require_runtime():
                    return
                return http.send_json(200, {"candidates": self.list_status()})
            if path.startswith("/api/agents/") and path.endswith("/teach-rl-status"):
                if not http.require_trusted_client() or not http.require_runtime():
                    return
                agent_id = path.split("/")[3]
                service = getattr(self.engine, "rl_teaching", None)
                result = service.status(agent_id) if service is not None else {"state": "idle", "report": {}}
                candidate = self.status(agent_id)
                result["candidate"] = candidate
                if candidate and candidate.get("state") in ("queued", "building"):
                    result["training_queue"] = candidate.get("queue") or {
                        "state": "active" if candidate.get("state") == "building" else "queued",
                        "position": 0,
                    }
                else:
                    result["training_queue"] = None
                if candidate and candidate.get("state") in ("comparing", "ready"):
                    result["state"] = "done"
                    result["report"] = {**(result.get("report") or {}), "candidate_state": candidate.get("state")}
                return http.send_json(200, result)
            return original_get(http)

        def do_post(http):
            path, _, _ = http.path.partition("?")
            if path.startswith("/api/agents/") and path.endswith("/candidate/promote"):
                if not http.require_trusted_client() or not http.require_runtime():
                    return
                agent_id = path.split("/")[3]
                try:
                    return http.send_json(200, self.promote(agent_id))
                except ValueError as exc:
                    return http.send_json(409, {"error": str(exc), "candidate": self.status(agent_id)})
            if path.startswith("/api/agents/") and path.endswith("/teach-rl-train"):
                if not http.require_trusted_client() or not http.require_runtime():
                    return
                agent_id = path.split("/")[3]
                try:
                    candidate = self.enqueue(agent_id, "teach_train")
                    return http.send_json(202, {"ok": True, "candidate": candidate,
                                                "training_queue": candidate.get("queue") if candidate else None})
                except ValueError as exc:
                    return http.send_json(404, {"error": str(exc)})
            return original_post(http)

        def do_delete(http):
            path, _, _ = http.path.partition("?")
            if path.startswith("/api/agents/") and path.endswith("/candidate"):
                if not http.require_trusted_client() or not http.require_runtime():
                    return
                return http.send_json(200, self.discard(path.split("/")[3]))
            if path.startswith("/api/agents/") and path.endswith("/learning"):
                if not http.require_trusted_client() or not http.require_runtime():
                    return
                agent_id = path.split("/")[3]
                # Rebuild belongs to the selected Live agent itself. Candidate lineage is
                # feedback/correction state and must never be recreated by this button.
                # Rebuilding a parent underneath an active Candidate would invalidate the
                # Candidate's immutable parent snapshot, so require the user to resolve it.
                if not is_candidate(self.store, agent_id):
                    existing = self._candidate_row(agent_id)
                    if existing:
                        return http.send_json(409, {
                            "error": "Discard or promote the current Candidate before rebuilding the Live agent",
                            "candidate": self.status(agent_id),
                        })
                return original_delete(http)
            return original_delete(http)

        handler.do_GET = do_get
        handler.do_POST = do_post
        handler.do_DELETE = do_delete
        handler._agent_candidates_installed = True

    def _maintenance(self):
        with self.store.lock, self.store.conn() as c:
            c.execute("DELETE FROM agent_generation_backups WHERE expires_ts<?", (time.time(),))

    def _worker_loop(self):
        while not self.stop_event.is_set():
            with self._worker_health_lock:
                self._worker_heartbeat_ts = time.time()
            changed = False
            try:
                rows = self._all_rows()
            except Exception as exc:
                self._record_worker_error("list_candidates", exc)
                rows = []

            for row in rows:
                state = str(row.get("state") or "queued")
                trace = (
                    RUNTIME_DEBUG.begin(
                        "candidate_lifecycle",
                        parent_agent_id=str(row.get("parent_agent_id") or ""),
                        candidate_id=str(row.get("candidate_id") or ""),
                        state=state,
                        reason=str(row.get("reason") or ""),
                    )
                    if RUNTIME_DEBUG.enabled else None
                )
                try:
                    if state == "queued":
                        changed = self._start_build(row) or changed
                    elif state == "building":
                        changed = self._finish_build_if_ready(row) or changed
                    elif state == "discarding":
                        changed = self._finish_build_if_ready(row) or changed
                    RUNTIME_DEBUG.end(trace, status="ok")
                except Exception as exc:
                    RUNTIME_DEBUG.end(
                        trace, status="error",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    # One malformed/stale Candidate must never kill the scheduler for all
                    # remaining generations. The row stays durable and is retried.
                    self._record_worker_error("candidate_lifecycle", exc, row)

            try:
                self._maintenance()
            except Exception as exc:
                self._record_worker_error("maintenance", exc)

            with self._worker_health_lock:
                self._worker_heartbeat_ts = time.time()
            self.wake_event.wait(self.poll_seconds if not changed else 0.05)
            self.wake_event.clear()

    def run(self):
        # The Candidate scheduler is product-critical. Keep the thread alive across
        # lifecycle/decorator/SQLite exceptions; _ensure_worker_alive() additionally
        # replaces the scheduler if the Thread itself ever exits unexpectedly.
        try:
            self._worker_loop()
        except BaseException as exc:
            if not self.stop_event.is_set():
                self._record_worker_error("worker_exit", exc)

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()


def install(core, *, start_worker=True):
    install_store_overlay(core.STORE)
    install_history_overlay(core.STORE)
    existing = getattr(core.ENGINE, "agent_candidates", None)
    if existing is not None:
        return existing
    manager = AgentCandidateManager(core, start_worker=start_worker)
    core.ENGINE.agent_candidates = manager
    core.STORE.event(None, "info", "agent_candidates_ready",
                     "Candidate generations enabled: Live keeps serving during rebuild and future A/B comparison",
                     {"promotion": "manual_after_paired_future_evidence", "rollback_snapshot_hours": 24,
                      "candidate_control": "forbidden_by_runtime_enumeration"})
    return manager
