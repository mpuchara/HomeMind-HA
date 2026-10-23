"""Read-only SQLite integrity inspection of an offline COPY, never a repair tool."""
import argparse
import json
from pathlib import Path
import sqlite3


def inspect_copy(path, *, full=False):
    path = Path(path).resolve(strict=True)
    result = dict(database_copy=str(path), sqlite_version=sqlite3.sqlite_version,
                  check='integrity_check' if full else 'quick_check', read_only=True)
    connection = None
    try:
        connection = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1.)
        connection.execute('PRAGMA query_only=ON')
        # Cap reported errors, not scanned pages. Even quick_check may take time
        # on a large copy. Never run automatically on the application's hot path.
        rows = connection.execute('PRAGMA '+result['check']+'(20)').fetchall()
        result.update(messages=[row[0] for row in rows], ok=rows == [('ok',)])
    except sqlite3.DatabaseError as exc:
        result.update(ok=False, error=str(exc), sqlite_errorcode=getattr(exc, 'sqlite_errorcode', None),
                      sqlite_errorname=getattr(exc, 'sqlite_errorname', None))
    finally:
        if connection is not None:
            connection.close()
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-copy', type=Path, required=True,
                        help='Offline copy; preserve accompanying -wal/-shm files when present')
    parser.add_argument('--full', action='store_true', help='Full integrity_check, including indexes')
    args=parser.parse_args()
    result=inspect_copy(args.database_copy, full=args.full)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result['ok'] else 2


if __name__=='__main__':
    raise SystemExit(main())
