"""HTTP/runtime diagnostics for Control qualification and Sensor Tournament state.

This module is intentionally outside the safety path.  It decorates the qualification
payload exposed by the web API, but it never replaces or relaxes the statistical checks
implemented in ``qualification.py``.  In particular, Executor keeps its direct import of
``assess_control_qualification`` and remains unaware of feature selection.

Architecture boundary:

    Context Tournament -> Policy -> ActionIntent -> Executor

The diagnostics layer only observes persisted Tournament state after the fact.
"""
import json
import threading
import time

from settings import OPTIONS, parse_ts


DIAGNOSTIC_TABLE = "context_schema_diagnostics"


def _schema_signature(features):
    return json.dumps([str(x) for x in (features or [])], separators=(",", ":"))


def _prequential_samples(agent):
    """Return only future/prequential evidence used for the current Control proof."""
    detail = dict((agent or {}).get("benchmark_detail") or {})
    explicit = detail.get("prequential_samples")
    if explicit is not None:
        try:
            return max(0, int(explicit))
        except (TypeError, ValueError):
            return 0
    contract = str(detail.get("rebenchmark_contract") or "").lower()
    if "prequential" not in contract:
        return 0
    counts = dict(detail.get("counts") or {})
    try:
        return max(0, int(counts.get("samples") or 0))
    except (TypeError, ValueError):
        return 0


def _probation_summary(service, agent_id):
    getter = getattr(service, "schema_probation", None)
    if not callable(getter):
        return None
    try:
        row = getter(agent_id)
    except Exception:
        return None
    if not row:
        return None
    return {
        "status": str(row.get("status") or "unknown"),
        "samples": int(row.get("samples") or 0),
        "history_id": row.get("history_id"),
    }


def _promotion_summary(service, agent):
    getter = getattr(service, "promotion_status", None)
    if not callable(getter):
        return {}
    try:
        row = getter(agent) or {}
    except Exception:
        return {}
    return {
        "last_promotion_ts": row.get("last_promotion_ts"),
        "last_promoted_entity": row.get("promoted_entity"),
        "last_replaced_entity": row.get("replaced_entity"),
    }


def feature_tournament_state(service, agent, tournament_state=None):
    """Compact diagnostics only; never gates Policy or Executor behaviour."""
    state = dict(tournament_state or service.state(agent["id"]) or {})
    challengers = list(state.get("challenger_features") or [])
    probation = _probation_summary(service, agent["id"])
    promotion = _promotion_summary(service, agent)
    enabled = bool(OPTIONS.get("context_tournament_enabled", True))
    if not enabled:
        phase = "disabled"
    elif probation and probation.get("status") == "active":
        phase = "schema_probation"
    elif challengers:
        phase = "evaluating"
    else:
        phase = "stable"
    return {
        "state": phase,
        "enabled": enabled,
        "active_feature_count": len(state.get("active_features") or []),
        "challenger_count": len(challengers),
        "challengers": challengers,
        "last_evaluation": state.get("last_evaluation"),
        "schema_probation": probation,
        **promotion,
    }


class SchemaAgeTracker:
    """Persist when a Tournament schema revision became current.

    Tournament's own ``updated_ts`` also changes for relevance/evaluation refreshes, so it
    is not a valid schema age.  This tiny diagnostic table tracks only revision/signature
    changes and therefore survives restarts without modifying Tournament control logic.
    """

    def __init__(self, store, service):
        self.store = store
        self.service = service
        self.lock = threading.RLock()
        self._cache = {}
        with self.store.lock, self.store.conn() as c:
            c.execute(
                f"""CREATE TABLE IF NOT EXISTS {DIAGNOSTIC_TABLE} (
                       agent_id TEXT PRIMARY KEY,
                       schema_revision INTEGER NOT NULL DEFAULT 0,
                       schema_signature TEXT NOT NULL DEFAULT '[]',
                       schema_changed_ts REAL NOT NULL,
                       updated_ts REAL NOT NULL
                   )"""
            )
            rows = c.execute(
                f"SELECT agent_id,schema_revision,schema_signature,schema_changed_ts FROM {DIAGNOSTIC_TABLE}"
            ).fetchall()
        with self.lock:
            self._cache = {
                str(row["agent_id"]): {
                    "schema_revision": int(row["schema_revision"] or 0),
                    "schema_signature": str(row["schema_signature"] or "[]"),
                    "schema_changed_ts": float(row["schema_changed_ts"]),
                }
                for row in rows
            }

    def _best_initial_ts(self, agent, active_features, now):
        history = getattr(self.service, "schema_history", None)
        if callable(history):
            try:
                for row in history(agent["id"], limit=20) or []:
                    # A successful promotion's new schema is the strongest timestamp we
                    # have. Rolled-back rows do not timestamp the rollback itself, so do
                    # not pretend the old schema started at the promotion time.
                    if (row.get("status") != "rolled_back"
                            and list(row.get("new_schema") or []) == list(active_features or [])):
                        return float(row.get("created_ts") or now)
            except Exception:
                pass
        trained = parse_ts((agent or {}).get("training_updated_at"))
        if trained is not None:
            return float(trained)
        return float(now)

    def observe(self, agent, tournament_state, now=None):
        now = float(time.time() if now is None else now)
        aid = str(agent["id"])
        revision = int((tournament_state or {}).get("schema_revision") or 0)
        active = list((tournament_state or {}).get("active_features") or [])
        signature = _schema_signature(active)
        with self.lock:
            cached = dict(self._cache.get(aid) or {})
        if (cached
                and int(cached.get("schema_revision") or 0) == revision
                and str(cached.get("schema_signature") or "[]") == signature):
            changed_ts = float(cached["schema_changed_ts"])
            return {
                "schema_revision": revision,
                "schema_changed_ts": changed_ts,
                "schema_age": max(0.0, now - changed_ts),
            }

        changed_ts = (
            self._best_initial_ts(agent, active, now)
            if not cached else now
        )
        with self.store.lock, self.store.conn() as c:
            c.execute(
                f"""INSERT INTO {DIAGNOSTIC_TABLE}
                    (agent_id,schema_revision,schema_signature,schema_changed_ts,updated_ts)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(agent_id) DO UPDATE SET
                      schema_revision=excluded.schema_revision,
                      schema_signature=excluded.schema_signature,
                      schema_changed_ts=excluded.schema_changed_ts,
                      updated_ts=excluded.updated_ts""",
                (aid, revision, signature, changed_ts, now),
            )
        with self.lock:
            self._cache[aid] = {
                "schema_revision": revision,
                "schema_signature": signature,
                "schema_changed_ts": changed_ts,
            }
        return {
            "schema_revision": revision,
            "schema_changed_ts": changed_ts,
            "schema_age": max(0.0, now - float(changed_ts)),
        }


def enrich_qualification(base, agent, tournament_state, schema_meta, tournament_diag):
    """Add observability fields without changing any pass/fail qualification result."""
    result = dict(base or {})
    result["schema_revision"] = int(schema_meta.get("schema_revision") or 0)
    result["schema_age"] = float(schema_meta.get("schema_age") or 0.0)
    result["prequential_samples"] = _prequential_samples(agent)
    result["feature_tournament_state"] = dict(tournament_diag or {})
    return result


def install_control_diagnostics(core, service):
    """Decorate HTTP/runtime diagnostics only; Executor keeps the base qualifier."""
    if getattr(service, "_control_diagnostics_installed", False):
        return getattr(service, "control_diagnostics", None)

    tracker = SchemaAgeTracker(service.store, service)
    original_sync = service.sync_agent
    original_state_for_agent = service.state_for_agent

    def sync_with_schema_age(agent, *args, **kwargs):
        state = original_sync(agent, *args, **kwargs)
        tracker.observe(agent, state)
        return state

    def state_with_schema_age(agent):
        state = original_state_for_agent(agent)
        meta = tracker.observe(agent, state)
        state["schema_changed_ts"] = meta["schema_changed_ts"]
        state["schema_age"] = meta["schema_age"]
        return state

    # The main/queue HTTP modules look up this global dynamically. Executor imported the
    # base function directly when engine.py was loaded, so this wrapper cannot alter the
    # ActionIntent -> Executor safety boundary.
    base_assess = core.assess_control_qualification

    def assess_with_diagnostics(agent):
        qualification = base_assess(agent)
        tournament = service.state(agent["id"])
        schema_meta = tracker.observe(agent, tournament)
        tournament_diag = feature_tournament_state(service, agent, tournament)
        return enrich_qualification(
            qualification, agent, tournament, schema_meta, tournament_diag,
        )

    service.sync_agent = sync_with_schema_age
    service.state_for_agent = state_with_schema_age
    core.assess_control_qualification = assess_with_diagnostics
    service.control_diagnostics = tracker
    service._control_diagnostics_installed = True
    return tracker
