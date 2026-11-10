"""Run the same isolated production training worker fixture in either checkout."""
import argparse
import json
from pathlib import Path
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=root, help="HomeMind checkout")
    parser.add_argument("--samples", type=int, default=1200)
    args = parser.parse_args()
    if args.samples < 24:
        parser.error("--samples must be at least 24")
    src = args.source.resolve() / "adaptive_ai" / "src"
    if not (src / "training_process.py").is_file():
        parser.error("--source must contain adaptive_ai/src/training_process.py")

    sys.path.insert(0, str(root / "tests"))
    import support  # Isolate the parent Store before importing the worker fixture.

    sys.path.insert(0, str(src))
    from test_release_080_training_process import ActualWorkerSmoke

    print(json.dumps(ActualWorkerSmoke().run_worker_fixture(args.samples), sort_keys=True))


if __name__ == "__main__":
    main()
