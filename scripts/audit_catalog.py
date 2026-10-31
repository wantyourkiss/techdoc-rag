"""Read-only audit of catalog, original files and preserved snapshot hashes."""
import argparse
import hashlib
import json
from pathlib import Path

from techdoc.catalog import Catalog
from techdoc.knowledge_migration import source_manifest


def audit(data_dir):
    root = Path(data_dir).resolve()
    if not (root/'catalog/app.sqlite3').is_file():
        raise ValueError('Catalog does not exist')
    catalog = Catalog(root, initialize=False)
    report = catalog.audit(); mismatches = []
    expected_paths = set()
    with catalog.reader() as conn:
        for row in conn.execute('SELECT * FROM snapshots'):
            for field,hash_field in [('snapshot_path','snapshot_sha256'),('original_path','source_sha256')]:
                expected_paths.add(row[field])
                path = root/row[field]
                if not path.resolve().is_relative_to(root) or path.is_symlink() or not path.is_file():
                    mismatches.append({'snapshot_id':row['snapshot_id'],'kind':field,'error':'unavailable'})
                elif hashlib.sha256(path.read_bytes()).hexdigest() != row[hash_field]:
                    mismatches.append({'snapshot_id':row['snapshot_id'],'kind':field,'error':'hash_mismatch'})
    report['source_mismatches'] = mismatches
    staged_mismatches = []
    with catalog.reader() as conn:
        # Version 1 is a valid migration input, but current audits require v2.
        has_inputs = conn.execute("SELECT 1 FROM sqlite_master WHERE name='import_inputs'").fetchone()
        if has_inputs:
            for row in conn.execute('SELECT id,staged_path,source_sha256 FROM import_inputs'):
                path = root/row['staged_path']
                if (row['staged_path']!='staging/'+row['id']+'/source' or path.is_symlink()
                        or not path.resolve().is_relative_to(root)):
                    staged_mismatches.append({'import_id':row['id'],'error':'invalid_path'})
                elif path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()!=row['source_sha256']:
                    staged_mismatches.append({'import_id':row['id'],'error':'hash_mismatch'})
    report['staged_input_mismatches'] = staged_mismatches
    report['unregistered_source_files'] = sorted(set(source_manifest(root))-expected_paths)
    report['ok'] = report['ok'] and not mismatches and not staged_mismatches and not report['unregistered_source_files']
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',required=True)
    args = parser.parse_args(); result = audit(args.data_dir)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if not result['ok']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
