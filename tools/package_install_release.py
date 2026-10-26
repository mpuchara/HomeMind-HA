"""Build and verify repository/add-on install ZIPs from tracked source files."""
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    # Read committed bytes rather than the checkout (which can have CRLF or local
    # edits). The packaged commit is explicit and reproducible, including run.sh.
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
    raw = subprocess.check_output(["git", "archive", "--format=zip", commit], cwd=ROOT)
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        tracked = {name: source.read(name) for name in source.namelist() if not name.endswith('/')}
    version = re.search(r'^version: "([^"]+)"', tracked['adaptive_ai/config.yaml'].decode(), re.M).group(1)
    assert f'APP_VERSION = "{version}"' in tracked['adaptive_ai/src/settings.py'].decode()
    assert json.loads(tracked['adaptive_ai/BUILD_INFO.json'])['version'] == version
    assert f'ARG BUILD_VERSION={version}' in tracked['adaptive_ai/Dockerfile'].decode()
    assert b'\r' not in tracked['adaptive_ai/src/run.sh']
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    for kind, prefix in (("repository", ""), ("addon", "adaptive_ai/")):
        path = output / f"HomeMind-HA-{version}-{kind}.zip"
        contents = {name[len(prefix):]: data
                    for name, data in tracked.items() if not prefix or name.startswith(prefix)}
        contents['SOURCE_COMMIT.txt'] = (commit+'\n').encode('ascii')
        manifest = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(contents.items()))
        contents["MANIFEST.sha256"] = manifest.encode()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in sorted(contents.items()):
                info = zipfile.ZipInfo(name, (2026, 10, 5, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (0o100755 if name.endswith(".sh") else 0o100644) << 16
                archive.writestr(info, data)
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            addon = "adaptive_ai/" if kind == "repository" else ""
            for required in ("config.yaml", "Dockerfile", "src/run.sh", "src/training_quality.py", "src/training_quality_benchmark.py", "src/binary_state_classifier.py", "src/observation_contract.py"):
                assert addon + required in archive.namelist()
            for name, data in contents.items():
                assert archive.read(name) == data
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        path.with_suffix(".zip.sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")
        print(json.dumps({"version": version, "source_commit": commit, "archive": str(path), "sha256": digest, "files": len(contents)}))


if __name__ == "__main__":
    main()
