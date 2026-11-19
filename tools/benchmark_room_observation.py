"""Pair room observation updates with and without unused diagnostic materialization."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'adaptive_ai' / 'src'))
from home_state import RoomBeliefModel


class DetailedObservations(RoomBeliefModel):
    def _fuse_room(self, area, ts, *, include_evidence=True):
        return super()._fuse_room(area, ts, include_evidence=True)


def run(model_type, rows):
    model = model_type()
    started = time.perf_counter()
    cpu = time.process_time()
    for entity, value, stamp, evidence in rows:
        model.observe(entity, 'room', value, stamp, evidence=evidence)
    stats = {'wall_seconds': time.perf_counter() - started, 'cpu_seconds': time.process_time() - cpu}
    return stats, (model.export(), model.forecast('room', rows[-1][2]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sensors', type=int, default=48)
    parser.add_argument('--observations', type=int, default=3000)
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args()
    if min(args.sensors, args.observations, args.repeats) < 1:
        parser.error('all counts must be positive')
    roles = ('radar_occupancy', 'pir', 'occupancy_binary', 'radar_activity')
    rows = [
        (f'sensor.fixture_{index % args.sensors}', float((index // args.sensors) % 2),
         1000. + index / 20., {'role': roles[(index % args.sensors) % len(roles)]})
        for index in range(args.observations)
    ]
    variants = {'detailed_reference': DetailedObservations, 'compact_observe': RoomBeliefModel}
    timings = {variant: [] for variant in variants}
    for repeat in range(args.repeats):
        order = list(variants) if repeat % 2 == 0 else list(reversed(variants))
        outputs = {}
        for variant in order:
            stats, outputs[variant] = run(variants[variant], rows)
            timings[variant].append(stats)
        assert outputs['detailed_reference'] == outputs['compact_observe']
    medians = {variant: {key: statistics.median(row[key] for row in measurements)
                        for key in ('wall_seconds', 'cpu_seconds')}
               for variant, measurements in timings.items()}
    print(json.dumps({'sensors': args.sensors, 'observations': args.observations, 'repeats': args.repeats,
                      'parity': True, 'medians': medians, 'timings': timings}, indent=2))


if __name__ == '__main__':
    main()
