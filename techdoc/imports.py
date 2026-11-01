"""Single-process durable imports: stage -> parse -> build -> publish under catalog checks.

The global DocumentStore lock serializes index publication. No parser or model work
holds a SQLite write transaction. Unregistered crash leftovers are reconciled from
the durable publication journal before DocumentStore loads root snapshots.
"""
from __future__ import annotations

from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

from .catalog import CatalogError, encoded, new_id, utc_now
from .core import DocParser, MAX_BYTES, RetrieverBuildFailed
from .jobs import JobQueue
from .inference import inference_slot,checked_lock
from .knowledge import bind_version, require_document
from .snapshot_identity import parse_snapshot_v2

LOG = logging.getLogger(__name__)
TERMINAL = ('succeeded','failed','cancelled','interrupted')
MAX_SNAPSHOT_BYTES = 32*1024*1024


def atomic_bytes(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.tmp_import_',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)


def reconcile_import_files(catalog):
    """Run before loading the index, and only with this data directory stopped."""
    from .index_staging import recover_stages
    recover_stages(catalog)
    with catalog.reader() as conn:
        registered = {r[0] for r in conn.execute('SELECT snapshot_id FROM snapshots')}
        rows = conn.execute('SELECT * FROM import_inputs WHERE published_snapshot_id IS NOT NULL').fetchall()
    removed = []
    for row in rows:
        sid = row['published_snapshot_id']
        # IDs originate on the server, but validate before using a persisted path.
        import re
        if not re.fullmatch('[0-9a-f]{28}',sid):
            raise CatalogError('IMPORT_JOURNAL_INVALID')
        if sid in registered: continue
        for relative in (sid+'.json','originals/'+sid+Path(row['filename']).suffix.lower()):
            path = catalog.data_dir/relative
            if path.is_symlink() or not path.resolve().is_relative_to(catalog.data_dir):
                raise CatalogError('IMPORT_JOURNAL_INVALID')
            path.unlink(missing_ok=True)
        removed.append(sid)
    with catalog.reader() as conn:
        completed=conn.execute("""SELECT i.* FROM import_inputs i JOIN jobs j ON j.target_id=i.id
            WHERE j.kind='import' AND j.state='succeeded'""").fetchall()
    from .index_staging import safe_path
    for row in completed:
        if row['staged_path']!='staging/'+row['id']+'/source':raise CatalogError('IMPORT_JOURNAL_INVALID')
        path=safe_path(catalog.data_dir,row['staged_path'])
        if path.is_file():
            if sha256(path.read_bytes()).hexdigest()!=row['source_sha256']:raise CatalogError('IMPORT_INPUT_CORRUPT')
            path.unlink()
        for name in ('mineru-request.json','mineru-result.zip'):
            artifact=path.parent/name
            if artifact.is_symlink():raise CatalogError('IMPORT_JOURNAL_INVALID')
            artifact.unlink(missing_ok=True)
    return removed


class ImportService:
    def __init__(self,catalog,store=None,*,workspace_id='ws_personal',storage_limit=512*1024*1024):
        self.catalog,self.store,self.workspace_id = catalog,store,workspace_id
        self.queue = JobQueue(catalog,workspace_id=workspace_id)
        self.storage_limit = storage_limit
        self.stop_event = threading.Event(); self.wake_event = threading.Event(); self.thread = None
        self.submit_lock = threading.RLock()

    def _storage_size(self):
        return sum(p.stat().st_size for p in self.catalog.data_dir.rglob('*') if p.is_file() and not p.is_symlink())

    def _input_path(self,row):
        expected = 'staging/'+row['id']+'/source'
        path = self.catalog.data_dir/row['staged_path']
        if (row['staged_path']!=expected or path.is_symlink()
                or not path.resolve().is_relative_to(self.catalog.data_dir)):
            raise CatalogError('IMPORT_INPUT_UNAVAILABLE',status=503)
        return path

    def submit(self,kb_id,filename,data,*,request_key,revision,version_label='',display_name=None,
               document_id=None,origin='file'):
        filename = Path(filename.replace('\\','/')).name
        if (not filename or len(filename)>200 or any(ord(c)<32 for c in filename)
                or Path(filename).suffix.lower() not in DocParser.EXTENSIONS):
            raise CatalogError('INVALID_UPLOAD_FILENAME',status=422)
        if not data or len(data)>MAX_BYTES:
            raise CatalogError('EMPTY_OR_OVERSIZED_UPLOAD',status=422)
        if not 1<=len(request_key)<=128 or len(version_label)>120 or origin not in ('file','pasted_text'):
            raise CatalogError('INVALID_IMPORT_REQUEST',status=422)
        if display_name is not None:
            display_name = display_name.strip()
            if not display_name or len(display_name)>200 or any(ord(c)<32 for c in display_name):
                raise CatalogError('INVALID_DOCUMENT_NAME',status=422)
        source_sha = sha256(data).hexdigest()
        identity = sha256(encoded([kb_id,filename,source_sha,version_label,display_name,
                                    document_id,origin,revision]).encode()).hexdigest()
        with self.submit_lock:
            # Check replay before revision, so a lost success response is recoverable.
            with self.catalog.reader() as conn:
                self.catalog.require_kb(conn,kb_id,workspace_id=self.workspace_id)
                old = conn.execute("SELECT * FROM jobs WHERE kb_id=? AND kind='import' AND request_key=?",(kb_id,request_key)).fetchone()
                if old:
                    if old['input_sha256']!=identity: raise CatalogError('JOB_REQUEST_KEY_CONFLICT')
                    return self.get(old['id'])
            used = self._storage_size()
            if used+len(data)*3>self.storage_limit or shutil.disk_usage(self.catalog.data_dir).free<len(data)*3+32*1024*1024:
                raise CatalogError('WORKSPACE_STORAGE_LIMIT',status=507)
            ident = new_id('import'); path = self.catalog.data_dir/'staging'/ident/'source'
            atomic_bytes(path,data)
            try:
                with self.catalog.transaction() as conn:
                    kb = self.catalog.require_kb(conn,kb_id,active=True,workspace_id=self.workspace_id)
                    self.catalog.check_revision(kb,revision)
                    parent = epoch = None
                    if document_id:
                        doc = require_document(conn,document_id,kb_id=kb_id)
                        if not version_label.strip(): raise CatalogError('REVISION_VERSION_REQUIRED',status=422)
                        if filename!=doc['original_filename']: raise CatalogError('REVISION_FILENAME_MISMATCH',status=422)
                        parent,epoch = doc['active_version_id'],doc['membership_epoch']
                    elif display_name is None:
                        same = conn.execute('''SELECT d.id,s.source_sha256 FROM documents d
                            JOIN document_versions v ON v.id=d.active_version_id
                            JOIN snapshots s ON s.snapshot_id=v.snapshot_id
                            WHERE d.kb_id=? AND d.original_filename=? AND d.state='active' ''',(kb_id,filename)).fetchall()
                        if any(r['source_sha256']!=source_sha for r in same):
                            raise CatalogError('SAME_NAME_REQUIRES_EXPLICIT_CHOICE')
                        pending = conn.execute('''SELECT 1 FROM import_inputs i JOIN jobs j ON j.target_id=i.id
                            WHERE i.kb_id=? AND i.filename=? AND i.source_sha256<>?
                            AND j.state IN ('queued','running','cancel_requested') LIMIT 1''',
                            (kb_id,filename,source_sha)).fetchone()
                        if pending: raise CatalogError('SAME_NAME_REQUIRES_EXPLICIT_CHOICE')
                    conn.execute('''INSERT INTO import_inputs(id,kb_id,document_id,parent_version_id,membership_epoch,
                        filename,display_name,version_label,origin,staged_path,source_sha256,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(ident,kb_id,document_id,parent,epoch,filename,
                        display_name or filename,version_label,origin,path.relative_to(self.catalog.data_dir).as_posix(),source_sha,utc_now()))
                    job = self.queue.enqueue(kb_id,'import',ident,request_key,identity,conn=conn)
                self.wake_event.set()
                return self.get(job['id'])
            except BaseException:
                # A receipt/read failure can happen AFTER the durable transaction.
                # Delete only after positively proving no committed task owns input.
                # If even that read fails, keep the file for recovery, never guess.
                try:
                    with self.catalog.reader() as conn:
                        owned = conn.execute("SELECT 1 FROM jobs WHERE kind='import' AND target_id=?",(ident,)).fetchone()
                    if not owned:path.unlink(missing_ok=True)
                except Exception:
                    LOG.warning('Import submission cleanup deferred: ownership unavailable')
                raise

    def get(self,job_id):
        with self.catalog.reader() as conn:
            conn.execute('BEGIN')
            job = self.queue._owned(conn,job_id)
            self.catalog.require_kb(conn,job['kb_id'],workspace_id=self.workspace_id)
            row = conn.execute('SELECT * FROM import_inputs WHERE id=?',(job['target_id'],)).fetchone()
            if not row: raise CatalogError('IMPORT_NOT_FOUND',status=404)
            # No lease tokens or local filesystem paths leave the server.
            result = {k:job[k] for k in ('id','kb_id','state','stage','attempt','error_code','created_at','updated_at','request_key')}
            result.update(import_id=row['id'],document_id=row['document_id'],display_name=row['display_name'],
                          retryable=job['state'] in ('failed','cancelled','interrupted') and self._input_path(row).is_file())
            if job['error_code'] in ('MINERU_SUBMISSION_UNCERTAIN','MINERU_PARSE_FAILED','MINERU_UPLOAD_EXPIRED'):
                result['retryable']=False
            result['result'] = json.loads(row['result_json']) if row['result_json'] else None
            return result

    def by_request(self,kb_id,request_key):
        with self.catalog.reader() as conn:
            self.catalog.require_kb(conn,kb_id,workspace_id=self.workspace_id)
            row = conn.execute("SELECT id FROM jobs WHERE kb_id=? AND kind='import' AND request_key=?",(kb_id,request_key)).fetchone()
            if not row: raise CatalogError('IMPORT_NOT_FOUND',status=404)
        return self.get(row['id'])

    def retry(self,job_id):
        current = self.get(job_id)
        if not current['retryable']: raise CatalogError('IMPORT_NOT_RETRYABLE')
        self.queue.retry(job_id); self.wake_event.set()
        return self.get(job_id)

    def cancel(self,job_id):
        self.get(job_id)
        self.queue.cancel(job_id)
        return self.get(job_id)

    def start(self):
        if self.thread: return
        self.stop_event.clear()
        self.queue.recover_expired()
        def loop():
            while not self.stop_event.is_set():
                try:
                    self.queue.recover_expired()
                    if self.process_next(): continue
                except Exception:
                    LOG.exception('Import worker failed')
                self.wake_event.wait(1); self.wake_event.clear()
        self.thread = threading.Thread(target=loop,name='knowledge-import',daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set(); self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=5)
            # An in-flight model kernel may still own the index; never start another worker.

    def _check_job(self,conn,job):
        row = self.queue._owned(conn,job['id'])
        if row['lease_token']!=job['lease_token'] or row['state'] not in ('running','cancel_requested') or row['lease_until']<time.time():
            raise CatalogError('JOB_LEASE_LOST')
        self.catalog.require_kb(conn,row['kb_id'],active=True,workspace_id=self.workspace_id)
        if row['cancel_requested']: raise CatalogError('IMPORT_CANCELLED')
        return row

    def process_next(self):
        job = self.queue.claim(lease_seconds=120,kind='import')
        if not job: return False
        done = threading.Event()
        def heartbeat():
            while not done.wait(20):
                try: self.queue.renew(job['id'],job['lease_token'])
                except CatalogError: return
        pulse = threading.Thread(target=heartbeat,daemon=True); pulse.start()
        try:
            with self.catalog.reader() as conn:
                row = conn.execute('SELECT * FROM import_inputs WHERE id=?',(job['target_id'],)).fetchone()
                if not row: raise CatalogError('IMPORT_INPUT_UNAVAILABLE')
                source = dict(row)
            self.queue.renew(job['id'],job['lease_token'],stage='parsing')
            data = self._input_path(source).read_bytes()
            if sha256(data).hexdigest()!=source['source_sha256']: raise CatalogError('IMPORT_INPUT_CORRUPT')
            def check():
                if self.stop_event.is_set():raise CatalogError('IMPORT_CANCELLED')
                with self.catalog.reader() as conn:self._check_job(conn,job)
            from .mineru_api import parser_mode,fetch_result,convert_result
            if parser_mode()=='mineru_api' and Path(source['filename']).suffix.lower() in ('.pdf','.docx'):
                raw,model=fetch_result(source['filename'],data,self._input_path(source).parent,check,
                    lambda stage:self.queue.renew(job['id'],job['lease_token'],stage=stage))
                meta,blocks=convert_result(source['filename'],data,raw,model,source['version_label'],source['origin'])
            else:
                meta,blocks = parse_snapshot_v2(source['filename'],data,source['version_label'],origin=source['origin'])
            check()
            if not blocks or not any(b.get('indexable') for b in blocks): raise CatalogError('NO_INDEXABLE_CONTENT',status=422)
            meta['original_stored'] = True
            self._publish(job,source,meta,blocks,data)
            self._input_path(source).unlink(missing_ok=True)
            for name in ('mineru-request.json','mineru-result.zip'):
                self._input_path(source).parent.joinpath(name).unlink(missing_ok=True)
        except Exception as exc:
            # External library exception messages can contain paths/content; store stable codes only.
            from .parse_quality import ERRORS
            code = exc.code if isinstance(exc,CatalogError) else ('RETRIEVER_INDEX_BUILD_FAILED' if isinstance(exc,RetrieverBuildFailed) else (str(exc) if isinstance(exc,ValueError) and str(exc) in ERRORS else 'DOCUMENT_IMPORT_FAILED'))
            try: self.queue.finish(job['id'],job['lease_token'],error_code=code)
            except CatalogError: pass  # Lease loss is resolved by recovery, never by late publication.
        finally:
            done.set(); pulse.join(timeout=1)
        return True

    def _publish(self,job,source,meta,blocks,data):
        policy=os.getenv('TECHDOC_INDEX_POLICY','legacy')
        if policy not in ('legacy','staged_v1'):raise ValueError('BAD_INDEX_POLICY')
        if policy=='staged_v1':
            from .inference import enabled
            if not enabled():raise ValueError('STAGED_INDEX_REQUIRES_INFERENCE_BUDGET')
            from .staged_imports import publish
            return publish(self,job,source,meta,blocks,data)
        return self._publish_legacy(job,source,meta,blocks,data)

    def _publish_legacy(self,job,source,meta,blocks,data):
        sid = meta['snapshot_id']; root = self.catalog.data_dir
        target = root/(sid+'.json'); original = root/'originals'/(sid+Path(meta['filename']).suffix.lower())
        raw = json.dumps({'metadata':meta,'blocks':blocks},ensure_ascii=False).encode()
        if len(raw)>MAX_SNAPSHOT_BYTES:
            raise CatalogError('PARSED_SNAPSHOT_BUDGET_EXCEEDED',status=422)
        self.queue.renew(job['id'],job['lease_token'],stage='indexing')
        def check():
            if self.stop_event.is_set():raise CatalogError('IMPORT_CANCELLED')
            with self.catalog.reader() as conn:self._check_job(conn,job)
        with inference_slot(check),checked_lock(self.store.lock,check),self.store._index_views.pin() as previous_view:
            self.store._index_views.check_capacity()
            with self.catalog.transaction() as conn:
                self._check_job(conn,job)
                conn.execute('UPDATE import_inputs SET published_snapshot_id=? WHERE id=?',(sid,source['id']))
                known = conn.execute('SELECT * FROM snapshots WHERE snapshot_id=?',(sid,)).fetchone()
            fresh = sid not in self.store.docs
            if bool(known)==fresh: raise CatalogError('SNAPSHOT_CATALOG_INDEX_MISMATCH')
            owned_files = False
            try:
                if fresh:
                    if target.exists() or original.exists(): raise CatalogError('UNREGISTERED_SNAPSHOT_FILES')
                    required=len(raw)+len(data)
                    if self._storage_size()+required>self.storage_limit or shutil.disk_usage(root).free<required+32*1024*1024:
                        raise CatalogError('WORKSPACE_STORAGE_LIMIT',status=507)
                    owned_files = True
                    atomic_bytes(original,data); atomic_bytes(target,raw)
                    self.store.docs[sid]=meta; self.store.blocks[sid]=blocks
                    try: self.store._rebuild()
                    except Exception as exc: raise RetrieverBuildFailed() from exc
                    if self._storage_size()>self.storage_limit:
                        raise CatalogError('WORKSPACE_STORAGE_LIMIT',status=507)
                elif (sha256(target.read_bytes()).hexdigest()!=known['snapshot_sha256']
                      or sha256(original.read_bytes()).hexdigest()!=source['source_sha256']):
                    raise CatalogError('SOURCE_UNAVAILABLE',status=503)
                self.queue.renew(job['id'],job['lease_token'],stage='publishing')
                with self.catalog.transaction() as conn:
                    self._bind_import(conn,job,source,meta,target,original,raw,known)
                if fresh: self.store._stamp_snapshot(sid)
            except BaseException:
                if fresh and owned_files:
                    # Never remove a committed shared object if commit acknowledgment was lost.
                    with self.catalog.reader() as conn:
                        committed = conn.execute('SELECT 1 FROM snapshots WHERE snapshot_id=?',(sid,)).fetchone()
                    if not committed:
                        self.store.docs.pop(sid,None); self.store.blocks.pop(sid,None)
                        target.unlink(missing_ok=True); original.unlink(missing_ok=True)
                        if self.store._index_views.current is not previous_view:self.store._restore_view(previous_view)
                    elif sid not in self.store._snapshot_epochs:
                        # A commit acknowledgement can fail after SQLite has
                        # committed. Keep the shared source and its physical epoch.
                        self.store._stamp_snapshot(sid)
                raise

    def _bind_import(self,conn,job,source,meta,target,original,raw,known):
        sid=meta['snapshot_id'];root=self.catalog.data_dir
        self._check_job(conn,job)
        duplicate = conn.execute('''SELECT document_id,version_id FROM kb_snapshot_bindings
            WHERE kb_id=? AND snapshot_id=? AND binding_role='current' AND state='active' ''',(source['kb_id'],sid)).fetchone()
        if source['document_id']:
            doc = require_document(conn,source['document_id'],kb_id=source['kb_id'])
            if doc['active_version_id']!=source['parent_version_id'] or doc['membership_epoch']!=source['membership_epoch']:
                raise CatalogError('REVISION_PARENT_CHANGED')
            if duplicate and duplicate['document_id']!=doc['id']: raise CatalogError('SNAPSHOT_ALREADY_IN_LIBRARY')
        if duplicate:
            result = dict(duplicate,snapshot_id=sid,duplicate=True)
        else:
            if not known:
                conn.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?)',(sid,meta['snapshot_scheme'],
                    meta['source_sha256'],encoded(meta['parser_identity']),target.name,
                    original.relative_to(root).as_posix(),sha256(raw).hexdigest(),encoded(meta),'verified'))
            doc_id = source['document_id'] or new_id('doc'); ver_id = new_id('ver')
            if not source['document_id']:
                conn.execute('''INSERT INTO documents(id,kb_id,display_name,original_filename)
                    VALUES(?,?,?,?)''',(doc_id,source['kb_id'],source['display_name'],source['filename']))
            sequence = conn.execute('SELECT coalesce(max(sequence),0)+1 FROM document_versions WHERE document_id=?',(doc_id,)).fetchone()[0]
            conn.execute('INSERT INTO document_versions VALUES(?,?,?,?,?,?,?,?,?)',(ver_id,source['kb_id'],doc_id,
                sid,source['version_label'],sequence,source['parent_version_id'],'ready',utc_now()))
            bind_version(conn,doc_id,ver_id)
            self.catalog.touch(conn,source['kb_id'])
            result = {'document_id':doc_id,'version_id':ver_id,'snapshot_id':sid,'duplicate':False}
        conn.execute('UPDATE import_inputs SET result_json=? WHERE id=?',(encoded(result),source['id']))
        conn.execute("UPDATE jobs SET state='succeeded',stage='succeeded',error_code=NULL,updated_at=? WHERE id=?",(utc_now(),job['id']))
