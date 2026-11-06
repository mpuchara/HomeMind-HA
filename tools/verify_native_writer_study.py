"""Verify benchmark trees against their exact git archives and prior manifests."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile
from run_gate_d_snapshot_study import manifest


def main():
    p = argparse.ArgumentParser()
    p.add_argument('root', type=Path)
    p.add_argument('archives', type=Path)
    args = p.parse_args()
    archives = dict(base='writer-base-951dcd70.tar', before='writer-before-4b989c69.tar',
                    current='writer-current-79bf0674.tar', source='writer-source-frozen.tar')
    checks = {}
    for arm, archive in archives.items():
        differences = []
        count = 0
        with tarfile.open(args.archives/archive) as source:
            for item in source:
                if item.isfile() and item.name.startswith('adaptive_ai/'):
                    count += 1
                    expected = hashlib.sha256(source.extractfile(item).read()).hexdigest()
                    target = args.root/arm/item.name
                    if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest()!=expected:
                        differences.append(item.name)
        checks[arm] = dict(addon_files=count, differences=differences)
    prior = json.loads((args.root/'writer-profiles/protocol.json').read_text())['manifests']
    for arm in archives:
        actual = {str(f.relative_to(args.root/arm)):hashlib.sha256(f.read_bytes()).hexdigest()
                  for folder in ('adaptive_ai/src', 'tools')
                  for f in (args.root/arm/folder).rglob('*.py')}
        checks[arm]['profile_manifest_unchanged'] = actual==prior[arm]
    for phase in ('matrix', 'variance'):
        prior = json.loads((args.root/phase/'source-manifests.json').read_text())
        checks[phase] = {arm:manifest(args.root/arm)==value for arm,value in prior.items()}
    left, right = manifest(args.root/'writer-only'), manifest(args.root/'source')
    different = [key for key in sorted(set(left)|set(right)) if left.get(key)!=right.get(key)]
    checks['ablation'] = dict(changed_files=different,
        only_preflight_differs=different==['adaptive_ai/src/universal_predecision.py'],
        original_predecision=left['adaptive_ai/src/universal_predecision.py']==manifest(args.root/'current')['adaptive_ai/src/universal_predecision.py'])
    valid = (all(not checks[arm]['differences'] and checks[arm]['profile_manifest_unchanged'] for arm in archives)
             and all(all(checks[phase].values()) for phase in ('matrix','variance'))
             and checks['ablation']['only_preflight_differs'] and checks['ablation']['original_predecision'])
    (args.root/'post-run-integrity.json').write_text(json.dumps(dict(valid=valid, checks=checks), indent=2))
    if not valid:
        raise RuntimeError('source integrity differs; do not accept performance results')
    print('Frozen source identity and one-file ablation verified')


if __name__ == '__main__':
    main()
