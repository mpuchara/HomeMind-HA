#!/usr/bin/env python3
"""Repeatable Raspberry Pi 4 runtime/training measurement collector.

Run inside the Adaptive AI add-on container (or any namespace that can reach port 8099).
The tool does not generate HA actions. Start the named scenario in UI/Home Assistant,
then collect the same-duration trace for before/after releases.

Examples:
  python tools/profile_pi_training.py --scenario idle --duration 60
  python tools/profile_pi_training.py --scenario training --duration 120
  python tools/profile_pi_training.py --scenario correct --duration 60
  python tools/profile_pi_training.py --scenario training-correct --duration 120
  python tools/profile_pi_training.py --scenario steady-events --duration 120
  python tools/profile_pi_training.py --scenario burst --duration 60

CPU is reported two ways:
- one_core_percent: CPU seconds / wall seconds * 100 (100% == one fully busy core)
- host_percent: one_core_percent / logical_cpu_count
This avoids the ambiguous "90% CPU" normalization that motivated the performance audit.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import platform
import statistics
import time
from urllib.request import Request, urlopen


SCENARIOS = (
    "idle", "training", "correct", "training-correct", "steady-events", "burst",
)


def percentile(values, q):
    values = sorted(float(v) for v in values)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * float(q)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return values[lo]
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def proc_snapshot(pid):
    out = {
        "pid": int(pid), "cpu_seconds": None, "rss_mb": None,
        "read_bytes": None, "write_bytes": None, "threads": None,
    }
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
        ticks = float(os.sysconf("SC_CLK_TCK"))
        out["cpu_seconds"] = (float(fields[13]) + float(fields[14])) / ticks
    except Exception:
        pass
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                out["rss_mb"] = float(line.split()[1]) / 1024.0
            elif line.startswith("Threads:"):
                out["threads"] = int(line.split()[1])
    except Exception:
        pass
    try:
        for line in Path(f"/proc/{pid}/io").read_text().splitlines():
            key, _, raw = line.partition(":")
            if key == "read_bytes":
                out["read_bytes"] = int(raw.strip())
            elif key == "write_bytes":
                out["write_bytes"] = int(raw.strip())
    except Exception:
        pass
    return out


def find_runtime_pid():
    candidates = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace"
            )
        except Exception:
            continue
        score = 0
        if "queue_main.py" in raw or "fast_queue_main.py" in raw:
            score += 4
        if "adaptive_ai" in raw or "HomeMind" in raw:
            score += 2
        if "python" in raw.lower():
            score += 1
        if score:
            candidates.append((score, int(entry.name), raw))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip("\\x00\\n ")
    except OSError:
        return None


def _meminfo():
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, raw = line.partition(":")
            value = raw.strip().split()
            if value:
                out[key] = float(value[0]) / 1024.0
    except (OSError, ValueError):
        pass
    return out


def host_facts():
    mem = _meminfo()
    model = _read_text("/sys/firmware/devicetree/base/model")
    return {
        "model": model,
        "is_raspberry_pi_4": bool(model and "raspberry pi 4" in model.lower()),
        "machine": platform.machine(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "logical_cpu_count": max(1, int(os.cpu_count() or 1)),
        "mem_total_mb": mem.get("MemTotal"),
    }


def host_runtime_snapshot():
    mem = _meminfo()
    temp = None
    try:
        temp = float(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()) / 1000.0
    except (OSError, ValueError):
        pass
    load1 = None
    try:
        load1 = float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return {
        "temperature_c": temp,
        "mem_available_mb": mem.get("MemAvailable"),
        "load1": load1,
    }


def find_training_worker_pids():
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes().replace(b"\\0", b" ").decode(
                "utf-8", "replace"
            )
        except OSError:
            continue
        if "training_process.py" in raw and "--worker" in raw:
            pids.append(int(entry.name))
    return sorted(set(pids))


def _summary(values):
    values = [float(v) for v in values if isinstance(v, (int, float))]
    if not values:
        return {"p50": None, "p95": None, "p99": None, "max": None, "samples": 0}
    return {
        "p50": round(percentile(values, .50), 3),
        "p95": round(percentile(values, .95), 3),
        "p99": round(percentile(values, .99), 3),
        "max": round(max(values), 3),
        "samples": len(values),
    }


def fetch_status(base_url, timeout=3.0):
    started = time.perf_counter()
    req = Request(base_url.rstrip("/") + "/api/status")
    with urlopen(req, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload, (time.perf_counter() - started) * 1000.0


def fetch_readonly_path(base_url, path, timeout=3.0):
    path = str(path or "").strip()
    if not path.startswith("/api/"):
        raise ValueError("--correct-path must be a read-only /api/... path")
    started = time.perf_counter()
    req = Request(base_url.rstrip("/") + path, method="GET")
    with urlopen(req, timeout=timeout) as response:
        response.read()
    return (time.perf_counter() - started) * 1000.0


def _metric_value(report, path, key="p95"):
    value = report
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    if isinstance(value, dict):
        value = value.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _delta(candidate, baseline):
    if candidate is None or baseline is None:
        return None
    return round(float(candidate) - float(baseline), 4)


def comparison(candidate, baseline):
    """Compare two same-scenario reports without hiding worker CPU in 0.14.80."""
    warnings = []
    if baseline.get("scenario") != candidate.get("scenario"):
        warnings.append("scenario mismatch")
    bdur = float(baseline.get("duration_seconds") or 0.0)
    cdur = float(candidate.get("duration_seconds") or 0.0)
    if bdur and cdur and abs(bdur - cdur) / max(bdur, cdur) > .05:
        warnings.append("duration differs by more than 5%")

    fields = {
        "http_status_p95_ms": (
            _metric_value(candidate, "http_status_latency_ms", "p95"),
            _metric_value(baseline, "http_status_latency_ms", "p95"),
        ),
        "http_status_p99_ms": (
            _metric_value(candidate, "http_status_latency_ms", "p99"),
            _metric_value(baseline, "http_status_latency_ms", "p99"),
        ),
        "correct_http_p95_ms": (
            _metric_value(candidate, "correct_http_latency_ms", "p95"),
            _metric_value(baseline, "correct_http_latency_ms", "p95"),
        ),
        "runtime_cpu_one_core_percent": (
            _metric_value(candidate, "runtime", "cpu_one_core_percent"),
            _metric_value(baseline, "runtime", "cpu_one_core_percent"),
        ),
        "combined_cpu_one_core_percent": (
            _metric_value(candidate, "combined", "cpu_one_core_percent"),
            _metric_value(baseline, "combined", "cpu_one_core_percent"),
        ),
        "runtime_rss_p95_mb": (
            _metric_value(candidate, "runtime", "rss_mb_p95"),
            _metric_value(baseline, "runtime", "rss_mb_p95"),
        ),
        "combined_rss_p95_mb": (
            _metric_value(candidate, "combined", "rss_p95_mb_sum"),
            _metric_value(baseline, "combined", "rss_p95_mb_sum"),
        ),
        "training_progress_delta": (
            _metric_value(candidate, "training_progress", "delta"),
            _metric_value(baseline, "training_progress", "delta"),
        ),
    }
    out = {}
    for name, (cand, base) in fields.items():
        out[name] = {
            "baseline": base,
            "candidate": cand,
            "candidate_minus_baseline": _delta(cand, base),
        }

    shared_runtime = {}
    bmetrics = baseline.get("runtime_latency_metrics") or {}
    cmetrics = candidate.get("runtime_latency_metrics") or {}
    for key in sorted(set(bmetrics) & set(cmetrics)):
        base = (bmetrics.get(key) or {}).get("p95")
        cand = (cmetrics.get(key) or {}).get("p95")
        if isinstance(base, (int, float)) and isinstance(cand, (int, float)):
            shared_runtime[key] = {
                "baseline_p95": float(base),
                "candidate_p95": float(cand),
                "candidate_minus_baseline": _delta(cand, base),
            }
    return {
        "contract": "pi_training_profile_comparison_v1",
        "warnings": warnings,
        "fields": out,
        "shared_runtime_latency_p95": shared_runtime,
        "interpretation": (
            "Negative latency/CPU/RSS delta is lower on candidate; progress delta is "
            "reported raw and should be interpreted with equal data/scenario duration."
        ),
    }


def recursive_metrics(value, prefix=""):
    out = {}
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            low = str(key).lower()
            if isinstance(item, (int, float)) and any(
                token in low
                for token in (
                    "event_to_intent", "event_to_service", "intent_to_service",
                    "p50", "p95", "p99", "backlog", "queue_age",
                )
            ):
                out[path] = float(item)
            out.update(recursive_metrics(item, path))
    elif isinstance(value, list):
        for idx, item in enumerate(value[:20]):
            out.update(recursive_metrics(item, f"{prefix}[{idx}]"))
    return out


def summarize_process(samples, elapsed, cpu_count):
    valid = [x for x in samples if x and x.get("cpu_seconds") is not None]
    if not valid:
        return None
    first, last = valid[0], valid[-1]
    cpu_delta = max(0.0, float(last["cpu_seconds"]) - float(first["cpu_seconds"]))
    one_core = cpu_delta / max(float(elapsed), 1e-9) * 100.0
    rss = [float(x["rss_mb"]) for x in valid if x.get("rss_mb") is not None]
    read_delta = None
    write_delta = None
    if first.get("read_bytes") is not None and last.get("read_bytes") is not None:
        read_delta = max(0, int(last["read_bytes"]) - int(first["read_bytes"]))
    if first.get("write_bytes") is not None and last.get("write_bytes") is not None:
        write_delta = max(0, int(last["write_bytes"]) - int(first["write_bytes"]))
    return {
        "pid": int(last["pid"]),
        "cpu_seconds_delta": round(cpu_delta, 3),
        "cpu_one_core_percent": round(one_core, 2),
        "cpu_host_percent": round(one_core / max(1, int(cpu_count)), 2),
        "cpu_normalization": (
            "one_core_percent=CPU_seconds/wall_seconds*100; "
            "host_percent=one_core_percent/logical_cpu_count"
        ),
        "rss_mb_p50": None if not rss else round(percentile(rss, .50), 3),
        "rss_mb_p95": None if not rss else round(percentile(rss, .95), 3),
        "rss_mb_max": None if not rss else round(max(rss), 3),
        "read_bytes_delta": read_delta,
        "write_bytes_delta": write_delta,
        "threads_last": last.get("threads"),
    }


def run(args):
    runtime_pid = args.runtime_pid or find_runtime_pid()
    if runtime_pid is None:
        raise SystemExit("Could not find Adaptive AI runtime PID; pass --runtime-pid")

    cpu_count = max(1, int(os.cpu_count() or 1))
    started = time.monotonic()
    deadline = started + float(args.duration)
    status_latencies = []
    correct_latencies = []
    last_correct_probe = 0.0
    runtime_samples = []
    worker_samples = []
    status_metrics = []
    progress = []
    versions = set()
    worker_pids = set()
    failures = []
    status_attempts = 0
    status_successes = 0
    consecutive_failures = 0
    max_consecutive_failures = 0
    host_samples = []
    worker_count_samples = []
    scanned_worker_pids = set()
    loop_durations_ms = []

    while time.monotonic() < deadline:
        loop_started = time.monotonic()
        runtime_samples.append(proc_snapshot(runtime_pid))
        host_samples.append(host_runtime_snapshot())
        scanned = find_training_worker_pids()
        scanned_worker_pids.update(scanned)
        worker_count_samples.append(len(scanned))
        status_attempts += 1
        try:
            status, latency = fetch_status(args.base_url, timeout=args.timeout)
            status_successes += 1
            consecutive_failures = 0
            status_latencies.append(latency)
            versions.add(str(status.get("version")))
            status_metrics.append(recursive_metrics(status))
            if (
                args.correct_path
                and time.monotonic() - last_correct_probe >= float(args.correct_interval)
            ):
                try:
                    correct_latencies.append(
                        fetch_readonly_path(
                            args.base_url, args.correct_path, timeout=args.timeout
                        )
                    )
                except Exception as exc:
                    failures.append(
                        f"Correct GET {type(exc).__name__}: {exc}"
                    )
                last_correct_probe = time.monotonic()
            history = status.get("history") or {}
            if history.get("training_overall_progress") is not None:
                progress.append(float(history["training_overall_progress"]))
            worker = history.get("training_process") or {}
            pid = worker.get("pid")
            if pid:
                try:
                    pid = int(pid)
                    worker_pids.add(pid)
                    worker_samples.append(proc_snapshot(pid))
                except (TypeError, ValueError):
                    pass
        except Exception as exc:
            consecutive_failures += 1
            max_consecutive_failures = max(max_consecutive_failures, consecutive_failures)
            failures.append(f"{type(exc).__name__}: {exc}")

        spent = time.monotonic() - loop_started
        loop_durations_ms.append(spent * 1000.0)
        time.sleep(max(0.0, float(args.interval) - spent))

    elapsed = time.monotonic() - started
    merged_metrics = {}
    for row in status_metrics:
        for key, value in row.items():
            merged_metrics.setdefault(key, []).append(value)
    metric_summary = {
        key: {
            "p50": round(percentile(values, .50), 3),
            "p95": round(percentile(values, .95), 3),
            "p99": round(percentile(values, .99), 3),
            "max": round(max(values), 3),
            "samples": len(values),
        }
        for key, values in merged_metrics.items()
        if values
    }

    runtime_summary = summarize_process(runtime_samples, elapsed, cpu_count)
    worker_summary = summarize_process(worker_samples, elapsed, cpu_count)
    runtime_cpu = float((runtime_summary or {}).get("cpu_seconds_delta") or 0.0)
    worker_cpu = float((worker_summary or {}).get("cpu_seconds_delta") or 0.0)
    combined_cpu = runtime_cpu + worker_cpu
    runtime_rss = (runtime_summary or {}).get("rss_mb_p95")
    worker_rss = (worker_summary or {}).get("rss_mb_p95")
    combined = {
        "cpu_seconds_delta": round(combined_cpu, 3),
        "cpu_one_core_percent": round(
            combined_cpu / max(elapsed, 1e-9) * 100.0, 2
        ),
        "cpu_host_percent": round(
            combined_cpu / max(elapsed, 1e-9) * 100.0 / cpu_count, 2
        ),
        "rss_p95_mb_sum": (
            None
            if runtime_rss is None
            else round(float(runtime_rss) + float(worker_rss or 0.0), 3)
        ),
        "read_bytes_delta": int(
            ((runtime_summary or {}).get("read_bytes_delta") or 0)
            + ((worker_summary or {}).get("read_bytes_delta") or 0)
        ),
        "write_bytes_delta": int(
            ((runtime_summary or {}).get("write_bytes_delta") or 0)
            + ((worker_summary or {}).get("write_bytes_delta") or 0)
        ),
    }

    temperatures = [
        row.get("temperature_c") for row in host_samples
        if isinstance(row.get("temperature_c"), (int, float))
    ]
    mem_available = [
        row.get("mem_available_mb") for row in host_samples
        if isinstance(row.get("mem_available_mb"), (int, float))
    ]
    load1 = [
        row.get("load1") for row in host_samples
        if isinstance(row.get("load1"), (int, float))
    ]
    status_failures = max(0, status_attempts - status_successes)
    all_worker_pids = sorted(set(worker_pids) | scanned_worker_pids)

    report = {
        "contract": "pi_training_profile_v2",
        "scenario": args.scenario,
        "release_versions_seen": sorted(versions),
        "duration_seconds": round(elapsed, 3),
        "interval_seconds": float(args.interval),
        "logical_cpu_count": cpu_count,
        "host": host_facts(),
        "host_runtime": {
            "temperature_c_p95": None if not temperatures else round(percentile(temperatures, .95), 3),
            "temperature_c_max": None if not temperatures else round(max(temperatures), 3),
            "mem_available_mb_p05": None if not mem_available else round(percentile(mem_available, .05), 3),
            "mem_available_mb_min": None if not mem_available else round(min(mem_available), 3),
            "load1_p95": None if not load1 else round(percentile(load1, .95), 3),
            "load1_max": None if not load1 else round(max(load1), 3),
        },
        "status_probe": {
            "attempts": status_attempts,
            "successes": status_successes,
            "failures": status_failures,
            "failure_rate": (status_failures / status_attempts) if status_attempts else None,
            "max_consecutive_failures": max_consecutive_failures,
        },
        "probe_loop_latency_ms": _summary(loop_durations_ms),
        "runtime": runtime_summary,
        "worker": worker_summary,
        "combined": combined,
        "worker_pids_seen": all_worker_pids,
        "worker_concurrency": {
            "max": max(worker_count_samples) if worker_count_samples else 0,
            "samples_over_one": sum(1 for value in worker_count_samples if value > 1),
            "pids_seen": all_worker_pids,
        },
        "http_status_latency_ms": {
            "p50": percentile(status_latencies, .50),
            "p95": percentile(status_latencies, .95),
            "p99": percentile(status_latencies, .99),
            "max": max(status_latencies) if status_latencies else None,
            "samples": len(status_latencies),
        },
        "correct_http_latency_ms": {
            "path": args.correct_path,
            "p50": percentile(correct_latencies, .50),
            "p95": percentile(correct_latencies, .95),
            "p99": percentile(correct_latencies, .99),
            "max": max(correct_latencies) if correct_latencies else None,
            "samples": len(correct_latencies),
        },
        "training_progress": {
            "first": progress[0] if progress else None,
            "last": progress[-1] if progress else None,
            "delta": (progress[-1] - progress[0]) if len(progress) >= 2 else None,
        },
        "runtime_latency_metrics": metric_summary,
        "poll_failures": failures[:20],
        "notes": [
            "Run the same scenario/duration on baseline and candidate release.",
            "steady-events and burst are observational labels: generate real HA sensor traffic during collection.",
            "correct and training-correct: open/use the normal Correct UI during collection.",
            "No synthetic HA service calls are generated by this tool.",
            "Stage-9 gate requires host.model to identify a real Raspberry Pi 4 and real HA event traffic for event_to_intent evidence.",
        ],
    }
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        report["comparison_to_baseline"] = comparison(report, baseline)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--interval", type=float, default=.5)
    parser.add_argument("--base-url", default="http://127.0.0.1:8099")
    parser.add_argument("--runtime-pid", type=int)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument(
        "--correct-path",
        help="Optional read-only /api/... GET used to measure Correct HTTP latency",
    )
    parser.add_argument("--correct-interval", type=float, default=2.0)
    parser.add_argument(
        "--baseline",
        help="Optional prior JSON report for same-scenario before/after comparison",
    )
    parser.add_argument("--output")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    report = run(args)
    raw = json.dumps(
        report, ensure_ascii=False, sort_keys=True,
        indent=None if args.compact else 2,
    )
    if args.output:
        Path(args.output).write_text(raw + "\n", encoding="utf-8")
    print(raw)


if __name__ == "__main__":
    main()
