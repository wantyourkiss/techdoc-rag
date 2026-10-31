"""Persistent score-cache identity, invalidation, and fail-safe I/O contracts."""
import json,hashlib,os
from types import SimpleNamespace
import pytest
from techdoc.score_cache import ScoreCache,model_cache_identity


def chunk(cid='c',text='PRIVATE_SOURCE',sid='s'):
    return {'chunk_id':cid,'snapshot_id':sid,'index_text':text}


def test_restart_exact_identity_lru_bound_pruning_and_no_raw_text(tmp_path):
    path=tmp_path/'scores.json';identity={'model':'hash','runtime':'v1'};cache=ScoreCache(2,path=path,identity=identity)
    c=chunk();key=cache.key(identity,'PRIVATE_QUESTION',c);cache.put(key,0.0,c);assert cache.flush()
    raw=path.read_text();assert 'PRIVATE_SOURCE' not in raw and 'PRIVATE_QUESTION' not in raw
    if os.name!='nt':assert path.stat().st_mode&0o777==0o600
    restored=ScoreCache(2,path=path,identity=identity);assert restored.get(key)==0.0
    assert restored.stats()['load_state']=='loaded'
    other=chunk('other');otherkey=cache.key(identity,'other',other);restored.put(otherkey,1.0,other);restored.get(key)
    third=chunk('third');thirdkey=cache.key(identity,'third',third);restored.put(thirdkey,2.0,third);restored.flush()
    reopened=ScoreCache(2,path=path,identity=identity)
    assert reopened.get(otherkey) is None and reopened.get(key)==0.0 and reopened.stats()['size']==2
    reopened.prune([third]);reopened.flush();assert ScoreCache(2,path=path,identity=identity).get(key) is None
    assert ScoreCache(2,path=path,identity={'model':'changed'}).stats()['load_state']=='identity_changed'


@pytest.mark.parametrize('damage',['truncated','deeply_nested','checksum','nonfinite','bad_scope','duplicate','version'])
def test_corrupt_cache_is_a_miss_without_publishing_error_content(tmp_path,damage):
    path=tmp_path/'scores.json';cache=ScoreCache(2,path=path,identity='id');c=chunk();key=cache.key('id','q',c);cache.put(key,1,c);cache.flush()
    obj=json.loads(path.read_text());payload=obj['payload']
    if damage=='truncated':path.write_text('{PRIVATE_ERROR')
    elif damage=='deeply_nested':path.write_text('['*2000+'0'+']'*2000)
    elif damage=='checksum':obj['sha256']='0'*64;path.write_text(json.dumps(obj))
    else:
        if damage=='nonfinite':payload['entries'][0]['score']='NaN'
        elif damage=='bad_scope':payload['entries'][0]['scope']=['wrong']
        elif damage=='duplicate':payload['entries']*=2
        else:payload['version']=2
        obj['sha256']=hashlib.sha256(cache._encoded(payload)).hexdigest();path.write_bytes(cache._encoded(obj))
    restored=ScoreCache(2,path=path,identity='id')
    assert restored.get(key) is None and restored.stats()['load_state']=='invalid'
    assert 'PRIVATE' not in json.dumps(restored.stats())


def test_failed_atomic_replace_retains_previous_cache_and_uses_memory(tmp_path,monkeypatch):
    path=tmp_path/'scores.json';cache=ScoreCache(2,path=path,identity='id');c=chunk();key=cache.key('id','q',c);cache.put(key,1,c);cache.flush();before=path.read_bytes()
    cache.put(key,2,c)
    def fail(*args):raise OSError('PRIVATE_IO_ERROR')
    monkeypatch.setattr('techdoc.score_cache.os.replace',fail)
    assert cache.flush() is False and path.read_bytes()==before and cache.get(key)==2
    assert cache.stats()['io_failed'] and not list(tmp_path.glob('.tmp_score_*'))


def test_cache_hit_changes_only_memory_lru_without_any_persistence_io(tmp_path,monkeypatch):
    path=tmp_path/'scores.json';cache=ScoreCache(2,path=path,identity='id')
    c=chunk();key=cache.key('id','q',c);cache.put(key,1,c);assert cache.flush()
    before=path.read_bytes();stamp=path.stat().st_mtime_ns
    def denied(*args):raise AssertionError('Read-only cache hit attempted persistence')
    monkeypatch.setattr('techdoc.score_cache.os.replace',denied)
    monkeypatch.setattr('techdoc.score_cache.os.fsync',denied)
    for _ in range(3):assert cache.get(key)==1 and cache.flush() is False
    assert not cache.dirty and path.read_bytes()==before and path.stat().st_mtime_ns==stamp


def test_model_fingerprint_checks_contents_not_mtime(tmp_path):
    pytest.importorskip('torch');pytest.importorskip('transformers')
    weights=tmp_path/'model.safetensors';weights.write_bytes(b'abcd');(tmp_path/'tokenizer.json').write_text('{"a":1}')
    original=model_cache_identity(tmp_path);stamp=weights.stat();weights.write_bytes(b'abce');os.utime(weights,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    assert model_cache_identity(tmp_path)!=original
    weights.write_bytes(b'abcd');assert model_cache_identity(tmp_path)==original
    (tmp_path/'tokenizer.json').write_text('{"a":2}');assert model_cache_identity(tmp_path)!=original


def test_actual_rerank_control_flow_restarts_without_rescoring_and_invalidates_replacement(tmp_path):
    torch=pytest.importorskip('torch')
    from techdoc.retrieval import CrossEncoderReranker
    calls=[];path=tmp_path/'scores.json'
    def scorer():
        value=object.__new__(CrossEncoderReranker);value.model_id='TEST_DOUBLE';value.max_length=30
        value.tokenizer=lambda q,t,**kw:{'input_ids':torch.tensor([[len(t)]])}
        def model(**kw):calls.append(True);return SimpleNamespace(logits=kw['input_ids'].float())
        value.model=model;value._stable_cache_identity={'model':'same_loaded_weights'}
        value._cache_objects=(id(value.model),id(value.tokenizer))
        value.score_cache=ScoreCache(10,path=path,identity=value._stable_cache_identity);return value
    hits=[{'score':1,'chunk':chunk()}];first=scorer();ranking,a=first.rerank('q',hits)
    second=scorer();same,b=second.rerank('q',hits)
    assert same==ranking and len(calls)==1 and a['cache_misses']==1 and b['cache_hits']==1
    hits[0]['chunk']['index_text']='changed';second.rerank('q',hits);assert len(calls)==2
    second.model=lambda **kw:SimpleNamespace(logits=torch.tensor([[99.0]]))
    changed,meta=second.rerank('q',hits);assert changed[0]['score']==99 and meta['cache_misses']==1
    assert second.score_cache.stats()['load_state']=='runtime_objects_changed'


def test_persistence_cannot_replace_a_missing_model(tmp_path,monkeypatch):
    pytest.importorskip('transformers')
    from techdoc.retrieval import CrossEncoderReranker
    monkeypatch.setenv('TECHDOC_RERANK_CACHE',str(tmp_path/'scores.json'))
    with pytest.raises(ValueError,match='REQUIRES_LOCAL_MODEL'):CrossEncoderReranker(str(tmp_path/'missing'))
