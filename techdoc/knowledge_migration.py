"""Offline, idempotent mapping of legacy snapshots. Does not reparse or rewrite evidence."""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import json
import re
from uuid import NAMESPACE_URL, uuid5

from .catalog import Catalog, CatalogError, encoded, normalized_name, utc_now
from .knowledge import bind_version


MIGRATION_NAME = 'legacy_knowledge_v1'
EXTENSIONS = {'.pdf','.docx','.md','.txt','.html','.htm'}


def stable_id(kind, value):
    return kind + '_' + uuid5(NAMESPACE_URL, 'techdoc/knowledge/v1/'+kind+'/'+value).hex


def source_manifest(data_dir):
    root = Path(data_dir)
    if not (root/'originals').is_dir():
        raise CatalogError('ORIGINALS_DIRECTORY_MISSING')
    if list(root.glob('*.deleting')) or list(root.glob('.tmp_*')) or list((root/'originals').glob('.tmp_*')):
        raise CatalogError('UNFINISHED_LEGACY_MUTATION')
    files = sorted(root.glob('*.json')) + sorted((root/'originals').glob('*'))
    result = {}
    for path in files:
        if not path.is_file():
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise CatalogError('UNSAFE_SOURCE_PATH')
        if path.stat().st_size > 32*1024*1024:
            raise CatalogError('SOURCE_FILE_BUDGET_EXCEEDED')
        result[path.relative_to(root).as_posix()] = sha256(path.read_bytes()).hexdigest()
    return result


def plan_migration(data_dir, course_config=None, case_ids=None):
    root = Path(data_dir).resolve()
    manifest = source_manifest(root)
    records = {}
    for path in sorted(root.glob('*.json')):
        if not re.fullmatch('[0-9a-f]{28}', path.stem):
            raise CatalogError('UNEXPECTED_LEGACY_SNAPSHOT_FILE')
        obj = json.loads(path.read_text(encoding='utf-8'))
        meta, blocks = obj['metadata'], obj['blocks']
        sid, name = meta['snapshot_id'], meta['filename']
        if sid != path.stem or Path(name.replace('\\','/')).name != name or Path(name).suffix.lower() not in EXTENSIONS:
            raise CatalogError('INVALID_LEGACY_SNAPSHOT_IDENTITY')
        if meta.get('block_count') != len(blocks) or not blocks:
            raise CatalogError('INVALID_LEGACY_BLOCK_COUNT')
        original = 'originals/'+sid+Path(name).suffix.lower()
        if not meta.get('original_stored') or manifest.get(original) != meta['source_sha256']:
            raise CatalogError('ORIGINAL_SOURCE_HASH_MISMATCH')
        seen = set()
        for i, block in enumerate(blocks):
            expected = sha256(json.dumps([sid,i,block['kind'],block['text'],block['locator']],
                                        ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:20]
            if block['snapshot_id'] != sid or block['block_id'] != expected or block['block_id'] in seen:
                raise CatalogError('LEGACY_BLOCK_IDENTITY_MISMATCH')
            seen.add(block['block_id'])
        records[sid] = {'metadata':meta,'snapshot_path':path.name,'original_path':original,
                        'snapshot_sha256':manifest[path.name]}
    selectors = (course_config or {}).get('documents', {})
    selected = {}
    for key, spec in selectors.items():
        if not spec or not set(spec) <= {'filename','product_version','source_sha256'}:
            raise CatalogError('INVALID_COURSE_SOURCE_SELECTOR')
        matches = [sid for sid,r in records.items() if all(r['metadata'].get(k)==v for k,v in spec.items())]
        if len(matches) != 1:
            raise CatalogError('COURSE_SOURCE_NOT_UNIQUE_'+key)
        selected[key] = matches[0]
    parent = {sid:r['metadata'].get('supersedes_snapshot_id') for sid,r in records.items()}
    roots = {}
    for sid in records:
        current, visited = sid, set()
        while parent[current]:
            if current in visited:
                raise CatalogError('REVISION_CYCLE')
            visited.add(current)
            previous = parent[current]
            if previous not in records:
                raise CatalogError('REVISION_PARENT_MISSING')
            if records[previous]['metadata']['filename'] != records[current]['metadata']['filename']:
                raise CatalogError('REVISION_FILENAME_MISMATCH')
            current = previous
        roots[sid] = current
    groups = {}
    for sid, first in roots.items():
        groups.setdefault(first, []).append(sid)
    chains = []
    for first, members in sorted(groups.items()):
        leaves = set(members) - {parent[sid] for sid in members if parent[sid]}
        if len(leaves) != 1:
            raise CatalogError('REVISION_BRANCH_REQUIRES_EXPLICIT_REVIEW')
        current = next(iter(leaves)); order = [current]
        while parent[order[-1]]:
            order.append(parent[order[-1]])
        order.reverse()
        if set(order) != set(members):
            raise CatalogError('INVALID_REVISION_CHAIN')
        library = 'tutorial' if set(members) & set(selected.values()) else 'default'
        chains.append({'root':first,'snapshots':order,'current':current,'library':library})
    payload = {'source_manifest':manifest,'course_documents':selectors,'case_ids':case_ids or []}
    return {'data_dir':str(root),'input_sha256':sha256(encoded(payload).encode()).hexdigest(),
            'source_manifest':manifest,'records':records,'chains':chains,'course_sources':selected,
            'case_ids':case_ids or []}


def summarize_plan(plan):
    return {'dry_run':True,'migration':MIGRATION_NAME,'input_sha256':plan['input_sha256'],
            'source_files':len(plan['source_manifest']),'snapshots':len(plan['records']),
            'logical_documents':len(plan['chains']),
            'libraries':sorted({c['library'] for c in plan['chains']}),
            'documents':[{'filename':plan['records'][c['root']]['metadata']['filename'],
                          'library':c['library'],'versions':c['snapshots'],'current':c['current']} for c in plan['chains']],
            'course_sources':plan['course_sources'],'case_ids':plan['case_ids']}


def migrate_legacy(data_dir, course_config=None, case_ids=None):
    """Call only on a stopped/isolated data directory; the CLI checks live-service ownership."""
    plan = plan_migration(data_dir, course_config, case_ids)
    catalog = Catalog(data_dir)
    with catalog.transaction() as conn:
        old = conn.execute('SELECT * FROM data_migrations WHERE name=?', (MIGRATION_NAME,)).fetchone()
        if old:
            if old['input_sha256'] != plan['input_sha256']:
                raise CatalogError('MIGRATION_INPUT_CHANGED')
            return dict(json.loads(old['result_json']), already_applied=True)
        if conn.execute('SELECT 1 FROM knowledge_bases LIMIT 1').fetchone():
            raise CatalogError('MIGRATION_REQUIRES_EMPTY_CATALOG')
        catalog.workspace(conn)
        now = utc_now(); library_ids = {}
        kinds = {c['library'] for c in plan['chains']}
        for key in sorted(kinds):
            title = 'RAG 与 Agent 教程' if key == 'tutorial' else '默认知识库'
            ident = stable_id('kb', 'ws_personal/'+key); library_ids[key] = ident
            conn.execute('''INSERT INTO knowledge_bases(id,workspace_id,name,normalized_name,created_at,content_updated_at)
                            VALUES(?,?,?,?,?,?)''', (ident,'ws_personal',title,normalized_name(title)[1],now,now))
        for sid, record in plan['records'].items():
            meta = record['metadata']
            conn.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?)',
                         (sid,'legacy_v1',meta['source_sha256'],meta.get('parser','legacy'),record['snapshot_path'],
                          record['original_path'],record['snapshot_sha256'],encoded(meta),'verified'))
        mapping, used_names = {}, {}
        for chain in plan['chains']:
            kb_id = library_ids[chain['library']]
            doc_id = stable_id('doc', kb_id+'/'+chain['root'])
            name = plan['records'][chain['root']]['metadata']['filename']
            count_key = (kb_id,name); used_names[count_key] = used_names.get(count_key,0)+1
            display = name if used_names[count_key] == 1 else name+f" ({used_names[count_key]})"
            conn.execute('INSERT INTO documents(id,kb_id,display_name,original_filename) VALUES(?,?,?,?)',
                         (doc_id,kb_id,display,name))
            previous_id = None
            for sequence,sid in enumerate(chain['snapshots'],1):
                meta = plan['records'][sid]['metadata']; version_id = stable_id('ver',doc_id+'/'+sid)
                conn.execute('INSERT INTO document_versions VALUES(?,?,?,?,?,?,?,?,?)',
                             (version_id,kb_id,doc_id,sid,meta.get('product_version',''),sequence,previous_id,'ready',now))
                mapping[sid] = {'kb_id':kb_id,'document_id':doc_id,'version_id':version_id}
                previous_id = version_id
            bind_version(conn, doc_id, mapping[chain['current']]['version_id'])
        course_id = None
        if plan['course_sources']:
            kb_id = library_ids['tutorial']; course_id = stable_id('artifact',kb_id+'/tutorial_dev')
            content = {'case_ids':plan['case_ids'],'source_keys':plan['course_sources'],
                       'reference_type':'ASSISTED_DEVELOPMENT_NOT_HUMAN_GOLD'}
            conn.execute('''INSERT INTO artifacts(id,kb_id,type,title,content_json,recipe_identity,created_at)
                            VALUES(?,?,?,?,?,?,?)''',
                         (course_id,kb_id,'course','RAG 与 Agent 入门练习',encoded(content),'tutorial_dev_fixed_v1',now))
            for sid in dict.fromkeys(plan['course_sources'].values()):
                ref = mapping[sid]
                bind_version(conn,ref['document_id'],ref['version_id'],role='course_fixed',course_artifact_id=course_id)
                conn.execute('INSERT INTO artifact_sources VALUES(?,?,?,?,?,?)',
                             (course_id,kb_id,ref['document_id'],ref['version_id'],sid,'fixed'))
        if source_manifest(data_dir) != plan['source_manifest']:
            raise CatalogError('SOURCES_CHANGED_DURING_MIGRATION')
        result = {'migration':MIGRATION_NAME,'input_sha256':plan['input_sha256'],
                  'knowledge_bases':library_ids,'course_artifact_id':course_id,'snapshot_mapping':mapping,
                  'source_files':len(plan['source_manifest']),'snapshots':len(plan['records']),
                  'logical_documents':len(plan['chains']),'already_applied':False}
        conn.execute('INSERT INTO data_migrations VALUES(?,?,?,?)',
                     (MIGRATION_NAME,plan['input_sha256'],encoded(result),now))
    return result
