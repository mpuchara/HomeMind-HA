#!/usr/bin/env python3
"""Synthetic U03 cardinality and incremental-cost benchmark (NOT a Pi4 gate)."""
from pathlib import Path
import argparse
import json
import statistics
import sys
import time

SRC = Path(__file__).resolve().parents[1] / "adaptive_ai" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from universal_neural_encoder import IncrementalNeuralHome, SharedSetEncoder
from universal_observation import encode_sensor


def token(index, at, room_count):
    kind = index % 3
    if kind == 0:
        entity, val, attrs = f"binary_sensor.motion_{index}", "on" if int(at * 10) % 2 else "off", {}
    elif kind == 1:
        entity, val, attrs = f"sensor.temp_{index}", 20.0 + index % 8, {
            "device_class": "temperature", "unit_of_measurement": "°C"
        }
    else:
        entity, val, attrs = f"sensor.lux_{index}", 50 + (index * 17) % 2000, {
            "device_class": "illuminance", "unit_of_measurement": "lx"
        }
    return encode_sensor(
        entity, {"state": val, "attributes": attrs},
        area_id=f"room_{index % room_count}",
        event_ts=at, received_ts=at, at_ts=at,
        max_age_seconds=900,
    )


def percentile(values, percent):
    values = sorted(values)
    if not values:
        return 0.0
    return values[min(len(values) - 1, max(0, int((len(values) - 1) * percent / 100.0)))]


def benchmark(sensors, updates, rooms):
    cache = IncrementalNeuralHome(SharedSetEncoder())
    started = time.perf_counter()
    for n in range(sensors):
        cache.upsert(token(n, 1.0, rooms), now=1.0)
    initial_ms = (time.perf_counter() - started) * 1000.0
    samples = []
    for i in range(updates):
        stamp = 2.0 + (i * 0.001)
        began = time.perf_counter_ns()
        cache.upsert(token(i % sensors, stamp, rooms), now=stamp)
        cache.home()
        samples.append((time.perf_counter_ns() - began) / 1_000_000.0)
    return {
        "sensors": sensors, "rooms": rooms, "updates": updates,
        "initial_build_ms": round(initial_ms, 4),
        "update_home_p50_ms": round(statistics.median(samples), 4),
        "update_home_p95_ms": round(percentile(samples, 95), 4),
        "update_home_p99_ms": round(percentile(samples, 99), 4),
        "cache": cache.diagnostics(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", default="5,100,1000")
    parser.add_argument("--updates", type=int, default=300)
    parser.add_argument("--rooms", type=int, default=16)
    args = parser.parse_args()
    sizes = tuple(int(x) for x in args.sizes.split(","))
    if not sizes or min(sizes) < 1 or args.updates < 1 or args.rooms < 1:
        parser.error("sizes, updates and rooms must be positive")
    print(json.dumps({
        "benchmark": "u03_neural_set_synthetic",
        "note": "synthetic host timing only; does not replace real Pi4 event-to-decision p95",
        "results": [benchmark(n, args.updates, args.rooms) for n in sizes],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
