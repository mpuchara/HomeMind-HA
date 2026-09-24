#!/usr/bin/env python3
"""Repository wrapper for the packaged Stage-6 Automatic Correct benchmark."""
from pathlib import Path
import os
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "adaptive_ai" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Importing storage constructs its compatibility global Store immediately. Host-side
# benchmark runs do not have the add-on's /data mount, so bind a disposable data root
# before importing any packaged application module.
_HOST_DATA = tempfile.TemporaryDirectory(prefix="hm-auto-correct-host-")
os.environ.setdefault("ADAPTIVE_AI_DATA", _HOST_DATA.name)

from automatic_correct_benchmark import main


if __name__ == "__main__":
    main()
