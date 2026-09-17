"""Durable, generation-bound knowledge for bounded physical experiments.

Stage 11 fixes the Free Explore ownership gap without introducing a second control path.
Physical probes are still proposed by ``Experiments`` and dispatched only by Executor.
This layer journals every started trial as a versioned TrialRecord and, for Free Explore,
removes residual-learning side effects from Live.  A direct child Candidate starts from an
exact parent snapshot and is updated exactly once from the labelled TrialRecords assigned
to that Explore session.

The integration deliberately chooses explicit child training from trial experiences over a
runtime base+residual composition.  A generation therefore remains one complete policy
snapshot: rollback restores the parent policy atomically and ordinary historical replay is
never allowed to reinterpret a negative trial as a positive demonstration.
"""
from __future__ import annotations

import copy
import json
import math
import time
import uuid

from agent_candidate_conservative_correct import (
    _benchmark_stats,
    _copy_parent_snapshot,
    _offline_gate,
    _persist_gate,
)
from agent_candidate_lineage import _refresh_generation_metadata, _row as lineage_row
from agent_explore import (
    FREE_MODE,
    FREE_REASON,
    FREE_TRAIN_REASON,
    _active_free_for_live,
    _edge,
    _merge_result,
    _row as explore_row,
    _session_for_child,
)
from context import state_scalar, target_value
from control import legal_value, same_value, timing_for
from settings import OPTIONS, iso_now


TRIAL_RECORD_VERSION = 1
SAFE_HYPOTHESES = {"earlier_on", "small_brightness_adjustment"}
BRIGHTNESS_PROPERTIES = {"brightness", "brightness_pct", "percentage", "level", "value"}


def _json(raw, default=None):
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {} if default is None else default


def _dumps(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str, allow_nan=False)


def _finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def ensure_trial_tables(store):
    """Additive migration. Existing experiment metadata and policies remain untouched."""
    with store.lock, store.conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS experiment_trial_records (
                trial_id TEXT PRIMARY KEY,
                record_version INTEGER NOT NULL,
                owner_agent_id TEXT NOT NULL,
                root_agent_id TEXT,
                session_id TEXT,
                child_generation_id TEXT,
                hypothesis_json TEXT NOT NULL DEFAULT '{}',
                context_json TEXT NOT NULL DEFAULT '{}',
                model_versions_json TEXT NOT NULL DEFAULT '{}',
                action_set_json TEXT NOT NULL DEFAULT '[]',
                assigned_action_json TEXT NOT NULL DEFAULT '{}',
                propensity REAL,
                baseline_json TEXT NOT NULL DEFAULT '{}',
                dispatch_json TEXT NOT NULL DEFAULT '{}',
                ack_json TEXT NOT NULL DEFAULT '{}',
                outcome_sources_json TEXT NOT NULL DEFAULT '{}',
                episode_result_json TEXT NOT NULL DEFAULT '{}',
                termination_reason TEXT,
                reward REAL,
                status TEXT NOT NULL,
                learning_applied_generation_id TEXT,
                learning_applied_model_revision TEXT,
                learning_applied_ts REAL,
                created_ts REAL NOT NULL,
                updated_ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trial_records_session
                ON experiment_trial_records(session_id,created_ts);
            CREATE INDEX IF NOT EXISTS idx_trial_records_owner
                ON experiment_trial_records(owner_agent_id,created_ts);
            CREATE INDEX IF NOT EXISTS idx_trial_records_child
                ON experiment_trial_records(child_generation_id,created_ts);
            """
        )


class TrialJournal:
    """Versioned source of truth for experimental assignment and labelled outcomes."""

    VERSION = TRIAL_RECORD_VERSION

    def __init__(self, store):
        self.store = store
        ensure_trial_tables(store)

    def start(self, owner_agent_id, trial, session=None):
        trial = dict(trial or {})
        trial_id = str(trial.get("trial_id") or "")
        if not trial_id:
            raise ValueError("TrialRecord requires trial_id")
        meta = dict(trial.get("trial_record") or {})
        now = float(trial.get("started") or time.time())
        session = dict(session or {})
        hypothesis = dict(meta.get("hypothesis") or {})
        context = {
            "x": dict(trial.get("x") or {}),
            "policy_features": dict(meta.get("policy_features") or {}),
            "prediction_inputs": dict(trial.get("prediction_inputs") or {}),
            "background_dependencies": dict(trial.get("background_dependencies") or {}),
            "focus": trial.get("focus"),
            "target": trial.get("target"),
            "property": trial.get("property"),
            "support": trial.get("support"),
            "novelty": trial.get("novelty"),
            "confidence": trial.get("confidence"),
            "horizon": meta.get("horizon"),
            "selection_reason": meta.get("selection_reason"),
        }
        versions = dict(meta.get("model_versions") or {})
        actions = list(meta.get("action_set") or [])
        assigned = dict(meta.get("assigned_action") or {
            "index": trial.get("index"), "value": trial.get("value"), "kind": trial.get("kind"),
        })
        propensity = _finite(meta.get("assigned_propensity"))
        baseline = {
            "index": meta.get("baseline_index"),
            "value": trial.get("baseline"),
            "reference": bool(trial.get("kind") == "reference"),
        }
        dispatch = ({"required": False, "status": "reference_baseline", "at": now}
                    if trial.get("kind") == "reference" else
                    {"required": True, "status": "reserved", "at": None})
        ack = ({"status": "reference_baseline", "at": trial.get("ack") or now}
               if trial.get("kind") == "reference" else {"status": "pending", "at": None})
        root = session.get("root_agent_id") or owner_agent_id
        child = session.get("child_generation_id")
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """INSERT INTO experiment_trial_records
                   (trial_id,record_version,owner_agent_id,root_agent_id,session_id,
                    child_generation_id,hypothesis_json,context_json,model_versions_json,
                    action_set_json,assigned_action_json,propensity,baseline_json,
                    dispatch_json,ack_json,outcome_sources_json,episode_result_json,
                    status,created_ts,updated_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?)
                   ON CONFLICT(trial_id) DO UPDATE SET
                     hypothesis_json=excluded.hypothesis_json,
                     context_json=excluded.context_json,
                     model_versions_json=excluded.model_versions_json,
                     action_set_json=excluded.action_set_json,
                     assigned_action_json=excluded.assigned_action_json,
                     propensity=excluded.propensity,
                     baseline_json=excluded.baseline_json,
                     outcome_sources_json=excluded.outcome_sources_json,
                     session_id=COALESCE(experiment_trial_records.session_id,excluded.session_id),
                     child_generation_id=COALESCE(experiment_trial_records.child_generation_id,excluded.child_generation_id),
                     updated_ts=excluded.updated_ts""",
                (
                    trial_id, self.VERSION, str(owner_agent_id), str(root) if root else None,
                    str(session.get("session_id")) if session.get("session_id") else None,
                    str(child) if child else None, _dumps(hypothesis), _dumps(context),
                    _dumps(versions), _dumps(actions), _dumps(assigned), propensity,
                    _dumps(baseline), _dumps(dispatch), _dumps(ack),
                    _dumps(trial.get("outcome_sources") or {}), _dumps({}), now, time.time(),
                ),
            )
        return self.get(trial_id)

    def get(self, trial_id):
        with self.store.conn() as c:
            row = c.execute(
                "SELECT * FROM experiment_trial_records WHERE trial_id=?", (str(trial_id),)
            ).fetchone()
        return dict(row) if row else None

    def dispatch(self, trial_id, at, *, intent_id=None, desired_value=None):
        row = self.get(trial_id)
        if not row:
            return None
        payload = _json(row.get("dispatch_json"), {})
        payload.update({"required": True, "status": "dispatched", "at": float(at)})
        if intent_id is not None:
            payload["intent_id"] = str(intent_id)
        if desired_value is not None:
            payload["desired_value"] = float(desired_value)
        with self.store.lock, self.store.conn() as c:
            c.execute("UPDATE experiment_trial_records SET dispatch_json=?,updated_ts=? WHERE trial_id=?",
                      (_dumps(payload), time.time(), str(trial_id)))
        return self.get(trial_id)

    def ack(self, trial_id, at, value=None):
        row = self.get(trial_id)
        if not row:
            return None
        payload = _json(row.get("ack_json"), {})
        payload.update({"status": "acknowledged", "at": float(at)})
        if value is not None:
            payload["value"] = float(value)
        with self.store.lock, self.store.conn() as c:
            c.execute("UPDATE experiment_trial_records SET ack_json=?,updated_ts=? WHERE trial_id=?",
                      (_dumps(payload), time.time(), str(trial_id)))
        return self.get(trial_id)

    def finish(self, trial, reward, reason, at=None):
        trial = dict(trial or {})
        trial_id = str(trial.get("trial_id") or "")
        if not trial_id:
            return None
        if self.get(trial_id) is None:
            self.start(str(trial.get("owner_agent_id") or "unknown"), trial, None)
        at = time.time() if at is None else float(at)
        result = {
            "reward": None if reward is None else float(reward),
            "reason": str(reason),
            "started": trial.get("started"),
            "action_at": trial.get("action_at"),
            "ack_at": trial.get("ack"),
            "observation_start": trial.get("observation_start"),
            "observation_end": trial.get("observation_end"),
            "finished_at": at,
            "kind": trial.get("kind"),
            "focus": trial.get("focus"),
        }
        status = "labelled" if reward is not None else "unlabelled"
        with self.store.lock, self.store.conn() as c:
            c.execute(
                """UPDATE experiment_trial_records SET reward=?,termination_reason=?,status=?,
                   episode_result_json=?,outcome_sources_json=?,updated_ts=? WHERE trial_id=?""",
                (None if reward is None else float(reward), str(reason), status, _dumps(result),
                 _dumps(trial.get("outcome_sources") or {}), at, trial_id),
            )
        return self.get(trial_id)

    def records_for_session(self, session_id):
        with self.store.conn() as c:
            rows = c.execute(
                "SELECT * FROM experiment_trial_records WHERE session_id=? ORDER BY created_ts,trial_id",
                (str(session_id),),
            ).fetchall()
        return [dict(r) for r in rows]

    def off_policy_report(self, session_id, action_value, tolerance=1e-9):
        """Refuse counterfactual inference unless the logged policy covered the action."""
        target = float(action_value)
        covered = []
        records = self.records_for_session(session_id)
        for row in records:
            for action in _json(row.get("action_set_json"), []):
                value = _finite(action.get("value"))
                propensity = _finite(action.get("propensity"))
                if value is not None and propensity is not None and propensity > 0 and abs(value-target) <= tolerance:
                    covered.append({"trial_id": row["trial_id"], "propensity": propensity,
                                    "assigned": _json(row.get("assigned_action_json"), {})})
        if not covered:
            return {"supported": False, "reason": "no_propensity_coverage",
                    "action_value": target, "records": len(records), "estimate": None}
        labelled = [r for r in records if r.get("reward") is not None]
        return {"supported": True, "reason": "covered_logged_action", "action_value": target,
                "records": len(records), "labelled_records": len(labelled), "coverage": covered,
                "estimate": None,
                "note": "coverage permits later off-policy analysis; Stage 11 does not invent an estimator"}


def _session_for_live(store, aid):
    return _active_free_for_live(store, aid)


def _safe_catalog(agent):
    target = str(agent.get("target_entity") or "")
    prop = str(agent.get("target_property") or "")
    if not target.startswith("light."):
        return None
    if prop == "power":
        return "earlier_on"
    if prop in BRIGHTNESS_PROPERTIES:
        return "small_brightness_adjustment"
    return None


def _safe_probe_actions(agent, chosen, arms, current, max_step):
    hypothesis = _safe_catalog(agent)
    if not hypothesis:
        return []
    out = []
    for arm in arms or []:
        if abs(int(arm.get("index", -99)) - int(chosen.get("index", -999))) != 1:
            continue
        delta = float(arm.get("value")) - float(chosen.get("value"))
        if hypothesis == "earlier_on" and delta <= 0:
            continue
        desired = float(arm.get("value"))
        bound = float(max_step)
        if hypothesis == "small_brightness_adjustment":
            bound = min(bound, max(.01, (float(agent["max_value"])-float(agent["min_value"]))*0.05))
            desired = float(chosen["value"]) + math.copysign(bound, delta)
        try:
            legal = legal_value(agent, None, desired)
        except Exception:
            # legal_value needs a state for a subset of HA properties. The caller can
            # retry with the actual target state through _safe_action_with_state.
            legal = desired
        if abs(legal-float(chosen["value"])) > bound+1e-9:
            continue
        if current is not None and abs(legal-float(current)) > bound+1e-9 and hypothesis != "earlier_on":
            continue
        out.append(dict(arm, value=legal, hypothesis=hypothesis))
    return out


def _safe_action_with_state(agent, target_state, chosen, arm, current, max_step):
    hypothesis = _safe_catalog(agent)
    if not hypothesis:
        return None
    if abs(int(arm.get("index", -99)) - int(chosen.get("index", -999))) != 1:
        return None
    delta = float(arm.get("value")) - float(chosen.get("value"))
    if hypothesis == "earlier_on" and delta <= 0:
        return None
    desired = float(arm.get("value"))
    bound = float(max_step)
    if hypothesis == "small_brightness_adjustment":
        bound = min(bound, max(.01, (float(agent["max_value"])-float(agent["min_value"]))*0.05))
        desired = float(chosen["value"]) + math.copysign(bound, delta)
    try:
        legal = legal_value(agent, target_state, desired)
    except (TypeError, ValueError):
        return None
    if abs(legal-float(chosen["value"])) > bound+1e-9:
        return None
    if current is not None and abs(legal-float(current)) > bound+1e-9 and hypothesis != "earlier_on":
        return None
    if current is not None and same_value(legal, current, agent["deadband"]):
        return None
    return dict(arm, value=legal, hypothesis=hypothesis)


def _core_candidate_metadata(experiments, agent, policy, states, registry, features, labels,
                             chosen, arms, horizon, data):
    """Reconstruct the exact legacy candidate to log its real assignment propensity."""
    cfg = data["config"]
    selected, x, prediction_inputs = experiments._inputs(
        agent, policy, states, registry, features, labels, cfg["focus"], data
    )
    span = max(.01, agent["max_value"]-agent["min_value"])
    x["baseline_setting"] = (chosen["value"]-agent["min_value"])/span
    x["trial_strength"] = cfg["intensity"]
    current = target_value(states.get(agent["target_entity"]), agent["target_property"])
    candidates = []
    head = policy.heads[horizon]
    with policy.lock:
        for raw_arm in arms:
            arm = _safe_action_with_state(agent, states.get(agent["target_entity"]), chosen,
                                          raw_arm, current, cfg["max_step"])
            if not arm:
                continue
            delta = arm["value"]-chosen["value"]
            arm_id = 1 if delta > 0 else 2
            gain = cfg["intensity"]*.25 if cfg["focus"] == "devices" else 0.0
            for index in selected:
                gradient = (head.b[arm["index"]][index]/head.a[arm["index"]][index]
                            - head.b[chosen["index"]][index]/head.a[chosen["index"]][index])
                value = features.get(index, 0)
                shift = min(cfg["intensity"], max(0., 1-value if gradient > 0 else value+1))
                if cfg["focus"] != "presence" or gradient > 0:
                    gain += abs(gradient)*shift
            gap = max(0., chosen["mean"]-arm["mean"])
            if (gain <= 0 or gap > gain or
                arm.get("support", 0) < float(OPTIONS.get("min_historical_support", .2)) or
                arm.get("novelty", 1) > float(OPTIONS.get("max_context_novelty", .85))):
                continue
            candidates.append((arm_id, arm, gap, gain))
    if not candidates:
        return None
    learner = experiments._learner(data)
    selected_candidate = max(candidates, key=lambda c: experiments._score(learner, c[0], x))
    arm_id, arm, gap, gain = selected_candidate
    forced_reference = experiments._score(learner, 0, x) > experiments._score(learner, arm_id, x)
    reference_propensity = 1.0 if forced_reference else 0.25
    probe_propensity = 0.0 if forced_reference else 0.75
    return {"arm_id": arm_id, "arm": arm, "gap": gap, "gain": gain, "x": x,
            "prediction_inputs": prediction_inputs, "selected": selected,
            "reference_propensity": reference_propensity, "probe_propensity": probe_propensity}


def _decorate_trial(trial, ctx, experiments, session, *, information=False,
                    reference_propensity=None, probe_propensity=None):
    if not trial or not ctx:
        return trial
    agent, policy = ctx["agent"], ctx["policy"]
    hypothesis = _safe_catalog(agent) if session else "bounded_context_probe"
    action_set = []
    meta = None
    if session:
        meta = _core_candidate_metadata(
            experiments, agent, policy, ctx["states"], ctx["registry"], ctx["features"],
            ctx["labels"], ctx["chosen"], ctx["arms"], ctx["horizon"],
            experiments._get(agent["id"]),
        )
    if reference_propensity is None:
        reference_propensity = (meta or {}).get("reference_propensity")
    if probe_propensity is None:
        probe_propensity = (meta or {}).get("probe_propensity")
    baseline_idx = int(ctx["chosen"]["index"])
    baseline_value = float(ctx["chosen"]["value"])
    probe = (meta or {}).get("arm")
    if trial.get("kind") == "probe":
        probe = {"index": int(trial["index"]), "value": float(trial["value"])}
    if reference_propensity is not None:
        action_set.append({"role": "reference", "index": baseline_idx, "value": baseline_value,
                           "propensity": float(reference_propensity)})
    if probe is not None and probe_propensity is not None:
        action_set.append({"role": "probe", "index": int(probe["index"]),
                           "value": float(probe["value"]), "propensity": float(probe_propensity)})
    assigned_propensity = (reference_propensity if trial.get("kind") == "reference" else probe_propensity)
    versions = {
        "trial_record_version": TRIAL_RECORD_VERSION,
        "policy_version": int(getattr(policy, "VERSION", trial.get("policy_version") or 0)),
        "model_revision": str(getattr(policy, "model_revision", trial.get("model_revision") or "")),
        "schema_version": int(getattr(getattr(policy, "schema", None), "version", 0) or 0),
        "experiment_revision": int(experiments._get(agent["id"]).get("revision") or 0),
    }
    trial["trial_record"] = {
        "hypothesis": {"id": hypothesis, "catalog_version": 1,
                       "information_exploration": bool(information)},
        "policy_features": {str(k): float(v) for k, v in ctx["features"].items()
                            if _finite(v) is not None},
        "horizon": float(ctx["horizon"]),
        "model_versions": versions,
        "action_set": action_set,
        "assigned_action": {"kind": trial.get("kind"), "index": int(trial["index"]),
                            "value": float(trial["value"])},
        "assigned_propensity": assigned_propensity,
        "baseline_index": baseline_idx,
        "selection_reason": "information_exploration" if information else "legacy_bounded_trial",
        "session_id": session.get("session_id") if session else None,
        "child_generation_id": session.get("child_generation_id") if session else None,
    }
    return trial


def _information_trial(experiments, ctx, session):
    """Safe information probe when the base gradient cannot nominate an action.

    Called only after the legacy proposer reached its final "no nearby action" branch, so
    its control-mode, confidence, support, novelty, budget, cooldown, manual-priority and
    global physical-probe guards have already passed.
    """
    agent, policy = ctx["agent"], ctx["policy"]
    data = experiments._get(agent["id"])
    cfg = data["config"]
    if not _safe_catalog(agent):
        return None
    registry = ctx["registry"]
    selected, x, prediction_inputs = experiments._inputs(
        agent, policy, ctx["states"], registry, ctx["features"], ctx["labels"], cfg["focus"], data
    )
    outcome_sources = (experiments._presence_outcome_sources(agent, selected, ctx["states"], registry)
                       if cfg["focus"] == "presence" else {})
    now = experiments.clock()
    for source in outcome_sources.values():
        source["anchored_at"] = now
    if len(x) <= 1 or (cfg["focus"] == "presence" and
                       not any(v > .01 for k, v in x.items() if k != "bias")):
        return None
    span = max(.01, agent["max_value"]-agent["min_value"])
    x["baseline_setting"] = (ctx["chosen"]["value"]-agent["min_value"])/span
    x["trial_strength"] = cfg["intensity"]
    current = target_value(ctx["states"].get(agent["target_entity"]), agent["target_property"])
    safe = []
    for raw_arm in ctx["arms"]:
        arm = _safe_action_with_state(agent, ctx["states"].get(agent["target_entity"]),
                                      ctx["chosen"], raw_arm, current, cfg["max_step"])
        if not arm:
            continue
        if (arm.get("support", 0) < float(OPTIONS.get("min_historical_support", .2)) or
            arm.get("novelty", 1) > float(OPTIONS.get("max_context_novelty", .85))):
            continue
        safe.append(arm)
    if not safe:
        return None
    # Information value is uncertainty among already-safe adjacent actions, never an
    # excuse to expand the physical action set.
    arm = max(safe, key=lambda a: (float(a.get("uncertainty") or 0.0), -abs(a["value"]-ctx["chosen"]["value"])))
    reference = experiments.rng.random() < .25
    timing = timing_for(agent)
    window = max(cfg["observation_seconds"], timing.settling)
    trial = dict(
        trial_id=str(uuid.uuid4()), kind="reference" if reference else "probe",
        arm=0 if reference else (1 if arm["value"] > ctx["chosen"]["value"] else 2),
        value=ctx["chosen"]["value"] if reference else arm["value"],
        baseline=ctx["chosen"]["value"], index=ctx["chosen"]["index"] if reference else arm["index"],
        x=x, prediction_inputs=prediction_inputs, background_dependencies={},
        outcome_sources=outcome_sources, snapshot=dict(prediction_inputs),
        outcome_confirmers=sorted(outcome_sources), focus=cfg["focus"], revision=data["revision"],
        policy_version=policy.VERSION, model_revision=policy.model_revision,
        target=agent["target_entity"], property=agent["target_property"],
        confidence=ctx["confidence"], support=arm.get("support", 0), novelty=arm.get("novelty", 1),
        gap=max(0., float(ctx["chosen"].get("mean") or 0)-float(arm.get("mean") or 0)),
        gain=None, information_gain="uncertainty_coverage", started=now,
        deadline=now+timing.acknowledgement+window, action_at=None,
        observation_start=now if reference else None, observation_end=now+window if reference else None,
        ack=now if reference else None, window=window,
    )
    _decorate_trial(trial, ctx, experiments, session, information=True,
                    reference_propensity=.25, probe_propensity=.75)
    if reference:
        experiments._start(agent["id"], trial)
        return None
    return experiments._prepare(agent["id"], data, trial, now)


def _record_features(row):
    context = _json(row.get("context_json"), {})
    features = context.get("policy_features") or {}
    out = {}
    for key, value in features.items():
        try:
            idx, number = int(key), float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out[idx] = number
    return out


def _train_child_from_trials(manager, journal, candidate_row):
    generation = lineage_row(manager.store, agent_id=candidate_row.get("candidate_id"))
    session = _session_for_child(manager.store, (generation or {}).get("generation_id"))
    if not generation or not session or session.get("mode") != FREE_MODE:
        return False
    edge, parent_generation, child_generation = _edge(manager, generation["generation_id"])
    if not edge or not parent_generation or not child_generation:
        raise RuntimeError("Free Explore generation edge disappeared")
    parent = manager.store.get_agent_config(str(parent_generation["agent_id"]))
    child = manager.store.get_agent_config(str(child_generation["agent_id"]))
    if not parent or not child:
        raise RuntimeError("Free Explore parent or child disappeared")

    records = journal.records_for_session(session["session_id"])
    already = [r for r in records if r.get("learning_applied_generation_id") == child_generation["generation_id"]]
    pending = [r for r in records if r.get("reward") is not None and not r.get("learning_applied_generation_id")]
    if not pending:
        if already:
            return True  # restart/idempotent worker retry: exact same knowledge already applied.
        return False

    # A child is always rebuilt from the exact parent snapshot before applying trial facts.
    child = _copy_parent_snapshot(manager, parent["id"], child["id"])
    manager.engine.models.pop(child["id"], None)
    policy = manager.engine.policy(child)
    applied = []
    positive = negative = 0
    for record in pending:
        hypothesis = _json(record.get("hypothesis_json"), {})
        if str(hypothesis.get("id") or "") not in SAFE_HYPOTHESES:
            continue
        features = _record_features(record)
        assigned = _json(record.get("assigned_action_json"), {})
        context = _json(record.get("context_json"), {})
        if not features or assigned.get("index") is None:
            continue
        horizon = _finite(context.get("horizon"))
        if horizon is None or int(horizon) not in {int(x) for x in policy.horizons}:
            continue
        reward = float(record["reward"])
        action_idx = int(assigned["index"])
        if action_idx < 0 or action_idx >= len(policy.actions):
            continue
        finished = _finite(_json(record.get("episode_result_json"), {}).get("finished_at"))
        policy.update(int(horizon), action_idx, features, reward, sample_ts=finished)
        applied.append(str(record["trial_id"]))
        positive += int(reward > 0)
        negative += int(reward < 0)
    if not applied:
        return False

    model = policy.serialize()
    revision = str(getattr(policy, "model_revision", "") or "")
    now = time.time()
    # Model bytes and application markers commit together. A restart cannot apply a
    # labelled TrialRecord twice or persist the child without its dedup marker.
    with manager.store.lock, manager.store.conn() as c:
        c.execute(
            """INSERT INTO rl_models(agent_id,model_json,updated_at) VALUES(?,?,?)
               ON CONFLICT(agent_id) DO UPDATE SET model_json=excluded.model_json,updated_at=excluded.updated_at""",
            (str(child["id"]), _dumps(model), iso_now()),
        )
        for trial_id in applied:
            c.execute(
                """UPDATE experiment_trial_records SET learning_applied_generation_id=?,
                   learning_applied_model_revision=?,learning_applied_ts=?,updated_ts=?
                   WHERE trial_id=? AND learning_applied_generation_id IS NULL""",
                (str(child_generation["generation_id"]), revision, now, now, trial_id),
            )
    manager.engine.models[child["id"]] = policy

    report = {
        "mode": "trial_record_finetune", "trial_record_version": TRIAL_RECORD_VERSION,
        "records_applied": len(applied), "positive_records": positive, "negative_records": negative,
        "unlabelled_records": sum(1 for r in records if r.get("reward") is None),
        "ordinary_history_replayed": False, "base_snapshot": str(parent.get("id")),
        "candidate_model_revision": revision,
    }
    fresh = manager._candidate_row(parent["id"]) or candidate_row
    gate = _offline_gate(parent, _benchmark_stats(parent), _benchmark_stats(child), report)
    state = _persist_gate(manager, fresh, gate)
    _refresh_generation_metadata(manager.store, child["id"], lifecycle_state=state,
                                 comparison_json=fresh.get("comparison_json") or "{}")
    _merge_result(
        manager.store, session["session_id"], status="complete" if gate.get("passed") else "blocked",
        patch={"message": "TrialRecords applied to the child Candidate" if gate.get("passed")
                       else "TrialRecords were applied but the child failed the offline regression gate",
               "trial_training": report, "offline_gate": gate},
    )
    manager.store.event(
        parent["id"], "info", "explore_trial_knowledge_applied",
        "Free Explore labelled trials were applied exactly once to the direct child Candidate",
        {"session_id": session["session_id"], "child_generation_id": child_generation["generation_id"],
         "trial_ids": applied, "positive": positive, "negative": negative,
         "ordinary_history_replayed": False},
    )
    return True


def install(manager):
    """Compose Stage 11 after agent_explore and before workers start."""
    if getattr(manager, "_trial_knowledge_installed", False):
        return manager
    experiments = getattr(manager.engine, "experiments", None)
    if experiments is None or not getattr(manager, "_agent_explore_installed", False):
        manager.trial_knowledge_contract = "unavailable_agent_explore_and_experiments_required"
        return manager

    journal = TrialJournal(manager.store)
    manager.trial_journal = journal
    proposal_context = {}

    previous_propose = experiments.propose
    previous_start = experiments._start
    previous_dispatched = experiments.dispatched
    previous_observe = experiments.observe
    # At this point this is agent_explore's existing outcome wrapper. Stage 11 wraps it
    # once so the Explore session lifecycle remains intact while Live residual mutation is
    # explicitly undone for Free sessions.
    previous_finish = experiments._finish
    previous_workflow_explore = manager.workflow_explore
    previous_start_build = manager._start_build

    def propose(agent, policy, states, registry, features, labels, chosen, confidence, arms, horizon, rt):
        aid = str(agent["id"])
        reg = registry() if callable(registry) else registry
        ctx = {"agent": agent, "policy": policy, "states": states, "registry": reg,
               "features": features, "labels": labels, "chosen": chosen,
               "confidence": confidence, "arms": arms, "horizon": horizon, "rt": rt}
        proposal_context[aid] = ctx
        try:
            result = previous_propose(agent, policy, states, reg, features, labels,
                                      chosen, confidence, arms, horizon, rt)
            session = _session_for_live(manager.store, aid)
            if result is not None and session:
                prepared = experiments.prepared.get(aid)
                if prepared:
                    _decorate_trial(prepared, ctx, experiments, session)
                    result = copy.deepcopy(prepared)
            if (result is None and session and
                bool(_json(session.get("requested_config_json"), {}).get("information_exploration", True)) and
                str(experiments.messages.get(aid, "")).startswith("No nearby action reachable")):
                result = _information_trial(experiments, ctx, session)
                if result is not None:
                    experiments.messages[aid] = "Safe information probe; no positive base gradient required"
            return result
        finally:
            proposal_context.pop(aid, None)

    def start(aid, trial):
        aid = str(aid)
        ctx = proposal_context.get(aid)
        session = _session_for_live(manager.store, aid)
        if ctx and session and not trial.get("trial_record"):
            _decorate_trial(trial, ctx, experiments, session)
        trial["owner_agent_id"] = aid
        result = previous_start(aid, trial)
        active = copy.deepcopy(experiments._get(aid).get("active") or trial)
        journal.start(aid, active, session)
        return result

    def dispatched(agent, intent, states=None):
        aid = str(agent["id"])
        trial = copy.deepcopy(experiments._get(aid).get("active") or {})
        result = previous_dispatched(agent, intent, states)
        active = experiments._get(aid).get("active") or trial
        if active.get("trial_id"):
            journal.dispatch(active["trial_id"], active.get("action_at") or experiments.clock(),
                             intent_id=getattr(intent, "intent_id", None),
                             desired_value=getattr(intent, "desired_value", None))
        return result

    def observe(agent, states, current, manual=False):
        aid = str(agent["id"])
        before = copy.deepcopy(experiments._get(aid).get("active") or {})
        old_ack = before.get("ack")
        result = previous_observe(agent, states, current, manual=manual)
        after = experiments._get(aid).get("active") or {}
        if after.get("trial_id") and old_ack is None and after.get("ack") is not None:
            journal.ack(after["trial_id"], after["ack"], current)
        return result

    def finish(aid, reward, reason):
        aid = str(aid)
        data = experiments._get(aid)
        trial = copy.deepcopy(data.get("active") or {})
        if not trial:
            return previous_finish(aid, reward, reason)
        session = _session_for_live(manager.store, aid)
        learner_snapshot = copy.deepcopy(data.get("learners") or {}) if session else None
        if journal.get(trial.get("trial_id")) is None:
            trial["owner_agent_id"] = aid
            journal.start(aid, trial, session)
        result = previous_finish(aid, reward, reason)
        if session is not None:
            # F19: Free Explore cannot train the Live residual owner. Restore exactly the
            # pre-outcome learner while preserving config/session changes made by the
            # existing Explore wrapper (including restoration of the previous config).
            current = experiments._get(aid)
            current["learners"] = learner_snapshot
            experiments._save(aid)
        journal.finish(trial, reward, reason, experiments.clock())
        return result

    def workflow_explore(ref, payload):
        body = copy.deepcopy(payload or {})
        if str(body.get("mode") or "") != FREE_MODE:
            return previous_workflow_explore(ref, body)
        generation, agent = manager.workflow_resolve_generation(ref) if hasattr(manager, "workflow_resolve_generation") else (None, None)
        if agent is None:
            from agent_workflow_actions import _resolve_generation
            generation, agent = _resolve_generation(manager, ref)
        hypothesis = _safe_catalog(agent)
        if not hypothesis:
            raise ValueError("Free Explore MVP supports light earlier-ON or a small brightness adjustment only")
        info = bool(body.pop("information_exploration", True))
        if isinstance(body.get("config"), dict):
            body["config"].pop("information_exploration", None)
        result = previous_workflow_explore(ref, body)
        session_id = ((result.get("session") or {}).get("session_id"))
        if session_id:
            session = explore_row(manager.store, session_id)
            requested = _json(session.get("requested_config_json"), {})
            requested["information_exploration"] = info
            with manager.store.lock, manager.store.conn() as c:
                c.execute("UPDATE agent_explore_sessions SET requested_config_json=?,updated_ts=? WHERE session_id=?",
                          (_dumps(requested), time.time(), str(session_id)))
            _merge_result(manager.store, session_id, patch={
                "trial_record_version": TRIAL_RECORD_VERSION,
                "knowledge_integration": "explicit_child_training_from_trial_records",
                "hypothesis_catalog": [hypothesis],
                "information_exploration": info,
                "live_residual_learning": False,
            })
            result["session"] = manager.workflow_explore_status(ref).get("session")
        return result

    def start_build(row):
        if str(row.get("reason") or "") == FREE_TRAIN_REASON:
            try:
                handled = _train_child_from_trials(manager, journal, row)
                if handled:
                    return True
                generation = lineage_row(manager.store, agent_id=row.get("candidate_id"))
                session = _session_for_child(manager.store, (generation or {}).get("generation_id"))
                if session:
                    _merge_result(manager.store, session["session_id"], status="no_evidence",
                                  patch={"message": "No labelled TrialRecord was eligible for child learning"})
                return True
            except Exception as exc:
                if hasattr(manager, "_fail"):
                    manager._fail(row, f"TrialRecord child training failed: {type(exc).__name__}: {exc}")
                return True
        return previous_start_build(row)

    experiments.propose = propose
    experiments._start = start
    experiments.dispatched = dispatched
    experiments.observe = observe
    experiments._finish = finish
    manager.workflow_explore = workflow_explore
    manager._start_build = start_build
    manager._trial_knowledge_installed = True
    manager.trial_knowledge_contract = "trial_record_v1_explicit_child_training_exactly_once"
    manager.trial_hypothesis_catalog = sorted(SAFE_HYPOTHESES)
    manager.trial_off_policy_contract = "refuse_without_positive_logged_propensity_coverage"
    manager.trial_rollback_contract = "exact_parent_snapshot_plus_trial_updates_child_only"
    return manager
