#!/usr/bin/env python3
"""Repository wrapper for the full hybrid inference hot-path baseline."""
from pathlib import Path
import os
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/"adaptive_ai"/"src"
if str(SRC) not in sys.path:
    sys.path.insert(0,str(SRC))

# Importing the production observation contract initializes Store. The benchmark must
# never touch the add-on's real /data and GitHub runners cannot write there anyway.
_SCRATCH=tempfile.TemporaryDirectory(prefix="homemind-hybrid-benchmark-")
os.environ["ADAPTIVE_AI_DATA"]=_SCRATCH.name

from hybrid_inference_benchmark import main

if __name__=="__main__":
    try:
        main()
    finally:
        _SCRATCH.cleanup()
