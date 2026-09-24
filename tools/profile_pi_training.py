#!/usr/bin/env python3
"""Developer wrapper for the profiler shipped inside the Adaptive AI add-on."""
from pathlib import Path
import sys

SRC = Path(__file__).resolve().parents[1] / "adaptive_ai" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pi_training_profile import main


if __name__ == "__main__":
    main()
