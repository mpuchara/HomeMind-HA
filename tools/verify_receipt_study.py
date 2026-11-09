"""Check immutable native source archives and all registered Python manifests."""
import argparse
import json
import tarfile
from pathlib import Path
from run_gate_d_snapshot_study import manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('archives', type=Path)
    args = parser.parse_args()
    archives = dict(base='receipt-baseline-951dcd70.tar', before='receipt-previous-9c245577.tar',
                    source='receipt-candidate-4dc2d2d6.tar', **{'final-validation':'receipt-final-c1dbfd9f.tar'})
    checks = {}
    for arm, filename in archives.items():
        differences = []; count = 0
        with tarfile.open(args.archives/filename) as archive:
            for entry in archive:
                if entry.isfile() and entry.name.startswith('adaptive_ai/'):
                    count += 1
                    expected = archive.extractfile(entry).read()
                    path = args.root/arm/entry.name
                    if not path.exists() or path.read_bytes()!=expected:
                        differences.append(entry.name)
        checks[arm] = dict(addon_files=count, differences=differences)
    prior = json.loads((args.root/'protocol.json').read_text())['manifests']
    unchanged = {arm:manifest(args.root/arm)==value for arm,value in prior.items()}
    phases = {}
    for phase in ('matrix','variance'):
        saved = json.loads((args.root/phase/'source-manifests.json').read_text())
        phases[phase] = {arm:manifest(args.root/arm)==value for arm,value in saved.items()}
    valid = (all(not row['differences'] for row in checks.values()) and all(unchanged.values())
             and all(all(row.values()) for row in phases.values()))
    result = dict(valid=valid, archives=checks, original_manifests_unchanged=unchanged,
                  registered_manifests_unchanged=phases)
    (args.root/'integrity.json').write_text(json.dumps(result,indent=2))
    if not valid: raise RuntimeError('Frozen source differs; do not accept these measurements')
    print('All four add-on archives and registered manifests match')


if __name__=='__main__':
    main()
