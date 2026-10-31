"""Actual subprocess protocol and cleanup; the scorer below is a control-flow fake."""
import os,subprocess,sys,threading,time
from concurrent.futures import ThreadPoolExecutor
import pytest
from techdoc.rerank_process import RerankProcess
from techdoc.diagnostics import QueryTrace,QueryCancelled
from techdoc.inference import BUDGET

WORKER='''
import sys,time,os
from techdoc.rerank_worker import serve
class Scorer:
    model_id='TEST_DOUBLE';max_length=1024;batch_size=1;_stable_cache_identity={'test':True}
    quantized_engine='TEST_DOUBLE';cpu_threads=4
    class Cache:
        n=0
        def stats(self):return {'enabled':True,'capacity':4,'size':self.n}
    score_cache=Cache()
    def rerank(self,question,hits,*,trace,**kwargs):
        if question=='bad-progress':trace.callback({'state':'progress','pairs_completed':True,'pairs_total':len(hits),'cache_hits':0,'scored_pairs':1})
        if question in ('progress','slow-progress'):
            trace.begin_pairs(len(hits))
            if question=='slow-progress':time.sleep(1.05)
            trace.report_pairs(1,len(hits),0,1,force=True)
            if question=='slow-progress':time.sleep(.5);trace.check_cancelled()
            trace.report_pairs(len(hits),len(hits),0,len(hits),force=True)
        if question=='crash':os._exit(9)
        if question=='slow':
            print('KERNEL_STARTED',file=sys.stderr,flush=True)
            time.sleep(.5);trace.check_cancelled()
        if question=='error':raise RuntimeError('SECRET_MUST_NOT_LEAK')
        trace.count('rerank_cache_hits',len(hits))
        self.score_cache.n+=len(hits)
        return hits,{'ranking':[]}
serve(Scorer(),sys.stdin,sys.stdout)
'''


@pytest.fixture
def backend(monkeypatch):
    original=subprocess.Popen
    def launch(args,**kwargs):return original([sys.executable,'-c',WORKER],**kwargs)
    monkeypatch.setattr('techdoc.rerank_process.subprocess.Popen',launch)
    monkeypatch.setenv('TECHDOC_INFERENCE_POLICY','isolated_v1')
    value=RerankProcess('TEST_DOUBLE')
    yield value
    value.close()


def test_process_roundtrip_metadata_and_cleanup(backend):
    assert backend.cache_stats()['enabled'] and backend.cache_stats()['size']==0
    trace=QueryTrace();hits=[{'score':1,'chunk':{'chunk_id':'a','snapshot_id':'s','index_text':'text'}}]
    actual,meta=backend.rerank('q',hits,trace=trace)
    assert actual==hits and meta=={'ranking':[]} and trace.counters['rerank_cache_hits']==1
    assert backend.quantized_engine=='TEST_DOUBLE' and backend.cpu_threads==4
    assert backend.cache_stats()['size']==1 and backend.cache_stats()['worker_available']
    pid=backend.process.pid;assert pid!=os.getpid()
    backend.close();backend.close();assert backend.process.poll() is not None
    assert not backend.cache_stats()['worker_available']
    with pytest.raises(RuntimeError,match='CLOSED'):backend.rerank('q',[])


def test_crashed_worker_is_reaped_before_cpu_permit_released(backend):
    with pytest.raises(RuntimeError,match='LOST'):backend.rerank('crash',[])
    assert backend.closed and backend.process.poll()==9 and BUDGET.stats()['active']==0


def test_failure_ends_request_and_next_request_recovers_once_for_concurrent_callers(backend):
    pid=backend.process.pid
    with pytest.raises(RuntimeError,match='LOST'):backend.rerank('crash',[])
    assert not backend.cache_stats()['worker_available']
    with pytest.raises(RuntimeError,match='BACKOFF'):backend.rerank('next',[])
    backend._retry_at=0
    with ThreadPoolExecutor(2) as pool:
        futures=[pool.submit(backend.rerank,'next',[]) for _ in range(2)]
        assert all(f.result(timeout=5)[0]==[] for f in futures)
    assert backend.process.pid!=pid and backend.cache_stats()['worker_generation']==2
    assert backend.cache_stats()['recovery_attempts_in_window']==1


def test_repeated_crashes_have_a_rolling_restart_budget_and_close_is_permanent(backend):
    for _ in range(3):
        with pytest.raises(RuntimeError):backend.rerank('crash',[])
        backend._retry_at=0
        assert backend.rerank('next',[])[0]==[]
    with pytest.raises(RuntimeError):backend.rerank('crash',[])
    backend._retry_at=0
    with pytest.raises(RuntimeError,match='BACKOFF'):backend.rerank('next',[])
    assert backend.cache_stats()['recovery_attempts_in_window']==3
    backend.close();backend._retry_at=0
    with pytest.raises(RuntimeError,match='CLOSED'):backend.rerank('next',[])


def test_recovery_validates_model_identity_and_reaps_changed_worker(backend,monkeypatch):
    with pytest.raises(RuntimeError):backend.rerank('crash',[])
    monkeypatch.setattr(sys.modules[__name__],'WORKER',WORKER.replace("{'test':True}","{'changed':True}"))
    backend._retry_at=0
    with pytest.raises(RuntimeError,match='IDENTITY_CHANGED'):backend.rerank('next',[])
    assert backend.closed and backend.process.poll() is not None and BUDGET.stats()['active']==0


def test_cancel_during_model_recovery_reaps_child_before_releasing_budget(backend,monkeypatch):
    with pytest.raises(RuntimeError):backend.rerank('crash',[])
    monkeypatch.setattr(sys.modules[__name__],'WORKER','import time;time.sleep(.7)\n'+WORKER)
    backend._retry_at=0;cancel=threading.Event()
    with ThreadPoolExecutor(1) as pool:
        future=pool.submit(backend.rerank,'next',[],trace=QueryTrace(cancel_event=cancel))
        deadline=time.monotonic()+3
        while backend.cache_stats()['worker_state']!='recovering':
            assert time.monotonic()<deadline;time.sleep(.01)
        cancel.set()
        with pytest.raises(QueryCancelled):future.result(timeout=4)
    assert backend.process.poll() is not None and BUDGET.stats()['active']==0


def test_malformed_child_progress_reaps_worker_before_releasing_permit(backend):
    with pytest.raises(RuntimeError,match='BAD_WORKER_PROGRESS'):backend.rerank('bad-progress',[{}],trace=QueryTrace())
    assert backend.closed and backend.process.poll() is not None and BUDGET.stats()['active']==0


def test_child_progress_is_forwarded_with_global_facet_offsets_and_no_result_confusion(backend):
    events=[];trace=QueryTrace(callback=events.append);trace.begin_pairs(4)
    for _ in range(2):assert backend.rerank('progress',[{},{}],trace=trace)[0]==[{},{}]
    rows=[e for e in events if e.get('state')=='progress']
    assert [e['pairs_completed'] for e in rows]==sorted(e['pairs_completed'] for e in rows)
    assert rows[-1]['pairs_completed']==rows[-1]['pairs_total']==4 and rows[-1]['scored_pairs']==4
    assert all('question' not in e and 'hits' not in e for e in rows)


def test_cancel_after_progress_does_not_release_permit_before_kernel_ack(backend):
    cancel=threading.Event();started=threading.Event()
    def progress(e):
        if e.get('pairs_completed')==1:started.set()
    trace=QueryTrace(callback=progress,cancel_event=cancel)
    with ThreadPoolExecutor(1) as pool:
        future=pool.submit(backend.rerank,'slow-progress',[{},{}],trace=trace)
        assert started.wait(3);cancel.set();time.sleep(.1)
        assert not future.done() and BUDGET.stats()['active']==1
        with pytest.raises(QueryCancelled):future.result(timeout=3)
    assert backend.process.poll() is None and BUDGET.stats()['active']==0


def test_worker_failure_has_safe_code_and_process_is_reaped(backend):
    with pytest.raises(RuntimeError,match='RERANK_WORKER_FAILED') as exc:backend.rerank('error',[])
    assert 'SECRET' not in str(exc.value) and backend.process.poll() is not None


def test_cancel_waits_for_kernel_response_and_worker_can_serve_next_request(backend):
    cancel=threading.Event();trace=QueryTrace(cancel_event=cancel)
    with ThreadPoolExecutor(1) as pool:
        future=pool.submit(backend.rerank,'slow',[],trace=trace)
        deadline=time.monotonic()+2
        while BUDGET.stats()['active']==0:
            assert time.monotonic()<deadline;time.sleep(.01)
        time.sleep(.1);cancel.set();time.sleep(.1)
        assert not future.done() and BUDGET.stats()['active']==1
        with pytest.raises(QueryCancelled):future.result(timeout=2)
    assert backend.process.poll() is None and BUDGET.stats()['active']==0
    assert backend.rerank('next',[])[0]==[]
