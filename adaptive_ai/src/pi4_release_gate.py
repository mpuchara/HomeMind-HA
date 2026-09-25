#!/usr/bin/env python3
"""Stage 9 Raspberry Pi 4 release/readiness gate.

This module evaluates real reports produced by pi_training_profile.py. It does not
simulate Raspberry Pi performance and it never changes agent authority. A green gate
is evidence that the runtime is ready for a *separate* controlled-rollout decision;
Stage-7 Offline-RL promotion remains blocked by its existing safety contract.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


CONTRACT = "pi4_release_gate_v1"
REQUIRED_SCENARIOS = ("idle", "training", "correct", "training-correct")
DEFAULT_THRESHOLDS = {
    "min_duration_seconds": 60.0,
    "status_p95_ms": 500.0,
    "status_p99_ms": 1000.0,
    "correct_p95_ms": 500.0,
    "correct_p99_ms": 1000.0,
    "event_to_intent_p95_ms": 500.0,
    "max_realtime_p95_ratio_vs_idle": 2.0,
    "max_status_failure_rate": 0.01,
    "max_consecutive_status_failures": 1,
    "max_combined_host_cpu_percent": 50.0,
    "max_system_cpu_p95_percent": 90.0,
    "max_disconnect_rate": 0.01,
    "max_consecutive_disconnect_samples": 1,
    "max_combined_rss_p95_mb": 768.0,
    "max_training_workers": 1,
    "min_available_memory_mb": 256.0,
    "max_temperature_c": 80.0,
}


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _nested(mapping, *keys):
    value = mapping
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _metric(report, section, key):
    return _number(_nested(report, section, key))


def _runtime_metric(report, token, percentile="p95"):
    """Return the conservative matching runtime metric from profiler summaries."""
    rows = report.get("runtime_latency_metrics") or {}
    matches = []
    for name, summary in rows.items():
        low = str(name).lower()
        if token.lower() not in low:
            continue
        value = _number((summary or {}).get(percentile))
        if value is None:
            continue
        # Prefer the telemetry's own p95/p99 series when it is present.
        score = 2 if low.endswith("." + percentile) else 1
        matches.append((score, value, str(name)))
    if not matches:
        return None, None
    preferred = [row for row in matches if row[0] == max(x[0] for x in matches)]
    value = max(row[1] for row in preferred)
    names = sorted(row[2] for row in preferred if row[1] == value)
    return value, names[0] if names else None


def _versions(report):
    return tuple(str(x) for x in (report.get("release_versions_seen") or ()) if x)


def _pi4_model(report):
    host = report.get("host") or {}
    return str(host.get("model") or "").strip()


def _status_failure_rate(report):
    probe = report.get("status_probe") or {}
    value = _number(probe.get("failure_rate"))
    if value is not None:
        return value
    attempts = _number(probe.get("attempts"))
    failures = _number(probe.get("failures"))
    if attempts and failures is not None:
        return failures / attempts
    legacy_failures = report.get("poll_failures") or []
    samples = _number(_nested(report, "http_status_latency_ms", "samples"))
    if samples is not None:
        total = samples + len(legacy_failures)
        if total > 0:
            return len(legacy_failures) / total
    return None


def _check(checks, check_id, status, *, value=None, limit=None, detail=None):
    row = {"id": check_id, "status": status}
    if value is not None:
        row["value"] = value
    if limit is not None:
        row["limit"] = limit
    if detail:
        row["detail"] = detail
    checks.append(row)
    return row


def evaluate_reports(reports, thresholds=None):
    thresholds = {**DEFAULT_THRESHOLDS, **dict(thresholds or {})}
    reports = {str(k): dict(v or {}) for k, v in dict(reports or {}).items()}
    checks = []
    warnings = []

    missing = [name for name in REQUIRED_SCENARIOS if name not in reports]
    _check(
        checks,
        "required_scenarios",
        "pass" if not missing else "inconclusive",
        detail=("all required scenarios present" if not missing else "missing: " + ", ".join(missing)),
    )

    profile_contracts = {
        scenario: str(report.get("contract") or "")
        for scenario, report in reports.items()
    }
    bad_contracts = {
        scenario: value for scenario, value in profile_contracts.items()
        if value != "pi_training_profile_v2"
    }
    _check(
        checks,
        "profile_contract_v2",
        "pass" if not bad_contracts else "inconclusive",
        value=profile_contracts,
        detail=None if not bad_contracts else "all Stage-9 reports must use pi_training_profile_v2",
    )

    version_rows = {
        scenario: _versions(report) for scenario, report in reports.items()
    }
    versions = sorted({v for values in version_rows.values() for v in values})
    complete_versions = bool(version_rows) and all(len(values) == 1 for values in version_rows.values())
    if complete_versions and len(versions) == 1:
        _check(checks, "single_release_version", "pass", value=versions[0])
    elif versions and complete_versions:
        _check(checks, "single_release_version", "fail", value=version_rows, detail="all profiles must measure the same release")
    else:
        _check(checks, "single_release_version", "inconclusive", value=version_rows, detail="every profile must record exactly one release version")

    model_rows = {scenario: _pi4_model(report) for scenario, report in reports.items()}
    models = sorted({model for model in model_rows.values() if model})
    if model_rows and all(
        model and "raspberry pi 4" in model.lower() for model in model_rows.values()
    ):
        _check(checks, "raspberry_pi_4_hardware", "pass", value=model_rows)
    elif all(model_rows.values()):
        _check(checks, "raspberry_pi_4_hardware", "fail", value=model_rows, detail="Stage 9 gate is specific to Raspberry Pi 4")
    else:
        _check(checks, "raspberry_pi_4_hardware", "inconclusive", value=model_rows, detail="every profiler report must identify host.model")

    scenario_mismatches = {
        expected: str(report.get("scenario") or "")
        for expected, report in reports.items()
        if str(report.get("scenario") or "") != expected
    }
    _check(
        checks,
        "scenario_identity",
        "pass" if not scenario_mismatches else "fail",
        value=scenario_mismatches or None,
        detail=None if not scenario_mismatches else "profile scenario labels do not match gate inputs",
    )

    for scenario in REQUIRED_SCENARIOS:
        report = reports.get(scenario)
        if report is None:
            continue
        duration = _number(report.get("duration_seconds"))
        _check(
            checks,
            f"{scenario}.duration",
            "pass" if duration is not None and duration >= thresholds["min_duration_seconds"] else "inconclusive",
            value=duration,
            limit={"min": thresholds["min_duration_seconds"]},
        )

        failure_rate = _status_failure_rate(report)
        if failure_rate is None:
            status = "inconclusive"
        else:
            status = "pass" if failure_rate <= thresholds["max_status_failure_rate"] else "fail"
        _check(
            checks,
            f"{scenario}.status_failure_rate",
            status,
            value=failure_rate,
            limit={"max": thresholds["max_status_failure_rate"]},
        )

        consecutive = _number(_nested(report, "status_probe", "max_consecutive_failures"))
        if consecutive is None:
            warnings.append(f"{scenario}: max consecutive status failures unavailable in legacy profile")
        else:
            _check(
                checks,
                f"{scenario}.max_consecutive_status_failures",
                "pass" if consecutive <= thresholds["max_consecutive_status_failures"] else "fail",
                value=consecutive,
                limit={"max": thresholds["max_consecutive_status_failures"]},
            )

        for percentile, threshold_key in (("p95", "status_p95_ms"), ("p99", "status_p99_ms")):
            value = _number(_nested(report, "http_status_latency_ms", percentile))
            _check(
                checks,
                f"{scenario}.http_status_{percentile}",
                "inconclusive" if value is None else ("pass" if value <= thresholds[threshold_key] else "fail"),
                value=value,
                limit={"max_ms": thresholds[threshold_key]},
            )

        host_cpu = _number(_nested(report, "combined", "cpu_host_percent"))
        if host_cpu is not None:
            _check(
                checks,
                f"{scenario}.combined_host_cpu",
                "pass" if host_cpu <= thresholds["max_combined_host_cpu_percent"] else "fail",
                value=host_cpu,
                limit={"max_percent": thresholds["max_combined_host_cpu_percent"]},
            )
        else:
            _check(checks, f"{scenario}.combined_host_cpu", "inconclusive")

        system_cpu = _number(_nested(report, "host_runtime", "system_cpu_percent_p95"))
        _check(
            checks,
            f"{scenario}.system_cpu_p95",
            "inconclusive" if system_cpu is None else (
                "pass" if system_cpu <= thresholds["max_system_cpu_p95_percent"] else "fail"
            ),
            value=system_cpu,
            limit={"max_percent": thresholds["max_system_cpu_p95_percent"]},
        )

        for kind in ("ha", "realtime"):
            rate = _number(_nested(report, "connectivity", f"{kind}_disconnect_rate"))
            consecutive = _number(
                _nested(report, "connectivity", f"{kind}_max_consecutive_disconnect_samples")
            )
            _check(
                checks,
                f"{scenario}.{kind}_disconnect_rate",
                "inconclusive" if rate is None else (
                    "pass" if rate <= thresholds["max_disconnect_rate"] else "fail"
                ),
                value=rate,
                limit={"max": thresholds["max_disconnect_rate"]},
            )
            _check(
                checks,
                f"{scenario}.{kind}_max_consecutive_disconnect_samples",
                "inconclusive" if consecutive is None else (
                    "pass"
                    if consecutive <= thresholds["max_consecutive_disconnect_samples"]
                    else "fail"
                ),
                value=consecutive,
                limit={"max": thresholds["max_consecutive_disconnect_samples"]},
            )

        rss = _number(_nested(report, "combined", "rss_p95_mb_sum"))
        _check(
            checks,
            f"{scenario}.combined_rss_p95",
            "inconclusive" if rss is None else ("pass" if rss <= thresholds["max_combined_rss_p95_mb"] else "fail"),
            value=rss,
            limit={"max_mb": thresholds["max_combined_rss_p95_mb"]},
        )

        workers = _number(_nested(report, "worker_concurrency", "max"))
        if scenario in ("training", "training-correct"):
            _check(
                checks,
                f"{scenario}.single_training_worker",
                "inconclusive" if workers is None else ("pass" if workers <= thresholds["max_training_workers"] else "fail"),
                value=workers,
                limit={"max": thresholds["max_training_workers"]},
            )
            evidence = bool(report.get("worker_pids_seen")) or (
                (_number(_nested(report, "training_progress", "delta")) or 0.0) > 0.0
            )
            _check(
                checks,
                f"{scenario}.training_observed",
                "pass" if evidence else "inconclusive",
                detail="worker PID or positive training progress required",
            )

        min_mem = _number(_nested(report, "host_runtime", "mem_available_mb_min"))
        if min_mem is not None:
            _check(
                checks,
                f"{scenario}.available_memory",
                "pass" if min_mem >= thresholds["min_available_memory_mb"] else "fail",
                value=min_mem,
                limit={"min_mb": thresholds["min_available_memory_mb"]},
            )

        temp = _number(_nested(report, "host_runtime", "temperature_c_max"))
        if temp is not None:
            _check(
                checks,
                f"{scenario}.temperature",
                "pass" if temp <= thresholds["max_temperature_c"] else "fail",
                value=temp,
                limit={"max_c": thresholds["max_temperature_c"]},
            )

    for scenario in ("correct", "training-correct"):
        report = reports.get(scenario)
        if report is None:
            continue
        samples = _number(_nested(report, "correct_http_latency_ms", "samples")) or 0.0
        for percentile, threshold_key in (("p95", "correct_p95_ms"), ("p99", "correct_p99_ms")):
            value = _number(_nested(report, "correct_http_latency_ms", percentile))
            status = "inconclusive"
            if value is not None and samples >= 5:
                status = "pass" if value <= thresholds[threshold_key] else "fail"
            _check(
                checks,
                f"{scenario}.correct_http_{percentile}",
                status,
                value=value,
                limit={"max_ms": thresholds[threshold_key], "min_samples": 5},
                detail=None if samples >= 5 else f"only {int(samples)} Correct HTTP samples",
            )

    realtime = {}
    for scenario in REQUIRED_SCENARIOS:
        report = reports.get(scenario)
        if report is None:
            continue
        value, source = _runtime_metric(report, "event_to_intent", "p95")
        realtime[scenario] = value
        status = "inconclusive" if value is None else (
            "pass" if value <= thresholds["event_to_intent_p95_ms"] else "fail"
        )
        _check(
            checks,
            f"{scenario}.event_to_intent_p95",
            status,
            value=value,
            limit={"max_ms": thresholds["event_to_intent_p95_ms"]},
            detail=(f"source={source}" if source else "generate real HA state changes during this scenario"),
        )

    idle_rt = realtime.get("idle")
    for scenario in ("training", "training-correct"):
        value = realtime.get(scenario)
        ratio = None
        if idle_rt is not None and value is not None and idle_rt > 0.0:
            ratio = value / idle_rt
        _check(
            checks,
            f"{scenario}.realtime_ratio_vs_idle",
            "inconclusive" if ratio is None else (
                "pass" if ratio <= thresholds["max_realtime_p95_ratio_vs_idle"] else "fail"
            ),
            value=ratio,
            limit={"max_ratio": thresholds["max_realtime_p95_ratio_vs_idle"]},
        )

    failures = [row["id"] for row in checks if row["status"] == "fail"]
    inconclusive = [row["id"] for row in checks if row["status"] == "inconclusive"]
    decision = "fail" if failures else ("inconclusive" if inconclusive else "pass")

    return {
        "contract": CONTRACT,
        "decision": decision,
        "required_scenarios": list(REQUIRED_SCENARIOS),
        "release_versions": versions,
        "hardware_models": models,
        "thresholds": thresholds,
        "checks": checks,
        "failures": failures,
        "inconclusive": inconclusive,
        "warnings": warnings,
        "controlled_rollout": {
            "eligible": decision == "pass",
            "scope": "runtime_canary_evidence_only",
            "offline_rl_control_unlock": False,
            "reason": (
                "A green Pi4 gate is required before a separate explicit controlled-rollout change. "
                "Stage-7 Offline-RL Candidate promotion remains blocked in this release."
            ),
        },
    }


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Evaluate the Stage-9 Raspberry Pi 4 profile suite")
    parser.add_argument("--idle", required=True)
    parser.add_argument("--training", required=True)
    parser.add_argument("--correct", required=True)
    parser.add_argument("--training-correct", required=True)
    parser.add_argument("--output")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--allow-inconclusive", action="store_true",
                        help="Return exit code 0 for an inconclusive gate; fail still returns non-zero")
    args = parser.parse_args()
    reports = {
        "idle": _load(args.idle),
        "training": _load(args.training),
        "correct": _load(args.correct),
        "training-correct": _load(args.training_correct),
    }
    result = evaluate_reports(reports)
    raw = json.dumps(result, ensure_ascii=False, sort_keys=True,
                     indent=None if args.compact else 2)
    if args.output:
        Path(args.output).write_text(raw + "\n", encoding="utf-8")
    print(raw)
    if result["decision"] == "fail":
        raise SystemExit(2)
    if result["decision"] == "inconclusive" and not args.allow_inconclusive:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
