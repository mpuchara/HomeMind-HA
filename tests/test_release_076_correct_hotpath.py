"""0.14.76 regressions for the interactive Correct/Teach history hot path."""
import tempfile
import threading
import unittest
from pathlib import Path

from agent_correct_generation_history import _live_observed_points
from storage import Store
from teach_observed_history import (
    DESIRED_STALE_SECONDS,
    _current_at as teach_current_at,
    _desired_at,
    _ensure_table,
    _recorded_rows,
    _timestamps,
)


def legacy_live_points(decisions, currents, start, end):
    """Exact pre-0.14.76 computation kept only as a regression oracle."""
    start = float(start)
    end = float(end)
    times = {start, end}
    times.update(float(row["ts"]) for row in decisions)
    times.update(float(row["ts"]) for row in currents)
    for row in decisions:
        cutoff = float(row["ts"]) + float(DESIRED_STALE_SECONDS) + 1e-4
        if start <= cutoff <= end:
            times.add(cutoff)

    points = []
    gaps = []
    gap_start = None
    for ts in sorted(times):
        if decisions:
            desired_times = [float(row["ts"]) for row in decisions]
            import bisect
            idx = bisect.bisect_right(desired_times, ts) - 1
            if idx < 0 or ts - float(decisions[idx]["ts"]) > DESIRED_STALE_SECONDS:
                desired = None
            else:
                value = decisions[idx].get("desired")
                desired = None if value is None else float(value)
        else:
            desired = None

        if currents:
            current_times = [float(row["ts"]) for row in currents]
            import bisect
            idx = bisect.bisect_right(current_times, ts) - 1
            current = None if idx < 0 else currents[idx].get("current")
        else:
            current = None

        points.append({"ts": ts, "current": current, "desired": desired})
        if desired is None and gap_start is None:
            gap_start = ts
        elif desired is not None and gap_start is not None:
            gaps.append({"start": gap_start, "end": ts})
            gap_start = None
    if gap_start is not None:
        gaps.append({"start": gap_start, "end": end})
    return points, gaps


class CorrectHotPathParityTests(unittest.TestCase):
    def assertParity(self, decisions, currents, start, end):
        expected = legacy_live_points(decisions, currents, start, end)
        actual = _live_observed_points(decisions, currents, start, end)
        self.assertEqual(actual, expected)

    def test_empty_streams_match_reference(self):
        self.assertParity([], [], 100.0, 200.0)

    def test_seed_and_range_boundaries_match_reference(self):
        decisions = [
            {"ts": 70.0, "desired": 1.0},
            {"ts": 100.0, "desired": 0.0},
            {"ts": 200.0, "desired": 1.0},
        ]
        currents = [
            {"ts": 50.0, "current": 0.0},
            {"ts": 100.0, "current": 1.0},
            {"ts": 200.0, "current": 0.0},
        ]
        self.assertParity(decisions, currents, 100.0, 200.0)

    def test_duplicate_timestamps_and_none_values_match_reference(self):
        decisions = [
            {"ts": 100.0, "desired": 0.0},
            {"ts": 100.0, "desired": 1.0},
            {"ts": 100.0, "desired": None},
            {"ts": 130.0, "desired": 1.0},
        ]
        currents = [
            {"ts": 99.0, "current": 0.0},
            {"ts": 110.0, "current": 0.0},
            {"ts": 110.0, "current": 1.0},
        ]
        self.assertParity(decisions, currents, 95.0, 160.0)

    def test_stale_gap_boundaries_match_reference(self):
        decisions = [
            {"ts": 100.0, "desired": 1.0},
            {"ts": 310.0, "desired": 0.0},
        ]
        currents = [
            {"ts": 90.0, "current": 0.0},
            {"ts": 250.0, "current": 1.0},
        ]
        self.assertParity(decisions, currents, 90.0, 430.0)

    def test_typical_history_matches_reference(self):
        decisions = [
            {"ts": 1000.0 + i * 30.0, "desired": float((i // 3) % 2)}
            for i in range(120)
        ]
        currents = [
            {"ts": 1001.0 + i * 37.0, "current": float((i // 2) % 2)}
            for i in range(95)
        ]
        self.assertParity(decisions, currents, 970.0, 4700.0)

    def test_work_scaling_is_linear_for_1k_2k_4k_8k_streams(self):
        samples = []
        for n in (1000, 2000, 4000, 8000):
            decisions = [{"ts": float(i * 30), "desired": float(i % 2)} for i in range(n)]
            currents = [{"ts": float(i * 30 + 1), "current": float(i % 2)} for i in range(n)]
            stats = {}
            points, gaps = _live_observed_points(decisions, currents, 0.0, float(n * 30), stats=stats)
            self.assertTrue(points)
            self.assertIsInstance(gaps, list)
            legacy_work = len(points) * (len(decisions) + len(currents))
            samples.append((n, stats["lookup_work"], legacy_work))
            self.assertLessEqual(stats["decision_advances"], n)
            self.assertLessEqual(stats["current_advances"], n)
            self.assertLess(stats["lookup_work"], legacy_work // 50)

        for previous, current in zip(samples, samples[1:]):
            new_ratio = current[1] / previous[1]
            legacy_ratio = current[2] / previous[2]
            self.assertLess(new_ratio, 2.2)
            self.assertGreater(legacy_ratio, 3.5)


class FakeTeaching:
    def __init__(self):
        self.lock = threading.RLock()
        self.buffer = []
        self.flush_calls = 0

    def flush(self, *args, **kwargs):
        self.flush_calls += 1
        raise AssertionError("interactive history read must not force Teaching.flush()")


class FakeEngine:
    def __init__(self):
        self.teaching = FakeTeaching()


class CorrectReadPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="correct-hotpath-")
        self.store = Store(Path(self.temp.name) / "correct.db")
        _ensure_table(self.store)
        self.engine = FakeEngine()
        self.agent_id = "agent-hot"

    def tearDown(self):
        self.temp.cleanup()

    def insert(self, ts, current, desired):
        with self.store.lock, self.store.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO decision_history(agent_id,ts,current,desired) VALUES(?,?,?,?)",
                (self.agent_id, float(ts), current, desired),
            )

    def test_recorded_rows_never_forces_flush_and_buffer_wins_same_timestamp(self):
        self.insert(100.0, 0.0, 0.0)
        self.engine.teaching.buffer.append((self.agent_id, 100.0, 1.0, 1.0))
        self.engine.teaching.buffer.append((self.agent_id, 110.0, 1.0, 0.0))
        rows = _recorded_rows(self.store, self.engine, self.agent_id, 100.0, 120.0)
        self.assertEqual(self.engine.teaching.flush_calls, 0)
        by_ts = {row["ts"]: row for row in rows}
        self.assertEqual(by_ts[100.0]["current"], 1.0)
        self.assertEqual(by_ts[100.0]["desired"], 1.0)
        self.assertEqual(by_ts[110.0]["desired"], 0.0)

    def test_recorded_rows_completes_while_store_writer_mutex_is_held(self):
        self.insert(100.0, 0.0, 1.0)
        done = threading.Event()
        errors = []

        def reader():
            try:
                rows = _recorded_rows(self.store, self.engine, self.agent_id, 100.0, 120.0)
                self.assertEqual(rows[0]["desired"], 1.0)
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        with self.store.lock:
            thread = threading.Thread(target=reader, daemon=True)
            thread.start()
            # This is a liveness assertion, not a performance threshold: the reader must
            # finish while the Python writer mutex is still owned by this thread.
            self.assertTrue(done.wait(2.0), "Correct read blocked on Store.writer mutex")
        thread.join(timeout=2.0)
        if errors:
            raise errors[0]
        self.assertEqual(self.engine.teaching.flush_calls, 0)

    def test_preindexed_teach_lookups_preserve_legacy_results(self):
        rows = [
            {"ts": 10.0, "current": 0.0, "desired": 1.0},
            {"ts": 20.0, "current": 1.0, "desired": None},
            {"ts": 40.0, "current": 1.0, "desired": 0.0},
        ]
        times = _timestamps(rows)
        for ts in (5.0, 10.0, 19.999, 20.0, 40.0, 140.0):
            self.assertEqual(_desired_at(rows, ts), _desired_at(rows, ts, times))
            self.assertEqual(teach_current_at(rows, ts), teach_current_at(rows, ts, times))

    def test_newest_buffered_pre_range_row_remains_the_seed(self):
        self.insert(50.0, 0.0, 0.0)
        self.engine.teaching.buffer.extend([
            (self.agent_id, 70.0, 0.0, 1.0),
            (self.agent_id, 90.0, 1.0, 1.0),
            (self.agent_id, 130.0, 1.0, 0.0),
        ])
        rows = _recorded_rows(self.store, self.engine, self.agent_id, 100.0, 140.0)
        self.assertEqual([row["ts"] for row in rows], [90.0, 130.0])
        self.assertEqual(rows[0]["desired"], 1.0)
        self.assertEqual(rows[1]["desired"], 0.0)


if __name__ == "__main__":
    unittest.main()
