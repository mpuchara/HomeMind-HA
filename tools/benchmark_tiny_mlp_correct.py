#!/usr/bin/env python3
"""Repository wrapper for the packaged Stage-5 incremental Tiny MLP Correct benchmark."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tiny_mlp_correct_benchmark import main


if __name__ == "__main__":
    main()
