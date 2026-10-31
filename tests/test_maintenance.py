from datetime import datetime,timezone
import json,sqlite3
from fastapi.testclient import TestClient
import pytest
from techdoc.api import create_app
from scripts.maintain_workspace import maintain
from scripts.backup_workspace import restore_workspace

def setup_chat(c):
    kb=c.post('/api/v1/knowledge-bases',json={'name':'Maintenance contract'}).json()['id']
    rev=c.get('/api/v1/knowledge-bases/'+kb).json()['catalog_revision']
    job=c.post('/api/v1/knowledge-bases/'+kb+'/documents',files={'file':('guide.md',b'The timeout is 30 seconds.')},
        data={'request_key':'upload','expected_revision':rev}).json()
    assert c.app.state.imports.process_next()
    source=c.get('/api/v1/jobs/'+job['id']).json()['result']
    session=c.post('/api/v1/knowledge-bases/'+kb+'/sessions',json={'request_key':'session'}).json()
    return kb,source,session


def ask(c,session):
    response=c.post('/api/v1/sessions/'+session['id']+'/messages',json={'question':'What is the timeout?',
        'request_key':'query','expected_session_revision':session['revision'],'expected_parent_message_id':None,'answer_mode':'evidence_only'})
    assert response.status_code==202,response.text
    return response.json()


def complete(c,run):
    assert c.app.state.runs.process_next()
    return c.get('/api/v1/runs/'+run['id']).json()


def test_event_retention_keeps_history_citations_idempotency_and_resumable_terminal(tmp_path,monkeypatch):
    monkeypatch.setenv('TECHDOC_RETRIEVER','bm25');monkeypatch.delenv('TECHDOC_RERANKER_MODEL',raising=False)
    root=tmp_path/'data';backup=tmp_path/'before.zip'
    with TestClient(create_app(root,knowledge_mode=True,import_worker=False)) as c:
        kb,source,session=setup_chat(c);run=complete(c,ask(c,session))
        original=run['result'];events=c.get('/api/v1/runs/'+run['id']+'/events').text
        with c.app.state.catalog.transaction() as conn:conn.execute("UPDATE runs SET finished_at='2000-01-01T00:00:00.000+00:00' WHERE id=?",(run['id'],))
        plan=maintain(root);assert plan['before']['eligible_progress_events']>0 and plan['deleted_progress_events']==0
        with pytest.raises(RuntimeError,match='ALREADY_IN_USE'):maintain(root,apply=True,backup=backup)
    result=maintain(root,apply=True,backup=backup)
    assert result['deleted_progress_events']>0 and result['receipts_and_business_history_preserved'] and backup.is_file()
    with TestClient(create_app(root,knowledge_mode=True,import_worker=False)) as c:
        assert c.get('/api/v1/runs/'+run['id']).json()['result']==original
        assert ask(c,session)['id']==run['id']
        after=c.get('/api/v1/runs/'+run['id']+'/events').text
        assert 'event: terminal' in after and 'event: progress' not in after
        final_id=int(next(line[4:] for line in after.splitlines() if line.startswith('id: ')))
        assert c.get('/api/v1/runs/'+run['id']+'/events',params={'after_seq':final_id}).text==''
    restored=tmp_path/'restored';restore_workspace(backup,restored)
    with sqlite3.connect(restored/'catalog/app.sqlite3') as conn:
        assert conn.execute("SELECT count(*) FROM run_events WHERE event_kind='progress'").fetchone()[0]>0
