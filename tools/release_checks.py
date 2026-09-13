"""Validate and package a source release. Does not contact Home Assistant."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.10.1"
ENV = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")


def run(*args):
    result = subprocess.run(args, cwd=ROOT, env=ENV, capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"{args!r}\n{result.stdout}\n{result.stderr}")
    return result


def main():
    docs = ROOT/"docs"
    result = run(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v")
    log = result.stdout + result.stderr
    match = re.search(r"Ran (\d+) tests", log)
    if not match or not log.rstrip().endswith("OK"):
        raise RuntimeError("Missing successful unittest report")
    count = int(match[1])
    (docs/"TEST_RESULTS_0_10_1.txt").write_text(log, encoding="utf-8")
    run(sys.executable, "-m", "compileall", "-q", "adaptive_ai/src")
    for name in ("app.js", "home.js", "settings.js", "p0.js", "queue.js", "experiments.js"):
        run("node", "--check", str(ROOT/"adaptive_ai/src/static"/name))
    for tool, artifact in (("simulate_anticipation.py", "SIMULATOR_0_10_1.json"),
                           ("benchmark.py", "BENCHMARK_LOCAL_0_10_1.json")):
        data = json.loads(run(sys.executable, str(ROOT/"tools"/tool)).stdout)
        (docs/artifact).write_text(json.dumps(data, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")

    info = json.loads((ROOT/"adaptive_ai/BUILD_INFO.json").read_text(encoding="utf-8"))
    assert info["version"] == VERSION and info["tests_passed"] == count
    assert f'version: "{VERSION}"' in (ROOT/"adaptive_ai/config.yaml").read_text()
    settings = ast.parse((ROOT/"adaptive_ai/src/settings.py").read_text())
    version = next(ast.literal_eval(node.value) for node in settings.body
                   if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "APP_VERSION" for t in node.targets))
    assert version == VERSION
    service_calls = []
    for path in (ROOT/"adaptive_ai/src").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "HA"
                and node.func.attr == "service"):
                service_calls.append((path.name, node.lineno))
    assert len(service_calls) == 1 and service_calls[0][0] == "executor.py", service_calls
    assert b"\r" not in (ROOT/"adaptive_ai/src/run.sh").read_bytes(), "Linux entrypoint requires LF"

    paths = []
    allowed_suffixes = {".py", ".js", ".html", ".css", ".md", ".json", ".yaml", ".yml", ".sh", ".txt"}
    for folder in ("adaptive_ai", "docs", "tests", "tools", ".github/workflows"):
        for path in (ROOT/folder).rglob("*"):
            if (path.is_file() and "__pycache__" not in path.parts
                and (path.suffix in allowed_suffixes or path.name == "Dockerfile")):
                paths.append(path)
    for name in ("README.md", "INSTALACJA_PL.md", "ARCHITECTURE.md", "TEST_REPORT.md",
                 "repository.yaml", ".gitattributes", ".gitignore"):
        paths.append(ROOT/name)
    payloads = {p.relative_to(ROOT).as_posix(): p.read_bytes() for p in sorted(set(paths))}
    manifest = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(payloads.items()))
    dist = ROOT/"dist"
    dist.mkdir(exist_ok=True)
    archive = dist/f"HomeMind-HA-{VERSION}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for name, data in sorted(payloads.items()):
            item = zipfile.ZipInfo(name, (2026, 9, 13, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.create_system = 3
            item.external_attr = (0o100755 if name.endswith(".sh") else 0o100644) << 16
            output.writestr(item, data)
        output.writestr("MANIFEST.sha256", manifest)
    with zipfile.ZipFile(archive) as checked:
        assert checked.testzip() is None
        assert len(checked.namelist()) == len(payloads)+1
        for name, expected in payloads.items():
            assert hashlib.sha256(checked.read(name)).digest() == hashlib.sha256(expected).digest()
        assert json.loads(checked.read("adaptive_ai/BUILD_INFO.json"))["tests_passed"] == count
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(json.dumps({"tests_passed": count, "python_compile": "passed", "javascript": "passed",
                      "service_calls": service_calls, "zip": str(archive), "files": len(payloads)+1,
                      "bytes": archive.stat().st_size, "sha256": digest}, indent=2))


if __name__ == "__main__":
    main()
