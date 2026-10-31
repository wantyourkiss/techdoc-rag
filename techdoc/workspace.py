"""Knowledge workspace metadata and bounded, relationship-checked source reading."""
from __future__ import annotations

import json
import sqlite3
from hashlib import sha256

from .catalog import CatalogError, normalized_name, encoded, utc_now
from .knowledge import KnowledgeService, require_document


class WorkspaceService(KnowledgeService):
    def libraries(self, *, state='active', search='', sort='updated', offset=0, limit=50):
        if state not in ('active','archived','trashed') or sort not in ('updated','name','created'):
            raise CatalogError('INVALID_LIST_FILTER',status=422)
        order = {'updated':'k.content_updated_at DESC,k.id','created':'k.created_at DESC,k.id',
                 'name':'k.normalized_name,k.id'}[sort]
        # instr performs a literal search; user '%' and '_' are not SQL wildcards.
        where = "k.workspace_id=? AND k.state=? AND instr(lower(k.name||' '||k.description),lower(?))>0"
        args = (self.workspace_id,state,search)
        with self.catalog.reader() as conn:
            conn.execute('BEGIN')
            total = conn.execute('SELECT count(*) FROM knowledge_bases k WHERE '+where,args).fetchone()[0]
            rows = conn.execute('''SELECT k.*,
                (SELECT count(*) FROM documents d WHERE d.kb_id=k.id AND d.state='active') AS document_count,
                (SELECT count(*) FROM documents d WHERE d.kb_id=k.id AND d.state='active'
                 AND d.active_version_id IS NOT NULL) AS ready_current_count,
                (SELECT count(*) FROM jobs j WHERE j.kb_id=k.id
                 AND j.state IN ('queued','running','cancel_requested')) AS pending_count
                FROM knowledge_bases k WHERE '''+where+' ORDER BY '+order+' LIMIT ? OFFSET ?',args+(limit,offset)).fetchall()
            return {'items':[dict(r) for r in rows],'total':total,
                    'next_offset':offset+limit if offset+limit<total else None}

    def library(self, kb_id):
        with self.catalog.reader() as conn:
            return dict(self.catalog.require_kb(conn,kb_id,workspace_id=self.workspace_id))

    def update_library(self, kb_id, *, revision, name=None, description=None):
        if description is not None and (len(description)>1000 or '\x00' in description):
            raise CatalogError('INVALID_DESCRIPTION',status=422)
        with self.catalog.transaction() as conn:
            kb = self.catalog.require_kb(conn,kb_id,active=True,workspace_id=self.workspace_id)
            self.catalog.check_revision(kb,revision)
            title,key = normalized_name(name if name is not None else kb['name'])
            try:
                conn.execute('UPDATE knowledge_bases SET name=?,normalized_name=?,description=? WHERE id=?',
                             (title,key,description if description is not None else kb['description'],kb_id))
            except sqlite3.IntegrityError as exc:
                raise CatalogError('KNOWLEDGE_BASE_NAME_CONFLICT') from exc
            self.catalog.touch(conn,kb_id,content=False)
            return dict(conn.execute('SELECT * FROM knowledge_bases WHERE id=?',(kb_id,)).fetchone())

    def documents(self, kb_id, *, state='active', search='', offset=0, limit=50):
        if state not in ('active','trashed'):
            raise CatalogError('INVALID_LIST_FILTER',status=422)
        with self.catalog.reader() as conn:
            conn.execute('BEGIN')
            kb = self.catalog.require_kb(conn,kb_id,workspace_id=self.workspace_id)
            base = "d.kb_id=? AND d.state=? AND instr(lower(d.display_name),lower(?))>0"
            args = (kb_id,state,search)
            total = conn.execute('SELECT count(*) FROM documents d WHERE '+base,args).fetchone()[0]
            rows = conn.execute('''SELECT d.*,v.snapshot_id,v.version_label,v.sequence,v.created_at,
                s.metadata_json FROM documents d
                LEFT JOIN document_versions v ON v.id=d.active_version_id
                LEFT JOIN snapshots s ON s.snapshot_id=v.snapshot_id WHERE '''+base+
                ' ORDER BY d.display_name,d.id LIMIT ? OFFSET ?',args+(limit,offset)).fetchall()
            items = []
            for row in rows:
                item = dict(row); metadata = json.loads(item.pop('metadata_json') or '{}')
                item.update(processing_state='ready' if row['active_version_id'] else 'unavailable',
                            warnings=metadata.get('warnings',[]),origin=metadata.get('origin','file'))
                items.append(item)
            # Counts cover the entire KB, independent of this page or search.
            ready = conn.execute("SELECT count(*) FROM documents WHERE kb_id=? AND state='active' AND active_version_id IS NOT NULL",(kb_id,)).fetchone()[0]
            jobs = conn.execute('''SELECT j.id AS job_id,j.state,j.stage,j.error_code,i.id AS import_id,
                i.document_id,i.filename,i.display_name,i.version_label,i.origin FROM jobs j
                JOIN import_inputs i ON i.id=j.target_id WHERE j.kb_id=?
                AND j.state<>'succeeded' ORDER BY j.created_at DESC,j.id LIMIT 100''',(kb_id,)).fetchall()
            counts = dict(conn.execute('SELECT state,count(*) FROM jobs WHERE kb_id=? GROUP BY state',(kb_id,)).fetchall())
            return {'items':items,'total':total,'ready_current_count':ready,'catalog_revision':kb['catalog_revision'],
                    'imports':[dict(r) for r in jobs],
                    'pending_count':sum(counts.get(s,0) for s in ('queued','running','cancel_requested')),
                    'failed_count':sum(counts.get(s,0) for s in ('failed','interrupted')),
                    'next_offset':offset+limit if offset+limit<total else None}

    def versions(self, kb_id, document_id):
        with self.catalog.reader() as conn:
            conn.execute('BEGIN')
            self.catalog.require_kb(conn,kb_id,workspace_id=self.workspace_id)
            doc = require_document(conn,document_id,kb_id=kb_id)
            rows = conn.execute('''SELECT id,document_id,kb_id,snapshot_id,version_label,sequence,
                parent_version_id,processing_state,created_at FROM document_versions
                WHERE document_id=? ORDER BY sequence DESC''',(document_id,)).fetchall()
            return {'items':[dict(r) for r in rows],'active_version_id':doc['active_version_id']}

    def version(self, version_id, *, kb_id=None):
        with self.catalog.reader() as conn:
            conn.execute('BEGIN')
            row = conn.execute('''SELECT v.*,d.display_name,d.state AS document_state,d.active_version_id,
                s.snapshot_path,s.original_path,s.source_sha256,s.snapshot_sha256,s.integrity_state,
                s.metadata_json FROM document_versions v JOIN documents d ON d.id=v.document_id
                JOIN snapshots s ON s.snapshot_id=v.snapshot_id WHERE v.id=?''',(version_id,)).fetchone()
            if not row or (kb_id is not None and row['kb_id'] != kb_id):
                raise CatalogError('VERSION_NOT_FOUND',status=404)
            self.catalog.require_kb(conn,row['kb_id'],workspace_id=self.workspace_id)
            if row['document_state'] != 'active':
                raise CatalogError('DOCUMENT_TRASHED',status=410)
            if row['processing_state'] != 'ready' or row['integrity_state'] != 'verified':
                raise CatalogError('VERSION_NOT_READY')
            return dict(row)

    def _checked_file(self, row, *, original=False):
        field, digest = ('original_path','source_sha256') if original else ('snapshot_path','snapshot_sha256')
        root = self.catalog.data_dir; path = root/row[field]
        if not path.resolve().is_relative_to(root) or path.is_symlink() or not path.is_file():
            raise CatalogError('SOURCE_UNAVAILABLE',status=503)
        try:raw=path.read_bytes()
        except OSError:raise CatalogError('SOURCE_UNAVAILABLE',status=503) from None
        if sha256(raw).hexdigest()!=row[digest]:raise CatalogError('SOURCE_UNAVAILABLE',status=503)
        return path,raw

    def checked_path(self,row,*,original=False):
        return self._checked_file(row,original=original)[0]

    def snapshot(self,row):
        # Parse exactly the bytes whose digest was checked, without a second read.
        _,raw=self._checked_file(row)
        try:
            obj=json.loads(raw);blocks=obj['blocks']
            if obj['metadata']['snapshot_id']!=row['snapshot_id'] or not isinstance(blocks,list):raise ValueError()
            ids=[b['block_id'] for b in blocks]
            if len(set(ids))!=len(ids) or any(b['snapshot_id']!=row['snapshot_id'] or not isinstance(b['text'],str) for b in blocks):raise ValueError()
        except (ValueError,KeyError,TypeError):raise CatalogError('SOURCE_UNAVAILABLE',status=503) from None
        return obj

    def content(self, version_id, *, kb_id=None, offset=0, limit=40):
        row = self.version(version_id,kb_id=kb_id)
        obj = self.snapshot(row)
        blocks = obj['blocks']; selected = []; size = 0
        for block in blocks[offset:offset+limit]:
            # One exceptionally long block is clipped for reading only, never for evidence.
            item = {k:block[k] for k in ('block_id','kind','text','section','locator') if k in block}
            clipped = len(item['text'])>16000
            item['text'] = item['text'][:16000]; item['truncated'] = clipped
            cost = len(encoded(item).encode())
            if selected and size+cost>96*1024: break
            selected.append(item); size += cost
        # Revalidate after file I/O so a concurrent trash cannot disclose a late response.
        self.version(version_id,kb_id=kb_id)
        return {'version_id':version_id,'document_id':row['document_id'],'knowledge_base_id':row['kb_id'],
                'display_name':row['display_name'],'version_label':row['version_label'],
                'historical':row['active_version_id']!=version_id,'blocks':selected,'total_blocks':len(blocks),
                'next_offset':offset+len(selected) if offset+len(selected)<len(blocks) else None,
                'warnings':obj['metadata'].get('warnings',[])}

    def rename_document(self,kb_id,document_id,name,*,revision):
        name = name.strip()
        if not name or len(name)>200 or any(ord(c)<32 for c in name):
            raise CatalogError('INVALID_DOCUMENT_NAME',status=422)
        with self.catalog.transaction() as conn:
            kb = self.catalog.require_kb(conn,kb_id,active=True,workspace_id=self.workspace_id)
            self.catalog.check_revision(kb,revision)
            require_document(conn,document_id,kb_id=kb_id)
            conn.execute('UPDATE documents SET display_name=? WHERE id=?',(name,document_id))
            self.catalog.touch(conn,kb_id)
            return {'document_id':document_id,'display_name':name}

    def _removal_preview(self,conn,kb_id,document_ids):
        if not document_ids or len(document_ids)!=len(set(document_ids)) or len(document_ids)>100:
            raise CatalogError('INVALID_MANAGEMENT_SELECTION',status=422)
        kb = self.catalog.require_kb(conn,kb_id,active=True,workspace_id=self.workspace_id)
        items = []
        for ident in sorted(document_ids):
            doc = require_document(conn,ident,kb_id=kb_id)
            runs = conn.execute('SELECT count(DISTINCT run_id) FROM run_sources WHERE document_id=?',(ident,)).fetchone()[0]
            artifacts = conn.execute('SELECT count(DISTINCT artifact_id) FROM artifact_sources WHERE document_id=?',(ident,)).fetchone()[0]
            items.append({'document_id':ident,'display_name':doc['display_name'],'state':doc['state'],
                          'affected_run_count':runs,'affected_artifact_count':artifacts})
        preview = {'knowledge_base_id':kb_id,'catalog_revision':kb['catalog_revision'],'items':items}
        return preview | {'preview_token':sha256(encoded(preview).encode()).hexdigest()}

    def removal_preview(self,kb_id,document_ids):
        with self.catalog.reader() as conn:
            conn.execute('BEGIN')
            return self._removal_preview(conn,kb_id,document_ids)

    def remove_documents(self,kb_id,document_ids,*,revision,preview_token):
        with self.catalog.transaction() as conn:
            kb = self.catalog.require_kb(conn,kb_id,active=True,workspace_id=self.workspace_id)
            self.catalog.check_revision(kb,revision)
            preview = self._removal_preview(conn,kb_id,document_ids)
            if preview['preview_token'] != preview_token:
                raise CatalogError('REMOVAL_PREVIEW_CHANGED')
            for ident in document_ids:
                conn.execute("UPDATE documents SET state='trashed',membership_epoch=membership_epoch+1 WHERE id=?",(ident,))
                conn.execute("UPDATE kb_snapshot_bindings SET state='revoked' WHERE document_id=? AND state='active'",(ident,))
                conn.execute("INSERT INTO trash_entries VALUES('document',?,'active',?)",(ident,utc_now()))
                conn.execute('''UPDATE runs SET status='cancel_requested',cancel_requested=1 WHERE id IN
                    (SELECT run_id FROM run_sources WHERE document_id=?) AND status IN ('queued','running')''',(ident,))
                conn.execute('''UPDATE jobs SET cancel_requested=1,state=CASE WHEN state='queued'
                    THEN 'cancelled' ELSE 'cancel_requested' END WHERE target_id IN
                    (SELECT id FROM import_inputs WHERE document_id=?) AND state IN ('queued','running')''',(ident,))
            self.catalog.touch(conn,kb_id)
            return {'document_ids':document_ids,'state':'trashed','catalog_revision':revision+1}
