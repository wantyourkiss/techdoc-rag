"""Per-query telemetry with safe progress events; never logs prompts or source text."""
from contextlib import contextmanager
from collections import defaultdict
from time import perf_counter
from uuid import uuid4

LABELS={'queue':'等待处理','lexical_retrieval':'检索文档','dense_retrieval':'检索相关内容',
        'rerank':'筛选相关资料','rerank_recovery':'正在恢复资料筛选服务','context_build':'整理原文依据','evidence_gate':'校验原文',
        'generation':'生成回答','support_review':'核对回答依据','claim_review':'核对回答依据',
        'query_plan':'选择检索策略','query_plan_shadow':'比较检索策略',
        'query_embedding':'理解查询内容','dense_search':'检索相关内容',
        'source_revalidation':'核对文档版本'}

class QueryCancelled(RuntimeError):pass

class QueryTrace:
    def __init__(self, callback=None, cancel_event=None):
        self.request_id=uuid4().hex
        self.started=perf_counter();self.timings=defaultdict(float);self.counters=defaultdict(int)
        self.events=[];self.errors=[];self.callback=callback;self.cancel_event=cancel_event
        self.current_stage=None;self.outcome='RUNNING'
        self.pairs_total=None;self.pairs_completed=0;self.pairs_cached=0;self.pairs_scored=0;self._pair_sent=0;self.pairs_planned=False;self._pairs_disabled=False

    def begin_pairs(self,total):
        self._pairs_disabled=type(total) is not int or not 0<=total<=64
        if self._pairs_disabled:return
        self.pairs_planned=True
        self.pairs_total=total;self.pairs_completed=0;self.pairs_cached=0;self.pairs_scored=0;self._pair_sent=0
        self.report_pairs(0,total,0,0,force=True)

    def report_pairs(self,completed,total,cached,scored,*,force=False):
        """Safe counts only. Never raise cancellation here while a child kernel runs."""
        if self._pairs_disabled:return
        if any(type(v) is not int for v in (completed,total,cached,scored)) or not 0<=completed<=total<=64 or cached<0 or scored<0 or cached+scored!=completed:
            raise ValueError('INVALID_RERANK_PROGRESS')
        if completed<self.pairs_completed or cached<self.pairs_cached or scored<self.pairs_scored or (self.pairs_total is not None and total!=self.pairs_total):raise ValueError('NONMONOTONIC_RERANK_PROGRESS')
        self.pairs_total=total;self.pairs_completed=completed;self.pairs_cached=cached;self.pairs_scored=scored
        now=perf_counter()
        if not force and completed!=total and now-self._pair_sent<1:return
        self._pair_sent=now
        event={'stage':'rerank','state':'progress','label':'筛选相关资料','elapsed_ms':round((now-self.started)*1000,3),
               'pairs_completed':completed,'pairs_total':total,'cache_hits':cached,'scored_pairs':scored}
        if len(self.events)<256:self.events.append(event)
        if self.callback:
            try:self.callback(dict(event))
            except Exception:pass

    def check_cancelled(self):
        if self.cancel_event is not None and self.cancel_event.is_set():raise QueryCancelled('QUERY_CANCELLED')

    def emit(self,stage,state):
        event={'request_id':self.request_id,'stage':stage,'state':state,
               'label':LABELS.get(stage,'处理请求'),'elapsed_ms':round((perf_counter()-self.started)*1000,3)}
        if len(self.events)<256:self.events.append(event)
        if self.callback:
            try:self.callback(dict(event))
            except Exception:pass  # Consumer failures never change evidence decisions.

    @contextmanager
    def stage(self,name):
        self.check_cancelled();previous=self.current_stage;self.current_stage=name
        started=perf_counter();self.emit(name,'started')
        try:yield
        except QueryCancelled:
            self.emit(name,'cancelled');raise
        except Exception as exc:
            self.error(name,type(exc).__name__);self.emit(name,'failed');raise
        else:
            self.check_cancelled();self.emit(name,'finished')
        finally:
            self.timings[name]+= (perf_counter()-started)*1000;self.current_stage=previous

    def call(self,name,fn,*args,**kwargs):
        with self.stage(name):return fn(*args,**kwargs)

    def count(self,name,value=1):self.counters[name]+=value

    def error(self,stage,code):
        # Only implementation-defined error categories; never exception messages.
        import re
        if not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9_]{1,80}',code):code='STAGE_FAILED'
        value={'stage':stage,'code':code}
        if value not in self.errors:self.errors.append(value)

    def export(self):
        return {'request_id':self.request_id,'outcome':self.outcome,'total_ms':round((perf_counter()-self.started)*1000,3),
                'timings_ms':{k:round(v,3) for k,v in self.timings.items()},
                'counters':dict(self.counters),'stage_errors':list(self.errors),'events':list(self.events)}
