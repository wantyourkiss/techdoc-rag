"""Private newline-JSON subprocess protocol; never binds a network port."""
from contextlib import redirect_stdout
import json,queue,sys,threading
import os
from .diagnostics import QueryTrace,QueryCancelled

MAX_MESSAGE=2*1024*1024


def configure_rerank_backend(torch):
    """Startup only, inside the private scorer process; Encoder is unaffected."""
    requested=os.getenv('TECHDOC_RERANK_QUANT_ENGINE','auto')
    if requested=='auto':return torch.backends.quantized.engine
    if requested not in ('x86','fbgemm','onednn') or requested not in torch.backends.quantized.supported_engines:
        raise RuntimeError('RERANK_QUANT_ENGINE_UNAVAILABLE')
    torch.backends.quantized.engine=requested
    return requested


def serve(scorer,input_stream,output_stream):
    commands=queue.Queue(maxsize=2);active={};lock=threading.Lock()
    def reply(value):
        output_stream.write(json.dumps(value,ensure_ascii=False,allow_nan=False)+'\n');output_stream.flush()
    def cache_stats():
        cache=getattr(scorer,'score_cache',None)
        return cache.stats() if cache is not None else {'enabled':False}
    def read():
        try:
            while True:
                line=input_stream.readline(MAX_MESSAGE+1)
                if not line:break
                if len(line)>MAX_MESSAGE:break
                obj=json.loads(line);op=obj.get('op');ident=obj.get('id')
                if op=='cancel':
                    with lock:
                        if ident in active:active[ident].set()
                elif op in ('rerank','stop'):
                    if op=='rerank':
                        with lock:active[ident]=threading.Event()
                    commands.put(obj)
                else:break
        except Exception:pass
        finally:
            with lock:
                for event in active.values():event.set()
            commands.put({'op':'stop'})
    threading.Thread(target=read,daemon=True).start()
    reply({'ready':True,'model_id':scorer.model_id,'max_length':scorer.max_length,'batch_size':scorer.batch_size,
           'cache_identity':scorer._stable_cache_identity,'cache_stats':cache_stats(),
           'quantized_engine':getattr(scorer,'quantized_engine',None),'cpu_threads':getattr(scorer,'cpu_threads',None)})
    while True:
        command=commands.get()
        if command['op']=='stop':return
        ident=command['id']
        def progress(event):
            if event.get('state')=='progress':reply({'id':ident,'op':'progress','progress':{k:event[k] for k in ('pairs_completed','pairs_total','cache_hits','scored_pairs')}})
        trace=QueryTrace(cancel_event=active[ident],callback=progress)
        try:
            hits=command['hits']
            if not isinstance(hits,list) or len(hits)>64:raise ValueError('RERANK_PAIR_BUDGET')
            with redirect_stdout(sys.stderr):
                ranked,meta=scorer.rerank(command['question'],hits,per_snapshot=command['per_snapshot'],
                    trace=trace,capture_scores=bool(command['capture_scores']))
            reply({'id':ident,'ranked':ranked,'meta':meta,'counters':dict(trace.counters),'cache_stats':cache_stats()})
        except QueryCancelled:reply({'id':ident,'error':'QUERY_CANCELLED','counters':dict(trace.counters),'cache_stats':cache_stats()})
        except Exception:reply({'id':ident,'error':'RERANK_WORKER_FAILED','counters':dict(trace.counters),'cache_stats':cache_stats()})
        finally:
            with lock:active.pop(ident,None)


def main():
    import os
    # Child sets its own fixed budget once; parent Encoder keeps its original budget.
    os.environ['TECHDOC_INFERENCE_POLICY']='shared_v1'
    os.environ['TECHDOC_CPU_THREADS']=os.environ.get('TECHDOC_RERANK_CPU_THREADS','4')
    import torch
    engine=configure_rerank_backend(torch)
    from .retrieval import CrossEncoderReranker
    with redirect_stdout(sys.stderr):scorer=CrossEncoderReranker(os.environ['TECHDOC_RERANKER_MODEL'])
    scorer.quantized_engine=engine;scorer.cpu_threads=torch.get_num_threads()
    serve(scorer,sys.stdin,sys.stdout)


if __name__=='__main__':main()
