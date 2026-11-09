"""Independent preflight effect, keeping the optimized native writer identical."""
import argparse
import json
from pathlib import Path
from run_gate_d_snapshot_study import compare, manifest, run, save


def main():
    p = argparse.ArgumentParser()
    p.add_argument('root', type=Path)
    args = p.parse_args()
    root = args.root
    out = root/'preflight-ablation'
    out.mkdir()
    save(out/'protocol.json', dict(diagnostic_only=True, ticks=24, period=.4, fanout=16,
        scale='1000:200', mode='busy', candidates=200,
        plan=[['writer-only', 'source'], ['source', 'writer-only']],
        explanation='Both arms use the same optimized journal writer; only native cardinality preflight differs.',
        manifests={arm:manifest(root/arm) for arm in ('writer-only', 'source')}))
    results = []
    for index, order in enumerate((('writer-only', 'source'), ('source', 'writer-only'))):
        rows = {arm:run(root/arm, out, f'{index}-{arm}', '1000:200', 'busy', 200) for arm in order}
        result = dict(pair=index, diagnostic_only=True, **compare(rows['writer-only'], rows['source']))
        results.append(result)
        print(json.dumps(result), flush=True)
    save(out/'summary.json', results)


if __name__ == '__main__':
    main()
