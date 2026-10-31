"""Real parser/context and explicit retrieval/scorer doubles, not dense quality."""
from unittest.mock import patch
from dataclasses import replace
import pytest
from fastapi.testclient import TestClient
from techdoc.api import create_app
from techdoc.core import DocumentStore,MAX_BYTES
from techdoc.diagnostics import QueryTrace


def test_four_source_multifacet_scores_at_most_64_pairs_with_each_source_reserved(tmp_path,monkeypatch):
    monkeypatch.setenv('TECHDOC_RETRIEVER','bm25');monkeypatch.delenv('TECHDOC_RERANKER_MODEL',raising=False)
    store=DocumentStore(tmp_path)
    selected=[]
    for i in range(4):
        text='\n\n'.join('## Section '+str(j)+'\n'+('The server timeout is 30 seconds and retry limit is 5. '*18) for j in range(25))
        selected.append(store.ingest(str(i)+'.md',text.encode())['snapshot_id'])
    chunks={sid:[c for c in store.chunks if c['snapshot_id']==sid] for sid in selected}
    assert all(len(c)>=20 for c in chunks.values())
    def first(question,sid,topk):return [{'score':1,'chunk':c} for c in chunks[sid][:topk]]
    class DenseDouble:
        def query(self,question,sid,topk):return [{'score':1,'chunk':c} for c in chunks[sid][-topk:]]
    class ScorerDouble:
        def __init__(self):self.seen=[]
        def rerank(self,question,hits,trace,**kwargs):
            assert len(hits)<=64
            self.seen.append(hits)
            offset=trace.pairs_completed;cached=trace.pairs_cached;scored=trace.pairs_scored
            trace.report_pairs(offset+len(hits),trace.pairs_total,cached,scored+len(hits),force=True)
            return hits,{'elapsed_ms':0,'ranking':[{'chunk_id':h['chunk']['chunk_id'],'snapshot_id':h['chunk']['snapshot_id'],'score':h['score']} for h in hits]}
    store.index.query=first;store.dense=DenseDouble();store.retriever_mode='hybrid';store.reranker=ScorerDouble()
    store._index_views.current=replace(store._index_views.current,dense=store.dense)
    trace=QueryTrace()
    result=store.query('What is the server timeout? Why set the retry limit?',snapshot_ids=selected,
        mode='answer',inspect_only=True,trace=trace)
    assert result['status']=='RETRIEVAL_INSPECTION_ONLY'
    assert len(store.reranker.seen)>=2 and sum(map(len,store.reranker.seen))==64
    assert all({h['chunk']['snapshot_id'] for h in rows}==set(selected) for rows in store.reranker.seen)
    assert trace.pairs_completed==trace.pairs_total==64


@pytest.mark.parametrize('path',['/documents','/documents/'+'a'*28+'/revisions'])
@pytest.mark.parametrize('declared',[True,False])
def test_legacy_upload_limit_applies_before_endpoint_or_parser_commit(tmp_path,monkeypatch,path,declared):
    monkeypatch.setenv('TECHDOC_RETRIEVER','bm25');monkeypatch.delenv('TECHDOC_RERANKER_MODEL',raising=False)
    app=create_app(tmp_path,knowledge_mode=False)
    head=b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="large.txt"\r\n\r\n'
    def body():
        yield head
        for _ in range(9):yield b'x'*(1024*1024)
        yield b'\r\n--boundary--\r\n'
    headers={'Content-Type':'multipart/form-data; boundary=boundary'}
    if declared:headers['Content-Length']=str(9*1024*1024+len(head)+18)
    with TestClient(app) as c,patch.object(app.state.store,'ingest',side_effect=AssertionError('Oversized request reached ingest')) as ingest:
        response=c.post(path,content=body(),headers=headers)
        assert response.status_code==413,response.text
        assert not ingest.called and not app.state.store.docs
