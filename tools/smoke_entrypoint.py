#!/usr/bin/env python3
"""Boot the exact shipped Python entrypoint from source and require HTTP readiness."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"
ENTRY = SRC / "trial_queue_main.py"
RUN_SH = SRC / "run.sh"


def main():
    run_text = RUN_SH.read_text(encoding="utf-8")
    if "/app/trial_queue_main.py" not in run_text:
        raise SystemExit("run.sh no longer points at trial_queue_main.py")
    env = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="homemind-entrypoint-") as data:
        env["ADAPTIVE_AI_DATA"] = data
        env.pop("SUPERVISOR_TOKEN", None)
        proc = subprocess.Popen(
            [sys.executable, "-u", str(ENTRY)], cwd=str(SRC), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        last = "no HTTP response"
        try:
            deadline = time.monotonic() + 45.0
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    output = proc.stdout.read() if proc.stdout else ""
                    raise SystemExit(f"entrypoint exited {proc.returncode}:\n{output}")
                try:
                    with urllib.request.urlopen("http://127.0.0.1:8099/health", timeout=1.0) as response:
                        status = json.load(response)
                    last = status
                    if status.get("startup", {}).get("error"):
                        raise SystemExit(json.dumps(status))
                    if status.get("ready"):
                        print(json.dumps({"entrypoint": str(ENTRY.relative_to(ROOT)), "health": status}, sort_keys=True))
                        return 0
                except OSError as exc:
                    last = str(exc)
                time.sleep(0.15)
            output = proc.stdout.read() if proc.stdout else ""
            raise SystemExit(f"source entrypoint did not become ready: {last}\n{output}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
