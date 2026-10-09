"""Paired full-config scan vs fresh indexed target query, including connection setup."""
from pathlib import Path
import argparse
import hashlib
import json
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adaptive_ai/src"))
from storage import Store
from agent_candidates import install_store_overlay


def run(passes=40):
    output = []
    for count in (9, 64):
        with tempfile.TemporaryDirectory(prefix="target-config-bench-") as directory:
            store = Store(Path(directory) / "test.db")
            store.start_wal_keeper()
            try:
                install_store_overlay(store)
                first = store.create_agent(dict(name="Light", target_entity="light.bathroom",
                    target_property="power", min_value=0, max_value=1, input_entities=["binary_sensor.presence"]))
                with store.conn() as c:
                    template = dict(c.execute("SELECT * FROM agents WHERE id=?", (first["id"],)).fetchone())
                    c.execute("DELETE FROM agents")
                    columns = list(template)
                    records = []
                    for i in range(count):
                        row = dict(template, id=f"agent-{i}", name=f"Device {i}",
                            target_entity="light.bathroom" if i < 2 else f"light.other_{i}",
                            target_property="power" if i % 2 == 0 else "brightness",
                            created_at=f"time-{i:04}", benchmark_detail_json=json.dumps({
                                "held_out": [{"actual": j % 2, "predicted": (i + j) % 2,
                                              "features": [k / (j + 1.) for k in range(64)]}
                                             for j in range(24)]}))
                        records.append(tuple(row[key] for key in columns))
                    c.executemany("INSERT INTO agents(" + ",".join(columns) + ") VALUES(" +
                                  ",".join("?" for _ in columns) + ")", records)
                for target in ("light.bathroom", "sensor.unrelated"):
                    expected = [a for a in store.list_agent_configs() if a["target_entity"] == target]
                    previous = lambda: [a for a in store.list_agent_configs() if a["target_entity"] == target]
                    current = lambda: store.list_agent_configs_for_target(target)
                    repeats = []
                    for repeat in range(3):
                        metrics = {}
                        for name, operation in ([("previous", previous), ("current", current)] if repeat % 2 == 0
                                                else [("current", current), ("previous", previous)]):
                            samples = []
                            for _ in range(passes):
                                started = time.perf_counter()
                                actual = operation()
                                samples.append((time.perf_counter() - started) * 1000.)
                                assert actual == expected
                            metrics[name] = dict(median_ms=statistics.median(samples), total_ms=sum(samples))
                        metrics["speedup"] = metrics["previous"]["median_ms"] / metrics["current"]["median_ms"]
                        repeats.append(metrics)
                    decoded = {}
                    for name, operation in (("previous", previous), ("current", current)):
                        with patch.object(store, "_agent_dict", wraps=store._agent_dict) as decoder:
                            assert operation() == expected
                            decoded[name] = decoder.call_count
                    assert decoded == {"previous": count, "current": len(expected)}
                    output.append(dict(agents=count, target=target, returned=len(expected), passes=passes,
                                       decoded_rows=decoded, every_config_parity=True,
                                       canonical_digest=hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest(),
                                       repeats=repeats))
            finally:
                store.stop_wal_keeper()
    return dict(cases=output, includes_connection_setup=True, durable_wal_keeper=True,
                limits="Synthetic 9/64-agent config retrieval only; no HA CPU or whole-decision speedup claim.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--passes", type=int, default=40)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    if args.passes < 1:
        parser.error("--passes must be positive")
    print(json.dumps(run(args.passes), indent=None if args.compact else 2))
