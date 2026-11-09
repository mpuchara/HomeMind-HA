"""Verify reproducible, test-only Pi installation artifacts (no HA access)."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FLAGS = ("universal_observer_enabled", "universal_action_observer_enabled", "universal_training_collector_enabled")


def check(expected_version):
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    out = ROOT / "dist"
    packages = {}
    for kind in ("repository", "addon"):
        path = out / f"HomeMind-HA-{expected_version}-{kind}.zip"
        check_path = path.with_suffix(".zip.sha256")
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        assert check_path.read_text(encoding="ascii") == f"{expected}  {path.name}\n"
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            names = set(archive.namelist())
            assert archive.read("SOURCE_COMMIT.txt") == (commit + "\n").encode("ascii")
            manifest = archive.read("MANIFEST.sha256").decode("ascii")
            manifest_names = set()
            for row in manifest.splitlines():
                digest, name = row.split("  ", 1)
                assert re.fullmatch(r"[0-9a-f]{64}", digest)
                assert name not in manifest_names
                manifest_names.add(name)
                assert hashlib.sha256(archive.read(name)).hexdigest() == digest, name
            assert names == manifest_names | {"MANIFEST.sha256"}, (kind, len(names), len(manifest_names))
            prefix = "adaptive_ai/" if kind == "repository" else ""
            config = archive.read(prefix+"config.yaml").decode()
            settings = archive.read(prefix+"src/settings.py").decode()
            dockerfile = archive.read(prefix+"Dockerfile").decode()
            info = json.loads(archive.read(prefix+"BUILD_INFO.json"))
            assert f'version: "{expected_version}"' in config
            assert f'APP_VERSION = "{expected_version}"' in settings
            assert f"ARG BUILD_VERSION={expected_version}" in dockerfile
            assert info["version"] == expected_version
            assert info["release_status"].startswith("pi5-manual-validation-candidate")
            assert "  - aarch64" in config
            for flag in FLAGS:
                assert f"  {flag}: false" in config
            assert prefix+"src/run.sh" in names
            assert b"\r" not in archive.read(prefix+"src/run.sh")
            packages[kind] = {name[len(prefix):]: archive.read(name)
                              for name in names if name.startswith(prefix) and name not in ("MANIFEST.sha256", "SOURCE_COMMIT.txt")}
        print(json.dumps({"kind": kind, "path": str(path), "sha256": expected, "source": commit, "files": len(names)}, sort_keys=True))
    for name, data in packages["addon"].items():
        assert packages["repository"].get(name) == data, name
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    check(args.expected_version)
