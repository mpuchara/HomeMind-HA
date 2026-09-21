"""Persistent single-branch Candidate lineage.

This layer is intentionally additive.  ``agent_candidates`` remains the active edge/work
queue table used by the existing Candidate implementation, while
``agent_candidate_generations`` becomes the durable lineage ledger.  Existing 0.13.2
Candidate rows are migrated in place without DROP/rename operations.

A child Candidate always snapshots its direct parent generation.  Once a Candidate owns
a child it is frozen as that child's parent/champion; comparison is direct-parent only.
Full model retention is bounded while lineage metadata remains durable.
"""
import hashlib
import json
import time
import uuid

from settings import OPTIONS


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _config_payload(agent):
    keys = (
        "target_entity", "target_property", "min_value", "max_value",
        "confidence_threshold", "deadband", "action_interval", "exploration_step",
        "exploration_interval", "input_entities", "micro_exploration", "auto_created",
    )
    out = {}
    for key in keys:
        value = agent.get(key)
        if key == "input_entities":
            value = list(value or ["*"])
        out[key] = value
    return out


def config_fingerprint(agent):
    return _sha(_config_payload(agent))


def model_metadata(model):
    model = dict(model or {})
    schema = dict(model.get("schema") or {})
    selection = dict(model.get("selection_meta") or {})
    schema_revision = (
        selection.get("schema_revision")
        or schema.get("revision")
        or schema.get("schema_revision")
        or f"v{schema.get('version', 'unknown')}:{_sha(schema)[:16]}"
    )
    return {
        "model_identity": _sha(model) if model else None,
        "model_revision": model.get("model_revision"),
        "schema_revision": str(schema_revision) if schema_revision is not None else None,
    }


def ensure_lineage_tables(store):
    with store.lock, store.conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_candidate_generations (
                generation_id TEXT PRIMARY KEY,
                root_agent_id TEXT NOT NULL,
                agent_id TEXT,
                parent_generation_id TEXT,
                generation_number INTEGER NOT NULL,
                generation_type TEXT NOT NULL,
                parent_type TEXT,
                model_identity TEXT,
                config_fingerprint TEXT NOT NULL,
                schema_revision TEXT,
                model_revision TEXT,
                created_reason TEXT,
                created_ts REAL NOT NULL,
                updated_ts REAL NOT NULL,
                lifecycle_state TEXT NOT NULL,
                comparison_json TEXT NOT NULL DEFAULT '{}',
                resume_state TEXT,
                model_retained INTEGER NOT NULL DEFAULT 1,
                retired_ts REAL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_generation_agent
                ON agent_candidate_generations(agent_id) WHERE agent_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_candidate_generation_root
                ON agent_candidate_generations(root_agent_id,generation_number);
            CREATE INDEX IF NOT EXISTS idx_candidate_generation_parent
                ON agent_candidate_generations(parent_generation_id);
            """
        )


def _root_generation_id(root_agent_id):
    return f"root:{root_agent_id}"


def _candidate_generation_id(candidate_id):
    return f"candidate:{candidate_id}"


def _row(store, *, generation_id=None, agent_id=None):
    if generation_id is None and agent_id is None:
        return None
    with store.conn() as c:
        if generation_id is not None:
            found = c.execute(
                "SELECT * FROM agent_candidate_generations WHERE generation_id=?",
                (str(generation_id),),
            ).fetchone()
        else:
            found = c.execute(
                "SELECT * FROM agent_candidate_generations WHERE agent_id=?",
                (str(agent_id),),
            ).fetchone()
    return dict(found) if found else None


def _generation_children(store, generation_id, include_retired=False):
    sql = "SELECT * FROM agent_candidate_generations WHERE parent_generation_id=?"
    args = [str(generation_id)]
    if not include_retired:
        sql += " AND lifecycle_state NOT IN ('discarded','pruned')"
    sql += " ORDER BY generation_number,created_ts"
    with store.conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def _ensure_root(store, root_agent_id, generation_number=None):
    existing = _row(store, generation_id=_root_generation_id(root_agent_id))
    if existing:
        return existing
    agent = store.get_agent_config(str(root_agent_id))
    if not agent:
        return None
    if generation_number is None:
        with store.conn() as c:
            state = c.execute(
                "SELECT generation FROM agent_generation_state WHERE agent_id=?",
                (str(root_agent_id),),
            ).fetchone()
        generation_number = int(state[0]) if state else 0
    meta = model_metadata(store.get_model(root_agent_id))
    now = time.time()
    with store.lock, store.conn() as c:
        c.execute(
            """INSERT OR IGNORE INTO agent_candidate_generations
               (generation_id,root_agent_id,agent_id,parent_generation_id,generation_number,
                generation_type,parent_type,model_identity,config_fingerprint,schema_revision,
                model_revision,created_reason,created_ts,updated_ts,lifecycle_state,model_retained)
               VALUES(?,?,?,?,?,'live',NULL,?,?,?,?,?,?,?,'live',1)""",
            (
                _root_generation_id(root_agent_id), str(root_agent_id), str(root_agent_id), None,
                int(generation_number), meta.get("model_identity"), config_fingerprint(agent),
                meta.get("schema_revision"), meta.get("model_revision"), "root_live",
                now, now,
            ),
        )
    return _row(store, generation_id=_root_generation_id(root_agent_id))


def _refresh_live_generation_snapshot(store, generation):
    """Bind a new Candidate to the model/config that is Live *now*.

    A full Rebuild intentionally replaces the policy in place, while online learning can
    also update the persisted model between Candidate cycles. The durable Live generation
    row is therefore refreshed exactly when it becomes a new Candidate parent.
    """
    if not generation or generation.get("generation_type") != "live" or not generation.get("agent_id"):
        return generation
    agent = store.get_agent_config(str(generation["agent_id"]))
    if not agent:
        return generation
    meta = model_metadata(store.get_model(str(generation["agent_id"])))
    now = time.time()
    with store.lock, store.conn() as c:
        c.execute(
            """UPDATE agent_candidate_generations
               SET model_identity=?,config_fingerprint=?,schema_revision=?,model_revision=?,updated_ts=?
               WHERE generation_id=?""",
            (
                meta.get("model_identity"), config_fingerprint(agent),
                meta.get("schema_revision"), meta.get("model_revision"), now,
                str(generation["generation_id"]),
            ),
        )
    return _row(store, generation_id=generation["generation_id"])


def _register_generation(store, root_id, parent_generation, candidate_id, number, reason, state="queued"):
    agent = store.get_agent_config(str(candidate_id))
    if not agent:
        raise RuntimeError("Candidate surrogate missing while registering generation")
    meta = model_metadata(store.get_model(candidate_id))
    now = time.time()
    generation_id = _candidate_generation_id(candidate_id)
    with store.lock, store.conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO agent_candidate_generations
               (generation_id,root_agent_id,agent_id,parent_generation_id,generation_number,
                generation_type,parent_type,model_identity,config_fingerprint,schema_revision,
                model_revision,created_reason,created_ts,updated_ts,lifecycle_state,
                comparison_json,resume_state,model_retained,retired_ts)
               VALUES(?,?,?,?,?,'candidate',?,?,?,?,?,?,?,?,?,'{}',NULL,1,NULL)""",
            (
                generation_id, str(root_id), str(candidate_id), str(parent_generation["generation_id"]),
                int(number), str(parent_generation["generation_type"]), meta.get("model_identity"),
                config_fingerprint(agent), meta.get("schema_revision"), meta.get("model_revision"),
                str(reason), now, now, str(state),
            ),
        )
    return _row(store, generation_id=generation_id)


def migrate_existing_candidates(store):
    """Backfill lineage for 0.13.2 rows without mutating or replacing them."""
    ensure_lineage_tables(store)
    with store.conn() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_candidates'").fetchone():
            return
        rows = [dict(r) for r in c.execute("SELECT * FROM agent_candidates ORDER BY generation,updated_ts").fetchall()]
    # Legacy 0.13.2 has one edge rooted at Live.  This loop is also restart-safe for
    # already-created descendant edges because parent generations are resolved by agent id.
    for edge in rows:
        parent_gen = _row(store, agent_id=edge["parent_agent_id"])
        if parent_gen is None:
            root = str(edge["parent_agent_id"])
            parent_gen = _ensure_root(store, root, max(0, int(edge.get("generation") or 1) - 1))
        if parent_gen is None:
            continue
        root = str(parent_gen["root_agent_id"])
        existing = _row(store, agent_id=edge["candidate_id"])
        if existing is None and store.get_agent_config(edge["candidate_id"]):
            _register_generation(
                store, root, parent_gen, edge["candidate_id"],
                int(edge.get("generation") or int(parent_gen["generation_number"]) + 1),
                edge.get("reason") or "legacy_0_13_2", edge.get("state") or "queued",
            )
        generation = _row(store, agent_id=edge["candidate_id"])
        if generation:
            with store.lock, store.conn() as c:
                c.execute(
                    """UPDATE agent_candidate_generations SET lifecycle_state=?,comparison_json=?,updated_ts=?
                       WHERE generation_id=?""",
                    (
                        str(edge.get("state") or generation["lifecycle_state"]),
                        str(edge.get("comparison_json") or "{}"), time.time(), generation["generation_id"],
                    ),
                )


def _all_hidden_ids(store):
    lock = getattr(store, "_candidate_ids_ram_lock", None)
    if lock is not None and getattr(store, "_candidate_ids_ram_ready", False):
        with lock:
            return set(getattr(store, "_candidate_ids_ram", set()) or set())
    try:
        from agent_candidates import refresh_candidate_ids_cache
        return refresh_candidate_ids_cache(store)
    except Exception:
        return set()


def _is_candidate(store, agent_id):
    return str(agent_id) in _all_hidden_ids(store)


def _refresh_generation_metadata(store, agent_id, lifecycle_state=None, comparison_json=None):
    generation = _row(store, agent_id=agent_id)
    agent = store.get_agent_config(str(agent_id))
    if not generation or not agent:
        return generation
    meta = model_metadata(store.get_model(agent_id))
    fields = [
        "model_identity=?", "config_fingerprint=?", "schema_revision=?", "model_revision=?", "updated_ts=?",
    ]
    values = [
        meta.get("model_identity"), config_fingerprint(agent), meta.get("schema_revision"),
        meta.get("model_revision"), time.time(),
    ]
    if lifecycle_state is not None:
        fields.append("lifecycle_state=?")
        values.append(str(lifecycle_state))
    if comparison_json is not None:
        fields.append("comparison_json=?")
        values.append(str(comparison_json))
    values.append(generation["generation_id"])
    with store.lock, store.conn() as c:
        c.execute(
            f"UPDATE agent_candidate_generations SET {','.join(fields)} WHERE generation_id=?",
            values,
        )
    return _row(store, generation_id=generation["generation_id"])


def _active_tip(store, root_id):
    with store.conn() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT * FROM agent_candidate_generations
               WHERE root_agent_id=? AND generation_type='candidate'
                 AND lifecycle_state NOT IN ('discarded','pruned','promoted')
               ORDER BY generation_number DESC,created_ts DESC""",
            (str(root_id),),
        ).fetchall()]
    return rows[0] if rows else None


def _active_direct_edge(manager, root_id):
    tip = _active_tip(manager.store, root_id)
    if not tip or not tip.get("parent_generation_id"):
        return None, None, None
    parent_gen = _row(manager.store, generation_id=tip["parent_generation_id"])
    if not parent_gen or parent_gen.get("generation_type") != "candidate":
        return None, None, None
    with manager.store.conn() as c:
        edge = c.execute(
            "SELECT * FROM agent_candidates WHERE parent_agent_id=? AND candidate_id=?",
            (str(parent_gen.get("agent_id")), str(tip.get("agent_id"))),
        ).fetchone()
    return (dict(edge) if edge else None), parent_gen, tip


def _retention(manager, root_id):
    keep = max(2, int(OPTIONS.get("agent_candidate_model_retention", 3)))
    with manager.store.conn() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT * FROM agent_candidate_generations
               WHERE root_agent_id=? AND generation_type='candidate' AND model_retained=1
               ORDER BY generation_number DESC,created_ts DESC""",
            (str(root_id),),
        ).fetchall()]
    for generation in rows[keep:]:
        children = _generation_children(manager.store, generation["generation_id"])
        # Never prune a model that is still the direct parent of an active child.
        if any(child.get("lifecycle_state") not in ("discarded", "pruned", "promoted") for child in children):
            continue
        agent_id = generation.get("agent_id")
        if not agent_id:
            continue
        with manager.store.lock, manager.store.conn() as c:
            c.execute(
                "DELETE FROM agent_candidates WHERE candidate_id=? AND state='parent'",
                (str(agent_id),),
            )
            c.execute(
                "DELETE FROM agent_candidates WHERE parent_agent_id=? AND state='parent'",
                (str(agent_id),),
            )
        try:
            manager.store.delete_agent(str(agent_id))
        except Exception:
            continue
        with manager.store.lock, manager.store.conn() as c:
            c.execute(
                """UPDATE agent_candidate_generations
                   SET agent_id=NULL,model_retained=0,lifecycle_state='pruned',retired_ts=?,updated_ts=?
                   WHERE generation_id=?""",
                (time.time(), time.time(), generation["generation_id"]),
            )
    try:
        from agent_candidates import refresh_candidate_ids_cache
        refresh_candidate_ids_cache(manager.store)
    except Exception:
        pass


def install(manager):
    if getattr(manager, "_candidate_lineage_installed", False):
        return manager

    import agent_candidates as candidate_module
    try:
        import agent_candidate_config_guard as config_guard_module
    except Exception:
        config_guard_module = None

    ensure_lineage_tables(manager.store)
    migrate_existing_candidates(manager.store)
    try:
        candidate_module.refresh_candidate_ids_cache(manager.store)
    except Exception:
        pass

    # Existing Store overlays resolve these module globals dynamically.  Extending them
    # here keeps every retained ancestor hidden from ordinary live-agent enumeration.
    candidate_module._candidate_ids = _all_hidden_ids
    candidate_module.is_candidate = _is_candidate
    if config_guard_module is not None:
        config_guard_module.is_candidate = _is_candidate

    original_create = manager._create_candidate
    original_start = manager._start_build
    original_finish = manager._finish_build_if_ready
    original_status = manager.status
    original_list_status = manager.list_status
    original_delete = manager._delete_candidate
    original_discard = manager.discard
    original_promote = manager.promote
    original_before = manager.before_live_process
    original_after = manager.after_live_process

    def register_created(parent, row, reason=None):
        parent_gen = _row(manager.store, agent_id=parent["id"])
        if parent_gen is None:
            parent_gen = _ensure_root(manager.store, parent["id"], manager._generation(parent["id"]))
        if parent_gen is None:
            raise RuntimeError("Cannot resolve Candidate parent generation")
        parent_gen = _refresh_live_generation_snapshot(manager.store, parent_gen)
        root = parent_gen["root_agent_id"]
        existing = _row(manager.store, agent_id=row["candidate_id"])
        if existing is None:
            existing = _register_generation(
                manager.store, root, parent_gen, row["candidate_id"],
                int(parent_gen["generation_number"]) + 1,
                reason or row.get("reason") or "feedback", row.get("state") or "queued",
            )
        return existing

    def create_candidate(parent):
        row = original_create(parent)
        register_created(parent, row, row.get("reason") if row else None)
        return row

    def spawn_child(parent_ref, reason="candidate_correct"):
        parent_gen = _row(manager.store, generation_id=parent_ref) or _row(manager.store, agent_id=parent_ref)
        if not parent_gen:
            raise ValueError("Candidate generation not found")
        if parent_gen.get("generation_type") != "candidate":
            raise ValueError("Use the normal Live Candidate workflow for the root generation")
        if not parent_gen.get("agent_id") or not manager.store.get_model(parent_gen["agent_id"]):
            raise ValueError("Parent generation model has been pruned and cannot create a child")
        existing_children = _generation_children(manager.store, parent_gen["generation_id"])
        if existing_children:
            raise ValueError("This generation already has a child; only one active branch is allowed")
        tip = _active_tip(manager.store, parent_gen["root_agent_id"])
        if tip and tip["generation_id"] != parent_gen["generation_id"]:
            raise ValueError("Only the active lineage tip may create the next generation")

        parent_agent = manager.store.get_agent_config(parent_gen["agent_id"])
        if not parent_agent:
            raise ValueError("Parent Candidate surrogate is unavailable")
        # _create_candidate is already wrapped by conservative-correct, therefore the new
        # child begins as an exact snapshot of this Candidate parent, never Root Live.
        manager._set_generation(parent_agent["id"], int(parent_gen["generation_number"]))
        row = manager._create_candidate(parent_agent)
        with manager.store.lock, manager.store.conn() as c:
            c.execute(
                "UPDATE agent_candidates SET reason=?,generation=?,updated_ts=? WHERE parent_agent_id=?",
                (str(reason), int(parent_gen["generation_number"]) + 1, time.time(), parent_agent["id"]),
            )
            parent_edge = c.execute(
                "SELECT * FROM agent_candidates WHERE candidate_id=?",
                (str(parent_agent["id"]),),
            ).fetchone()
            resume_state = str(parent_edge["state"]) if parent_edge else "comparing"
            if parent_edge:
                c.execute(
                    "UPDATE agent_candidates SET state='parent',updated_ts=? WHERE candidate_id=?",
                    (time.time(), str(parent_agent["id"])),
                )
            c.execute(
                """UPDATE agent_candidate_generations
                   SET lifecycle_state='parent',resume_state=?,comparison_json=?,updated_ts=?
                   WHERE generation_id=?""",
                (
                    resume_state,
                    str(parent_edge["comparison_json"] if parent_edge else parent_gen.get("comparison_json") or "{}"),
                    time.time(), parent_gen["generation_id"],
                ),
            )
        child = register_created(parent_agent, manager._candidate_row(parent_agent["id"]), reason)
        manager.store.event(
            parent_gen["root_agent_id"], "info", "agent_candidate_child_created",
            "Candidate child created from its direct parent generation",
            {
                "root_agent_id": parent_gen["root_agent_id"],
                "parent_generation_id": parent_gen["generation_id"],
                "generation_id": child["generation_id"],
                "generation_number": child["generation_number"],
                "parent_type": "candidate",
            },
        )
        _retention(manager, parent_gen["root_agent_id"])
        manager.wake_event.set()
        return lineage_status(child["generation_id"])

    def lineage_status(ref):
        generation = _row(manager.store, generation_id=ref) or _row(manager.store, agent_id=ref)
        if not generation:
            return None
        parent_gen = _row(manager.store, generation_id=generation.get("parent_generation_id")) if generation.get("parent_generation_id") else None
        edge_status = None
        if parent_gen and parent_gen.get("agent_id") and generation.get("agent_id"):
            edge_status = original_status(parent_gen["agent_id"])
        out = dict(generation)
        out["parent_agent_id"] = parent_gen.get("agent_id") if parent_gen else None
        out["comparison_parent_generation_id"] = parent_gen.get("generation_id") if parent_gen else None
        out["comparison_parent_type"] = parent_gen.get("generation_type") if parent_gen else None
        if edge_status:
            out["edge"] = edge_status
            out["state"] = edge_status.get("state")
            out["comparison"] = edge_status.get("comparison")
            out["promotable"] = edge_status.get("promotable")
        else:
            out["state"] = generation.get("lifecycle_state")
        return out

    def start_build(row):
        result = original_start(row)
        fresh = manager._candidate_row(row["parent_agent_id"]) or row
        generation = _refresh_generation_metadata(
            manager.store, fresh.get("candidate_id"),
            lifecycle_state=fresh.get("state"), comparison_json=fresh.get("comparison_json"),
        )
        if generation and generation.get("parent_generation_id"):
            parent_gen = _row(manager.store, generation_id=generation["parent_generation_id"])
            # Snapshot identity must point at the direct parent at creation.  Keep parent
            # metadata immutable; only the child is refreshed after fine-tune.
            if parent_gen:
                pass
        return result

    def finish_build(row):
        result = original_finish(row)
        fresh = manager._candidate_row(row["parent_agent_id"]) or row
        _refresh_generation_metadata(
            manager.store, fresh.get("candidate_id"),
            lifecycle_state=fresh.get("state"), comparison_json=fresh.get("comparison_json"),
        )
        return result

    def status(parent_id):
        result = original_status(parent_id)
        if not result:
            return result
        generation = _row(manager.store, agent_id=result.get("candidate_id"))
        if not generation:
            return result
        parent_gen = _row(manager.store, generation_id=generation.get("parent_generation_id"))
        result.update({
            "root_agent_id": generation["root_agent_id"],
            "generation_id": generation["generation_id"],
            "parent_generation_id": generation.get("parent_generation_id"),
            "generation_number": int(generation["generation_number"]),
            "parent_type": generation.get("parent_type"),
            "model_identity": generation.get("model_identity"),
            "config_fingerprint": generation.get("config_fingerprint"),
            "schema_revision": generation.get("schema_revision"),
            "model_revision": generation.get("model_revision"),
            "created_reason": generation.get("created_reason"),
            "lineage_state": generation.get("lifecycle_state"),
            "comparison_parent_agent_id": parent_gen.get("agent_id") if parent_gen else None,
        })
        return result

    def list_status():
        # One compact card per root: show only the current leaf. Ancestor generations stay
        # durable and queryable through lineage_status/list_lineage.
        roots = set()
        with manager.store.conn() as c:
            roots.update(str(r[0]) for r in c.execute(
                "SELECT DISTINCT root_agent_id FROM agent_candidate_generations WHERE generation_type='candidate'"
            ).fetchall())
        output = []
        for root in sorted(roots):
            tip = _active_tip(manager.store, root)
            if not tip:
                continue
            parent_gen = _row(manager.store, generation_id=tip.get("parent_generation_id"))
            if not parent_gen or not parent_gen.get("agent_id"):
                continue
            item = status(parent_gen["agent_id"])
            if item:
                output.append(item)
        return output

    def list_lineage(root_agent_id):
        with manager.store.conn() as c:
            rows = [dict(r) for r in c.execute(
                """SELECT * FROM agent_candidate_generations WHERE root_agent_id=?
                   ORDER BY generation_number,created_ts""",
                (str(root_agent_id),),
            ).fetchall()]
        return [lineage_status(r["generation_id"]) or r for r in rows]

    def _active_cycle_generations(root_id):
        with manager.store.conn() as c:
            return [dict(r) for r in c.execute(
                """SELECT * FROM agent_candidate_generations
                   WHERE root_agent_id=? AND generation_type='candidate'
                     AND lifecycle_state NOT IN ('discarded','pruned','promoted')
                   ORDER BY generation_number DESC,created_ts DESC""",
                (str(root_id),),
            ).fetchall()]

    def _discard_cycle(root_id, *, source="user_discard"):
        """Retire the whole unpromoted Candidate branch for this Live root.

        The UI exposes one Candidate card and one Discard action. Returning to an older
        hidden Candidate after that action is therefore incorrect product behaviour.
        Durable lineage rows remain for audit, but every unpromoted surrogate/model is
        retired so the next correction starts a fresh Candidate cycle from Live.
        """
        generations = _active_cycle_generations(root_id)
        if not generations:
            return 0
        surrogate_ids = [str(g["agent_id"]) for g in generations if g.get("agent_id")]

        # Delete deepest edges first so no child can outlive its direct parent surrogate.
        for generation in generations:
            agent_id = generation.get("agent_id")
            if not agent_id:
                continue
            with manager.store.conn() as c:
                edge = c.execute(
                    "SELECT * FROM agent_candidates WHERE candidate_id=?",
                    (str(agent_id),),
                ).fetchone()
            if edge is not None:
                original_delete(dict(edge))
            else:
                queue = manager._queue()
                if queue is not None:
                    queue.cancel(str(agent_id))
                manager.engine.models.pop(str(agent_id), None)
                manager.engine.runtime.pop(str(agent_id), None)
                try:
                    manager.store.delete_agent(str(agent_id))
                except Exception:
                    pass

        now = time.time()
        with manager.store.lock, manager.store.conn() as c:
            c.execute(
                """UPDATE agent_candidate_generations
                   SET lifecycle_state='discarded',model_retained=0,agent_id=NULL,
                       resume_state=NULL,retired_ts=COALESCE(retired_ts,?),updated_ts=?
                   WHERE root_agent_id=? AND generation_type='candidate'
                     AND lifecycle_state NOT IN ('discarded','pruned','promoted')""",
                (now, now, str(root_id)),
            )
            if surrogate_ids:
                placeholders = ",".join("?" for _ in surrogate_ids)
                c.execute(
                    f"DELETE FROM agent_candidates WHERE candidate_id IN ({placeholders})",
                    surrogate_ids,
                )
                c.execute(
                    f"DELETE FROM agent_candidates WHERE parent_agent_id IN ({placeholders})",
                    surrogate_ids,
                )
                c.execute(
                    f"DELETE FROM agent_generation_state WHERE agent_id IN ({placeholders})",
                    surrogate_ids,
                )
        try:
            candidate_module.refresh_candidate_ids_cache(manager.store)
        except Exception:
            pass
        manager.runtime.pop(str(root_id), None)
        manager.store.event(
            str(root_id), "info", "agent_candidate_cycle_discarded",
            "Candidate cycle discarded; next Candidate will branch from the current Live model",
            {"retired_generations": len(generations), "source": str(source)},
        )
        return len(generations)

    def delete_candidate(row):
        generation = _row(manager.store, agent_id=row.get("candidate_id"))
        cascade_cycle = int(row.get("discard_requested") or 0) >= 2
        if generation and cascade_cycle:
            _discard_cycle(generation["root_agent_id"], source="user_discard")
            return None

        parent_gen = _row(manager.store, generation_id=generation.get("parent_generation_id")) if generation else None
        result = original_delete(row)
        if generation:
            now = time.time()
            with manager.store.lock, manager.store.conn() as c:
                c.execute(
                    """UPDATE agent_candidate_generations
                       SET lifecycle_state='discarded',model_retained=0,agent_id=NULL,retired_ts=?,updated_ts=?
                       WHERE generation_id=?""",
                    (now, now, generation["generation_id"]),
                )
                if parent_gen and parent_gen.get("generation_type") == "candidate" and parent_gen.get("agent_id"):
                    resume = parent_gen.get("resume_state") or "comparing"
                    c.execute(
                        "UPDATE agent_candidates SET state=?,updated_ts=? WHERE candidate_id=? AND state='parent'",
                        (resume, now, str(parent_gen["agent_id"])),
                    )
                    c.execute(
                        """UPDATE agent_candidate_generations
                           SET lifecycle_state=?,resume_state=NULL,updated_ts=? WHERE generation_id=?""",
                        (resume, now, parent_gen["generation_id"]),
                    )
        return result

    def discard(parent_id):
        row = manager._candidate_row(parent_id)
        if not row:
            return original_discard(parent_id)
        generation = _row(manager.store, agent_id=row.get("candidate_id"))
        if generation:
            # 2 means user-visible cycle discard. 1 remains the legacy/deferred single-edge
            # marker used by internal lifecycle cleanup.
            with manager.store.lock, manager.store.conn() as c:
                c.execute(
                    "UPDATE agent_candidates SET discard_requested=2,updated_ts=? WHERE parent_agent_id=?",
                    (time.time(), str(parent_id)),
                )
        result = original_discard(parent_id)
        if generation and isinstance(result, dict) and result.get("state") == "discarding":
            # Base discard writes 1 for an active training job; restore the stronger
            # durable intent so deferred cleanup still retires the whole cycle.
            with manager.store.lock, manager.store.conn() as c:
                c.execute(
                    "UPDATE agent_candidates SET discard_requested=2,updated_ts=? WHERE parent_agent_id=?",
                    (time.time(), str(parent_id)),
                )
        if isinstance(result, dict) and generation:
            result = dict(result)
            result["discard_scope"] = "candidate_cycle"
            result["root_agent_id"] = generation["root_agent_id"]
        return result

    def _repair_legacy_discard_rollbacks():
        marker = "candidate_discard_cycle_migration_v1"
        getter = getattr(manager.store, "meta_get", None)
        setter = getattr(manager.store, "meta_set", None)
        if callable(getter) and getter(marker) == "1":
            return
        repaired = []
        with manager.store.conn() as c:
            roots = [str(r[0]) for r in c.execute(
                """SELECT DISTINCT root_agent_id FROM agent_candidate_generations
                   WHERE generation_type='candidate'
                     AND lifecycle_state NOT IN ('discarded','pruned','promoted')"""
            ).fetchall()]
        for root_id in roots:
            with manager.store.conn() as c:
                live = c.execute(
                    """SELECT generation_number FROM agent_candidate_generations
                       WHERE root_agent_id=? AND generation_type='live' AND agent_id=?
                         AND lifecycle_state='live'
                       ORDER BY updated_ts DESC,generation_number DESC LIMIT 1""",
                    (root_id, root_id),
                ).fetchone()
                live_number = int(live[0]) if live else 0
                old_discard = c.execute(
                    """SELECT 1 FROM agent_candidate_generations
                       WHERE root_agent_id=? AND generation_type='candidate'
                         AND lifecycle_state='discarded' AND generation_number>?
                       LIMIT 1""",
                    (root_id, live_number),
                ).fetchone()
            if old_discard and _active_tip(manager.store, root_id):
                count = _discard_cycle(root_id, source="legacy_discard_repair")
                if count:
                    repaired.append((root_id, count))
        if callable(setter):
            setter(marker, "1")
        for root_id, count in repaired:
            manager.store.event(
                root_id, "warning", "legacy_candidate_discard_repaired",
                "Retired Candidate ancestors that an older Discard implementation could reactivate",
                {"retired_generations": count},
            )

    def before_live_process(agent, state_map):
        original_before(agent, state_map)
        edge, parent_gen, _ = _active_direct_edge(manager, agent["id"])
        if not edge or edge.get("state") not in ("comparing", "ready") or not parent_gen.get("agent_id"):
            return
        parent = manager.store.get_agent_config(parent_gen["agent_id"])
        if parent:
            original_before(parent, state_map)

    def after_live_process(agent, state_map):
        original_after(agent, state_map)
        edge, parent_gen, child_gen = _active_direct_edge(manager, agent["id"])
        if not edge or edge.get("state") not in ("comparing", "ready"):
            return
        parent = manager.store.get_agent_config(parent_gen.get("agent_id"))
        child = manager.store.get_agent_config(child_gen.get("agent_id"))
        if not parent or not child or not manager.store.get_model(parent["id"]) or not manager.store.get_model(child["id"]):
            return
        try:
            policy = manager.engine.models.get(parent["id"])
            if policy is None:
                policy = manager.engine.policy(parent)
            features, _, _ = policy.features(state_map, manager.engine.temporal_history, at_ts=time.time())
            prediction = float(policy.predict(features)[0]["value"])
        except Exception as exc:
            manager._fail(edge, f"parent Candidate inference failed: {type(exc).__name__}: {exc}")
            return
        manager.engine.runtime.setdefault(parent["id"], {})["last_prediction"] = prediction
        # Reuse the established Candidate inference/scoring path with this hidden parent
        # acting as the comparison champion.  It computes the child policy only; no
        # ActionIntent/Executor/HA service path is reachable here.
        original_after(parent, state_map)

    def promote(parent_id):
        parent_gen = _row(manager.store, agent_id=parent_id)
        if not parent_gen or parent_gen.get("generation_type") != "candidate":
            return original_promote(parent_id)
        row = manager._candidate_row(parent_id)
        if not row:
            raise ValueError("Candidate child not found")
        state = status(parent_id)
        if not state or not state.get("promotable"):
            raise ValueError("Candidate needs more future paired evidence before Promote")
        child_gen = _row(manager.store, agent_id=row["candidate_id"])
        root_id = str(parent_gen["root_agent_id"])
        root = manager.store.get_agent(root_id)
        candidate = manager.store.get_agent(row["candidate_id"])
        model = manager.store.get_model(row["candidate_id"])
        if not root or not candidate or not model:
            raise ValueError("Candidate model unavailable")
        comparison = state.get("comparison") or {}
        now = time.time()
        with manager.engine.executor.target_lock(root["target_entity"]):
            if root.get("mode") == "control":
                manager.engine.executor.release_control(root, reason="candidate_promote")
            old_model = manager.store.get_model(root_id)
            with manager.store.lock, manager.store.conn() as c:
                c.execute(
                    """INSERT INTO agent_generation_backups
                       (agent_id,generation,created_ts,expires_ts,model_json,agent_json,comparison_json)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        root_id, manager._generation(root_id), now, now + 86400.0,
                        json.dumps(old_model, separators=(",", ":")) if old_model else None,
                        json.dumps(root, separators=(",", ":"), default=str),
                        json.dumps(comparison, separators=(",", ":")),
                    ),
                )
            manager.store.save_model(root_id, model)
            manager.store.set_training_state(
                root_id, "qualified", score=candidate.get("benchmark_score"),
                samples=candidate.get("benchmark_samples") or 0,
                source=candidate.get("benchmark_source"), detail=candidate.get("benchmark_detail") or {},
            )
            manager.engine.models.pop(root_id, None)
            manager.engine.runtime.pop(root_id, None)
            manager._set_generation(root_id, int(child_gen["generation_number"]))
            cstate = "promoted"
            with manager.store.lock, manager.store.conn() as c:
                c.execute(
                    """UPDATE agent_candidate_generations SET lifecycle_state=?,comparison_json=?,
                       retired_ts=?,updated_ts=? WHERE generation_id=?""",
                    (cstate, json.dumps(comparison, separators=(",", ":")), now, now, child_gen["generation_id"]),
                )
                c.execute(
                    "DELETE FROM agent_candidates WHERE parent_agent_id IN (SELECT agent_id FROM agent_candidate_generations WHERE root_agent_id=?)",
                    (root_id,),
                )
                c.execute("DELETE FROM agent_candidates WHERE parent_agent_id=?", (root_id,))
        manager.store.event(
            root_id, "info", "agent_candidate_lineage_promoted",
            "Candidate lineage tip promoted to Root Live; direct-parent evidence retained in lineage metadata",
            {"generation_id": child_gen["generation_id"], "generation_number": child_gen["generation_number"]},
        )
        manager.engine.wake_event.set()
        _retention(manager, root_id)
        return {"ok": True, "agent_id": root_id, "generation": int(child_gen["generation_number"]), "mode": "shadow"}

    manager._create_candidate = create_candidate
    manager._start_build = start_build
    manager._finish_build_if_ready = finish_build
    manager.status = status
    manager.list_status = list_status
    manager._delete_candidate = delete_candidate
    manager.discard = discard
    manager.before_live_process = before_live_process
    manager.after_live_process = after_live_process
    manager.promote = promote
    manager.spawn_child = spawn_child
    manager.lineage_status = lineage_status
    manager.list_lineage = list_lineage
    manager._candidate_lineage_installed = True
    manager.candidate_lineage_contract = "single_branch_direct_parent_generation_chain"
    manager.candidate_model_retention = max(2, int(OPTIONS.get("agent_candidate_model_retention", 3)))

    # 0.14.57 migration: older Discard could expose an ancestor Candidate again. Repair
    # those rollback-shaped branches once, preserving their metadata as discarded audit.
    _repair_legacy_discard_rollbacks()

    # Refresh migrated metadata after all wrappers are active.
    for generation in list_lineage_roots(manager.store):
        _retention(manager, generation)
    return manager


def list_lineage_roots(store):
    with store.conn() as c:
        return [str(r[0]) for r in c.execute(
            "SELECT DISTINCT root_agent_id FROM agent_candidate_generations WHERE generation_type='candidate'"
        ).fetchall()]
