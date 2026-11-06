"""Opt-in Python allocation windows; separate from acceptance and CPU profiles.

tracemalloc is process-wide: concurrent collector/inference allocations are
included in these windows. SQLite native pages are reported separately.
"""
import argparse
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import tracemalloc
from unittest.mock import patch


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--memory-output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists() or args.memory_output.exists():
        parser.error('outputs must be new')
    rows=[]
    with tempfile.TemporaryDirectory(prefix='hm-memory-') as folder:
        os.environ['ADAPTIVE_AI_DATA']=folder
        sys.path[:0]=[str(args.source_root/'adaptive_ai/src'),str(args.source_root/'tools')]
        from universal_predecision import PredecisionEvidence
        original=PredecisionEvidence.prepare_one
        def measured(self):
            if not self.pending:
                return original(self)
            before=tracemalloc.get_traced_memory()[0]
            tracemalloc.reset_peak()
            try:
                return original(self)
            finally:
                current,peak=tracemalloc.get_traced_memory()
                cache=getattr(self,'arbiter_snapshot',None)
                db=getattr(cache,'db',None)
                pages=(db.execute('PRAGMA page_count').fetchone()[0]*
                       db.execute('PRAGMA page_size').fetchone()[0]) if db is not None else 0
                rows.append(dict(before_bytes=before,after_bytes=current,
                    process_window_peak_bytes=peak,net_bytes=current-before,
                    peak_growth_bytes=max(0,peak-before),sqlite_retained_page_bytes=pages))
        tracemalloc.start(1)
        sys.argv=[str(args.source_root/'tools/benchmark_qualified_evidence.py'),
                  '--cases','1000:200','--modes','normal','--ticks','24','--period','.4',
                  '--fanout','16','--candidates','0','--output',str(args.output)]
        try:
            with patch.object(PredecisionEvidence,'prepare_one',measured):
                runpy.run_path(sys.argv[0],run_name='__main__')
        finally:
            tracemalloc.stop()
            with args.memory_output.open('x') as stream:
                json.dump(dict(scope='process-wide Python allocations during preparation; concurrent allocations included; not acceptance',
                               rows=rows),stream,indent=2)


if __name__=='__main__':main()
