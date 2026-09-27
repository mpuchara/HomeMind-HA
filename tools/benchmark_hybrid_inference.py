#!/usr/bin/env python3
"""Repository wrapper for the full hybrid inference hot-path baseline."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/"adaptive_ai"/"src"
if str(SRC) not in sys.path: sys.path.insert(0,str(SRC))
from hybrid_inference_benchmark import main
if __name__=="__main__": main()
