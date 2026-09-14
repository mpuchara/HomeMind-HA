import threading
import time
import unittest
from types import SimpleNamespace
from support import state
import manual_context_learning as m
from storage import STORE
from manual_context_one_sided import install


class OneSidedContextTests(unittest.TestCase):
    def test_repeated_one_sided_presence_corrections_become_relevant(self):
        m._ensure_table(STORE)
        with STORE.lock, STORE.conn() as c: c.execute("DELETE FROM manual_context_feedback")
        m._SCORE_CACHE.clear(); m._OBSERVATION_CACHE.clear()
        sensor = "binary_sensor.bath_presence"
        engine = SimpleNamespace(lock=threading.RLock(), state_map={sensor: state(sensor, "on", device_class="presence")})
        install(SimpleNamespace(ENGINE=engine))
        now = time.time()
        for i in range(8):
            m._insert_snapshot(STORE, "bath-agent", 1, 0, "history_teach", "user",
                               {sensor: {"v": 1.0, "age": .5}}, created_ts=now-i)
        self.assertGreater(m.manual_scores(STORE, "bath-agent").get(sensor, 0), .55)


if __name__ == "__main__": unittest.main()
