"""One private model process. Cancellation waits for its actual kernel to exit."""
import json,os,queue,subprocess,sys,threading,time
from collections import deque
from pathlib import Path
from uuid import uuid4
from .diagnostics import QueryCancelled
from .inference import inference_slot
from .rerank_worker import MAX_MESSAGE

RECOVERY_DELAY=2.0
RECOVERY_WINDOW=300.0
RECOVERY_ATTEMPTS=3


class RerankProcess:
    def __init__(self,model_dir):
        self.lock=threading.RLock();self.closed=True;self._shutdown=False
        self.model_dir=model_dir;self.process=None;self.reader=None;self._reader_stop=None
        self._state='starting';self._retry_at=0.;self._restarts=deque();self._generation=0
        self._cache_stats={'enabled':False}
        try:
            with inference_slot(),self.lock:self._start()
        except BaseException:self.close();raise

    def _start(self,check=None):
        env=os.environ.copy();env['TECHDOC_RERANKER_MODEL']=self.model_dir
        env['PYTHONPATH']=str(Path(__file__).resolve().parent.parent)+os.pathsep+env.get('PYTHONPATH','')
        responses=queue.Queue(maxsize=4);stop=threading.Event()
        self.responses=responses;self._reader_stop=stop
        self.process=subprocess.Popen([sys.executable,'-m','techdoc.rerank_worker'],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,text=True,encoding='utf-8',env=env)
        process=self.process
        def deliver(value):
            while not stop.is_set():
                try:responses.put(value,timeout=.1);return
                except queue.Full:pass
        def read():
            try:
                while not stop.is_set():
                    line=process.stdout.readline(MAX_MESSAGE+1)
                    if not line or len(line)>MAX_MESSAGE:break
                    deliver(json.loads(line))
            except Exception:pass
            finally:deliver(None)
        self.reader=threading.Thread(target=read,daemon=True);self.reader.start()
        try:
            deadline=time.monotonic()+180
            while True:
                if check:check()
                if time.monotonic()>deadline:raise RuntimeError('RERANK_PROCESS_UNAVAILABLE')
                try:ready=responses.get(timeout=.1);break
                except queue.Empty:continue
            if not ready or ready.get('ready') is not True:raise RuntimeError('RERANK_PROCESS_UNAVAILABLE')
            if self._generation and (ready['cache_identity']!=self._stable_cache_identity or ready['model_id']!=self.model_id):
                raise RuntimeError('RERANK_RECOVERY_IDENTITY_CHANGED')
            self.model_id=ready['model_id'];self.max_length=ready['max_length'];self.batch_size=ready['batch_size']
            self._stable_cache_identity=ready['cache_identity']
            self._cache_stats=ready.get('cache_stats',{'enabled':False})
            self.quantized_engine=ready.get('quantized_engine');self.cpu_threads=ready.get('cpu_threads')
            if check:check()
            self._generation+=1;self.closed=False;self._state='ready';self._retry_at=0.
        except BaseException:self._stop_worker();raise

    def _broken(self):
        self.closed=True;self._state='unavailable';self._retry_at=time.monotonic()+RECOVERY_DELAY
        self._stop_worker()

    def _ensure_available(self,check,trace):
        if self._shutdown:raise RuntimeError('RERANK_PROCESS_CLOSED')
        if not self.closed and self.process.poll() is None:return
        if not self.closed:self._broken()
        now=time.monotonic()
        while self._restarts and self._restarts[0]<=now-RECOVERY_WINDOW:self._restarts.popleft()
        if len(self._restarts)>=RECOVERY_ATTEMPTS:
            self._retry_at=max(self._retry_at,self._restarts[0]+RECOVERY_WINDOW)
        if now<self._retry_at:raise RuntimeError('RERANK_RECOVERY_BACKOFF')
        if check:check()
        self._restarts.append(now);self._state='recovering'
        try:
            if trace:trace.call('rerank_recovery',self._start,check)
            else:self._start(check)
        except BaseException:
            self._broken();raise

    def _send(self,value):
        raw=json.dumps(value,ensure_ascii=False,allow_nan=False)+'\n'
        if len(raw.encode('utf-8'))>MAX_MESSAGE:raise ValueError('RERANK_PROCESS_MESSAGE_BUDGET')
        self.process.stdin.write(raw);self.process.stdin.flush()

    def rerank(self,question,hits,*,per_snapshot=5,trace=None,capture_scores=False):
        check=trace.check_cancelled if trace else None
        with inference_slot(check),self.lock:
            self._ensure_available(check,trace)
            if trace and not trace.pairs_planned:trace.begin_pairs(len(hits));trace.pairs_planned=False
            ident=uuid4().hex;cancelled=False;deadline=time.monotonic()+600
            offset=(getattr(trace,'pairs_completed',0),getattr(trace,'pairs_cached',0),getattr(trace,'pairs_scored',0));last=(0,0,0)
            try:
                self._send({'op':'rerank','id':ident,'question':question,'hits':hits,
                    'per_snapshot':per_snapshot,'capture_scores':capture_scores})
                while True:
                    if time.monotonic()>deadline:raise RuntimeError('RERANK_PROCESS_TIMEOUT')
                    if check and not cancelled:
                        try:check()
                        except QueryCancelled:self._send({'op':'cancel','id':ident});cancelled=True
                    try:result=self.responses.get(timeout=.1)
                    except queue.Empty:
                        if time.monotonic()>deadline:raise RuntimeError('RERANK_PROCESS_TIMEOUT')
                        continue
                    if result is None or result.get('id')!=ident:raise RuntimeError('RERANK_PROCESS_LOST')
                    if result.get('op')=='progress':
                        p=result.get('progress',{});values=[p.get(k) for k in ('pairs_completed','pairs_total','cache_hits','scored_pairs')]
                        if set(p)!={'pairs_completed','pairs_total','cache_hits','scored_pairs'} or any(type(v) is not int for v in values):raise RuntimeError('BAD_WORKER_PROGRESS')
                        completed,total,cached,scored=values
                        if not last[0]<=completed<=total==len(hits) or cached<last[1] or scored<last[2] or cached+scored!=completed:raise RuntimeError('BAD_WORKER_PROGRESS')
                        last=(completed,cached,scored)
                        if trace and not cancelled:
                            trace.report_pairs(offset[0]+completed,getattr(trace,'pairs_total',None) or total,offset[1]+cached,offset[2]+scored)
                        continue
                    self._cache_stats=result.get('cache_stats',self._cache_stats)
                    if trace:
                        for key,value in result.get('counters',{}).items():trace.count(key,value)
                    if cancelled or result.get('error')=='QUERY_CANCELLED':raise QueryCancelled('QUERY_CANCELLED')
                    if result.get('error'):raise RuntimeError('RERANK_WORKER_FAILED')
                    if check:check()
                    return result['ranked'],result['meta']
            except QueryCancelled:raise
            except BaseException:
                # EOF/timeouts cannot release the parent's permit while an orphan kernel runs.
                self._broken();raise

    def cache_stats(self):
        # Last completed worker response; no RPC or model lock on capabilities reads.
        process=self.process
        available=not self.closed and process is not None and process.poll() is None
        return {**self._cache_stats,'worker_available':available,
                'worker_state':self._state if available or self._state!='ready' else 'unavailable',
                'worker_generation':self._generation,'recovery_attempts_in_window':len(self._restarts),
                'retry_after_seconds':max(0,round(self._retry_at-time.monotonic(),1)),
                'observation':'last_worker_response'}

    def close(self):
        with self.lock:
            if self._shutdown:return
            self._shutdown=True;self.closed=True;self._state='closed';self._stop_worker()

    def _stop_worker(self):
        process=self.process
        if process is None:return
        if process.poll() is None:
            try:self._send({'op':'stop'});process.wait(timeout=3)
            except Exception:
                process.kill();process.wait()
        if self._reader_stop:self._reader_stop.set()
        for stream in (process.stdin,process.stdout):
            if stream:
                try:stream.close()
                except OSError:pass
        if self.reader:self.reader.join(timeout=1)
