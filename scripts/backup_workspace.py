"""Quiesced knowledge-workspace backup/restore; excludes credentials, models and caches."""
from __future__ import annotations

import argparse
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import tempfile
import time
import zipfile

from techdoc.catalog import Catalog
from techdoc.catalog_owner import CatalogOwner
from techdoc.knowledge_migration import source_manifest
from scripts.audit_catalog import audit
from scripts.migrate_knowledge import require_not_live_data


MAX_ARCHIVE_BYTES = 512*1024*1024


def _rename_restored(stage,destination):
    for attempt in range(3):
        if destination.exists():raise ValueError('Restore destination must not exist')
        try:stage.rename(destination);return
        except PermissionError:
            # Windows may briefly retain a file handle after verification.
            if os.name!='nt' or attempt==2:raise
            time.sleep(.05*(attempt+1))


def _allowed(name):
    p = PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name or p.as_posix() != name:
        return False
    import re
    if name == 'catalog/app.sqlite3':
        return True
    if len(p.parts)==3 and p.parts[0]=='staging' and p.parts[2]=='source':
        return bool(re.fullmatch(r'import_[0-9a-f]{32}',p.parts[1]))
    if len(p.parts) == 1:
        return bool(re.fullmatch(r'[0-9a-f]{28}\.json',name))
    return len(p.parts) == 2 and p.parts[0] == 'originals' and bool(
        re.fullmatch(r'[0-9a-f]{28}\.(pdf|docx|md|txt|html|htm)',p.name))


def backup_workspace(data_dir, output):
    root = Path(data_dir).resolve()
    if not (root/'catalog/app.sqlite3').is_file():
        raise ValueError('Catalog does not exist')
    owner = CatalogOwner(root)
    try:
        return _backup_workspace(root,output)
    finally:
        owner.close()


def _backup_workspace(data_dir, output):
    root, output = Path(data_dir).resolve(), Path(output)
    if output.exists():
        raise ValueError('Backup destination already exists')
    if output.resolve().is_relative_to(root):
        raise ValueError('Write backup outside the source data directory')
    catalog = Catalog(root,initialize=False)
    with catalog.reader() as conn:
        if conn.execute("SELECT 1 FROM runs WHERE status IN ('queued','running','cancel_requested') LIMIT 1").fetchone():
            raise ValueError('Quiesce pending runs before backup')
        if conn.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running','cancel_requested') LIMIT 1").fetchone():
            raise ValueError('Quiesce pending jobs before backup')
        if conn.execute("SELECT 1 FROM document_versions WHERE processing_state IN ('queued','processing') LIMIT 1").fetchone():
            raise ValueError('Quiesce pending versions before backup')
    # Terminal attempts contain only reconstructible index staging. Reconcile
    # their owned links while quiesced; durable raw retry inputs are archived below.
    from techdoc.imports import reconcile_import_files
    reconcile_import_files(catalog)
    source_check = audit(root)
    if not source_check['ok']:
        raise ValueError('Catalog or source audit failed')
    before = source_manifest(root)
    if not all(_allowed(name) for name in before):
        raise ValueError('Unexpected source file')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='workspace-backup-',dir=output.parent) as temp:
        db_path = Path(temp)/'app.sqlite3'
        with catalog.reader() as conn, closing(sqlite3.connect(db_path)) as destination:
            conn.backup(destination)
        blobs = {name:(root/name).read_bytes() for name in before}
        # Terminal failed/cancelled imports remain explicitly retryable after restore.
        # Include only the durable raw inputs named by the DB, not parser/index caches.
        with catalog.reader() as conn:
            staged = conn.execute('SELECT id,staged_path,source_sha256 FROM import_inputs').fetchall()
        for row in staged:
            name = row['staged_path']; path = root/name
            if name!='staging/'+row['id']+'/source' or not _allowed(name) or path.is_symlink():
                raise ValueError('Invalid import input path')
            if path.is_file():
                data = path.read_bytes()
                if sha256(data).hexdigest()!=row['source_sha256']:
                    raise ValueError('Import input hash mismatch')
                blobs[name]=data
        for name, data in blobs.items():
            if name in before and sha256(data).hexdigest() != before[name]:
                raise ValueError('Source changed while copying')
        blobs['catalog/app.sqlite3'] = db_path.read_bytes()
        if sum(map(len,blobs.values())) > MAX_ARCHIVE_BYTES:
            raise ValueError('Backup exceeds restore budget')
        if source_manifest(root) != before:
            raise ValueError('Source changed while copying')
        manifest = {name:sha256(data).hexdigest() for name,data in blobs.items()}
        with zipfile.ZipFile(output,'x',zipfile.ZIP_DEFLATED) as archive:
            for name,data in sorted(blobs.items()):
                archive.writestr(name,data)
            archive.writestr('WORKSPACE_BACKUP_MANIFEST.json',json.dumps(
                {'format':'techdoc_workspace_v2','files':manifest},sort_keys=True))
    return {'files':len(blobs),'archive_sha256':sha256(output.read_bytes()).hexdigest(),
            'source_audit':source_check,'browser_local_progress_included':False}


def restore_workspace(archive_path, destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError('Restore destination must not exist')
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist(); names = [entry.filename for entry in entries]
        if len(names) != len(set(names)) or sum(entry.file_size for entry in entries) > MAX_ARCHIVE_BYTES:
            raise ValueError('Duplicate entries or archive budget exceeded')
        meta = json.loads(archive.read('WORKSPACE_BACKUP_MANIFEST.json'))
        if meta.get('format') not in ('techdoc_workspace_v1','techdoc_workspace_v2'):
            raise ValueError('Unknown workspace backup format')
        manifest = meta['files']
        if set(names) != set(manifest) | {'WORKSPACE_BACKUP_MANIFEST.json'} or 'catalog/app.sqlite3' not in manifest:
            raise ValueError('Unexpected backup entries')
        blobs = {}
        for name,digest in manifest.items():
            if not _allowed(name) or (meta['format']=='techdoc_workspace_v1' and name.startswith('staging/')):
                raise ValueError('Unsafe workspace path')
            data = archive.read(name)
            if sha256(data).hexdigest() != digest:
                raise ValueError('Workspace backup hash mismatch')
            blobs[name] = data
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='workspace-restore-',dir=destination.parent) as temp:
        stage = Path(temp)/'data'; stage.mkdir()
        for name,data in blobs.items():
            path = stage/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(data)
        (stage/'originals').mkdir(exist_ok=True)
        # Verify old schema checksums and upgrade the restored copy, never the archive.
        Catalog(stage)
        check = audit(stage)
        if not check['ok']:
            raise ValueError('Restored catalog/source audit failed')
        if destination.exists():
            raise ValueError('Restore destination must not exist')
        _rename_restored(stage,destination)
    return {'restored_files':len(blobs),'catalog_audit':check,'browser_local_progress_included':False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['backup','restore'])
    parser.add_argument('source'); parser.add_argument('destination')
    args = parser.parse_args()
    if args.action == 'backup':
        require_not_live_data(args.source)
        result = backup_workspace(args.source,args.destination)
    else:
        result = restore_workspace(args.source,args.destination)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
