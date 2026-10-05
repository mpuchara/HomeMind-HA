"""Build and verify repository/add-on install ZIPs from tracked source files."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    version = re.search(r'^version: "([^"]+)"', (ROOT / "adaptive_ai/config.yaml").read_text(), re.M).group(1)
    assert f'APP_VERSION = "{version}"' in (ROOT / "adaptive_ai/src/settings.py").read_text()
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    files = [name for name in tracked if name and (ROOT / name).is_file()]
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    for kind, prefix in (("repository", ""), ("addon-root", "adaptive_ai/")):
        path = output / f"HomeMind-Adaptive-AI-{version}-{kind}.zip"
        contents = {name[len(prefix):]: (ROOT / name).read_bytes()
                    for name in files if not prefix or name.startswith(prefix)}
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
            for required in ("config.yaml", "Dockerfile", "src/run.sh", "src/training_quality.py", "src/training_quality_benchmark.py"):
                assert addon + required in archive.namelist()
            for name, data in contents.items():
                assert archive.read(name) == data
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        path.with_suffix(".zip.sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")
        print(json.dumps({"version": version, "archive": str(path), "sha256": digest, "files": len(contents)}))


if __name__ == "__main__":
    main()
