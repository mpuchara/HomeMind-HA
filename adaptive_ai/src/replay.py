"""Bounded-memory historical views. Large timelines and held-out vectors stay on disk."""
import json
import sqlite3
from collections import defaultdict, deque
from adaptive_presence import AdaptivePresenceModel
from context import archived_state, TemporalHistory, state_scalar
from home_state import RoomBeliefModel
from training_budget import TRAINING_BUDGET


class BoundedUsage:
    """Discovery needs a count and last transition, not the entire target history."""
    def __init__(self):
        self.count, self.last = 0, None

    def append(self, item):
        self.count += 1
        self.last = item

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if index != -1 or self.last is None:
            raise IndexError(index)
        return self.last


class DeferredUpdates:
    """Compatibility queue with prequential test-then-learn semantics."""
    def __init__(self, policies):
        self.policies = policies
        self.count = 0
        self.closed = False

    def append(self, update):
        if self.closed:
            raise RuntimeError("DeferredUpdates is closed")
        policy, horizon, action, features, reward, ts = update
        policy.update(int(horizon), int(action), features, float(reward), float(ts))
        self.count += 1

    def __len__(self):
        return self.count

    def __iter__(self):
        return iter(())

    def close(self):
        self.closed = True

    def __del__(self):
        self.close()


class HistoricalHomeView:
    def __init__(self, context, raw):
        self.context, self.raw = context, raw
        self.home = RoomBeliefModel(context.options.get('home_model_half_life_days', 45), raw)
        self.stats = self.home.graph, self.home.dwell, self.home.calibration
        self.adaptive = AdaptivePresenceModel()
        self.adaptive_cache = {}

    def reset(self):
        self.home = RoomBeliefModel(self.context.options.get('home_model_half_life_days', 45))
        self.home.graph, self.home.dwell, self.home.calibration = self.stats
        # Virtual ON/hysteresis is causal runtime state, not checkpoint state. Rebuild it
        # from the same as-of event window on every replay query.
        self.adaptive = AdaptivePresenceModel()
        self.adaptive_cache = {}

    def observe_adaptive(self, area, ts):
        if not area:
            return
        base = self.home.forecast(area, ts)
        self.context.augment_home_forecast(
            self.home, area, base, ts,
            presence_model=self.adaptive,
            cache=self.adaptive_cache,
        )

    def forecast(self, target, ts):
        area = self.context.area_for(target)
        base = self.home.forecast(area, ts)
        return self.context.augment_home_forecast(
            self.home, area, base, ts,
            presence_model=self.adaptive,
            cache=self.adaptive_cache,
        )


class SQLiteTemporalTracker:
    """Causal, bounded-memory replay cursor with incremental forward advancement.

    0.14.24 rebuilt every selected entity with one indexed SQLite query per entity on
    every feature timestamp. On a Pi that turned one chronological replay into thousands
    of tiny SQLite reads and repeatedly reconstructed the same 64-sample histories.

    The 0.14.25 contract is:
    * first access / rewind -> one bounded bulk as-of rebuild per SQLite variable chunk;
    * forward access -> consume only rows in (current_ts, requested_ts];
    * same timestamp -> zero SQLite work;
    * room-belief reconstruction keeps the previous exact 30-second causal semantics,
      but seeds are fetched with one window query rather than one query per source;
    * at most 64 selected-input samples are retained per entity.

    Rewinds remain supported because overlapping agent dwells can ask for timestamps in
    different orders. A rewind is deliberately a bulk rebuild, never a forward cursor
    that would leak future state into an earlier sample.
    """

    HISTORY_SAMPLES = 64
    # Small chunks are intentional: on Raspberry Pi one large UNION query can hold a
    # CPU core/SQLite connection long enough to starve Ingress despite a good average
    # training duty cycle. Python already merges/sorts the bounded result afterwards.
    SQL_ENTITY_CHUNK = 32

    def __init__(self, store, watched, context, start, end):
        self.conn = sqlite3.connect(store.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA cache_size=-2048')
        self.watched = sorted(set(watched or ()))
        self.context = context
        self.start = float(start)
        self.end = float(end)
        self.home_entities = sorted(set(context.relevant_entities()))
        self.state_map = {}
        self.history = TemporalHistory(maxlen=self.HISTORY_SAMPLES)
        self.current_ts = None
        self._watched_rows = {}
        self._home_seed_rows = {}
        self._home_window_rows = []
        self._home_cache_ts = None
        self._closed = False
        self._metrics = {
            "advances": 0,
            "same_ts_hits": 0,
            "forward_advances": 0,
            "bulk_rebuilds": 0,
            "rewinds": 0,
            "sql_queries": 0,
            "rows_loaded": 0,
            "home_rebuilds": 0,
            "home_cache_full_rebuilds": 0,
            "home_cache_forward_updates": 0,
            "legacy_asof_queries_estimate": 0,
        }
        try:
            row = self.conn.execute(
                'SELECT model FROM home_checkpoints WHERE ts<? ORDER BY ts DESC LIMIT 1',
                (self.start,),
            ).fetchone()
            self._metrics["sql_queries"] += 1
        except sqlite3.OperationalError:
            row = None
        self.home_view = HistoricalHomeView(context, json.loads(row[0]) if row else None)

    @classmethod
    def _chunks(cls, entity_ids):
        ids = sorted(set(str(x) for x in (entity_ids or ()) if x))
        for offset in range(0, len(ids), cls.SQL_ENTITY_CHUNK):
            yield ids[offset:offset + cls.SQL_ENTITY_CHUNK]

    @staticmethod
    def _availability_time(row):
        value = row.get("_feature_received_time")
        if value is None:
            value = row.get("received_ts")
        if value is None:
            value = row.get("ts")
        return float(value or 0.0)

    @classmethod
    def _row_order(cls, row):
        # Feature vectors remain ordered on event time; receive time is a causal
        # availability tie-breaker. Legacy rows with unknown receive time fall back to ts.
        raw_id = row.get("id")
        received = cls._availability_time(row)
        try:
            return (float(row.get("ts") or 0.0), received, 0, int(raw_id))
        except (TypeError, ValueError):
            return (float(row.get("ts") or 0.0), received, 1, str(raw_id or ""))

    @classmethod
    def _home_causal_order(cls, row):
        raw_id = row.get("id")
        try:
            suffix = (0, int(raw_id))
        except (TypeError, ValueError):
            suffix = (1, str(raw_id or ""))
        return (cls._availability_time(row), float(row.get("ts") or 0.0), *suffix)

    def _fetch_rows(self, sql, params):
        rows = [dict(row) for row in self.conn.execute(sql, params).fetchall()]
        self._metrics["sql_queries"] += 1
        self._metrics["rows_loaded"] += len(rows)
        return rows

    def _base_bulk_before(self, entity_ids, ts, count):
        """Last count rows per entity using indexed LIMIT subqueries in one round-trip.

        A window-function scan still walks every historical row for the selected
        entities. UNIONing small per-entity LIMIT queries keeps the entity_time index hot
        while eliminating Python's old N+1 connection/query loop.
        """
        result = []
        count = max(1, int(count))
        for ids in self._chunks(entity_ids):
            parts, params = [], []
            for eid in ids:
                parts.append(
                    "SELECT * FROM ("
                    "SELECT id,entity_id,ts,received_ts,state,attributes_json,context_user_id,source "
                    "FROM entity_history WHERE entity_id=? AND ts<=? "
                    "AND COALESCE(received_ts,ts)<=? "
                    "ORDER BY ts DESC,id DESC LIMIT ?)"
                )
                params.extend([eid, float(ts), float(ts), count])
            if not parts:
                continue
            sql = " UNION ALL ".join(parts)
            result.extend(self._fetch_rows(sql, params))
            TRAINING_BUDGET.checkpoint("temporal_before_query")
        result.sort(key=self._row_order)
        return result

    def _base_interval_rows(self, entity_ids, lo, hi, per_entity_limit=None):
        if float(hi) <= float(lo):
            return []
        result = []
        for ids in self._chunks(entity_ids):
            marks = ",".join("?" for _ in ids)
            if per_entity_limit is None:
                sql = (
                    "SELECT id,entity_id,ts,received_ts,state,attributes_json,context_user_id,source "
                    f"FROM entity_history WHERE entity_id IN ({marks}) "
                    "AND ts<=? AND COALESCE(received_ts,ts)<=? "
                    "AND (ts>? OR COALESCE(received_ts,ts)>?) ORDER BY ts,id"
                )
                params = [*ids, float(hi), float(hi), float(lo), float(lo)]
            else:
                limit = max(1, int(per_entity_limit))
                parts, params = [], []
                for eid in ids:
                    parts.append(
                        "SELECT * FROM ("
                        "SELECT id,entity_id,ts,received_ts,state,attributes_json,context_user_id,source "
                        "FROM entity_history WHERE entity_id=? "
                        "AND ts<=? AND COALESCE(received_ts,ts)<=? "
                        "AND (ts>? OR COALESCE(received_ts,ts)>?) "
                        "ORDER BY ts DESC,id DESC LIMIT ?)"
                    )
                    params.extend([eid, float(hi), float(hi), float(lo), float(lo), limit])
                sql = " UNION ALL ".join(parts)
            result.extend(self._fetch_rows(sql, params))
            TRAINING_BUDGET.checkpoint("temporal_forward_query")
        result.sort(key=self._row_order)
        return result

    def _compact_rows(self, rows, count):
        """Base archive has unique IDs; subclasses may merge additional observation rows."""
        unique = {}
        for row in rows:
            unique[str(row.get("id"))] = row
        ordered = sorted(unique.values(), key=self._row_order)
        return ordered[-max(1, int(count)):]

    def _bulk_before(self, entity_ids, ts, count=HISTORY_SAMPLES):
        return self._base_bulk_before(entity_ids, ts, count)

    def _interval_rows(self, entity_ids, lo, hi):
        # Only the final bounded history per selected input can affect features at hi.
        return self._base_interval_rows(
            entity_ids, lo, hi, per_entity_limit=self.HISTORY_SAMPLES
        )

    def _home_seed_interval_rows(self, entity_ids, lo, hi):
        # Seed advancement follows the same source contract as _bulk_before(). The base
        # tracker has only entity_history; observation-contract subclasses add their fast
        # received-time journal here so the moving t-30 seed remains exact.
        return self._base_interval_rows(
            entity_ids, lo, hi, per_entity_limit=None
        )

    def _home_interval_rows(self, entity_ids, lo, hi):
        # Deliberately raw archive only. Observation-contract v12 historically augmented
        # the as-of seed with its fast journal but replayed the recent home window from
        # entity_history. Keep that semantic boundary unchanged.
        return self._base_interval_rows(entity_ids, lo, hi, per_entity_limit=None)

    def _before(self, eid, ts, count=HISTORY_SAMPLES):
        # Compatibility surface for transition queries and Teach. The implementation is
        # now the same bulk path used by replay, even for a single entity.
        rows = self._bulk_before([eid], float(ts), int(count))
        return self._compact_rows(rows, count)

    def _set_entity_rows(self, eid, rows):
        compact = self._compact_rows(rows, self.HISTORY_SAMPLES)
        if compact:
            self._watched_rows[eid] = compact
            dq = deque(maxlen=self.HISTORY_SAMPLES)
            for row in compact:
                sample = (float(row["ts"]), archived_state(row))
                if dq and abs(float(dq[-1][0]) - sample[0]) < 1e-6:
                    dq[-1] = sample
                else:
                    dq.append(sample)
            self.history.samples[eid] = dq
            self.state_map[eid] = dq[-1][1]
        else:
            self._watched_rows.pop(eid, None)
            self.history.samples.pop(eid, None)
            self.state_map.pop(eid, None)

    def _rebuild_watched(self, ts):
        rows = self._bulk_before(self.watched, ts, self.HISTORY_SAMPLES)
        grouped = defaultdict(list)
        for row in rows:
            grouped[row["entity_id"]].append(row)
        self.state_map = {}
        self.history = TemporalHistory(maxlen=self.HISTORY_SAMPLES)
        self._watched_rows = {}
        for eid in self.watched:
            self._set_entity_rows(eid, grouped.get(eid, ()))
            # Preserve the 0.14.18 cooperative boundary while eliminating its N+1 SQL.
            TRAINING_BUDGET.checkpoint("temporal_watched_entity")

    def _forward_watched(self, lo, hi):
        rows = self._interval_rows(self.watched, lo, hi)
        grouped = defaultdict(list)
        for row in rows:
            grouped[row["entity_id"]].append(row)
        for eid, new_rows in grouped.items():
            self._set_entity_rows(eid, list(self._watched_rows.get(eid, ())) + new_rows)
            TRAINING_BUDGET.checkpoint("temporal_watched_entity")

    def _render_home_cache(self, ts):
        """Render the exact 30-second causal Room Belief view from cached rows.

        The semantic rebuild remains deliberate: movement hypotheses are window-relative.
        The expensive part was repeatedly asking SQLite for every seed on every feature
        timestamp. Seeds/window rows now advance incrementally in memory.
        """
        view = self.home_view
        view.reset()
        cutoff = float(ts) - 30.0
        ids = self.home_entities
        seeded_areas = set()

        for eid in ids:
            row = self._home_seed_rows.get(eid)
            if row is not None:
                st = archived_state(row)
                area = self.context.area_for(eid)
                event_ts = float(row["ts"])
                received_ts = self._availability_time(row)
                view.home.observe(
                    eid, area, self.context.sensor_probability(eid, st),
                    received_ts, learn=False, evidence=self.context.evidence_metadata(eid),
                    event_ts=event_ts, received_ts=received_ts,
                )
                if area:
                    seeded_areas.add(area)
            TRAINING_BUDGET.checkpoint("temporal_home_seed")

        view.home.reset_movement_state()
        for area in sorted(seeded_areas):
            view.observe_adaptive(area, cutoff)

        for row in sorted(self._home_window_rows, key=self._home_causal_order):
            event_ts = float(row["ts"])
            received_ts = self._availability_time(row)
            if event_ts > float(ts) or received_ts > float(ts):
                continue
            if event_ts <= cutoff and received_ts <= cutoff:
                continue
            eid = row["entity_id"]
            area = self.context.area_for(eid)
            view.home.observe(
                eid, area,
                self.context.sensor_probability(eid, archived_state(row)), received_ts,
                learn=False, evidence=self.context.evidence_metadata(eid),
                event_ts=event_ts, received_ts=received_ts,
            )
            view.observe_adaptive(area, received_ts)
            TRAINING_BUDGET.checkpoint("temporal_home_event")

        self.history.home_context = view
        self._metrics["home_rebuilds"] += 1

    def _rebuild_home_cache(self, ts):
        cutoff = float(ts) - 30.0
        ids = self.home_entities
        seeds = self._bulk_before(ids, cutoff, 1) if ids else []
        self._home_seed_rows = {row["entity_id"]: row for row in seeds}
        self._home_window_rows = (
            self._home_interval_rows(ids, cutoff, ts) if ids else []
        )
        self._home_window_rows.sort(key=self._row_order)
        self._home_cache_ts = float(ts)
        self._metrics["home_cache_full_rebuilds"] += 1
        self._render_home_cache(ts)

    def _forward_home_cache(self, lo, hi):
        """Advance the 30-second Room Belief source window without re-reading seeds."""
        ids = self.home_entities
        old_cutoff = float(lo) - 30.0
        cutoff = float(hi) - 30.0
        new_rows = (
            self._home_interval_rows(ids, lo, hi) if ids and float(hi) > float(lo) else []
        )
        seed_advances = (
            self._home_seed_interval_rows(ids, old_cutoff, cutoff)
            if ids and cutoff > old_cutoff else []
        )
        combined = list(self._home_window_rows)
        combined.extend(new_rows)
        combined.sort(key=self._home_causal_order)

        retained = []
        seeds = dict(self._home_seed_rows)
        for row in sorted(seed_advances, key=self._row_order):
            seeds[row["entity_id"]] = row
            TRAINING_BUDGET.checkpoint("temporal_home_seed_advance")
        for row in combined:
            received_ts = self._availability_time(row)
            if float(row["ts"]) <= cutoff and received_ts <= cutoff:
                # A row may become a seed only after it was causally available by the
                # seed cutoff. Delayed older events stay in the active window until their
                # receive time has passed.
                previous = seeds.get(row["entity_id"])
                if previous is None or self._row_order(row) >= self._row_order(previous):
                    seeds[row["entity_id"]] = row
            else:
                retained.append(row)
            TRAINING_BUDGET.checkpoint("temporal_home_cache_advance")

        self._home_seed_rows = seeds
        self._home_window_rows = retained
        self._home_cache_ts = float(hi)
        self._metrics["home_cache_forward_updates"] += 1
        self._render_home_cache(hi)

    def advance(self, ts):
        ts = min(float(ts), self.end)
        TRAINING_BUDGET.checkpoint("temporal_advance_start")
        self._metrics["advances"] += 1

        if self.current_ts is not None and abs(ts - float(self.current_ts)) <= 1e-9:
            self._metrics["same_ts_hits"] += 1
            TRAINING_BUDGET.checkpoint("temporal_advance_done")
            return

        # Approximate the old query count for on-device diagnostics: one selected-input
        # query per watched entity, one seed query per home source, plus one recent-window
        # query. Edge lookups are intentionally excluded from both sides.
        self._metrics["legacy_asof_queries_estimate"] += (
            len(self.watched) + len(self.home_entities) + (1 if self.home_entities else 0)
        )

        previous_ts = self.current_ts
        if previous_ts is None:
            self._metrics["bulk_rebuilds"] += 1
            self._rebuild_watched(ts)
            self._rebuild_home_cache(ts)
        elif ts > float(previous_ts):
            self._metrics["forward_advances"] += 1
            self._forward_watched(float(previous_ts), ts)
            self._forward_home_cache(float(previous_ts), ts)
        else:
            self._metrics["rewinds"] += 1
            self._metrics["bulk_rebuilds"] += 1
            self._rebuild_watched(ts)
            self._rebuild_home_cache(ts)
        self.current_ts = ts
        TRAINING_BUDGET.checkpoint("temporal_advance_done")

    def _edges(self, eid, lo, hi):
        previous = self._before(eid, lo, 1)
        prev = state_scalar(archived_state(previous[-1])) if previous else None
        for row in self._base_interval_rows([eid], lo, hi, per_entity_limit=None):
            cur = state_scalar(archived_state(row))
            if prev is not None and cur is not None:
                if cur > .25 and prev <= .25:
                    yield row["ts"], True
                elif cur < -.25 and prev >= -.25:
                    yield row["ts"], False
            prev = cur
            TRAINING_BUDGET.checkpoint("temporal_edge_scan")

    def directional_transition_before(self, eid, at_ts, positive, window):
        latest = None
        for ts, direction in self._edges(eid, float(at_ts) - float(window), at_ts):
            if direction == positive:
                latest = ts
        return latest

    def first_directional_transition_after(self, eid, start, end, positive):
        for ts, direction in self._edges(eid, start, end):
            if direction == positive:
                return ts
        return None

    def stats(self):
        out = dict(self._metrics)
        legacy = int(out.get("legacy_asof_queries_estimate") or 0)
        actual = int(out.get("sql_queries") or 0)
        out.update({
            "current_ts": self.current_ts,
            "watched_entities": len(self.watched),
            "home_entities": len(self.home_entities),
            "home_window_rows": len(self._home_window_rows),
            "home_seed_entities": len(self._home_seed_rows),
            "history_samples_per_entity": self.HISTORY_SAMPLES,
            "query_reduction_ratio": (
                max(0.0, 1.0 - (actual / legacy)) if legacy > 0 else None
            ),
        })
        return out

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.conn.close()
        except Exception:
            pass

    def __del__(self):
        self.close()

