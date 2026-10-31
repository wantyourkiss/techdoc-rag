from types import SimpleNamespace
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from techdoc.api import create_app
from techdoc.auth_throttle import AuthThrottle
from techdoc.runtime_config import llm_timeout_seconds


@pytest.fixture
def public(tmp_path,monkeypatch):
    monkeypatch.setenv('TECHDOC_DEPLOYMENT_MODE','public');monkeypatch.setenv('TECHDOC_BROWSER_USER','test')
    monkeypatch.setenv('TECHDOC_BROWSER_PASSWORD','test');monkeypatch.setenv('TECHDOC_RETRIEVER','bm25')
    monkeypatch.delenv('TECHDOC_RERANKER_MODEL',raising=False)
    with TestClient(create_app(tmp_path,knowledge_mode=True,import_worker=False)) as c:yield c


def test_public_health_is_minimal_and_tracks_worker_availability(public):
    assert public.get('/health').status_code==401
    response=public.get('/health',auth=('test','test'));assert response.json()=={'status':'ready'}
    public.app.state.store.reranker=SimpleNamespace(cache_stats=lambda:{'worker_available':False})
    response=public.get('/health',auth=('test','test'))
    assert response.status_code==503 and response.json()=={'status':'unavailable'}


def test_auth_challenges_do_not_consume_password_budget_and_failures_do(public):
    for _ in range(25):assert public.get('/health').status_code==401
    assert public.get('/health',auth=('test','test')).status_code==200
    for _ in range(20):assert public.get('/health',auth=('test','wrong')).status_code==401
    response=public.get('/health',auth=('test','test'))
    assert response.status_code==429 and 1<=int(response.headers['retry-after'])<=60


def test_untrusted_forwarding_does_not_override_source_and_windows_expire():
    clock=[0.];throttle=AuthThrottle('x'*32,limit=2,window=60,capacity=2,clock=lambda:clock[0])
    def req(ip,headers={}):return SimpleNamespace(client=SimpleNamespace(host=ip),headers=headers)
    assert throttle.source(req('192.0.2.1',{'x-forwarded-for':'198.51.100.1','x-techdoc-client-ip':'198.51.100.1'}))=='192.0.2.1'
    assert throttle.source(req('127.0.0.1',{'x-techdoc-proxy-auth':'x'*32,'x-techdoc-client-ip':'198.51.100.1'}))=='198.51.100.1'
    assert throttle.source(req('2001:db8::1'))==throttle.source(req('2001:db8::2'))
    throttle.failure('a');throttle.failure('a');assert throttle.retry_after('a')==60 and throttle.retry_after('b')==0
    throttle.failure('b');assert throttle.retry_after('c')==60 and len(throttle.failures)==2
    clock[0]=61;assert throttle.retry_after('a')==0 and not throttle.failures


@pytest.mark.parametrize('value',['nan','inf','-1','29','91','bad'])
def test_invalid_provider_timeout_is_rejected(value,monkeypatch):
    monkeypatch.setenv('LLM_TIMEOUT_S',value)
    with pytest.raises(RuntimeError,match='INVALID_LLM_TIMEOUT_S'):llm_timeout_seconds()


def test_workspace_csp_has_no_inline_script_exception(public):
    response=public.get('/',auth=('test','test'));policy=response.headers['content-security-policy']
    script=next(p for p in policy.split(';') if p.strip().startswith('script-src'))
    assert "'unsafe-inline'" not in script and "'self'" in script
