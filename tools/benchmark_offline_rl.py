#!/usr/bin/env python3
"""Repository wrapper for Stage-7 conservative Offline-RL benchmark."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from offline_rl_benchmark import main


if __name__ == "__main__":
    main()
