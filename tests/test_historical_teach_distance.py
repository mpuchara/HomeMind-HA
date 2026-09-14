import unittest
from historical_teach_distance import distance


class HistoricalTeachDistanceTests(unittest.TestCase):
    def test_similar_context_matches(self):
        old = {"time:hour_sin": .1, "binary_sensor.motion:value": 1.0,
               "sensor.radar:value": .55, "sensor.radar:lag_delta_1": .20}
        new = {"time:hour_sin": .7, "binary_sensor.motion:value": 1.0,
               "sensor.radar:value": .63, "sensor.radar:lag_delta_1": .12}
        self.assertIsNotNone(distance(new, old))

    def test_presence_flip_never_matches(self):
        old = {"time:hour_sin": .1, "binary_sensor.motion:value": 1.0}
        new = {"time:hour_sin": .1, "binary_sensor.motion:value": -1.0}
        self.assertIsNone(distance(new, old))


if __name__ == "__main__": unittest.main()
