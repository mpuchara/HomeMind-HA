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
    SQL_ENTITY_CHUNK = 700

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
    def _row_order(row):
        return (
            float(row.get("ts") or 0.0),
            float(row.get("_feature_received_time") or 0.0),
            str(row.get("id") or ""),
        )

    def _fetch_rows(self, sql, params):
        rows = [dict(row) for row in self.conn.execute(sql, params).fetchall()]
        self._metrics["sql_queries"] += 1
        self._metrics["rows_loaded"] += len(rows)
        return rows

    def _base_bulk_before(self, entity_ids, ts, count):
        """Last count history rows per entity in a bounded number of SQL queries."""
        result = []
        count = max(1, int(count))
        for ids in self._chunks(entity_ids):
            marks = ",".join("?" for _ in ids)
            sql = f"""
                SELECT id,entity_id,ts,state,attributes_json,context_user_id,source
                FROM (
                    SELECT h.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY entity_id ORDER BY ts DESC,id DESC
                           ) AS _hm_rank
                    FROM entity_history h
                    WHERE entity_id IN ({marks}) AND ts<=?
                )
                WHERE _hm_rank<=?
                ORDER BY ts,id
            """
            result.extend(self._fetch_rows(sql, [*ids, float(ts), count]))
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
                    "SELECT id,entity_id,ts,state,attributes_json,context_user_id,source "
                    f"FROM entity_history WHERE entity_id IN ({marks}) "
                    "AND ts>? AND ts<=? ORDER BY ts,id"
                )
                params = [*ids, float(lo), float(hi)]
            else:
                limit = max(1, int(per_entity_limit))
                sql = f"""
                    SELECT id,entity_id,ts,state,attributes_json,context_user_id,source
                    FROM (
                        SELECT h.*,
                               ROW_NUMBER() OVER (
                                   PARTITION BY entity_id ORDER BY ts DESC,id DESC
                               ) AS _hm_rank
                        FROM entity_history h
                        WHERE entity_id IN ({marks}) AND ts>? AND ts<=?
                    )
                    WHERE _hm_rank<=?
                    ORDER BY ts,id
                """
                params = [*ids, float(lo), float(hi), limit]
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

    def _rebuild_home(self, ts):
        """Reproduce the exact legacy 30-second causal room-belief window in bulk."""
        view = self.home_view
        view.reset()
        cutoff = float(ts) - 30.0
        ids = self.home_entities
        seeded_areas = set()

        seed_rows = self._bulk_before(ids, cutoff, 1) if ids else []
        seed_by_entity = {row["entity_id"]: row for row in seed_rows}
        # The legacy implementation seeded in sorted entity-id order at exactly t-30.
        for eid in ids:
            row = seed_by_entity.get(eid)
            if row is not None:
                st = archived_state(row)
                area = self.context.area_for(eid)
                view.home.observe(
                    eid, area, self.context.sensor_probability(eid, st),
                    cutoff, learn=False, evidence=self.context.evidence_metadata(eid),
                )
                if area:
                    seeded_areas.add(area)
            TRAINING_BUDGET.checkpoint("temporal_home_seed")

        view.home.reset_movement_state()
        for area in sorted(seeded_areas):
            view.observe_adaptive(area, cutoff)

        for row in self._home_interval_rows(ids, cutoff, ts) if ids else ():
            eid = row["entity_id"]
            area = self.context.area_for(eid)
            view.home.observe(
                eid, area,
                self.context.sensor_probability(eid, archived_state(row)), row["ts"],
                learn=False, evidence=self.context.evidence_metadata(eid),
            )
            view.observe_adaptive(area, row["ts"])
            TRAINING_BUDGET.checkpoint("temporal_home_event")

        self.history.home_context = view
        self._metrics["home_rebuilds"] += 1

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

        if self.current_ts is None:
            self._metrics["bulk_rebuilds"] += 1
            self._rebuild_watched(ts)
        elif ts > float(self.current_ts):
            self._metrics["forward_advances"] += 1
            self._forward_watched(float(self.current_ts), ts)
        else:
            self._metrics["rewinds"] += 1
            self._metrics["bulk_rebuilds"] += 1
            self._rebuild_watched(ts)

        # Room belief keeps its exact legacy semantics. The expensive per-source seed
        # lookup is now a partitioned bulk query; recent events remain a causal 30 s scan.
        self._rebuild_home(ts)
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

