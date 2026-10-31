"""Library-scoped version bindings; immutable source files are never edited here."""
from __future__ import annotations

from .catalog import CatalogError, encoded, new_id, normalized_name, utc_now


def require_document(conn, document_id, *, kb_id=None, active=True):
    row = conn.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
    if row is None or (kb_id is not None and row['kb_id'] != kb_id):
        raise CatalogError('DOCUMENT_NOT_FOUND', status=404)
    if active and row['state'] != 'active':
        raise CatalogError('DOCUMENT_TRASHED', status=410)
    return row


def bind_version(conn, document_id, version_id, *, role='current', course_artifact_id=None):
    doc = require_document(conn, document_id)
    version = conn.execute('SELECT * FROM document_versions WHERE id=? AND document_id=? AND kb_id=?',
                           (version_id, document_id, doc['kb_id'])).fetchone()
    if not version or version['processing_state'] != 'ready':
        raise CatalogError('VERSION_NOT_READY')
    if role not in ('current','course_fixed') or ((role == 'course_fixed') != bool(course_artifact_id)):
        raise CatalogError('INVALID_BINDING_ROLE', status=422)
    old = conn.execute('''SELECT * FROM kb_snapshot_bindings WHERE document_id=? AND binding_role=?
                          AND course_artifact_id IS ? AND state='active' ''',
                       (document_id, role, course_artifact_id)).fetchone()
    if old and old['version_id'] == version_id:
        return dict(old)
    epoch = conn.execute('''SELECT coalesce(max(binding_epoch),0)+1 FROM kb_snapshot_bindings
                            WHERE document_id=? AND binding_role=? AND course_artifact_id IS ?''',
                         (document_id, role, course_artifact_id)).fetchone()[0]
    if old:
        conn.execute("UPDATE kb_snapshot_bindings SET state='revoked' WHERE id=?", (old['id'],))
        conn.execute('''UPDATE runs SET cancel_requested=1,status='cancel_requested' WHERE id IN
            (SELECT run_id FROM run_sources WHERE binding_id=?) AND status IN ('queued','running')''', (old['id'],))
    ident = new_id('bind')
    conn.execute('''INSERT INTO kb_snapshot_bindings VALUES(?,?,?,?,?,?,?,?,?)''',
                 (ident, doc['kb_id'], version['snapshot_id'], document_id, version_id, role,
                  course_artifact_id, 'active', epoch))
    if role == 'current':
        conn.execute('UPDATE documents SET active_version_id=? WHERE id=?', (version_id, document_id))
    return dict(conn.execute('SELECT * FROM kb_snapshot_bindings WHERE id=?', (ident,)).fetchone())


class KnowledgeService:
    def __init__(self, catalog, workspace_id='ws_personal'):
        self.catalog, self.workspace_id = catalog, workspace_id

    def activate_version(self, kb_id, document_id, version_id, *, revision=None):
        with self.catalog.transaction() as conn:
            kb = self.catalog.require_kb(conn, kb_id, active=True, workspace_id=self.workspace_id)
            self.catalog.check_revision(kb, revision)
            require_document(conn, document_id, kb_id=kb_id)
            binding = bind_version(conn, document_id, version_id)
            self.catalog.touch(conn, kb_id)
            return binding

    def bind_course(self, kb_id, course_id, document_id, version_id):
        with self.catalog.transaction() as conn:
            self.catalog.require_kb(conn, kb_id, active=True, workspace_id=self.workspace_id)
            require_document(conn, document_id, kb_id=kb_id)
            course = conn.execute("SELECT 1 FROM artifacts WHERE id=? AND kb_id=? AND type='course'",
                                  (course_id, kb_id)).fetchone()
            if not course:
                raise CatalogError('COURSE_NOT_FOUND', status=404)
            binding = bind_version(conn, document_id, version_id, role='course_fixed', course_artifact_id=course_id)
            conn.execute('''INSERT INTO artifact_sources VALUES(?,?,?,?,?,?) ON CONFLICT(artifact_id,document_id,role)
                DO UPDATE SET version_id=excluded.version_id,snapshot_id=excluded.snapshot_id''',
                         (course_id, kb_id, document_id, version_id, binding['snapshot_id'], 'fixed'))
            self.catalog.touch(conn, kb_id, content=False)
            return binding

    def resolve_scope(self, kb_id, document_ids=None, *, course_id=None,required_document_ids=(),conn=None):
        if conn is None:
            with self.catalog.reader() as read:
                read.execute('BEGIN')
                try:
                    return self.resolve_scope(kb_id,document_ids,course_id=course_id,
                                              required_document_ids=required_document_ids,conn=read)
                finally:read.rollback()
        kb = self.catalog.require_kb(conn, kb_id, active=True, workspace_id=self.workspace_id)
        role = 'course_fixed' if course_id else 'current'
        if course_id and not conn.execute("SELECT 1 FROM artifacts WHERE id=? AND kb_id=? AND type='course'",
                                          (course_id, kb_id)).fetchone():
            raise CatalogError('COURSE_NOT_FOUND', status=404)
        rows = conn.execute('''SELECT b.*,d.membership_epoch,d.display_name,v.version_label FROM kb_snapshot_bindings b
            JOIN documents d ON d.id=b.document_id AND d.kb_id=b.kb_id
            JOIN document_versions v ON v.id=b.version_id
            WHERE b.kb_id=? AND b.state='active' AND b.binding_role=? AND b.course_artifact_id IS ?
            AND d.state='active' AND v.processing_state='ready' ORDER BY d.id''',
                            (kb_id, role, course_id)).fetchall()
        if document_ids is not None:
            if not document_ids or len(document_ids) != len(set(document_ids)):
                raise CatalogError('EMPTY_OR_DUPLICATE_SCOPE', status=422)
            known = {r['document_id'] for r in rows}
            if not set(document_ids) <= known:
                raise CatalogError('SOURCE_SCOPE_CHANGED')
            rows = [r for r in rows if r['document_id'] in document_ids]
        if not rows:
            raise CatalogError('NO_READY_SOURCES', status=422)
        versions = [{k: r[k] for k in ('document_id','version_id','snapshot_id','membership_epoch',
                                       'binding_role','binding_epoch','display_name','version_label')} | {'binding_id':r['id']} for r in rows]
        result = {'scope_contract_version':'kb_scope_v1','knowledge_base_id':kb_id,
                  'catalog_revision':kb['catalog_revision'],
                  'scope_mode':'course_fixed' if course_id else ('selected' if document_ids is not None else 'all_ready'),
                  'course_artifact_id':course_id,'allowed_versions':versions,
                  'required_document_ids':list(required_document_ids)}
        from .knowledge_scope import KnowledgeQueryScope
        try:KnowledgeQueryScope.from_resolved(result)
        except ValueError as exc:raise CatalogError('INVALID_SOURCE_SCOPE',status=422) from exc
        return result

    def scope_valid(self, scope, *, conn=None):
        if conn is None:
            with self.catalog.reader() as read:
                read.execute('BEGIN')
                try:
                    return self.scope_valid(scope, conn=read)
                finally:
                    read.rollback()
        kb = conn.execute("SELECT * FROM knowledge_bases WHERE id=? AND workspace_id=? AND state='active'",
                          (scope['knowledge_base_id'], self.workspace_id)).fetchone()
        if not kb:
            return False
        for item in scope['allowed_versions']:
            row = conn.execute('''SELECT b.*,d.state AS doc_state,d.membership_epoch FROM kb_snapshot_bindings b
                JOIN documents d ON d.id=b.document_id AND d.kb_id=b.kb_id WHERE b.id=? AND b.kb_id=?''',
                               (item['binding_id'], kb['id'])).fetchone()
            if not row or row['state'] != 'active' or row['doc_state'] != 'active':
                return False
            if any(row[k] != item[k] for k in ('document_id','version_id','snapshot_id','membership_epoch','binding_epoch','binding_role')):
                return False
            if row['course_artifact_id'] != scope.get('course_artifact_id'):
                return False
        return bool(scope['allowed_versions'])

    def trash_document(self, kb_id, document_id, *, revision=None):
        with self.catalog.transaction() as conn:
            kb = self.catalog.require_kb(conn, kb_id, active=True, workspace_id=self.workspace_id)
            self.catalog.check_revision(kb, revision)
            doc = require_document(conn, document_id, kb_id=kb_id, active=False)
            if doc['state'] == 'trashed':
                return {'document_id':document_id,'state':'trashed'}
            conn.execute("UPDATE documents SET state='trashed',membership_epoch=membership_epoch+1 WHERE id=?", (document_id,))
            conn.execute("UPDATE kb_snapshot_bindings SET state='revoked' WHERE document_id=? AND state='active'", (document_id,))
            conn.execute('''UPDATE runs SET cancel_requested=1,status='cancel_requested' WHERE id IN
                (SELECT run_id FROM run_sources WHERE document_id=?) AND status IN ('queued','running')''', (document_id,))
            conn.execute('''UPDATE jobs SET cancel_requested=1,state=CASE WHEN state='queued' THEN 'cancelled'
                ELSE 'cancel_requested' END WHERE kind='import' AND target_id IN
                (SELECT id FROM document_versions WHERE document_id=? UNION
                 SELECT id FROM import_inputs WHERE document_id=?)
                AND state IN ('queued','running')''', (document_id,document_id))
            conn.execute("INSERT INTO trash_entries VALUES('document',?,'active',?)", (document_id,utc_now()))
            self.catalog.touch(conn, kb_id)
            return {'document_id':document_id,'state':'trashed'}

    def restore_document(self, kb_id, document_id, *, revision=None):
        with self.catalog.transaction() as conn:
            kb = self.catalog.require_kb(conn, kb_id, active=True, workspace_id=self.workspace_id)
            self.catalog.check_revision(kb,revision)
            doc = require_document(conn, document_id, kb_id=kb_id, active=False)
            if doc['state'] == 'active':
                return dict(doc)
            conn.execute("UPDATE documents SET state='active',membership_epoch=membership_epoch+1 WHERE id=?", (document_id,))
            if doc['active_version_id'] is not None:
                bind_version(conn, document_id, doc['active_version_id'])
            # Restore fixed-course relationships with new identities, never revive old runs.
            for ref in conn.execute("SELECT * FROM artifact_sources WHERE document_id=? AND role='fixed'", (document_id,)).fetchall():
                bind_version(conn, document_id, ref['version_id'], role='course_fixed', course_artifact_id=ref['artifact_id'])
            conn.execute("DELETE FROM trash_entries WHERE entity_type='document' AND entity_id=?", (document_id,))
            self.catalog.touch(conn, kb_id)
            return dict(conn.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone())

    def set_library_state(self, kb_id, state, *, revision=None, name=None):
        if state not in ('active','archived','trashed'):
            raise CatalogError('INVALID_LIBRARY_STATE', status=422)
        with self.catalog.transaction() as conn:
            kb = conn.execute('SELECT * FROM knowledge_bases WHERE id=? AND workspace_id=?',
                              (kb_id,self.workspace_id)).fetchone()
            if not kb:
                raise CatalogError('KNOWLEDGE_BASE_NOT_FOUND', status=404)
            self.catalog.check_revision(kb, revision)
            if kb['state'] == state:
                return dict(kb)
            title, key = normalized_name(name if name is not None else kb['name'])
            if state == 'active' and conn.execute("SELECT 1 FROM knowledge_bases WHERE workspace_id=? AND normalized_name=? AND state='active' AND id<>?",
                                                  (self.workspace_id,key,kb_id)).fetchone():
                raise CatalogError('KNOWLEDGE_BASE_NAME_CONFLICT')
            if state == 'trashed':
                conn.execute("INSERT OR IGNORE INTO trash_entries VALUES('knowledge_base',?,?,?)", (kb_id,kb['state'],utc_now()))
            if kb['state'] == 'trashed':
                raise CatalogError('USE_EXPLICIT_TRASH_RESTORE')
            conn.execute('UPDATE knowledge_bases SET state=?,name=?,normalized_name=? WHERE id=?', (state,title,key,kb_id))
            if state != 'active':
                conn.execute("UPDATE runs SET cancel_requested=1,status='cancel_requested' WHERE kb_id=? AND status IN ('queued','running')", (kb_id,))
                conn.execute("UPDATE jobs SET cancel_requested=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE 'cancel_requested' END WHERE kb_id=? AND state IN ('queued','running')", (kb_id,))
            self.catalog.touch(conn, kb_id, content=False)
            return dict(conn.execute('SELECT * FROM knowledge_bases WHERE id=?', (kb_id,)).fetchone())

    def restore_library(self, kb_id, *, name=None, revision=None):
        with self.catalog.transaction() as conn:
            kb = conn.execute('SELECT * FROM knowledge_bases WHERE id=? AND workspace_id=?',
                              (kb_id,self.workspace_id)).fetchone()
            if not kb:
                raise CatalogError('KNOWLEDGE_BASE_NOT_FOUND', status=404)
            self.catalog.check_revision(kb,revision)
            entry = conn.execute("SELECT * FROM trash_entries WHERE entity_type='knowledge_base' AND entity_id=?", (kb_id,)).fetchone()
            if kb['state'] != 'trashed':
                return dict(kb)
            if not entry:
                raise CatalogError('TRASH_STATE_MISSING')
            state = entry['original_state']
            title, key = normalized_name(name if name is not None else kb['name'])
            if state == 'active' and conn.execute("SELECT 1 FROM knowledge_bases WHERE workspace_id=? AND normalized_name=? AND state='active' AND id<>?",
                                                  (self.workspace_id,key,kb_id)).fetchone():
                raise CatalogError('KNOWLEDGE_BASE_NAME_CONFLICT')
            conn.execute('UPDATE knowledge_bases SET state=?,name=?,normalized_name=? WHERE id=?', (state,title,key,kb_id))
            conn.execute("DELETE FROM trash_entries WHERE entity_type='knowledge_base' AND entity_id=?", (kb_id,))
            self.catalog.touch(conn, kb_id, content=False)
            return dict(conn.execute('SELECT * FROM knowledge_bases WHERE id=?', (kb_id,)).fetchone())
