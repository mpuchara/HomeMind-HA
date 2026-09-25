"""Isolated process runner for Stage-7 conservative Offline RL.

Realtime remains authoritative. The worker receives only an immutable serialized TinyMLP
and already-filtered offline rows; it has no Store, Executor or Home Assistant access.
The parent validates the source checksum/revision again before publishing the returned
child artifact.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from policy_tiny_mlp import TinyMLPBackend
from policy_tiny_mlp_offline_rl import (
    offline_rl_gate,
    train_conservative_offline_rl,
)


FORMAT = "homemind-offline-rl-job"
VERSION = 1
RESULT_FORMAT = "homemind-offline-rl-result"


def _canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    )


def _atomic(path, payload):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(_canonical(payload), encoding="utf-8")
    os.replace(temp, path)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _rss_mb(pid):
    try:
        text = Path(f"/proc/{int(pid)}/status").read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("VmRSS:"):
                return float(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return None


def _terminate(process, grace=2.0):
    if process.poll() is not None:
        return
    process.terminate()
    deadline = time.monotonic() + max(.2, float(grace))
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(.05)
    if process.poll() is None:
        process.kill()


def _deserialize(raw):
    return TinyMLPBackend.deserialize(
        raw,
        expected_schema_id=str(raw["schema_id"]),
        expected_mask_id=str(raw["mask_id"]),
        expected_feature_ids=tuple(str(x) for x in raw["feature_ids"]),
        expected_actions=tuple(float(x) for x in raw["actions"]),
        expected_horizons=tuple(int(x) for x in raw["horizons"]),
    )


def _worker(job_path):
    job = _read(job_path)
    if job.get("format") != FORMAT or int(job.get("version") or 0) != VERSION:
        raise ValueError("unsupported Offline-RL isolated job contract")
    nice = max(0, min(19, int(job.get("worker_nice") or 10)))
    try:
        os.nice(nice)
    except OSError:
        pass

    parent = _deserialize(dict(job["parent_model"]))
    child, trainer = train_conservative_offline_rl(
        parent,
        list(job.get("train_rows") or ()),
        manual_samples=list(job.get("manual_rows") or ()),
        **dict(job.get("trainer_options") or {}),
    )
    gate = offline_rl_gate(
        parent,
        child,
        train_rows=list(job.get("train_rows") or ()),
        holdout_rows=list(job.get("holdout_rows") or ()),
        manual_samples=list(job.get("manual_rows") or ()),
        **dict(job.get("gate_options") or {}),
    )
    result = {
        "format": RESULT_FORMAT,
        "version": VERSION,
        "job_id": job["job_id"],
        "ok": True,
        "parent_model_revision": str(parent.model_revision),
        "parent_model_checksum": parent.serialize().get("model_checksum"),
        "child_model": child.serialize(),
        "trainer": trainer,
        "gate": gate,
        "worker_pid": os.getpid(),
    }
    _atomic(job["result_path"], result)
    return 0


def run_isolated_offline_rl(
    parent,
    *,
    train_rows,
    holdout_rows,
    manual_rows,
    trainer_options,
    gate_options,
    stop_event=None,
    memory_limit_mb=520,
    timeout_seconds=120,
    worker_nice=10,
    poll_seconds=.10,
):
    """Run one bounded RL update in a clean Python process and return child+reports."""
    parent_raw = parent.serialize()
    parent_checksum = parent_raw.get("model_checksum")
    job_id = uuid.uuid4().hex[:16]
    memory_limit_mb = max(64.0, float(memory_limit_mb))
    timeout_seconds = max(5.0, float(timeout_seconds))
    poll_seconds = max(.05, min(1.0, float(poll_seconds)))

    with tempfile.TemporaryDirectory(prefix="hm-offline-rl-") as root:
        root = Path(root)
        job_path = root / f"{job_id}.job.json"
        result_path = root / f"{job_id}.result.json"
        log_path = root / f"{job_id}.log"
        job = {
            "format": FORMAT,
            "version": VERSION,
            "job_id": job_id,
            "parent_model": parent_raw,
            "train_rows": list(train_rows or ()),
            "holdout_rows": list(holdout_rows or ()),
            "manual_rows": list(manual_rows or ()),
            "trainer_options": dict(trainer_options or {}),
            "gate_options": dict(gate_options or {}),
            "worker_nice": int(worker_nice),
            "result_path": str(result_path),
        }
        _atomic(job_path, job)

        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        log_handle = open(log_path, "ab", buffering=0)
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", str(job_path)],
            cwd=str(Path(__file__).resolve().parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_handle.close()
        started = time.monotonic()
        peak_rss = 0.0
        memory_exceeded = False
        cancelled = False
        timed_out = False
        while process.poll() is None:
            rss = _rss_mb(process.pid)
            if rss is not None:
                peak_rss = max(peak_rss, float(rss))
                if float(rss) > memory_limit_mb:
                    memory_exceeded = True
                    _terminate(process)
                    break
            if stop_event is not None and stop_event.is_set():
                cancelled = True
                _terminate(process)
                break
            if time.monotonic() - started > timeout_seconds:
                timed_out = True
                _terminate(process)
                break
            time.sleep(poll_seconds)

        return_code = process.wait(timeout=4.0)
        wall = time.monotonic() - started
        if cancelled:
            raise InterruptedError("Offline-RL isolated worker cancelled")
        if memory_exceeded:
            raise MemoryError(
                f"Offline-RL worker exceeded {memory_limit_mb:.0f} MB RSS"
            )
        if timed_out:
            raise TimeoutError(
                f"Offline-RL worker exceeded {timeout_seconds:.0f}s timeout"
            )
        if return_code != 0 or not result_path.exists():
            try:
                detail = log_path.read_text(encoding="utf-8")[-4000:]
            except OSError:
                detail = ""
            raise RuntimeError(
                "Offline-RL isolated worker failed"
                + (": " + detail if detail else "")
            )
        result = _read(result_path)
        if (
            result.get("format") != RESULT_FORMAT
            or result.get("job_id") != job_id
            or not result.get("ok")
        ):
            raise RuntimeError("Offline-RL isolated result contract mismatch")
        if str(result.get("parent_model_checksum") or "") != str(parent_checksum or ""):
            raise RuntimeError("Offline-RL worker parent checksum mismatch")

        child = _deserialize(dict(result["child_model"]))
        trainer = dict(result.get("trainer") or {})
        gate = dict(result.get("gate") or {})
        process_meta = {
            "contract": "offline_rl_isolated_process_v1",
            "job_id": job_id,
            "worker_pid": result.get("worker_pid"),
            "wall_seconds": round(wall, 4),
            "peak_rss_mb": round(peak_rss, 3),
            "memory_limit_mb": float(memory_limit_mb),
            "timeout_seconds": float(timeout_seconds),
            "worker_nice": int(worker_nice),
            "parent_model_checksum": parent_checksum,
            "online_exploration": False,
            "physical_authority": False,
        }
        trainer["process"] = process_meta
        gate["process"] = process_meta
        return child, trainer, gate


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker")
    args = parser.parse_args(argv)
    if not args.worker:
        parser.error("--worker is required")
    try:
        return _worker(args.worker)
    except Exception as exc:
        try:
            job = _read(args.worker)
            _atomic(
                job.get("result_path"),
                {
                    "format": RESULT_FORMAT,
                    "version": VERSION,
                    "job_id": job.get("job_id"),
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())
