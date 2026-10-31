"""Transactional personal-workspace catalog. Never loads models or changes snapshots."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import sqlite3
import threading
import unicodedata
from uuid import uuid4


SCHEMA_VERSION = 4
_SCHEMA_LOCK = threading.RLock()
SCHEMA = """
CREATE TABLE schema_migrations (
 version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL
);
CREATE TABLE workspaces (
 id TEXT PRIMARY KEY, owner_principal TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE knowledge_bases (
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 60), normalized_name TEXT NOT NULL,
 description TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'active'
 CHECK(state IN ('active','archived','trashed')), catalog_revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, content_updated_at TEXT NOT NULL,
 UNIQUE(id, workspace_id)
);
CREATE UNIQUE INDEX active_kb_name ON knowledge_bases(workspace_id,normalized_name)
 WHERE state='active';
CREATE TABLE snapshots (
 snapshot_id TEXT PRIMARY KEY, snapshot_scheme TEXT NOT NULL,
 source_sha256 TEXT NOT NULL, parser_identity TEXT NOT NULL,
 snapshot_path TEXT NOT NULL UNIQUE, original_path TEXT NOT NULL,
 snapshot_sha256 TEXT NOT NULL, metadata_json TEXT NOT NULL,
 integrity_state TEXT NOT NULL CHECK(integrity_state IN ('verified','unavailable'))
);
CREATE TABLE documents (
 id TEXT PRIMARY KEY, kb_id TEXT NOT NULL REFERENCES knowledge_bases(id),
 display_name TEXT NOT NULL, original_filename TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','trashed')),
 active_version_id TEXT, membership_epoch INTEGER NOT NULL DEFAULT 1,
 UNIQUE(id,kb_id),
 FOREIGN KEY(active_version_id,id,kb_id) REFERENCES document_versions(id,document_id,kb_id)
);
CREATE TABLE document_versions (
 id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, document_id TEXT NOT NULL,
 snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
 version_label TEXT NOT NULL, sequence INTEGER NOT NULL CHECK(sequence>0),
 parent_version_id TEXT, processing_state TEXT NOT NULL CHECK(processing_state IN
 ('queued','processing','ready','failed')), created_at TEXT NOT NULL,
 UNIQUE(document_id,sequence), UNIQUE(id,document_id,kb_id),
 UNIQUE(id,document_id,kb_id,snapshot_id),
 FOREIGN KEY(document_id,kb_id) REFERENCES documents(id,kb_id),
 FOREIGN KEY(parent_version_id,document_id,kb_id) REFERENCES document_versions(id,document_id,kb_id)
);
CREATE TRIGGER current_version_ready BEFORE UPDATE OF active_version_id ON documents
 WHEN NEW.active_version_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM document_versions WHERE id=NEW.active_version_id AND document_id=NEW.id
 AND kb_id=NEW.kb_id AND processing_state='ready')
 BEGIN SELECT RAISE(ABORT,'CURRENT_VERSION_NOT_READY'); END;
CREATE TABLE artifacts (
 id TEXT PRIMARY KEY, kb_id TEXT NOT NULL REFERENCES knowledge_bases(id),
 type TEXT NOT NULL, title TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 status TEXT NOT NULL DEFAULT 'ready', content_json TEXT NOT NULL DEFAULT '{}',
 recipe_identity TEXT NOT NULL DEFAULT '', scope_json TEXT NOT NULL DEFAULT '{}',
 source_state TEXT NOT NULL DEFAULT 'valid', created_at TEXT NOT NULL,
 UNIQUE(id,kb_id)
);
CREATE TABLE kb_snapshot_bindings (
 id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
 document_id TEXT NOT NULL, version_id TEXT NOT NULL,
 binding_role TEXT NOT NULL CHECK(binding_role IN ('current','course_fixed')),
 course_artifact_id TEXT, state TEXT NOT NULL CHECK(state IN ('active','revoked')),
 binding_epoch INTEGER NOT NULL CHECK(binding_epoch>0),
 UNIQUE(id,kb_id,document_id,version_id,snapshot_id),
 CHECK((binding_role='current' AND course_artifact_id IS NULL) OR
 (binding_role='course_fixed' AND course_artifact_id IS NOT NULL)),
 FOREIGN KEY(version_id,document_id,kb_id,snapshot_id)
 REFERENCES document_versions(id,document_id,kb_id,snapshot_id),
 FOREIGN KEY(course_artifact_id,kb_id) REFERENCES artifacts(id,kb_id)
);
CREATE UNIQUE INDEX one_current_per_document ON kb_snapshot_bindings(kb_id,document_id)
 WHERE state='active' AND binding_role='current';
CREATE UNIQUE INDEX one_current_snapshot_per_kb ON kb_snapshot_bindings(kb_id,snapshot_id)
 WHERE state='active' AND binding_role='current';
CREATE UNIQUE INDEX one_fixed_version_per_course_document ON kb_snapshot_bindings
 (kb_id,course_artifact_id,document_id) WHERE state='active' AND binding_role='course_fixed';
CREATE TABLE artifact_sources (
 artifact_id TEXT NOT NULL, kb_id TEXT NOT NULL, document_id TEXT NOT NULL,
 version_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, role TEXT NOT NULL,
 PRIMARY KEY(artifact_id,document_id,role),
 FOREIGN KEY(artifact_id,kb_id) REFERENCES artifacts(id,kb_id),
 FOREIGN KEY(version_id,document_id,kb_id,snapshot_id)
 REFERENCES document_versions(id,document_id,kb_id,snapshot_id)
);
CREATE TABLE sessions (
 id TEXT PRIMARY KEY, kb_id TEXT NOT NULL REFERENCES knowledge_bases(id),
 title TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 state TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(id,kb_id)
);
CREATE TABLE runs (
 id TEXT PRIMARY KEY, kb_id TEXT NOT NULL REFERENCES knowledge_bases(id),
 session_id TEXT, purpose TEXT NOT NULL, submission_owner_id TEXT NOT NULL,
 request_key TEXT NOT NULL, request_sha256 TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('queued','running','cancel_requested','succeeded',
 'abstained','failed','cancelled','interrupted')),
 scope_json TEXT NOT NULL, result_json TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, finished_at TEXT,
 UNIQUE(submission_owner_id,request_key), UNIQUE(id,kb_id),
 FOREIGN KEY(session_id,kb_id) REFERENCES sessions(id,kb_id)
);
CREATE UNIQUE INDEX one_pending_chat_per_session ON runs(session_id)
 WHERE purpose='chat' AND status IN ('queued','running','cancel_requested');
CREATE TABLE run_sources (
 run_id TEXT NOT NULL, kb_id TEXT NOT NULL, document_id TEXT NOT NULL,
 version_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, binding_id TEXT NOT NULL,
 membership_epoch INTEGER NOT NULL,
 binding_epoch INTEGER NOT NULL, required INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(run_id,document_id), FOREIGN KEY(run_id,kb_id) REFERENCES runs(id,kb_id),
 FOREIGN KEY(version_id,document_id,kb_id,snapshot_id)
 REFERENCES document_versions(id,document_id,kb_id,snapshot_id),
 FOREIGN KEY(binding_id,kb_id,document_id,version_id,snapshot_id)
 REFERENCES kb_snapshot_bindings(id,kb_id,document_id,version_id,snapshot_id)
);
CREATE TABLE run_events (
 run_id TEXT NOT NULL REFERENCES runs(id), sequence INTEGER NOT NULL,
 event_kind TEXT NOT NULL, safe_payload TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(run_id,sequence)
);
CREATE TABLE jobs (
 id TEXT PRIMARY KEY, kb_id TEXT NOT NULL REFERENCES knowledge_bases(id),
 kind TEXT NOT NULL, target_id TEXT NOT NULL, request_key TEXT NOT NULL,
 input_sha256 TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN
 ('queued','running','cancel_requested','succeeded','failed','cancelled','interrupted')),
 stage TEXT NOT NULL DEFAULT 'queued', attempt INTEGER NOT NULL DEFAULT 1,
 lease_token TEXT, lease_until REAL, cancel_requested INTEGER NOT NULL DEFAULT 0,
 error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(kb_id,kind,request_key)
);
CREATE TABLE trash_entries (
 entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, original_state TEXT NOT NULL,
 deleted_at TEXT NOT NULL, PRIMARY KEY(entity_type,entity_id)
);
CREATE TABLE data_migrations (
 name TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL, result_json TEXT NOT NULL,
 applied_at TEXT NOT NULL
);
"""

# Version 1 is immutable: existing candidates keep their original checksum.
SCHEMA_V2 = """
CREATE TABLE import_inputs (
 id TEXT PRIMARY KEY, kb_id TEXT NOT NULL REFERENCES knowledge_bases(id),
 document_id TEXT, parent_version_id TEXT, membership_epoch INTEGER,
 filename TEXT NOT NULL, display_name TEXT NOT NULL, version_label TEXT NOT NULL,
 origin TEXT NOT NULL CHECK(origin IN ('file','pasted_text')),
 staged_path TEXT NOT NULL UNIQUE, source_sha256 TEXT NOT NULL,
 published_snapshot_id TEXT, result_json TEXT, created_at TEXT NOT NULL,
 FOREIGN KEY(document_id,kb_id) REFERENCES documents(id,kb_id),
 FOREIGN KEY(parent_version_id,document_id,kb_id) REFERENCES document_versions(id,document_id,kb_id)
);
CREATE INDEX imports_by_document ON import_inputs(document_id);
"""
SCHEMA_V3 = """
ALTER TABLE sessions ADD COLUMN last_message_id TEXT REFERENCES messages(id);
ALTER TABLE runs ADD COLUMN question TEXT NOT NULL DEFAULT '';
ALTER TABLE runs ADD COLUMN resolved_question TEXT NOT NULL DEFAULT '';
ALTER TABLE runs ADD COLUMN started_at TEXT;
ALTER TABLE runs ADD COLUMN worker_job_id TEXT REFERENCES jobs(id);
ALTER TABLE runs ADD COLUMN parent_run_id TEXT REFERENCES runs(id);
CREATE UNIQUE INDEX runs_session_identity ON runs(id,session_id,kb_id);
CREATE TABLE messages (
 id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, session_id TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('user','assistant')), body TEXT NOT NULL DEFAULT '',
 run_id TEXT NOT NULL, parent_message_id TEXT, sequence INTEGER NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(id,session_id,kb_id), UNIQUE(session_id,sequence),
 UNIQUE(run_id,role),
 FOREIGN KEY(session_id,kb_id) REFERENCES sessions(id,kb_id),
 FOREIGN KEY(run_id,session_id,kb_id) REFERENCES runs(id,session_id,kb_id),
 FOREIGN KEY(parent_message_id,session_id,kb_id) REFERENCES messages(id,session_id,kb_id)
);
CREATE TRIGGER valid_session_last_message BEFORE UPDATE OF last_message_id ON sessions
 WHEN NEW.last_message_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM messages WHERE id=NEW.last_message_id AND session_id=NEW.id AND kb_id=NEW.kb_id)
 BEGIN SELECT RAISE(ABORT,'INVALID_SESSION_LAST_MESSAGE'); END;
CREATE UNIQUE INDEX run_sources_citation_identity ON run_sources(run_id,kb_id,document_id,version_id,snapshot_id);
CREATE TABLE citations (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kb_id TEXT NOT NULL, document_id TEXT NOT NULL,
 version_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, evidence_key TEXT NOT NULL,
 block_id TEXT NOT NULL, quotes_json TEXT NOT NULL, quote_sha256 TEXT NOT NULL,
 locator_json TEXT NOT NULL, section TEXT NOT NULL, parser_verified INTEGER NOT NULL,
 UNIQUE(run_id,evidence_key), UNIQUE(id,kb_id),
 FOREIGN KEY(run_id,kb_id,document_id,version_id,snapshot_id)
 REFERENCES run_sources(run_id,kb_id,document_id,version_id,snapshot_id)
);
CREATE TABLE notes (
 id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, kb_id TEXT NOT NULL,
 origin_run_id TEXT, title TEXT NOT NULL, body TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, edited INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(id,kb_id), UNIQUE(artifact_id),
 FOREIGN KEY(artifact_id,kb_id) REFERENCES artifacts(id,kb_id),
 FOREIGN KEY(origin_run_id,kb_id) REFERENCES runs(id,kb_id)
);
CREATE TABLE note_citation_refs (
 note_id TEXT NOT NULL, citation_id TEXT NOT NULL, kb_id TEXT NOT NULL,
 PRIMARY KEY(note_id,citation_id),
 FOREIGN KEY(note_id,kb_id) REFERENCES notes(id,kb_id),
 FOREIGN KEY(citation_id,kb_id) REFERENCES citations(id,kb_id)
);
CREATE TABLE mutation_receipts (
 owner_id TEXT NOT NULL, request_key TEXT NOT NULL, operation TEXT NOT NULL,
 input_sha256 TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(owner_id,request_key)
);
CREATE TABLE overview_cache (
 kb_id TEXT NOT NULL REFERENCES knowledge_bases(id), scope_key TEXT NOT NULL,
 artifact_id TEXT NOT NULL, PRIMARY KEY(kb_id,scope_key),
 FOREIGN KEY(artifact_id,kb_id) REFERENCES artifacts(id,kb_id)
);
"""
SCHEMA_V4 = """
CREATE TABLE artifact_runs (
 artifact_id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, run_id TEXT NOT NULL UNIQUE,
 FOREIGN KEY(artifact_id,kb_id) REFERENCES artifacts(id,kb_id),
 FOREIGN KEY(run_id,kb_id) REFERENCES runs(id,kb_id)
);
"""
MIGRATIONS = ((1, SCHEMA), (2, SCHEMA_V2), (3, SCHEMA_V3), (4, SCHEMA_V4))


def schema_fingerprints():
    return [(version, sha256(sql.encode()).hexdigest()) for version, sql in MIGRATIONS]


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def new_id(prefix):
    return prefix + '_' + uuid4().hex


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class CatalogError(ValueError):
    def __init__(self, code, *, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def normalized_name(value):
    value = value.strip()
    if not 1 <= len(value) <= 60 or any(ord(c) < 32 for c in value):
        raise CatalogError('INVALID_KNOWLEDGE_BASE_NAME', status=422)
    return value, unicodedata.normalize('NFKC', value).casefold()


class Catalog:
    def __init__(self, data_dir, *, initialize=True):
        self.data_dir = Path(data_dir).resolve()
        self.path = self.data_dir / 'catalog' / 'app.sqlite3'
        if initialize:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.initialize()
        elif not self.path.is_file():
            raise CatalogError('CATALOG_NOT_FOUND', status=404)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=5000')
        conn.execute('PRAGMA synchronous=FULL')
        return conn

    @contextmanager
    def reader(self):
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.reader() as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def initialize(self):
        with _SCHEMA_LOCK, self.transaction() as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()
            installed = []
            if exists:
                versions = conn.execute('SELECT version,checksum FROM schema_migrations ORDER BY version').fetchall()
                installed = [(r['version'], r['checksum']) for r in versions]
                if not installed or installed != schema_fingerprints()[:len(installed)]:
                    raise CatalogError('CATALOG_SCHEMA_VERSION_MISMATCH')
            for version, sql in MIGRATIONS[len(installed):]:
                statement = ''
                for line in sql.splitlines(True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        conn.execute(statement)
                        statement = ''
                if statement.strip():
                    raise RuntimeError('Incomplete schema statement')
                conn.execute('INSERT INTO schema_migrations VALUES(?,?,?)',
                             (version, sha256(sql.encode()).hexdigest(), utc_now()))

    @staticmethod
    def workspace(conn, workspace_id='ws_personal'):
        conn.execute('INSERT OR IGNORE INTO workspaces VALUES(?,?,?,?)',
                     (workspace_id, workspace_id, '个人知识库', utc_now()))
        return workspace_id

    @staticmethod
    def require_kb(conn, kb_id, *, active=False, workspace_id='ws_personal'):
        row = conn.execute('SELECT * FROM knowledge_bases WHERE id=? AND workspace_id=?',
                           (kb_id, workspace_id)).fetchone()
        if row is None:
            raise CatalogError('KNOWLEDGE_BASE_NOT_FOUND', status=404)
        if row['state'] == 'trashed':
            raise CatalogError('KNOWLEDGE_BASE_TRASHED', status=410)
        if active and row['state'] != 'active':
            raise CatalogError('KNOWLEDGE_BASE_READ_ONLY')
        return row

    @staticmethod
    def check_revision(row, revision):
        if revision is not None and row['catalog_revision'] != revision:
            raise CatalogError('CATALOG_REVISION_CONFLICT')

    @staticmethod
    def touch(conn, kb_id, *, content=True):
        if content:
            conn.execute('UPDATE knowledge_bases SET catalog_revision=catalog_revision+1,content_updated_at=? WHERE id=?',
                         (utc_now(), kb_id))
        else:
            conn.execute('UPDATE knowledge_bases SET catalog_revision=catalog_revision+1 WHERE id=?', (kb_id,))

    def create_knowledge_base(self, name=None, *, workspace_id='ws_personal', kb_id=None):
        with self.transaction() as conn:
            self.workspace(conn, workspace_id)
            if name is None:
                name, number = '未命名知识库', 1
                while conn.execute("SELECT 1 FROM knowledge_bases WHERE workspace_id=? AND normalized_name=? AND state='active'",
                                   (workspace_id, normalized_name(name)[1])).fetchone():
                    number += 1
                    name = f'未命名知识库 {number}'
            name, key = normalized_name(name)
            ident, now = kb_id or new_id('kb'), utc_now()
            try:
                conn.execute('INSERT INTO knowledge_bases(id,workspace_id,name,normalized_name,created_at,content_updated_at) VALUES(?,?,?,?,?,?)',
                             (ident, workspace_id, name, key, now, now))
            except sqlite3.IntegrityError as exc:
                raise CatalogError('KNOWLEDGE_BASE_NAME_CONFLICT') from exc
            return dict(conn.execute('SELECT * FROM knowledge_bases WHERE id=?', (ident,)).fetchone())

    def audit(self):
        with self.reader() as conn:
            conn.execute('BEGIN')
            schema_rows = conn.execute('SELECT version,checksum FROM schema_migrations ORDER BY version').fetchall()
            compatible = [(r['version'],r['checksum']) for r in schema_rows] == schema_fingerprints()
            integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
            foreign_keys = [tuple(r) for r in conn.execute('PRAGMA foreign_key_check')]
            invalid_current = conn.execute('''SELECT d.id FROM documents d LEFT JOIN kb_snapshot_bindings b
                ON b.document_id=d.id AND b.binding_role='current' AND b.state='active'
                WHERE d.state='active' AND d.active_version_id IS NOT NULL
                AND (b.id IS NULL OR b.version_id<>d.active_version_id)''').fetchall()
            invalid_bindings = conn.execute('''SELECT b.id FROM kb_snapshot_bindings b
                JOIN documents d ON d.id=b.document_id JOIN document_versions v ON v.id=b.version_id
                LEFT JOIN artifacts a ON a.id=b.course_artifact_id
                WHERE b.state='active' AND (d.state<>'active' OR v.processing_state<>'ready'
                OR (b.binding_role='current' AND (d.active_version_id IS NULL OR d.active_version_id<>b.version_id))
                OR (b.binding_role='course_fixed' AND (a.id IS NULL OR a.type<>'course')))''').fetchall()
            counts = {t: conn.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in
                      ('knowledge_bases','documents','document_versions','snapshots','kb_snapshot_bindings','artifacts','runs','jobs')}
        return {'ok': compatible and integrity == 'ok' and not foreign_keys and not invalid_current and not invalid_bindings,
                'schema_compatible':compatible,'integrity': integrity, 'foreign_key_violations': foreign_keys,
                'invalid_current_documents': [r[0] for r in invalid_current],
                'invalid_active_bindings':[r[0] for r in invalid_bindings], 'counts': counts}
