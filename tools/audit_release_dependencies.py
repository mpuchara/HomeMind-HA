"""Resolve a saved GitHub PR inventory against locally fetched Git objects."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    inventory = json.loads((ROOT/'docs/benchmarks/FINAL_RELEASE_GITHUB.json').read_text())
    head = git('rev-parse', 'HEAD').decode().strip()
    rows = []
    for row in sorted(inventory['prs'], key=lambda r: r['number']):
        result = dict(row)
        for label, ancestor, descendant in (
            ('head_in_checkout', row['head_sha'], head),
            ('base_is_ancestor_of_head', row['base_sha'], row['head_sha']),
        ):
            code = subprocess.run(['git', 'merge-base', '--is-ancestor', ancestor, descendant], cwd=ROOT).returncode
            if code not in (0, 1):
                raise RuntimeError(f'Missing or invalid objects for PR {row["number"]}')
            result[label] = code == 0
        rows.append(result)
    source = git('ls-tree', '-r', head, 'adaptive_ai')
    result = dict(checked_date=inventory['checked_date'], checkout_sha=head,
                  main_sha=git('rev-parse', 'origin/main').decode().strip(),
                  addon_tree_manifest_sha256=hashlib.sha256(source).hexdigest(), prs=rows)
    with args.output.open('x', encoding='utf-8') as out:
        json.dump(result, out, indent=2)
        out.write('\n')
    print(json.dumps(dict(checkout_sha=head, prs=len(rows),
                         absent=[r['number'] for r in rows if not r['head_in_checkout']],
                         invalid_base=[r['number'] for r in rows if not r['base_is_ancestor_of_head']])))


if __name__ == '__main__':
    main()
