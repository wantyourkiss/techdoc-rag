"""One private model process. Cancellation waits for its actual kernel to exit."""
import json,os,queue,subprocess,sys,threading,time
from pathlib import Path
from uuid import uuid4
from .diagnostics import QueryCancelled
from .inference import inference_slot
from .rerank_worker import MAX_MESSAGE


class RerankProcess:
    def __init__(self,model_dir):
        self.lock=threading.RLock();self.responses=queue.Queue(maxsize=4);self.closed=False
        env=os.environ.copy();env['TECHDOC_RERANKER_MODEL']=model_dir
        env['PYTHONPATH']=str(Path(__file__).resolve().parent.parent)+os.pathsep+env.get('PYTHONPATH','')
        self.process=subprocess.Popen([sys.executable,'-m','techdoc.rerank_worker'],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,text=True,encoding='utf-8',env=env)
        def read():
            try:
                while True:
                    line=self.process.stdout.readline(MAX_MESSAGE+1)
                    if not line or len(line)>MAX_MESSAGE:break
                    self.responses.put(json.loads(line))
            except Exception:pass
            finally:self.responses.put(None)
        self.reader=threading.Thread(target=read,daemon=True);self.reader.start()
        try:
            ready=self.responses.get(timeout=180)
            if not ready or ready.get('ready') is not True:raise RuntimeError('RERANK_PROCESS_UNAVAILABLE')
            self.model_id=ready['model_id'];self.max_length=ready['max_length'];self.batch_size=ready['batch_size']
            self._stable_cache_identity=ready['cache_identity']
            self._cache_stats=ready.get('cache_stats',{'enabled':False})
            self.quantized_engine=ready.get('quantized_engine');self.cpu_threads=ready.get('cpu_threads')
        except BaseException:self.close();raise

    def _send(self,value):
        raw=json.dumps(value,ensure_ascii=False,allow_nan=False)+'\n'
        if len(raw.encode('utf-8'))>MAX_MESSAGE:raise ValueError('RERANK_PROCESS_MESSAGE_BUDGET')
        self.process.stdin.write(raw);self.process.stdin.flush()

    def rerank(self,question,hits,*,per_snapshot=5,trace=None,capture_scores=False):
        check=trace.check_cancelled if trace else None
        with inference_slot(check),self.lock:
            if self.closed:raise RuntimeError('RERANK_PROCESS_CLOSED')
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
                self.close();raise

    def cache_stats(self):
        # Last completed worker response; no RPC or model lock on capabilities reads.
        return {**self._cache_stats,'worker_available':not self.closed and self.process.poll() is None,
                'observation':'last_worker_response'}

    def close(self):
        with self.lock:
            if self.closed:return
            self.closed=True
            if self.process.poll() is None:
                try:self._send({'op':'stop'});self.process.wait(timeout=3)
                except Exception:
                    self.process.kill();self.process.wait()
            for stream in (self.process.stdin,self.process.stdout):
                if stream:
                    try:stream.close()
                    except OSError:pass  # A confirmed-dead worker may leave a broken pipe.
