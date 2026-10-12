"""Sequential real-worker A/B benchmark; synthetic history, no HA dispatch.

Run with the same Linux Python/environment and --baseline-root pointing to 0.14.186.
Both sources consume the current fixture, preserving identical inputs across versions.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def measure(source, case_name):
    sys.path[:0] = [str(source / "tests"), str(source / "tools"),
                   str(source / "adaptive_ai/src")]
    import support  # noqa: F401
    import benchmark_feature_snapshot_reuse as fixture
    original = fixture.run_worker
    captured = {}
    def worker(*args, **kwargs):
        result = original(*args, **kwargs)
        raw = result["store"].get_model(args[1])
        # Use the existing fixture's semantic comparison: exclude only the head's
        # wall-clock initialization timestamp, retain all learned arrays/classifier.
        captured["semantic_model_sha256"] = hashlib.sha256(json.dumps(
            fixture.semantic_ridge_model(result["store"], args[1]), sort_keys=True).encode()).hexdigest()
        captured["schema"] = raw["schema"]
        captured["phase_timings"] = result["result"].get("training_phase_timings")
        captured["replay_stats"] = result["result"].get("temporal_replay")
        return result
    fixture.run_worker = worker
    name = "stationary_relay" if case_name == "bathroom" else "kitchen_lighting"
    spec = importlib.util.spec_from_file_location("same_fixture", ROOT / "tools" / f"benchmark_{name}.py")
    case = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(case)
    report = (case.run(noisy=True, wildcard_unmapped=True, lamp_feedback=True)
              if case_name == "bathroom" else case.run(wildcard=True))
    report.update(captured)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--case", choices=("bathroom", "kitchen"))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if args.source_root:
        if not args.case:
            parser.error("--source-root requires --case")
        print(json.dumps(measure(args.source_root.resolve(), args.case)))
        return
    if not args.baseline_root:
        parser.error("--baseline-root is required")
    results = {}
    ignored = {"worker_wall_seconds", "worker_seconds", "phase_timings", "replay_stats"}
    for name in ("bathroom", "kitchen"):
        runs = {"baseline": [], "optimized": []}
        for iteration in range(args.repeats):
            # Alternate which source runs first to reduce warm-cache/order bias.
            order = ("baseline", "optimized") if iteration % 2 == 0 else ("optimized", "baseline")
            for label in order:
                source = args.baseline_root if label == "baseline" else ROOT
                output = subprocess.check_output([
                    sys.executable, str(Path(__file__).resolve()), "--source-root", str(source),
                    "--case", name], text=True, timeout=240)
                runs[label].append(json.loads(output))
        reference = {k: v for k, v in runs["baseline"][0].items() if k not in ignored}
        identical = all({k: v for k, v in row.items() if k not in ignored} == reference
                        for rows in runs.values() for row in rows)
        medians = {label: statistics.median(row["phase_timings"]["replay_seconds"] for row in rows)
                   for label, rows in runs.items()}
        results[name] = {"parity": identical, "replay_median_seconds": medians,
                         "speedup": medians["baseline"] / medians["optimized"],
                         "replay_time_reduction_percent": 100 * (1 - medians["optimized"] / medians["baseline"]),
                         "runs": runs}
        print(json.dumps({"case": name, "parity": identical, "medians": medians}), file=sys.stderr)
    report = {"scope": "WSL/Linux real isolated worker, synthetic inputs; not measured on Raspberry Pi",
              "baseline": "0.14.186", "optimized": "0.14.187", "repeats": args.repeats,
              "cases": results, "pass": all(
                  row["parity"] and all(run["pass"] for runs in row["runs"].values() for run in runs)
                  for row in results.values())}
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    else:
        print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["pass"] else 1)


if __name__ == "__main__":
    main()
