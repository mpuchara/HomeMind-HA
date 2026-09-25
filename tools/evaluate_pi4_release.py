#!/usr/bin/env python3
"""Developer wrapper for the Stage-9 Raspberry Pi 4 release gate."""
from pathlib import Path
import sys

SRC = Path(__file__).resolve().parents[1] / "adaptive_ai" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pi4_release_gate import main

if __name__ == "__main__":
    main()
