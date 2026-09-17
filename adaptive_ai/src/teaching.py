"""Retractable, contextual user teaching for the agent policy.

Historical inference uses only archived, as-of context. It is explicitly a replay
of the current policy, never a claim about an unrecorded past decision.

Stage 06 keeps Teaching as an immediate *decision override* while making its learning
fact durable in ManualFeedbackJournal. Context signatures now include the seven shared
home-trajectory features and explicit policy/schema versions. Missing historical home
trajectory is represented as unknown and never fabricated from current live state.
"""
from collections import deque
import hashlib
import json
import math
import threading
import time

from context import HistoricalTemporalTracker, target_value, parse_fast_series_lags
from home_state import FEATURE_NAMES
from manual_feedback import _manual_value
from policy import MultiHorizonPolicy
from settings import OPTIONS


def fingerprint(agent):
    fields = ("target_entity", "target_property", "min_value", "max_value", "input_entities")
    return hashlib.sha256(json.dumps({k: agent.get(k) for k in fields}, sort_keys=True).encode()).hexdigest()


def signature(policy, states, temporal, timestamp):
    """Semantic Teaching context including the previously omitted home forecast tail.

    Only features already admitted by the policy are stored. Pairwise interaction terms
    are deliberately omitted because they are deterministic derivatives of base inputs and
    would make context matching over-specific. The seven home features are always present;
    when replay cannot reconstruct them, ``meta:home_known`` is 0 and the values are kept
    as placeholders rather than being interpreted as an empty home.
    """
    entities = list(policy.schema.entities)
    if not entities or any(
        not states.get(e) or states[e].get("state") in ("unknown", "unavailable")
        for e in entities
    ):
        return None
    features, labels, meta = policy.features(states, temporal, at_ts=timestamp)
    result = {}
    for index, parts in labels.items():
        if int(index) <= 0:
            continue
        name = " / ".join(parts)
        if name.startswith("interaction:"):
            continue
        result[name] = float(features.get(index, 0.0))

    forecast = dict((meta or {}).get("home_forecast") or {})
    for offset, name in enumerate(FEATURE_NAMES):
        key = "home:" + name
        result.setdefault(key, float(features.get(policy.dims - 7 + offset, 0.0)))
    result["meta:home_known"] = 1.0 if forecast.get("known") else 0.0
    result["meta:feature_schema_version"] = float(getattr(policy.schema, "VERSION", 0) or 0)
    result["meta:policy_version"] = float(getattr(policy, "VERSION", 0) or 0)
    result["meta:signature_contract"] = 2.0

    if policy.agent.get("target_property") == "option_index":
        options = (states.get(policy.agent["target_entity"], {}).get("attributes") or {}).get("options")
        if not options:
            return None
        result["target_options:" + json.dumps(options)] = 1.0
    return result


def _critical_feature(name):
    text = str(name).lower().replace("_", " ")
    return any(token in text for token in (
        "occupancy", "presence", "motion", "obecno", "door", "window", "contact",
        "moving target", "still target", "move target", "radar", "activity",
    ))


def _metadata_key(name):
    return str(name).startswith("meta:")


def distance(left, right):
    """Semantic distance across compatible feature-schema revisions."""
    left = dict(left or {})
    right = dict(right or {})
    if not left or not right:
        return None

    old_options = {k: v for k, v in right.items() if str(k).startswith("target_options:")}
    new_options = {k: v for k, v in left.items() if str(k).startswith("target_options:")}
    if old_options or new_options:
        if old_options != new_options:
            return None

    for key in ("meta:feature_schema_version", "meta:policy_version"):
        if key in left and key in right:
            try:
                if int(float(left[key])) != int(float(right[key])):
                    return None
            except (TypeError, ValueError):
                return None

    left_home_known = bool(float(left.get("meta:home_known", 0.0) or 0.0) >= .5)
    right_home_known = bool(float(right.get("meta:home_known", 0.0) or 0.0) >= .5)
    compare_home = left_home_known and right_home_known

    old_keys = {
        k for k in right
        if not str(k).startswith("target_options:") and not _metadata_key(k)
        and (compare_home or not str(k).startswith("home:"))
    }
    new_keys = {
        k for k in left
        if not str(k).startswith("target_options:") and not _metadata_key(k)
        and (compare_home or not str(k).startswith("home:"))
    }
    shared = old_keys & new_keys
    if not shared:
        return None
    if len(shared) / max(1, len(old_keys)) < 0.50:
        return None

    old_critical = {k for k in old_keys if _critical_feature(k)}
    if old_critical and not (old_critical & shared):
        return None

    weighted_sq = 0.0
    total_weight = 0.0
    max_generic = 0.0
    for key in shared:
        try:
            old_value = float(right[key])
            new_value = float(left[key])
        except (TypeError, ValueError):
            return None
        diff = abs(new_value - old_value)
        home = str(key).startswith("home:")
        critical = _critical_feature(key) or home
        if critical:
            if old_value * new_value < -0.05 and diff > 0.60:
                return None
            if diff > (1.0 if home else 0.90):
                return None
            weight = 4.0 if home else 3.0
        else:
            if diff > 1.20:
                return None
            max_generic = max(max_generic, diff)
            weight = 1.0
        weighted_sq += weight * diff * diff
        total_weight += weight

    if total_weight <= 0:
        return 0.0
    rms = math.sqrt(weighted_sq / total_weight)
    max_rms = float(OPTIONS.get("teaching_context_rms", 0.20))
    max_analogue = float(OPTIONS.get("teaching_context_max_analogue_delta", 0.75))
    return rms if rms <= max_rms and max_generic <= max_analogue else None


class Teaching:
    MAX_LABELS = 256
    MAX_HISTORY_ROWS = 40000

    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.cache = {}
        self.revisions = {}
        self.buffer = deque(maxlen=8192)
        self.last_record = {}
        self.dropped_records = 0
        self.last_prune = 0
        self.history_slots = threading.BoundedSemaphore(1)
        self.feedback_journal = None
        self.candidate_feedback_listener = None
        with store.lock, store.conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS teaching_labels (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL,
                  created_ts REAL NOT NULL, sample_ts REAL NOT NULL, desired REAL NOT NULL,
                  previous_desired REAL, fingerprint TEXT NOT NULL, signature_json TEXT NOT NULL,
                  source TEXT NOT NULL, undone_ts REAL);
                CREATE INDEX IF NOT EXISTS idx_teaching_agent ON teaching_labels(agent_id,id DESC);
                CREATE TABLE IF NOT EXISTS decision_history (
                  agent_id TEXT NOT NULL, ts REAL NOT NULL, current REAL, desired REAL,
                  PRIMARY KEY(agent_id,ts));
                CREATE INDEX IF NOT EXISTS idx_decision_ts ON decision_history(ts);
            """)

    def labels(self, aid):
        with self.lock:
            if aid not in self.cache:
                with self.store.conn() as c:
                    rows = [dict(r) for r in c.execute(
                        "SELECT * FROM teaching_labels WHERE agent_id=? AND undone_ts IS NULL ORDER BY id DESC LIMIT ?",
                        (aid, self.MAX_LABELS),
                    ).fetchall()]
                    revision = c.execute(
                        "SELECT COALESCE(MAX(id),0)+COALESCE(SUM(undone_ts IS NOT NULL),0) "
                        "FROM teaching_labels WHERE agent_id=?", (aid,)
                    ).fetchone()[0]
                if self.feedback_journal is not None:
                    rows = self.feedback_journal.filter_linked_rows("teaching_label", rows)
                self.cache[aid] = [r | {"signature": json.loads(r["signature_json"])} for r in rows]
                self.revisions[aid] = int(revision)
            return list(self.cache[aid])

    def revision(self, aid):
        with self.lock:
            self.labels(aid)
            return self.revisions[aid]

    def match(self, agent, policy, states, temporal, timestamp):
        stamp = fingerprint(agent)
        candidates = [r for r in self.labels(agent["id"]) if r["fingerprint"] == stamp]
        if not candidates:
            return None
        sig = signature(policy, states, temporal, timestamp)
        if not sig:
            return None
        matches = []
        for row in candidates:
            rms = distance(sig, row["signature"])
            if rms is not None:
                matches.append((rms, -row["id"], row))
        if not matches:
            return None
        best = min(x[0] for x in matches)
        nearest = [x for x in matches if x[0] <= best + .015]
        values = {round(float(x[2]["desired"]), 9) for x in nearest if x[0] <= .015}
        if len(values) > 1:
            return None
        return min(nearest, key=lambda x: x[1])[2]

    def physical_correction(self, engine, agent, states, desired, timestamp):
        rows = self.labels(agent["id"])
        if not rows:
            return
        sig = signature(engine.policy(agent), states, engine.temporal_history, timestamp)
        if not sig:
            return
        stamp = fingerprint(agent)
        ids = [
            r["id"] for r in rows
            if r["fingerprint"] == stamp
            and distance(sig, r["signature"]) is not None
            and abs(r["desired"] - desired) > float(agent.get("deadband") or .01)
        ]
        if not ids:
            return
        with self.lock, self.store.lock, self.store.conn() as c:
            c.executemany(
                "UPDATE teaching_labels SET undone_ts=? WHERE id=?", [(timestamp, i) for i in ids]
            )
            self.cache.pop(agent["id"], None)
        self.store.event(
            agent["id"], "info", "teaching_superseded_by_user",
            "Physical user correction retired conflicting teaching labels", {"ids": ids},
        )

    def valid(self, agent, intent, engine):
        with engine.lock:
            states = dict(engine.state_map)
        label = self.match(agent, engine.models[agent["id"]], states, engine.temporal_history, time.time())
        return bool(label and label["id"] == intent.teaching_id and label["desired"] == intent.desired_value)

    def teach(self, engine, agent, desired=None, sample_ts=None, *, source=None,
              error_kind="state", scope="similar_context", decision_id=None,
              episode_id=None, generation_id=None, feedback_id=None):
        with engine.executor.target_lock(agent["target_entity"]):
            agent = self.store.get_agent_config(agent["id"])
            if not agent:
                raise ValueError("Agent no longer exists")
            if sample_ts is None:
                timestamp = time.time()
                with engine.lock:
                    states, temporal = dict(engine.state_map), engine.temporal_history
                policy = engine.policy(agent)
                previous = engine.runtime.get(agent["id"], {}).get("last_prediction")
            else:
                timestamp = self.timestamp(sample_ts)
                states, temporal, policy = self.point_context(engine, agent, timestamp)
                previous = self.predict(agent, policy, states, temporal, timestamp)[0]
            current = target_value(states.get(agent["target_entity"]), agent["target_property"])
            if current is None:
                raise ValueError("Brak stanu urządzenia w wybranej chwili")
            if desired is None:
                if agent["target_property"] != "power" or previous is None:
                    raise ValueError("Podaj poprawną wartość Desired")
                desired = 0 if previous >= .5 else 1
            desired = _manual_value(agent, states[agent["target_entity"]], desired)
            sig = signature(policy, states, temporal, timestamp)
            if not sig:
                raise ValueError("Niepełny kontekst czujników w tej chwili; wybierz inny punkt lub uzupełnij historię")

            journal_row = None
            journal = self.feedback_journal or getattr(engine, "manual_feedback_journal", None)
            effective_source = source or ("history" if sample_ts is not None else "wrong_decision")
            if journal is not None:
                journal_row = journal.record(
                    agent_id=agent["id"], selected_ts=timestamp, source=effective_source,
                    rejected_action=previous, correct_action=desired, error_kind=error_kind,
                    scope=scope, decision_id=decision_id, episode_id=episode_id,
                    generation_id=generation_id, fingerprint=fingerprint(agent),
                    context_signature=sig,
                    feature_schema_version=getattr(policy.schema, "VERSION", None),
                    policy_version=getattr(policy, "VERSION", None),
                    deadband=float(agent.get("deadband") or .01), feedback_id=feedback_id,
                )
                if journal_row.get("application_status") == "conflict":
                    return {
                        "ok": True, "label_id": None, "desired_value": desired,
                        "sample_ts": timestamp, "current_value": current,
                        "feedback_id": journal_row["feedback_id"], "feedback": journal_row,
                        "conflict": True, "ui_message": journal.ui_summary(journal_row),
                    }

            with self.lock, self.store.lock, self.store.conn() as c:
                count = c.execute(
                    "SELECT COUNT(*) FROM teaching_labels WHERE agent_id=? AND undone_ts IS NULL",
                    (agent["id"],),
                ).fetchone()[0]
                if count >= self.MAX_LABELS:
                    raise ValueError("Limit 256 aktywnych korekt agenta; cofnij zbędne korekty")
                row = c.execute(
                    """INSERT INTO teaching_labels
                       (agent_id,created_ts,sample_ts,desired,previous_desired,fingerprint,signature_json,source)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (agent["id"], time.time(), timestamp, desired, previous,
                     fingerprint(agent), json.dumps(sig), effective_source),
                )
                label_id = int(row.lastrowid)
                self.cache.pop(agent["id"], None)

            if journal is not None and journal_row is not None:
                journal.link(journal_row["feedback_id"], "learning", "teaching_label", label_id,
                             metadata={"source": effective_source})
                journal_row = journal.set_status(
                    journal_row["feedback_id"], "applied",
                    immediate_effect={"runtime_override": True, "physical_change": False},
                    learning_effect={"label_recorded": True, "teaching_label_id": label_id,
                                     "rebuild_required": True},
                )

            self.refresh(engine, agent)
            result = {"ok": True, "label_id": label_id, "desired_value": desired,
                      "sample_ts": timestamp, "current_value": current}
            if journal_row is not None:
                result.update(feedback_id=journal_row["feedback_id"], feedback=journal_row,
                              ui_message=journal.ui_summary(journal_row))
            listener = self.candidate_feedback_listener
            if callable(listener):
                listener("teaching_added", agent, result)
            return result

    def undo(self, engine, agent, feedback_id=None):
        with engine.executor.target_lock(agent["target_entity"]):
            journal = self.feedback_journal or getattr(engine, "manual_feedback_journal", None)
            linked_feedback = feedback_id
            if linked_feedback is None and journal is not None:
                with self.store.conn() as c:
                    row = c.execute(
                        """SELECT e.feedback_id,l.id FROM teaching_labels l
                           LEFT JOIN manual_feedback_effects e
                             ON e.ref_type='teaching_label' AND e.ref_id=CAST(l.id AS TEXT)
                           WHERE l.agent_id=? AND l.undone_ts IS NULL
                           ORDER BY l.id DESC LIMIT 1""",
                        (agent["id"],),
                    ).fetchone()
                if row and row["feedback_id"]:
                    linked_feedback = str(row["feedback_id"])
            if journal is not None and linked_feedback:
                manager = getattr(engine, "agent_candidates", None)
                result = journal.undo(linked_feedback, engine=engine, candidate_manager=manager)
                self.cache.pop(agent["id"], None)
                self.refresh(engine, agent)
                return {"ok": True, "feedback_id": linked_feedback, "feedback": result,
                        "undone_id": None, "ui_message": journal.ui_summary(result)}

            with self.lock, self.store.lock, self.store.conn() as c:
                row = c.execute(
                    "SELECT id FROM teaching_labels WHERE agent_id=? AND undone_ts IS NULL ORDER BY id DESC LIMIT 1",
                    (agent["id"],),
                ).fetchone()
                if not row:
                    raise ValueError("Brak nauki z przycisków do cofnięcia")
                c.execute("UPDATE teaching_labels SET undone_ts=? WHERE id=?", (time.time(), row[0]))
                self.cache.pop(agent["id"], None)
            self.refresh(engine, agent)
            result = {"ok": True, "undone_id": row[0]}
            listener = self.candidate_feedback_listener
            if callable(listener):
                listener("teaching_undone", agent, result)
            return result

    def refresh(self, engine, agent):
        with engine.lock:
            states = dict(engine.state_map)
        policy = engine.policy(agent)
        value, taught = self.predict(agent, policy, states, engine.temporal_history, time.time())
        rt = engine.runtime.setdefault(agent["id"], {})
        rt.update(last_prediction=value, teaching_id=taught, last_inference_ts=0)
        engine.wake_event.set()

    def predict(self, agent, policy, states, temporal, timestamp):
        label = self.match(agent, policy, states, temporal, timestamp)
        if label:
            return label["desired"], label["id"]
        features, _, _ = policy.features(states, temporal, at_ts=timestamp)
        return policy.predict(features)[0]["value"], None

    @staticmethod
    def timestamp(value):
        ts = float(value)
        if not math.isfinite(ts) or ts <= 0 or ts > time.time() + 2:
            raise ValueError("Nieprawidłowy czas próbki")
        return ts

    def clone(self, engine, agent):
        policy = engine.policy(agent)
        with engine.lock:
            states, registry = dict(engine.state_map), dict(engine.entity_registry)
        return MultiHorizonPolicy(agent, states, registry, set(), model=policy.serialize(), context_engine=None)

    def point_context(self, engine, agent, timestamp, policy=None):
        policy = policy or self.clone(engine, agent)
        rows = []
        with self.store.conn() as c:
            for eid in set(policy.schema.entities) | {agent["target_entity"]}:
                for lag in self.lags():
                    r = c.execute(
                        "SELECT * FROM entity_history WHERE entity_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                        (eid, timestamp - lag),
                    ).fetchone()
                    if r:
                        rows.append(dict(r))
        unique = {r["id"]: r for r in rows}
        tracker = HistoricalTemporalTracker(sorted(unique.values(), key=lambda r: (r["ts"], r["id"])))
        tracker.advance(timestamp)
        return tracker.state_map, tracker.history, policy

    @staticmethod
    def lags():
        return {0, *parse_fast_series_lags(), float(OPTIONS.get("temporal_short_seconds", 60)),
                float(OPTIONS.get("temporal_long_seconds", 300))}

    def point(self, engine, agent, timestamp):
        timestamp = self.timestamp(timestamp)
        states, temporal, policy = self.point_context(engine, agent, timestamp)
        current = target_value(states.get(agent["target_entity"]), agent["target_property"])
        value, label = self.predict(agent, policy, states, temporal, timestamp)
        return {"ts": timestamp, "current": current, "desired": value if current is not None else None,
                "teaching_id": label, "context_complete": bool(signature(policy, states, temporal, timestamp))}

    def history(self, engine, agent, start, end):
        start, end = self.timestamp(start), self.timestamp(end)
        if end <= start or end - start > 31 * 86400:
            raise ValueError("Wybierz zakres od 1 sekundy do 31 dni")
        if not self.history_slots.acquire(blocking=False):
            raise ValueError("Trwa przygotowanie wykresu; spróbuj ponownie za chwilę")
        try:
            policy = self.clone(engine, agent)
            ids = set(policy.schema.entities) | {agent["target_entity"]}
            rows, dense = [], False
            lookback = max(self.lags())
            with self.store.conn() as c:
                for eid in ids:
                    seed = c.execute(
                        "SELECT * FROM entity_history WHERE entity_id=? AND ts<? ORDER BY ts DESC LIMIT 1",
                        (eid, start - lookback),
                    ).fetchone()
                    if seed:
                        rows.append(dict(seed))
            for row in self.store.archive_iter(start - lookback, end, ids):
                rows.append(row)
                if len(rows) > self.MAX_HISTORY_ROWS:
                    dense = True
                    rows.clear()
                    break
            rows.sort(key=lambda r: (r["ts"], r["id"]))
            tracker = HistoricalTemporalTracker(rows)
            times = {start, end} | {r["ts"] for r in rows if r["ts"] >= start}
            if dense:
                with self.store.conn() as c:
                    edges = c.execute(
                        "SELECT MIN(ts),MAX(ts) FROM entity_history WHERE entity_id=? AND ts>=? AND ts<=? "
                        "GROUP BY CAST((ts-?)/? AS INTEGER)",
                        (agent["target_entity"], start, end, start, (end - start) / 350),
                    ).fetchall()
                    times.update(t for edge in edges for t in edge)
            times.update(start + (end - start) * i / 240 for i in range(241))
            times = sorted(times)
            reduced = dense or len(times) > 1000
            if len(times) > 1000:
                times = [times[round(i * (len(times) - 1) / 999)] for i in range(1000)]
            points = []
            for ts in times:
                if dense:
                    states, temporal, _ = self.point_context(engine, agent, ts, policy=policy)
                else:
                    tracker.advance(ts)
                    states, temporal = tracker.state_map, tracker.history
                current = target_value(states.get(agent["target_entity"]), agent["target_property"])
                complete = bool(signature(policy, states, temporal, ts))
                desired, _ = self.predict(agent, policy, states, temporal, ts) if current is not None and complete else (None, None)
                points.append({"ts": ts, "current": current, "desired": desired})
            with self.store.conn() as c:
                recorded = [dict(r) for r in c.execute(
                    "SELECT ts,desired FROM decision_history WHERE agent_id=? AND ts>=? AND ts<=? ORDER BY ts LIMIT 2001",
                    (agent["id"], start, end),
                )]
            return {"points": points, "recorded": recorded[:2000],
                    "recorded_truncated": len(recorded) > 2000,
                    "start": start, "end": end, "reduced": reduced,
                    "desired_source": "current_policy_replay",
                    "labels": [{k: r[k] for k in ("id", "sample_ts", "desired", "source")}
                               for r in self.labels(agent["id"])]}
        finally:
            self.history_slots.release()

    def record(self, aid, current, desired, timestamp):
        with self.lock:
            old = self.last_record.get(aid)
            if old and old[1:] == (current, desired) and timestamp - old[0] < 30:
                return
            self.last_record[aid] = (timestamp, current, desired)
            if len(self.buffer) == self.buffer.maxlen:
                self.dropped_records += 1
            self.buffer.append((aid, timestamp, current, desired))

    def flush(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            if not self.store.lock.acquire(blocking=False):
                return
            try:
                batch = list(self.buffer)
                if batch:
                    with self.store.conn() as c:
                        c.executemany("INSERT OR REPLACE INTO decision_history VALUES(?,?,?,?)", batch)
                        if time.time() - self.last_prune > 3600:
                            c.execute("DELETE FROM decision_history WHERE ts<?", (time.time() - 31 * 86400,))
                            self.last_prune = time.time()
                    self.buffer.clear()
            finally:
                self.store.lock.release()
        finally:
            self.lock.release()
