import unittest
from types import SimpleNamespace
import test_executor as fixtures
import manual_context_learning as m
from context import ExplicitFeatureSchema
from historical_teach_install import install
from support import state


class HistoricalTeachInstallTests(unittest.TestCase):
    setUp = fixtures.ExecutorTests.setUp
    tearDown = fixtures.ExecutorTests.tearDown

    def test_teach_records_full_context_feedback(self):
        m._ensure_table(self.store)
        self.e.state_map["binary_sensor.motion"] = state("binary_sensor.motion", "on", device_class="motion")
        self.model.schema = ExplicitFeatureSchema(self.model.dims, ["binary_sensor.motion"])
        self.e.runtime[self.a["id"]] = {"last_prediction": 0, "pending": {"defer": True}}
        install(SimpleNamespace(ENGINE=self.e, STORE=self.store))
        result = self.e.teaching.teach(self.e, self.a, 1)
        self.assertTrue(result["context_learning"]["recorded"])
        with self.store.conn() as c:
            count = c.execute("SELECT COUNT(*) FROM manual_context_feedback WHERE agent_id=?", (self.a["id"],)).fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__": unittest.main()
