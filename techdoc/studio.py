"""Notes are saved without a model call; edited notes never inherit answer review."""
import json
import os
import re
from hashlib import sha256

from .catalog import CatalogError,encoded,new_id,utc_now
from .receipts import replay,remember,request_digest,check_key
from .runs import SUCCESS


class StudioService:
    def __init__(self,catalog,runs,workspace):
        self.catalog,self.runs,self.workspace=catalog,runs,workspace
        self.workspace_id=workspace.workspace_id

    def _overview_scope(self,conn,kb_id,mode,document_ids):
        if mode not in ('all_ready','selected') or (mode=='all_ready' and document_ids is not None):
            raise CatalogError('INVALID_SOURCE_SCOPE',status=422)
        if mode=='selected' and not document_ids:raise CatalogError('EMPTY_OR_DUPLICATE_SCOPE',status=422)
        scope=self.workspace.resolve_scope(kb_id,document_ids if mode=='selected' else None,conn=conn)
        # Small overviews cover each selected document. Larger collections have
        # a fixed retrieval budget and expose actual coverage, never claim full reading.
        if len(scope['allowed_versions'])<=4:
            scope['required_document_ids']=[r['document_id'] for r in scope['allowed_versions']]
        scope['answer_mode']='answer'
        scope['window_policy']=os.getenv('TECHDOC_WINDOW_POLICY','legacy')
        if scope['window_policy'] not in ('legacy','zh_questions_v1'):raise CatalogError('INVALID_WINDOW_POLICY',status=422)
        scope['overview_sampling']=os.getenv('TECHDOC_OVERVIEW_SAMPLING','legacy')
        if scope['overview_sampling'] not in ('legacy','representative_v1'):raise CatalogError('INVALID_OVERVIEW_SAMPLING',status=422)
        scope['generation_policy']=os.getenv('TECHDOC_OVERVIEW_GENERATION_POLICY','citation_v2')
        policy={'recipe':'grounded_intro_overview_v2','retrieval_recipe':'kb_pairs64_total_v4','int8':os.getenv('TECHDOC_MODEL_INT8','0'),'model':os.getenv('LLM_MODEL',''),
                'provider':os.getenv('LLM_BASE_URL',''),'retriever':self.runs.store.retriever_mode,
                'context':self.runs.store.context_policy,'generation':scope['generation_policy'],
                'embedding':getattr(self.runs.store.dense,'cache_identity',None),
                'reranker':getattr(self.runs.store.reranker,'_stable_cache_identity',None) or getattr(self.runs.store.reranker,'model_id',None),
                'rerank_max_length':getattr(self.runs.store.reranker,'max_length',None)}
        if scope['window_policy']!='legacy':policy['window']=scope['window_policy']
        if scope['overview_sampling']!='legacy':policy['recipe']='grounded_intro_overview_v3'
        # Display-name edits do not trigger another paid request.
        refs=[{k:r[k] for k in ('document_id','version_id','snapshot_id','membership_epoch','binding_id','binding_epoch')}
              for r in scope['allowed_versions']]
        return scope,request_digest({'versions':refs,'policy':policy})

    def overview(self,kb_id,*,mode='all_ready',document_ids=None):
        with self.catalog.reader() as conn:
            conn.execute('BEGIN')
            scope,key=self._overview_scope(conn,kb_id,mode,document_ids)
            row=conn.execute('SELECT artifact_id FROM overview_cache WHERE kb_id=? AND scope_key=?',(kb_id,key)).fetchone()
            return {'scope_key':key,'source_count':len(scope['allowed_versions']),
                    'artifact':self._artifact(conn,row['artifact_id'],kb_id) if row else None}

    def request_overview(self,kb_id,*,request_key,mode='all_ready',document_ids=None,regenerate=False):
        check_key(request_key)
        value={'kb_id':kb_id,'scope_mode':mode,'document_ids':document_ids,'regenerate':regenerate}
        with self.catalog.transaction() as conn:
            self.catalog.require_kb(conn,kb_id,workspace_id=self.workspace_id)
            old=replay(conn,self.workspace_id,request_key,'overview',value)
            if old:return self._artifact(conn,old['artifact_id'],kb_id)
            if not all(os.getenv(k) for k in ('LLM_BASE_URL','LLM_MODEL','LLM_API_KEY')):
                raise CatalogError('OVERVIEW_MODEL_NOT_CONFIGURED',status=503)
            scope,key=self._overview_scope(conn,kb_id,mode,document_ids)
            existing=conn.execute('SELECT artifact_id FROM overview_cache WHERE kb_id=? AND scope_key=?',(kb_id,key)).fetchone()
            if existing:
                previous=self._artifact(conn,existing['artifact_id'],kb_id)
                if not regenerate or previous['status'] in ('queued','running','cancel_requested'):
                    remember(conn,self.workspace_id,request_key,'overview',value,{'artifact_id':previous['id']})
                    return previous
            ident,now=new_id('artifact'),utc_now();version=previous['version']+1 if existing else 1
            conn.execute('''INSERT INTO artifacts(id,kb_id,type,title,version,status,recipe_identity,scope_json,created_at)
                VALUES(?,?,'overview','资料概览',?,'queued','grounded_overview_v1',?,?)''',(ident,kb_id,version,encoded(scope),now))
            if scope['overview_sampling']!='legacy':conn.execute('UPDATE artifacts SET recipe_identity=? WHERE id=?',('grounded_intro_overview_v3',ident))
            question='请分别概括每份资料在这些原文片段中明确说明的主题和要点。只概括片段已有内容，无须覆盖全文或补全流程；每份资料至少给出一条带引用的要点。'
            self.runs.enqueue_artifact(conn,kb_id,ident,scope,question,request_key,value)
            for ref in scope['allowed_versions']:
                conn.execute('INSERT INTO artifact_sources VALUES(?,?,?,?,?,?)',
                    (ident,kb_id,ref['document_id'],ref['version_id'],ref['snapshot_id'],'input'))
            conn.execute('''INSERT INTO overview_cache VALUES(?,?,?) ON CONFLICT(kb_id,scope_key)
                DO UPDATE SET artifact_id=excluded.artifact_id''',(kb_id,key,ident))
            remember(conn,self.workspace_id,request_key,'overview',value,{'artifact_id':ident})
            result=self._artifact(conn,ident,kb_id)
        self.runs.wake_event.set()
        return result

    def _artifact(self,conn,artifact_id,kb_id):
        self.catalog.require_kb(conn,kb_id,workspace_id=self.workspace_id)
        row=conn.execute("SELECT * FROM artifacts WHERE id=? AND kb_id=? AND type IN ('note','overview')",(artifact_id,kb_id)).fetchone()
        if not row:raise CatalogError('ARTIFACT_NOT_FOUND',status=404)
        if row['type']=='note':
            note=self._public_note(conn,self._note(conn,artifact_id))
            return {'id':row['id'],'type':'note','kb_id':kb_id,'note':note,'title':note['title'],'source_state':note['source_state']}
        relation=conn.execute('SELECT run_id FROM artifact_runs WHERE artifact_id=?',(artifact_id,)).fetchone()
        run=self.runs._run(conn,relation['run_id']);public=self.runs._public(conn,run)
        scope=json.loads(row['scope_json']);stale=False
        for ref in scope['allowed_versions']:
            current=conn.execute("SELECT id FROM kb_snapshot_bindings WHERE kb_id=? AND document_id=? AND binding_role='current' AND state='active'",(kb_id,ref['document_id'])).fetchone()
            if not current or current['id']!=ref['binding_id']:stale=True
        if scope['scope_mode']=='all_ready':
            current_ids={r[0] for r in conn.execute("SELECT document_id FROM kb_snapshot_bindings WHERE kb_id=? AND binding_role='current' AND state='active'",(kb_id,))}
            stale=stale or current_ids!={r['document_id'] for r in scope['allowed_versions']}
        result=public['result'] if public['status']=='succeeded' and public['source_state']!='revoked' else None
        extracts=result.get('source_extracts',[]) if result else []
        questions=[]
        if result and result.get('status')=='GENERATED_ANSWER':
            for claim in result.get('generated_claims',[])[:5]:
                topic=re.sub(r'\s+',' ',claim.get('text','')).strip(' 。；;')[:60]
                if topic:questions.append('资料是如何说明“'+topic+'”的？')
            if questions and len(questions)<3:
                questions.extend(['这些要点分别有哪些原文依据？','资料中的这些概念之间有什么联系？'][:3-len(questions)])
        return {'id':row['id'],'kb_id':kb_id,'type':'overview','title':row['title'],'version':row['version'],
                'status':public['status'],'source_state':public['source_state'],'outdated':stale,'created_at':row['created_at'],
                'source_count':len(scope['allowed_versions']),'covered_source_count':len({e['document_id'] for e in extracts}),
                'run_id':run['id'],'result':result,'questions':questions,'scope_mode':scope['scope_mode']}

    def artifact(self,artifact_id,kb_id):
        with self.catalog.reader() as conn:
            conn.execute('BEGIN');return self._artifact(conn,artifact_id,kb_id)

    def artifacts(self,kb_id,*,offset=0,limit=30):
        with self.catalog.reader() as conn:
            conn.execute('BEGIN');self.catalog.require_kb(conn,kb_id,workspace_id=self.workspace_id)
            rows=conn.execute("SELECT id FROM artifacts WHERE kb_id=? AND type='overview' ORDER BY created_at DESC,id LIMIT ? OFFSET ?",(kb_id,limit+1,offset)).fetchall()
            return {'items':[self._artifact(conn,r[0],kb_id) for r in rows[:limit]],'next_offset':offset+limit if len(rows)>limit else None}

    @staticmethod
    def _validate(title,body):
        title=title.strip()
        if not title or len(title)>200 or len(body)>100000 or '\x00' in title+body:
            raise CatalogError('INVALID_NOTE',status=422)
        return title

    def _note(self,conn,note_id):
        row=conn.execute('SELECT * FROM notes WHERE id=? OR artifact_id=?',(note_id,note_id)).fetchone()
        if not row:raise CatalogError('NOTE_NOT_FOUND',status=404)
        self.catalog.require_kb(conn,row['kb_id'],workspace_id=self.workspace_id)
        return row

    def _public_note(self,conn,row):
        valid=True;historical=False;reviewed=False
        if row['origin_run_id']:
            run=self.runs._run(conn,row['origin_run_id'])
            valid,historical=self.runs._history_valid(conn,run)
            reviewed=bool(valid and not row['edited'] and json.loads(run['result_json'] or '{}').get('model_support_review_passed'))
        refs=conn.execute('''SELECT c.id,c.evidence_key,c.document_id,c.version_id FROM note_citation_refs n
            JOIN citations c ON c.id=n.citation_id WHERE n.note_id=?''',(row['id'],)).fetchall()
        return {'id':row['id'],'artifact_id':row['artifact_id'],'kb_id':row['kb_id'],'title':row['title'],
                'body':row['body'] if valid else '', 'revision':row['revision'],'edited':bool(row['edited']),
                'source_state':'revoked' if not valid else ('historical' if historical else 'valid'),
                'origin_run_id':row['origin_run_id'],'citations':[dict(r) for r in refs] if valid else [],
                'model_support_review_passed':reviewed,'user_content_verified':False,
                'created_at':row['created_at'],'updated_at':row['updated_at']}

    def get_note(self,note_id):
        with self.catalog.reader() as conn:
            conn.execute('BEGIN');return self._public_note(conn,self._note(conn,note_id))

    def notes(self,kb_id,*,offset=0,limit=50):
        with self.catalog.reader() as conn:
            conn.execute('BEGIN');self.catalog.require_kb(conn,kb_id,workspace_id=self.workspace_id)
            rows=conn.execute('SELECT * FROM notes WHERE kb_id=? ORDER BY updated_at DESC,id LIMIT ? OFFSET ?',(kb_id,limit+1,offset)).fetchall()
            items=[]
            for row in rows[:limit]:
                note=self._public_note(conn,row);note.pop('body');note.pop('citations');items.append(note)
            return {'items':items,'next_offset':offset+limit if len(rows)>limit else None}

    def create_note(self,kb_id,*,request_key,title=None,body='',origin_run_id=None,copy_from_note_id=None,client_revision=0):
        if origin_run_id and copy_from_note_id:raise CatalogError('INVALID_NOTE',status=422)
        value={'kb_id':kb_id,'title':title,'body':body,'origin_run_id':origin_run_id,
               'copy_from_note_id':copy_from_note_id,'client_revision':client_revision}
        with self.catalog.transaction() as conn:
            self.catalog.require_kb(conn,kb_id,workspace_id=self.workspace_id)
            old=replay(conn,self.workspace_id,request_key,'create_note',value)
            if old:return old
            self.catalog.require_kb(conn,kb_id,active=True,workspace_id=self.workspace_id)
            refs=[];edited=True
            if origin_run_id:
                run=self.runs._run(conn,origin_run_id)
                if run['kb_id']!=kb_id:raise CatalogError('RUN_NOT_FOUND',status=404)
                public=self.runs._public(conn,run)
                result=public['result']
                if public['source_state']=='revoked' or not result or result.get('status') not in SUCCESS:
                    raise CatalogError('ANSWER_NOT_COLLECTABLE')
                if body:raise CatalogError('INVALID_NOTE',status=422)
                body=result['safe_output'];title=title or run['question'][:60] or '收藏的回答';edited=False
                refs=[r[0] for r in conn.execute('SELECT id FROM citations WHERE run_id=?',(origin_run_id,))]
            elif copy_from_note_id:
                source=self._note(conn,copy_from_note_id)
                if source['kb_id']!=kb_id:raise CatalogError('NOTE_NOT_FOUND',status=404)
                if self._public_note(conn,source)['source_state']=='revoked':raise CatalogError('NOTE_SOURCE_REVOKED')
                origin_run_id=source['origin_run_id']
                refs=[r[0] for r in conn.execute('SELECT citation_id FROM note_citation_refs WHERE note_id=?',(source['id'],))]
            title=self._validate(title or '新笔记',body)
            ident,artifact,now=new_id('note'),new_id('artifact'),utc_now()
            conn.execute('''INSERT INTO artifacts(id,kb_id,type,title,source_state,created_at) VALUES(?,?,'note',?,'valid',?)''',(artifact,kb_id,title,now))
            conn.execute('INSERT INTO notes VALUES(?,?,?,?,?,?,1,?,?,?)',(ident,artifact,kb_id,origin_run_id,title,body,int(edited),now,now))
            for cite in refs:conn.execute('INSERT INTO note_citation_refs VALUES(?,?,?)',(ident,cite,kb_id))
            result={'id':ident,'artifact_id':artifact,'kb_id':kb_id,'revision':1,'client_revision':client_revision}
            remember(conn,self.workspace_id,request_key,'create_note',value,result)
            return result

    def update_note(self,note_id,*,request_key,expected_revision,title,body,client_revision=0):
        title=self._validate(title,body)
        value={'note_id':note_id,'expected_revision':expected_revision,'title':title,'body':body,'client_revision':client_revision}
        with self.catalog.transaction() as conn:
            note=self._note(conn,note_id)
            old=replay(conn,self.workspace_id,request_key,'update_note',value)
            if old:return old
            self.catalog.require_kb(conn,note['kb_id'],active=True,workspace_id=self.workspace_id)
            if note['revision']!=expected_revision:raise CatalogError('NOTE_REVISION_CONFLICT',status=412)
            if self._public_note(conn,note)['source_state']=='revoked':raise CatalogError('NOTE_SOURCE_REVOKED')
            now=utc_now()
            conn.execute('UPDATE notes SET title=?,body=?,revision=revision+1,edited=1,updated_at=? WHERE id=?',(title,body,now,note['id']))
            conn.execute('UPDATE artifacts SET title=?,version=version+1 WHERE id=?',(title,note['artifact_id']))
            result={'id':note['id'],'artifact_id':note['artifact_id'],'kb_id':note['kb_id'],
                    'revision':note['revision']+1,'client_revision':client_revision}
            remember(conn,self.workspace_id,request_key,'update_note',value,result)
            return result

    def citation(self,citation_id,*,kb_id,session_id=None,artifact_id=None):
        with self.catalog.reader() as conn:
            conn.execute('BEGIN')
            row=conn.execute('SELECT * FROM citations WHERE id=? AND kb_id=?',(citation_id,kb_id)).fetchone()
            if not row:raise CatalogError('CITATION_NOT_FOUND',status=404)
            run=self.runs._run(conn,row['run_id'])
            if artifact_id:
                artifact=conn.execute('SELECT * FROM artifacts WHERE id=? AND kb_id=?',(artifact_id,kb_id)).fetchone()
                if not artifact:raise CatalogError('ARTIFACT_NOT_FOUND',status=404)
                authorized=conn.execute('''SELECT 1 FROM note_citation_refs r JOIN notes n ON n.id=r.note_id
                    WHERE n.artifact_id=? AND r.citation_id=?''',(artifact_id,citation_id)).fetchone()
                if not authorized:
                    authorized=conn.execute('''SELECT 1 FROM artifact_runs ar JOIN artifact_sources s ON s.artifact_id=ar.artifact_id
                        WHERE ar.artifact_id=? AND ar.run_id=? AND ar.kb_id=? AND s.document_id=? AND s.version_id=? AND s.snapshot_id=?''',
                        (artifact_id,row['run_id'],kb_id,row['document_id'],row['version_id'],row['snapshot_id'])).fetchone()
                if not authorized:raise CatalogError('CITATION_RELATION_MISMATCH',status=404)
                if session_id and self.runs._session(conn,session_id)['kb_id']!=kb_id:
                    raise CatalogError('SESSION_NOT_FOUND',status=404)
            elif session_id:
                if run['session_id']!=session_id or self.runs._session(conn,session_id)['kb_id']!=kb_id:
                    raise CatalogError('CITATION_RELATION_MISMATCH',status=404)
            else:raise CatalogError('CITATION_CONTEXT_REQUIRED',status=422)
            valid,historical=self.runs._history_valid(conn,run)
            if not valid:raise CatalogError('CITATION_SOURCE_REVOKED',status=410)
            if sha256(row['quotes_json'].encode()).hexdigest()!=row['quote_sha256']:
                raise CatalogError('CITATION_INTEGRITY_FAILED',status=503)
            extract={'citation_id':row['id'],'run_id':row['run_id'],'session_id':run['session_id'],
                'document_id':row['document_id'],'version_id':row['version_id'],'snapshot_id':row['snapshot_id'],
                'evidence_key':row['evidence_key'],'block_id':row['block_id'],'quotes':json.loads(row['quotes_json']),
                'locator':json.loads(row['locator_json']),'section':row['section'],'historical':historical}
            scope=json.loads(run['scope_json'])
            version=self.workspace.version(row['version_id'],kb_id=kb_id)
            extract.update(document_name=version['display_name'],version_label=version['version_label'])
        snapshot=self.workspace.snapshot(version)
        blocks={b['block_id']:b for b in snapshot['blocks']}
        self.runs._validated_extracts({'source_extracts':[extract]},scope,blocks=blocks,verify_snapshots=False)
        spans=extract['locator'].get('source_spans')
        text=('\n'.join(blocks[s['block_id']]['text'][s['start']:s['end']] for s in spans)
              if spans else blocks[extract['block_id']]['text'])
        start=max(0,text.find(extract['quotes'][0])-1000)
        extract['context_text']=text[start:start+6000]
        with self.catalog.reader() as conn:
            conn.execute('BEGIN')
            valid,historical=self.runs._history_valid(conn,self.runs._run(conn,row['run_id']))
            if not valid:
                raise CatalogError('CITATION_SOURCE_REVOKED',status=410)
            extract['historical']=historical
        return extract
