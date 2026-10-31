"""Durable scoped conversations. Reconnect reads existing work; it never resubmits.

Worker output remains private until the same short transaction validates source
epochs, cancellation and job ownership, inserts citations, and publishes terminal
state. Historical reads check logical membership, not the mutable current binding.
"""
from __future__ import annotations

from hashlib import sha256
import json
import logging
import os
import re
import threading
import time

from .catalog import CatalogError,encoded,new_id,utc_now
from .diagnostics import QueryTrace,QueryCancelled
from .jobs import JobQueue
from .knowledge_scope import KnowledgeQueryScope
from .receipts import check_key,request_digest,replay,remember

LOG=logging.getLogger(__name__)
TERMINAL=frozenset(('succeeded','abstained','failed','cancelled','interrupted'))
SUCCESS=frozenset(('GENERATED_ANSWER','EVIDENCE_ONLY','VERBATIM_ANSWER'))
REFERENCE_ABSTENT_RESULTS=frozenset(('MODEL_ABSTAIN','ABSTAIN_SUPPORT_REVIEW','ABSTAIN_INCOMPLETE_ANSWER'))


class RunService:
    def __init__(self,catalog,store,workspace):
        self.catalog,self.store,self.workspace=catalog,store,workspace
        self.workspace_id=workspace.workspace_id;self.queue=JobQueue(catalog,workspace_id=self.workspace_id)
        self.stop_event=threading.Event();self.wake_event=threading.Event();self.threads=[]
        self.active={};self.active_lock=threading.RLock()

    def _session(self,conn,session_id,*,active=False):
        row=conn.execute('SELECT * FROM sessions WHERE id=?',(session_id,)).fetchone()
        if not row:raise CatalogError('SESSION_NOT_FOUND',status=404)
        self.catalog.require_kb(conn,row['kb_id'],active=active,workspace_id=self.workspace_id)
        if row['state']!='active':raise CatalogError('SESSION_NOT_AVAILABLE',status=410)
        return row

    def _run(self,conn,run_id):
        row=conn.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row:raise CatalogError('RUN_NOT_FOUND',status=404)
        self.catalog.require_kb(conn,row['kb_id'],workspace_id=self.workspace_id)
        return row

    def create_session(self,kb_id,*,request_key,title='新对话'):
        title=title.strip()
        if not title or len(title)>100:raise CatalogError('INVALID_SESSION_TITLE',status=422)
        value={'kb_id':kb_id,'title':title};op='create_session'
        with self.catalog.transaction() as conn:
            self.catalog.require_kb(conn,kb_id,workspace_id=self.workspace_id)
            old=replay(conn,self.workspace_id,request_key,op,value)
            if old:return old
            self.catalog.require_kb(conn,kb_id,active=True,workspace_id=self.workspace_id)
            ident,now=new_id('session'),utc_now()
            conn.execute('INSERT INTO sessions(id,kb_id,title,created_at,updated_at) VALUES(?,?,?,?,?)',(ident,kb_id,title,now,now))
            result=dict(conn.execute('SELECT * FROM sessions WHERE id=?',(ident,)).fetchone())
            remember(conn,self.workspace_id,request_key,op,value,result)
            return result

    def sessions(self,kb_id,*,offset=0,limit=30,state='active',search=''):
        if state not in ('active','archived') or len(search)>200:raise CatalogError('INVALID_REQUEST',status=422)
        with self.catalog.reader() as conn:
            conn.execute('BEGIN');self.catalog.require_kb(conn,kb_id,workspace_id=self.workspace_id)
            rows=conn.execute('SELECT * FROM sessions WHERE kb_id=? AND state=? AND instr(lower(title),lower(?))>0 ORDER BY updated_at DESC,id LIMIT ? OFFSET ?',
                              (kb_id,state,search.strip(),limit+1,offset)).fetchall()
            return {'items':[dict(r) for r in rows[:limit]],'next_offset':offset+limit if len(rows)>limit else None}

    def set_session_state(self,session_id,*,kb_id,revision,request_key,state):
        if state not in ('active','archived'):raise CatalogError('INVALID_REQUEST',status=422)
        value={'session_id':session_id,'kb_id':kb_id,'expected_revision':revision,'state':state}
        operation='restore_session' if state=='active' else 'archive_session'
        with self.catalog.transaction() as conn:
            self.catalog.require_kb(conn,kb_id,workspace_id=self.workspace_id)
            row=conn.execute('SELECT * FROM sessions WHERE id=? AND kb_id=?',(session_id,kb_id)).fetchone()
            if not row:raise CatalogError('SESSION_NOT_FOUND',status=404)
            old=replay(conn,self.workspace_id,request_key,operation,value)
            if old:return old
            self.catalog.require_kb(conn,kb_id,active=True,workspace_id=self.workspace_id)
            if row['revision']!=revision:raise CatalogError('SESSION_REVISION_CONFLICT')
            if row['state']==state:raise CatalogError('SESSION_STATE_CHANGED')
            if conn.execute("SELECT 1 FROM runs WHERE session_id=? AND status IN ('queued','running','cancel_requested')",(session_id,)).fetchone():
                raise CatalogError('SESSION_RUN_PENDING')
            conn.execute('UPDATE sessions SET state=?,revision=revision+1,updated_at=? WHERE id=?',(state,utc_now(),session_id))
            result=dict(conn.execute('SELECT * FROM sessions WHERE id=?',(session_id,)).fetchone())
            remember(conn,self.workspace_id,request_key,operation,value,result)
            return result

    def session(self,session_id):
        with self.catalog.reader() as conn:return dict(self._session(conn,session_id))

    def rename_session(self,session_id,title,revision):
        title=title.strip()
        if not title or len(title)>100:raise CatalogError('INVALID_SESSION_TITLE',status=422)
        with self.catalog.transaction() as conn:
            row=self._session(conn,session_id,active=True)
            if row['revision']!=revision:raise CatalogError('SESSION_REVISION_CONFLICT')
            conn.execute('UPDATE sessions SET title=?,revision=revision+1,updated_at=? WHERE id=?',(title,utc_now(),session_id))
            return dict(conn.execute('SELECT * FROM sessions WHERE id=?',(session_id,)).fetchone())

    @staticmethod
    def _event(conn,run_id,kind,payload):
        seq=conn.execute('SELECT coalesce(max(sequence),0)+1 FROM run_events WHERE run_id=?',(run_id,)).fetchone()[0]
        conn.execute('INSERT INTO run_events VALUES(?,?,?,?,?)',(run_id,seq,kind,encoded(payload),utc_now()))

    def submit(self,session_id,*,question,request_key,expected_session_revision,expected_parent_message_id,
               scope_mode='all_ready',document_ids=None,required_document_ids=(),answer_mode='answer',parent_run_id=None):
        check_key(request_key);question=question.strip()
        if not question or len(question)>2000 or answer_mode not in ('answer','evidence_only'):
            raise CatalogError('INVALID_QUESTION',status=422)
        if scope_mode not in ('all_ready','selected') or (scope_mode=='all_ready' and document_ids is not None):
            raise CatalogError('INVALID_SOURCE_SCOPE',status=422)
        if scope_mode=='selected' and not document_ids:raise CatalogError('EMPTY_OR_DUPLICATE_SCOPE',status=422)
        identity={'session_id':session_id,'question':question,'scope_mode':scope_mode,'document_ids':document_ids,
            'required_document_ids':list(required_document_ids),'answer_mode':answer_mode,
            'expected_session_revision':expected_session_revision,'expected_parent_message_id':expected_parent_message_id,'parent_run_id':parent_run_id}
        digest=request_digest(identity)
        with self.catalog.transaction() as conn:
            session=self._session(conn,session_id)
            old=conn.execute('SELECT * FROM runs WHERE submission_owner_id=? AND request_key=?',(self.workspace_id,request_key)).fetchone()
            if old:
                if old['request_sha256']!=digest:raise CatalogError('REQUEST_KEY_CONFLICT')
                return self._public(conn,old)
            self._session(conn,session_id,active=True)
            if session['revision']!=expected_session_revision or session['last_message_id']!=expected_parent_message_id:
                raise CatalogError('SESSION_REVISION_CONFLICT')
            if conn.execute("SELECT 1 FROM runs WHERE session_id=? AND status IN ('queued','running','cancel_requested')",(session_id,)).fetchone():
                raise CatalogError('SESSION_RUN_PENDING')
            scope=self.workspace.resolve_scope(session['kb_id'],document_ids if scope_mode=='selected' else None,
                required_document_ids=required_document_ids,conn=conn)
            scope['answer_mode']=answer_mode
            scope['window_policy']=os.getenv('TECHDOC_WINDOW_POLICY','legacy')
            if scope['window_policy'] not in ('legacy','zh_questions_v1'):raise CatalogError('INVALID_WINDOW_POLICY',status=422)
            scope['generation_policy']=os.getenv('TECHDOC_COMPARISON_GENERATION_POLICY','citation_v2') if required_document_ids else self.store.generation_policy
            scope['followup_recipe']=os.getenv('TECHDOC_FOLLOWUP_RECIPE','legacy')
            if scope['followup_recipe'] not in ('legacy','separate_v1'):raise CatalogError('INVALID_FOLLOWUP_RECIPE',status=422)
            resolved,clarify=self._resolve_followup(conn,session,question,scope=scope)
            ident,now=new_id('run'),utc_now()
            conn.execute('''INSERT INTO runs(id,kb_id,session_id,purpose,submission_owner_id,request_key,request_sha256,
                status,scope_json,created_at,question,resolved_question,parent_run_id) VALUES(?,?,?,?,?,?,?,'queued',?,?,?,?,?)''',
                (ident,session['kb_id'],session_id,'chat',self.workspace_id,request_key,digest,encoded(scope),now,question,resolved,parent_run_id))
            for item in scope['allowed_versions']:
                conn.execute('INSERT INTO run_sources VALUES(?,?,?,?,?,?,?,?,?)',(ident,session['kb_id'],
                    item['document_id'],item['version_id'],item['snapshot_id'],item['binding_id'],item['membership_epoch'],
                    item['binding_epoch'],int(item['document_id'] in required_document_ids)))
            user,assistant=new_id('message'),new_id('message')
            seq=conn.execute('SELECT coalesce(max(sequence),0)+1 FROM messages WHERE session_id=?',(session_id,)).fetchone()[0]
            conn.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)',(user,session['kb_id'],session_id,'user',question,ident,session['last_message_id'],seq,now))
            conn.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)',(assistant,session['kb_id'],session_id,'assistant','',ident,user,seq+1,now))
            title=question[:50] if session['last_message_id'] is None and session['title']=='新对话' else session['title']
            conn.execute('UPDATE sessions SET title=?,last_message_id=?,revision=revision+1,updated_at=? WHERE id=?',(title,assistant,now,session_id))
            self._event(conn,ident,'progress',{'stage':'queued','label':'等待处理'})
            if clarify:
                result={'status':'CLARIFY_CONTEXT','reason':'请说明“这个/它/继续”具体指什么，或补充完整问题。','safe_output':'','llm_invoked':False}
                conn.execute("UPDATE runs SET status='abstained',result_json=?,finished_at=? WHERE id=?",(encoded(result),now,ident))
                self._event(conn,ident,'terminal',{'run_id':ident,'status':'abstained'})
            else:
                job=self.queue.enqueue(session['kb_id'],'query',ident,request_key,digest,conn=conn)
                conn.execute('UPDATE runs SET worker_job_id=? WHERE id=?',(job['id'],ident))
            output=self._public(conn,conn.execute('SELECT * FROM runs WHERE id=?',(ident,)).fetchone())
        self.wake_event.set()
        return output

    def _resolve_followup(self,conn,session,question,*,scope=None):
        from .followup import needs_history
        if not needs_history(question):
            return question,False
        previous=conn.execute('''SELECT r.* FROM runs r JOIN messages m ON m.run_id=r.id AND m.role='assistant'
            WHERE r.session_id=? ORDER BY m.sequence DESC LIMIT 1''',(session['id'],)).fetchone()
        if not previous or previous['status']!='succeeded' or not self._history_valid(conn,previous)[0]:return question,True
        if scope is not None and scope.get('followup_recipe')=='separate_v1':
            from .followup import FollowupContext,topic_words
            previous_question=previous['question'][:500]
            scope['followup_context']=FollowupContext(previous_question,topic_words(previous_question)).public()
            return question,False
        # Only a previous USER question supplies topic words. Old answers never
        # enter retrieval or generation as new factual evidence.
        resolved='本次问题：'+question+'\n上一题的话题（仅用于消歧，不是事实依据）：'+previous['question'][:500]
        return resolved[:2500],False

    def enqueue_artifact(self,conn,kb_id,artifact_id,scope,question,request_key,identity):
        """Same scope, queue and publisher as chat; no hidden conversation messages."""
        digest=request_digest(identity)
        old=conn.execute('SELECT * FROM runs WHERE submission_owner_id=? AND request_key=?',
                         (self.workspace_id,request_key)).fetchone()
        if old:
            if old['request_sha256']!=digest:raise CatalogError('REQUEST_KEY_CONFLICT')
            return old['id']
        self.catalog.require_kb(conn,kb_id,active=True,workspace_id=self.workspace_id)
        ident,now=new_id('run'),utc_now()
        conn.execute('''INSERT INTO runs(id,kb_id,purpose,submission_owner_id,request_key,request_sha256,
            status,scope_json,created_at,question,resolved_question) VALUES(?,?,'overview',?,?,?,'queued',?,?,?,?)''',
            (ident,kb_id,self.workspace_id,request_key,digest,encoded(scope),now,question,question))
        for item in scope['allowed_versions']:
            conn.execute('INSERT INTO run_sources VALUES(?,?,?,?,?,?,?,?,?)',(ident,kb_id,item['document_id'],
                item['version_id'],item['snapshot_id'],item['binding_id'],item['membership_epoch'],item['binding_epoch'],
                int(item['document_id'] in scope['required_document_ids'])))
        job=self.queue.enqueue(kb_id,'query',ident,request_key,digest,conn=conn)
        conn.execute('UPDATE runs SET worker_job_id=? WHERE id=?',(job['id'],ident))
        conn.execute('INSERT INTO artifact_runs VALUES(?,?,?)',(artifact_id,kb_id,ident))
        self._event(conn,ident,'progress',{'stage':'queued','label':'等待整理资料'})
        return ident

    def _history_valid(self,conn,run):
        scope=json.loads(run['scope_json']);allrefs=scope['allowed_versions']
        used=conn.execute('SELECT DISTINCT document_id,version_id FROM citations WHERE run_id=?',(run['id'],)).fetchall()
        relevant={(r['document_id'],r['version_id']) for r in used}
        refs=[r for r in allrefs if not relevant or (r['document_id'],r['version_id']) in relevant]
        historical=False
        for ref in refs:
            row=conn.execute('''SELECT d.state,d.membership_epoch,d.active_version_id,v.processing_state,s.integrity_state
                FROM documents d JOIN document_versions v ON v.document_id=d.id AND v.kb_id=d.kb_id
                JOIN snapshots s ON s.snapshot_id=v.snapshot_id
                WHERE d.id=? AND d.kb_id=? AND v.id=? AND v.snapshot_id=?''',
                (ref['document_id'],run['kb_id'],ref['version_id'],ref['snapshot_id'])).fetchone()
            if not row or row['state']!='active' or row['membership_epoch']!=ref['membership_epoch'] or row['processing_state']!='ready' or row['integrity_state']!='verified':
                return False,False
            historical=historical or row['active_version_id']!=ref['version_id']
        return bool(refs),historical

    def _public(self,conn,run):
        scope=json.loads(run['scope_json']);valid,historical=self._history_valid(conn,run)
        session=conn.execute('SELECT revision,last_message_id FROM sessions WHERE id=?',(run['session_id'],)).fetchone() if run['session_id'] else None
        messages=conn.execute('SELECT id,role FROM messages WHERE run_id=?',(run['id'],)).fetchall()
        body={k:run[k] for k in ('id','kb_id','session_id','purpose','status','created_at','started_at','finished_at','request_key')}
        body.update(run_id=run['id'],source_state='historical' if historical and valid else ('valid' if valid else 'revoked'),
            message_ids={r['role']:r['id'] for r in messages},session_revision=session['revision'] if session else None,
            last_message_id=session['last_message_id'] if session else None,
            scope_contract_version=scope['scope_contract_version'],scope_mode=scope['scope_mode'],
            allowed_document_count=len(scope['allowed_versions']),required_document_ids=scope.get('required_document_ids',[]),result=None)
        if run['status'] in TERMINAL and run['result_json']:
            if not valid:
                body['result']={'status':'ABSTAIN_SOURCE_REVOKED','reason':'回答所依赖的资料已移除，内容暂不可查看。',
                                'safe_output':'','source_extracts':[],'generated_claims':[]}
            else:
                result=json.loads(run['result_json']);sources=[]
                # Legacy results may contain raw candidates; only persisted citations are public.
                result.pop('retrieved_evidence',None);result.pop('retrieved_evidence_kind',None)
                for row in conn.execute('''SELECT c.*,d.display_name,v.version_label FROM citations c
                    JOIN documents d ON d.id=c.document_id JOIN document_versions v ON v.id=c.version_id
                    WHERE c.run_id=? ORDER BY c.rowid''',(run['id'],)):
                    if sha256(row['quotes_json'].encode()).hexdigest()!=row['quote_sha256']:
                        body['source_state']='revoked'
                        body['result']={'status':'SOURCE_INTEGRITY_FAILED','reason':'来源完整性检查未通过。',
                                        'safe_output':'','source_extracts':[],'generated_claims':[]}
                        return body
                    sources.append({'citation_id':row['id'],'evidence_key':row['evidence_key'],'snapshot_id':row['snapshot_id'],
                        'document_id':row['document_id'],'version_id':row['version_id'],'document_name':row['display_name'],
                        'version_label':row['version_label'],'block_id':row['block_id'],'quotes':json.loads(row['quotes_json']),
                        'locator':json.loads(row['locator_json']),'section':row['section'],'parser_verified':bool(row['parser_verified']),
                        'semantic_support_verified':False})
                result['source_extracts']=sources
                result.pop('question_evidence',None)
                if run['purpose']=='chat' and run['status'] in ('succeeded','abstained'):
                    from .evidence_map import associate
                    mapping=associate(run['question'],sources)
                    if mapping is not None:result['question_evidence']=mapping
                if run['status']=='abstained':
                    result.update(safe_output='',generated_claims=[],model_support_review_passed=False,answer_semantics_verified=False)
                    result.pop('draft',None);result.pop('supports',None);result.pop('claim_supports',None)
                    result['evidence_only_reference']=bool(sources)
                if result.get('status')=='EVIDENCE_ONLY':
                    result['safe_output']='\n\n'.join(' '.join(e['quotes'])+' ['+e['evidence_key']+']' for e in sources)
                body['result']=result
        relation=conn.execute('SELECT artifact_id FROM artifact_runs WHERE run_id=?',(run['id'],)).fetchone()
        if relation:body['artifact_id']=relation['artifact_id']
        return body

    def get(self,run_id):
        with self.catalog.reader() as conn:
            conn.execute('BEGIN');return self._public(conn,self._run(conn,run_id))

    def by_request(self,session_id,key):
        with self.catalog.reader() as conn:
            conn.execute('BEGIN');self._session(conn,session_id)
            row=conn.execute('SELECT * FROM runs WHERE session_id=? AND submission_owner_id=? AND request_key=?',(session_id,self.workspace_id,key)).fetchone()
            if not row:raise CatalogError('SUBMISSION_NOT_FOUND',status=404)
            return self._public(conn,row)

    def messages(self,session_id,*,after=None,before=None,limit=40,around_run_id=None):
        with self.catalog.reader() as conn:
            conn.execute('BEGIN');session=self._session(conn,session_id)
            if around_run_id:
                target=conn.execute("SELECT sequence FROM messages WHERE session_id=? AND run_id=? AND role='user'",(session_id,around_run_id)).fetchone()
                if not target:raise CatalogError('RUN_NOT_FOUND',status=404)
                after=max(0,target['sequence']-limit//2)
            if after is None:
                rows=conn.execute('SELECT * FROM messages WHERE session_id=? AND sequence<? ORDER BY sequence DESC LIMIT ?',
                    (session_id,before or 2**62,limit+1)).fetchall()
                page=list(reversed(rows[:limit]));next_before=page[0]['sequence'] if len(rows)>limit else None;next_after=None
            else:
                rows=conn.execute('SELECT * FROM messages WHERE session_id=? AND sequence>? ORDER BY sequence LIMIT ?',(session_id,after,limit+1)).fetchall()
                page=rows[:limit];next_before=None;next_after=page[-1]['sequence'] if len(rows)>limit else None
            items=[]
            for row in page:
                message={k:row[k] for k in ('id','role','body','run_id','sequence','created_at')}
                if row['role']=='assistant':message['run']=self._public(conn,conn.execute('SELECT * FROM runs WHERE id=?',(row['run_id'],)).fetchone())
                items.append(message)
            return {'items':items,'session_revision':session['revision'],'last_message_id':session['last_message_id'],
                    'next_after':next_after,'next_before':next_before}

    def events(self,run_id,after=0):
        with self.catalog.reader() as conn:
            conn.execute('BEGIN');run=self._run(conn,run_id)
            rows=conn.execute('SELECT * FROM run_events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT 100',(run_id,after)).fetchall()
            return {'events':[{'sequence':r['sequence'],'kind':r['event_kind'],'payload':json.loads(r['safe_payload'])} for r in rows],
                    'terminal':run['status'] in TERMINAL,'status':run['status']}

    def cancel(self,run_id):
        with self.catalog.transaction() as conn:
            run=self._run(conn,run_id)
            if run['status'] in TERMINAL:return self._public(conn,run)
            job=conn.execute('SELECT * FROM jobs WHERE id=?',(run['worker_job_id'],)).fetchone()
            if job and job['state']=='queued':
                conn.execute("UPDATE jobs SET state='cancelled',stage='cancelled',cancel_requested=1,updated_at=? WHERE id=?",(utc_now(),job['id']))
                conn.execute("UPDATE runs SET status='cancelled',cancel_requested=1,finished_at=? WHERE id=?",(utc_now(),run_id))
                self._event(conn,run_id,'terminal',{'run_id':run_id,'status':'cancelled'})
                conn.execute("UPDATE artifacts SET status='cancelled' WHERE id IN (SELECT artifact_id FROM artifact_runs WHERE run_id=?)",(run_id,))
            else:
                conn.execute("UPDATE runs SET status='cancel_requested',cancel_requested=1 WHERE id=?",(run_id,))
                if job:conn.execute("UPDATE jobs SET state='cancel_requested',cancel_requested=1,updated_at=? WHERE id=?",(utc_now(),job['id']))
            result=self._public(conn,conn.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone())
        with self.active_lock:
            if run_id in self.active:self.active[run_id].set()
        return result

    def _progress(self,run_id,event,cancel):
        with self.catalog.transaction() as conn:
            row=conn.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
            if row['cancel_requested'] or row['status'] in TERMINAL:cancel.set();return
            count=conn.execute('SELECT count(*) FROM run_events WHERE run_id=?',(run_id,)).fetchone()[0]
            if count<255:self._event(conn,run_id,'progress',{k:event[k] for k in ('stage','state','label','elapsed_ms','pairs_completed','pairs_total','cache_hits','scored_pairs') if k in event})

    def recover(self):
        self.queue.recover_expired()
        query="""SELECT r.id,j.state FROM runs r JOIN jobs j ON j.id=r.worker_job_id
                WHERE r.status IN ('queued','running','cancel_requested') AND j.state IN
                ('cancelled','failed','interrupted') AND r.submission_owner_id=?"""
        with self.catalog.reader() as conn:
            if conn.execute(query,(self.workspace_id,)).fetchone() is None:return 0
        with self.catalog.transaction() as conn:
            rows=conn.execute(query,(self.workspace_id,)).fetchall()
            for row in rows:
                state='cancelled' if row['state']=='cancelled' else 'interrupted'
                conn.execute('UPDATE runs SET status=?,finished_at=? WHERE id=?',(state,utc_now(),row['id']))
                self._event(conn,row['id'],'terminal',{'run_id':row['id'],'status':state})
                conn.execute('UPDATE artifacts SET status=? WHERE id IN (SELECT artifact_id FROM artifact_runs WHERE run_id=?)',(state,row['id']))
            return len(rows)

    def start(self):
        if self.threads:return
        self.stop_event.clear();self.recover()
        def loop():
            while not self.stop_event.is_set():
                try:
                    self.recover()
                    if self.process_next():continue
                except Exception:LOG.exception('Query worker failed')
                self.wake_event.wait(.5);self.wake_event.clear()
        for i in range(2):
            thread=threading.Thread(target=loop,name='knowledge-query-'+str(i),daemon=True);self.threads.append(thread);thread.start()

    def stop(self):
        self.stop_event.set();self.wake_event.set()
        with self.active_lock:
            for event in self.active.values():event.set()
        for thread in self.threads:thread.join(timeout=2)

    def alive(self):return any(t.is_alive() for t in self.threads)

    def process_next(self):
        job=self.queue.claim(kind='query',lease_seconds=120,parallel_query_limit=2)
        if not job:return False
        run_id=job['target_id'];cancel=threading.Event();done=threading.Event()
        with self.active_lock:self.active[run_id]=cancel
        def watch():
            last=time.monotonic()
            while not done.wait(.3):
                try:
                    with self.catalog.reader() as conn:
                        row=conn.execute('SELECT cancel_requested,status FROM runs WHERE id=?',(run_id,)).fetchone()
                    if self.stop_event.is_set() or row['cancel_requested'] or row['status'] in TERMINAL:cancel.set()
                    if time.monotonic()-last>20:
                        self.queue.renew(job['id'],job['lease_token']);last=time.monotonic()
                except Exception:cancel.set();return
        watcher=threading.Thread(target=watch,daemon=True);watcher.start()
        try:
            with self.catalog.transaction() as conn:
                row=conn.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
                if row['status'] in TERMINAL:return True
                if not self.workspace.scope_valid(json.loads(row['scope_json']),conn=conn):cancel.set()
                conn.execute("UPDATE runs SET status=CASE WHEN cancel_requested=1 THEN 'cancel_requested' ELSE 'running' END,started_at=? WHERE id=?",(utc_now(),run_id))
                run=dict(row)
            last_pair_at=float('-inf')
            def progress(event):
                nonlocal last_pair_at
                if event.get('state')=='progress' and event.get('pairs_completed')!=event.get('pairs_total'):
                    now=time.monotonic()
                    if now-last_pair_at<2:return
                    last_pair_at=now
                # Stage transitions (especially provider started) and final
                # counts are durable, even when intermediate pair counts merge.
                self._progress(run_id,event,cancel)
            trace=QueryTrace(callback=progress,cancel_event=cancel)
            scope=json.loads(run['scope_json'])
            result=self.store.query(run['resolved_question'] or run['question'],mode=scope.get('answer_mode','answer'),
                knowledge_scope=KnowledgeQueryScope.from_resolved(scope),trace=trace,
                generation_policy=scope.get('generation_policy',self.store.generation_policy),
                **({'overview':True,'overview_sampling':scope.get('overview_sampling','legacy')} if run['purpose']=='overview' else {}),
                **({'followup_context':scope['followup_context']} if scope.get('followup_context') else {}),
                window_policy=scope.get('window_policy','legacy'))
            self._publish(job,result)
        except QueryCancelled:self._publish(job,None,error='WORKER_STOPPED' if self.stop_event.is_set() else 'QUERY_CANCELLED')
        except Exception:self._publish(job,None,error='QUERY_FAILED')
        finally:
            done.set();watcher.join(timeout=1)
            with self.active_lock:self.active.pop(run_id,None)
        return True

    def _validated_extracts(self,result,scope,*,blocks=None,verify_snapshots=True):
        if not verify_snapshots and blocks is None:raise ValueError('VALIDATED_SNAPSHOT_BLOCKS_REQUIRED')
        catalog_blocks=self.store.by_block if blocks is None else blocks
        extracts=result.get('source_extracts',result.get('extracts',[]))
        if not isinstance(extracts,list) or len(extracts)>16:raise CatalogError('RESULT_EVIDENCE_BUDGET')
        total=0
        for extract in extracts:
            quotes=extract.get('quotes')
            if not isinstance(quotes,list) or not quotes or any(not isinstance(q,str) or not q for q in quotes):
                raise CatalogError('RESULT_SOURCE_INVALID')
            total+=sum(len(q) for q in quotes)
        if total>3500:raise CatalogError('RESULT_EVIDENCE_BUDGET')
        allowed={r['snapshot_id']:r for r in scope['allowed_versions']};keys=set()
        for extract in extracts:
            sid=extract['snapshot_id'];quotes=extract['quotes'];key=extract['evidence_key']
            if sid not in allowed or key in keys or not quotes or sum(len(q) for q in quotes)>3500:
                raise CatalogError('RESULT_SOURCE_INVALID')
            keys.add(key);spans=extract.get('locator',{}).get('source_spans')
            if spans:
                pieces=[]
                for span in spans:
                    block=catalog_blocks.get(span['block_id']);a,z=span['start'],span['end']
                    if (not block or block['snapshot_id']!=sid or not block['indexable'] or
                            not isinstance(a,int) or not isinstance(z,int) or not 0<=a<z<=len(block['text'])):
                        raise CatalogError('RESULT_SOURCE_INVALID')
                    pieces.append(block['text'][a:z])
                text='\n'.join(pieces)
            else:
                block=catalog_blocks.get(extract['block_id'])
                if not block or block['snapshot_id']!=sid or not block['indexable']:raise CatalogError('RESULT_SOURCE_INVALID')
                text=block['text']
            if any(not isinstance(q,str) or q not in text for q in quotes):raise CatalogError('RESULT_SOURCE_INVALID')
            if verify_snapshots:
                row=self.workspace.version(allowed[sid]['version_id'],kb_id=scope['knowledge_base_id'])
                self.workspace.checked_path(row)
        if result.get('status')=='GENERATED_ANSWER' and not result.get('model_support_review_passed'):
            raise CatalogError('RESULT_NOT_REVIEWED')
        return extracts

    def _publish(self,job,result,*,error=None):
        run_id=job['target_id'];extracts=[];invoked=bool(result and result.get('llm_invoked'))
        if result is not None:
            try:
                with self.catalog.reader() as conn:scope=json.loads(conn.execute('SELECT scope_json FROM runs WHERE id=?',(run_id,)).fetchone()[0])
                candidate=result
                if result.get('status') not in SUCCESS:
                    eligible=(result.get('status') in REFERENCE_ABSTENT_RESULTS
                              and result.get('retrieved_evidence_kind')=='source_excerpts_not_an_answer'
                              and result.get('llm_invoked') is True)
                    candidate={**result,'source_extracts':result.get('retrieved_evidence',[]) if eligible else []}
                with self.store.lock:extracts=self._validated_extracts(candidate,scope)
            except Exception:result=None;error='RESULT_SOURCE_INVALID'
        with self.catalog.transaction() as conn:
            run=conn.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
            current=conn.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone()
            if run['status'] in TERMINAL:return
            if current['lease_token']!=job['lease_token'] or current['state'] not in ('running','cancel_requested') or current['lease_until']<time.time():return
            valid=self.workspace.scope_valid(json.loads(run['scope_json']),conn=conn)
            if run['cancel_requested'] or current['cancel_requested'] or not valid:
                state='cancelled';result=None;extracts=[];error='SOURCE_CHANGED' if not valid else 'QUERY_CANCELLED'
            elif error:state='interrupted' if error=='WORKER_STOPPED' else 'failed'
            else:state='succeeded' if result.get('status') in SUCCESS else 'abstained'
            if state=='succeeded' and not extracts:state='failed';result=None;error='RESULT_WITHOUT_EVIDENCE'
            payload=dict(result or {'status':error or 'QUERY_FAILED','reason':'本次处理未完成，请检查来源状态后重试。',
                                     'safe_output':'','generated_claims':[],'llm_invoked':invoked})
            if result is None:
                events=conn.execute("SELECT safe_payload FROM run_events WHERE run_id=? AND event_kind='progress'",(run_id,)).fetchall()
                may_have_called=any((e:=json.loads(row[0])).get('stage') in ('generation','support_review')
                                    and e.get('state')=='started' for row in events)
                payload['llm_invoked']=invoked or may_have_called
            # Actual quotes have one authoritative citation record, assembled on read.
            for key in ('source_extracts','extracts','excerpt_details','source_download_path','source_download_paths',
                        'retrieved_evidence','retrieved_evidence_kind','evidence_only_reference','question_evidence'):
                payload.pop(key,None)
            payload.pop('draft',None)
            if state=='abstained':
                payload.update(safe_output='',generated_claims=[],model_support_review_passed=False,answer_semantics_verified=False)
                payload.pop('supports',None);payload.pop('claim_supports',None)
                if extracts:payload['evidence_only_reference']=True
            if payload.get('status')=='EVIDENCE_ONLY':payload['safe_output']=''
            if state not in ('succeeded','abstained'):
                payload.update(safe_output='',generated_claims=[]);extracts=[]
            allowed={r['snapshot_id']:r for r in json.loads(run['scope_json'])['allowed_versions']}
            for extract in extracts:
                ref=allowed[extract['snapshot_id']];quotes=encoded(extract['quotes'])
                conn.execute('''INSERT INTO citations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(new_id('cite'),run_id,run['kb_id'],
                    ref['document_id'],ref['version_id'],ref['snapshot_id'],extract['evidence_key'],extract['block_id'],
                    quotes,sha256(quotes.encode()).hexdigest(),encoded(extract.get('locator',{})),extract.get('section',''),int(bool(extract.get('parser_verified')))))
            now=utc_now()
            conn.execute('UPDATE runs SET status=?,result_json=?,finished_at=? WHERE id=?',(state,encoded(payload),now,run_id))
            job_state='succeeded' if state in ('succeeded','abstained') else state
            conn.execute('UPDATE jobs SET state=?,stage=?,error_code=?,updated_at=? WHERE id=?',(job_state,job_state,error,now,job['id']))
            conn.execute('UPDATE artifacts SET status=? WHERE id IN (SELECT artifact_id FROM artifact_runs WHERE run_id=?)',
                         ('ready' if state=='succeeded' else state,run_id))
            self._event(conn,run_id,'terminal',{'run_id':run_id,'status':state})
