"""Teach-chart Desired must mean the decision the user actually saw live.

The original Teach-RL history endpoint replayed the *current* policy over archived sensor
states.  That is useful for model analysis, but it is not historical Desired: after online
learning, teaching overrides, experiments, schema changes, or a rebuild it can differ
substantially from what the agent card showed at the time.

The runtime already records the effective post-override/post-experiment Desired in
``decision_history`` from the same ``chosen`` value assigned to ``runtime.last_prediction``.
This extension overlays that recorded stream onto the Teach chart and point inspector.
Current continues to come from HA entity history.  Missing/stale Desired is shown as a gap
rather than silently inventing a replayed decision.
"""
from bisect import bisect_right
import time


# Engine.Teaching records an unchanged Current/Desired pair at least every 30 seconds.
# A 95 second freshness window tolerates scheduling jitter while still exposing addon
# downtime/restarts as a visible gap instead of extending an old decision indefinitely.
DESIRED_STALE_SECONDS = 95.0


def _ensure_table(store):
    # Teaching normally creates this table before RLTeaching is installed.  Repeating the
    # additive declaration here makes the read contract explicit and keeps tests/recovery
    # robust if extension order changes.
    with store.lock, store.conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS decision_history (
              agent_id TEXT NOT NULL, ts REAL NOT NULL, current REAL, desired REAL,
              PRIMARY KEY(agent_id,ts));
            CREATE INDEX IF NOT EXISTS idx_decision_ts ON decision_history(ts);
            """
        )


def _buffer_snapshot(engine, agent_id, end):
    teaching = getattr(engine, "teaching", None)
    buffer = getattr(teaching, "buffer", None)
    lock = getattr(teaching, "lock", None)
    if buffer is None:
        return []
    try:
        if lock is None:
            rows = list(buffer)
        else:
            with lock:
                rows = list(buffer)
    except Exception:
        return []
    out = []
    for row in rows:
        try:
            aid, ts, current, desired = row
            ts = float(ts)
        except Exception:
            continue
        if str(aid) == str(agent_id) and ts <= float(end):
            out.append({"ts": ts, "current": current, "desired": desired})
    return out


def _recorded_rows(store, engine, agent_id, start, end):
    """Read one seed plus the range without turning a chart GET into a writer.

    Correct/Teach is a read path.  Do not force Teaching.flush() here: that can acquire the
    shared Store writer mutex and make an interactive chart wait behind historical
    training.  Instead take a bounded RAM snapshot before and after the SQLite read.  The
    double snapshot closes the small race where a concurrent flush removes a row from the
    deque after the SELECT snapshot was chosen; timestamp de-duplication below makes a row
    present in both SQLite and RAM harmless.
    """
    buffered_before = _buffer_snapshot(engine, agent_id, end)
    with store.conn() as c:
        seed = c.execute(
            "SELECT ts,current,desired FROM decision_history "
            "WHERE agent_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (str(agent_id), float(start)),
        ).fetchone()
        rows = [dict(seed)] if seed else []
        rows.extend(
            dict(r) for r in c.execute(
                "SELECT ts,current,desired FROM decision_history "
                "WHERE agent_id=? AND ts>? AND ts<=? ORDER BY ts",
                (str(agent_id), float(start), float(end)),
            ).fetchall()
        )

    # A freshly displayed card value can still be in Teaching's non-blocking write buffer.
    # Merge both bounded snapshots so opening Correct immediately after observing the card
    # cannot lose the latest Desired merely because a background flush raced the SELECT.
    rows.extend(buffered_before)
    rows.extend(_buffer_snapshot(engine, agent_id, end))
    by_ts = {}
    for row in rows:
        try:
            ts = float(row["ts"])
        except Exception:
            continue
        if ts <= float(end):
            by_ts[ts] = {"ts": ts, "current": row.get("current"), "desired": row.get("desired")}
    ordered = [by_ts[k] for k in sorted(by_ts)]
    # Keep only the newest pre-range seed; buffered rows can otherwise add several.
    before = [r for r in ordered if r["ts"] <= float(start)]
    inside = [r for r in ordered if r["ts"] > float(start)]
    return ([before[-1]] if before else []) + inside


def _timestamps(rows):
    """Build one immutable timestamp index for repeated as-of lookups."""
    return tuple(float(row["ts"]) for row in (rows or []))


def _desired_at(rows, timestamp, times=None):
    if not rows:
        return None
    times = _timestamps(rows) if times is None else times
    idx = bisect_right(times, float(timestamp)) - 1
    if idx < 0:
        return None
    row = rows[idx]
    if float(timestamp) - float(row["ts"]) > DESIRED_STALE_SECONDS:
        return None
    value = row.get("desired")
    return None if value is None else float(value)


def _current_at(points, timestamp, times=None):
    if not points:
        return None
    times = _timestamps(points) if times is None else times
    idx = bisect_right(times, float(timestamp)) - 1
    if idx < 0:
        return None
    return points[idx].get("current")


def _event_times(rows, start, end):
    """Exact Desired edges plus explicit stale-gap boundaries for step rendering."""
    result = set()
    previous = None
    for row in rows:
        ts = float(row["ts"])
        if start <= ts <= end:
            if previous is None or row.get("desired") != previous.get("desired") or ts-previous["ts"] > DESIRED_STALE_SECONDS:
                result.add(ts)
        if previous is not None and ts-previous["ts"] > DESIRED_STALE_SECONDS:
            cutoff = previous["ts"] + DESIRED_STALE_SECONDS + 1e-4
            if start <= cutoff <= end:
                result.add(cutoff)
        previous = row
    if rows:
        cutoff = rows[-1]["ts"] + DESIRED_STALE_SECONDS + 1e-4
        if start <= cutoff <= end:
            result.add(cutoff)
    return result


def install(store, engine, service):
    if service is None or getattr(service, "_observed_desired_history_installed", False):
        return service
    _ensure_table(store)
    original_history = service.history
    original_point = service.point

    def history(agent, start, end):
        base = original_history(agent, start, end)
        start_ts, end_ts = float(base["start"]), float(base["end"])
        base_points = sorted(base.get("points") or [], key=lambda p: float(p["ts"]))
        rows = _recorded_rows(store, engine, agent["id"], start_ts, end_ts)

        # Preserve the HA Current curve produced by the existing historical path, but
        # replace every replayed Desired value with the effective runtime decision log.
        # Add exact Desired change times so a transition is not shifted to the next chart
        # sampling tick.
        times = {float(p["ts"]) for p in base_points}
        times.update(_event_times(rows, start_ts, end_ts))
        current_times = _timestamps(base_points)
        desired_times = _timestamps(rows)
        points = []
        for ts in sorted(t for t in times if start_ts <= t <= end_ts):
            points.append({
                "ts": ts,
                "current": _current_at(base_points, ts, current_times),
                "desired": _desired_at(rows, ts, desired_times),
            })
        base["points"] = points
        base["desired_source"] = "observed_runtime_decision_history"
        base["desired_semantics"] = "effective Desired actually shown by the live agent runtime/card"
        base["desired_freshness_seconds"] = DESIRED_STALE_SECONDS
        base["recorded_desired_rows"] = len(rows)
        return base

    def point(agent, timestamp):
        base = original_point(agent, timestamp)
        ts = float(base.get("ts", timestamp))
        rows = _recorded_rows(store, engine, agent["id"], ts, ts)
        base["desired"] = _desired_at(rows, ts) if base.get("current") is not None else None
        base["desired_source"] = "observed_runtime_decision_history"
        return base

    service.history = history
    service.point = point
    service._observed_desired_history_installed = True
    service.observed_desired_contract = {
        "source": "decision_history",
        "matches_runtime_field": "last_prediction",
        "stale_after_seconds": DESIRED_STALE_SECONDS,
        "policy_replay_used_for_displayed_desired": False,
    }
    return service
