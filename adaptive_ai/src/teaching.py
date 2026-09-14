"""Retractable, contextual user labels alongside (not inside) the RL matrices.

Historical inference uses only archived, as-of context. It is explicitly a replay
of the current policy, never a claim about an unrecorded past decision.
"""
from collections import deque
import hashlib
import json
import math
import threading
import time
from context import HistoricalTemporalTracker, build_explicit_features, target_value, parse_fast_series_lags
from manual_feedback import _manual_value
from policy import MultiHorizonPolicy
from settings import OPTIONS


def fingerprint(agent):
    fields = ("target_entity", "target_property", "min_value", "max_value", "input_entities")
    return hashlib.sha256(json.dumps({k: agent.get(k) for k in fields}, sort_keys=True).encode()).hexdigest()


def signature(policy, states, temporal, timestamp):
    entities = list(policy.schema.entities)
    if not entities or any(not states.get(e) or states[e].get("state") in ("unknown", "unavailable") for e in entities):
        return None
    features, labels, _ = build_explicit_features(policy.schema, states, temporal, timestamp,
        policy.agent, excluded_entities=policy.excluded_context_entities)
    # Semantic names survive reordering. Include zero values; unknown is not OFF.
    result = {" / ".join(labels[i]): float(features.get(i, 0)) for i in labels
              if 0 < i < policy.dims - 7}
    if policy.agent.get('target_property') == 'option_index':
        options = (states.get(policy.agent['target_entity'], {}).get('attributes') or {}).get('options')
        if not options: return None
        result['target_options:' + json.dumps(options)] = 1.0
    return result


def distance(left, right):
    if left.keys() != right.keys():
        return None
    diffs = [abs(left[k]-v) for k,v in right.items()]
    rms = math.sqrt(sum(d*d for d in diffs)/max(1,len(diffs)))
    return rms if max(diffs, default=1) <= .20 and rms <= .07 else None


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
                    rows = c.execute("SELECT * FROM teaching_labels WHERE agent_id=? AND undone_ts IS NULL ORDER BY id DESC LIMIT ?", (aid, self.MAX_LABELS)).fetchall()
                    revision = c.execute('SELECT COALESCE(MAX(id),0)+COALESCE(SUM(undone_ts IS NOT NULL),0) FROM teaching_labels WHERE agent_id=?', (aid,)).fetchone()[0]
                self.cache[aid] = [dict(r) | {"signature": json.loads(r["signature_json"])} for r in rows]
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
            rms = distance(sig, row['signature'])
            # A changed motion edge cannot be diluted by dozens of constant inputs.
            if rms is not None:
                matches.append((rms, -row["id"], row))
        if not matches:
            return None
        # Newest explicit correction wins within effectively identical contexts.
        best = min(x[0] for x in matches)
        return min((x for x in matches if x[0] <= best + .005), key=lambda x: x[1])[2]

    def physical_correction(self, engine, agent, states, desired, timestamp):
        """A newer real user action retires conflicting button instructions."""
        rows = self.labels(agent['id'])
        if not rows: return
        sig = signature(engine.policy(agent), states, engine.temporal_history, timestamp)
        if not sig: return
        stamp = fingerprint(agent)
        ids = [r['id'] for r in rows if r['fingerprint'] == stamp
               and distance(sig, r['signature']) is not None
               and abs(r['desired']-desired) > float(agent.get('deadband') or .01)]
        if not ids: return
        with self.lock, self.store.lock, self.store.conn() as c:
            c.executemany('UPDATE teaching_labels SET undone_ts=? WHERE id=?', [(timestamp, i) for i in ids])
            self.cache.pop(agent['id'], None)
        self.store.event(agent['id'], 'info', 'teaching_superseded_by_user',
                         'Physical user correction retired conflicting teaching labels', {'ids': ids})

    def valid(self, agent, intent, engine):
        with engine.lock:
            states = dict(engine.state_map)
        label = self.match(agent, engine.models[agent['id']], states, engine.temporal_history, time.time())
        return bool(label and label['id'] == intent.teaching_id and label['desired'] == intent.desired_value)

    def teach(self, engine, agent, desired=None, sample_ts=None):
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
            with self.lock, self.store.lock, self.store.conn() as c:
                count = c.execute("SELECT COUNT(*) FROM teaching_labels WHERE agent_id=? AND undone_ts IS NULL", (agent["id"],)).fetchone()[0]
                if count >= self.MAX_LABELS:
                    raise ValueError("Limit 256 aktywnych korekt agenta; cofnij zbędne korekty")
                row = c.execute("INSERT INTO teaching_labels(agent_id,created_ts,sample_ts,desired,previous_desired,fingerprint,signature_json,source) VALUES(?,?,?,?,?,?,?,?)",
                    (agent["id"], time.time(), timestamp, desired, previous, fingerprint(agent), json.dumps(sig), "history" if sample_ts is not None else "wrong_decision"))
                label_id = row.lastrowid
                self.cache.pop(agent["id"], None)
            self.refresh(engine, agent)
            return {"ok": True, "label_id": label_id, "desired_value": desired, "sample_ts": timestamp, "current_value": current}

    def undo(self, engine, agent):
        with engine.executor.target_lock(agent["target_entity"]):
            with self.lock, self.store.lock, self.store.conn() as c:
                row = c.execute("SELECT id FROM teaching_labels WHERE agent_id=? AND undone_ts IS NULL ORDER BY id DESC LIMIT 1", (agent["id"],)).fetchone()
                if not row:
                    raise ValueError("Brak nauki z przycisków do cofnięcia")
                c.execute("UPDATE teaching_labels SET undone_ts=? WHERE id=?", (time.time(), row[0]))
                self.cache.pop(agent["id"], None)
            self.refresh(engine, agent)
            return {"ok": True, "undone_id": row[0]}

    def refresh(self, engine, agent):
        # No HA call here: normal Control pipeline consumes the corrected decision.
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
        # Historical policy has no live home-context provider: no future leakage.
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
                    r = c.execute("SELECT * FROM entity_history WHERE entity_id=? AND ts<=? ORDER BY ts DESC LIMIT 1", (eid, timestamp-lag)).fetchone()
                    if r: rows.append(dict(r))
        unique = {r["id"]: r for r in rows}
        tracker = HistoricalTemporalTracker(sorted(unique.values(), key=lambda r: (r["ts"], r["id"])))
        tracker.advance(timestamp)
        return tracker.state_map, tracker.history, policy

    @staticmethod
    def lags():
        return {0, *parse_fast_series_lags(), float(OPTIONS.get('temporal_short_seconds', 60)),
                float(OPTIONS.get('temporal_long_seconds', 300))}

    def point(self, engine, agent, timestamp):
        timestamp = self.timestamp(timestamp)
        states, temporal, policy = self.point_context(engine, agent, timestamp)
        current = target_value(states.get(agent["target_entity"]), agent["target_property"])
        value, label = self.predict(agent, policy, states, temporal, timestamp)
        return {"ts": timestamp, "current": current, "desired": value if current is not None else None,
                "teaching_id": label, "context_complete": bool(signature(policy, states, temporal, timestamp))}

    def history(self, engine, agent, start, end):
        start, end = self.timestamp(start), self.timestamp(end)
        if end <= start or end-start > 31*86400:
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
                    seed = c.execute("SELECT * FROM entity_history WHERE entity_id=? AND ts<? ORDER BY ts DESC LIMIT 1", (eid, start-lookback)).fetchone()
                    if seed: rows.append(dict(seed))
            for row in self.store.archive_iter(start-lookback, end, ids):
                rows.append(row)
                if len(rows) > self.MAX_HISTORY_ROWS:
                    dense = True
                    rows.clear()
                    break
            rows.sort(key=lambda r: (r["ts"], r["id"]))
            tracker = HistoricalTemporalTracker(rows)
            times = {start, end} | {r["ts"] for r in rows if r["ts"] >= start}
            if dense:
                # High-rate radars must not force loading millions of rows to draw
                # one screen. Retain target edges and use indexed as-of queries.
                with self.store.conn() as c:
                    edges = c.execute('SELECT MIN(ts),MAX(ts) FROM entity_history WHERE entity_id=? AND ts>=? AND ts<=? GROUP BY CAST((ts-?)/? AS INTEGER)',
                                      (agent['target_entity'], start, end, start, (end-start)/350)).fetchall()
                    times.update(t for edge in edges for t in edge)
            times.update(start+(end-start)*i/240 for i in range(241))
            times = sorted(times)
            reduced = dense or len(times) > 1000
            if len(times) > 1000: times = [times[round(i*(len(times)-1)/999)] for i in range(1000)]
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
                recorded = [dict(r) for r in c.execute("SELECT ts,desired FROM decision_history WHERE agent_id=? AND ts>=? AND ts<=? ORDER BY ts LIMIT 2001", (agent["id"], start, end))]
            return {"points": points, "recorded": recorded[:2000], "recorded_truncated": len(recorded)>2000,
                    "start": start, "end": end, "reduced": reduced, "desired_source": "current_policy_replay",
                    "labels": [{k:r[k] for k in ("id", "sample_ts", "desired", "source")} for r in self.labels(agent["id"])]}
        finally:
            self.history_slots.release()

    def record(self, aid, current, desired, timestamp):
        with self.lock:
            old = self.last_record.get(aid)
            if old and old[1:] == (current, desired) and timestamp-old[0] < 30:
                return
            self.last_record[aid] = (timestamp, current, desired)
            if len(self.buffer) == self.buffer.maxlen: self.dropped_records += 1
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
                        if time.time()-self.last_prune > 3600:
                            c.execute("DELETE FROM decision_history WHERE ts<?", (time.time()-31*86400,))
                            self.last_prune = time.time()
                    self.buffer.clear()
            finally:
                self.store.lock.release()
        finally:
            self.lock.release()
