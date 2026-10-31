"""Pinned index generations. Models are shared; retired Dense data is reader-owned."""
from dataclasses import dataclass,replace
import threading
from uuid import uuid4


class IndexViewBusy(RuntimeError):pass


class FrozenDict(dict):
    def _readonly(self,*a,**k):raise TypeError('INDEX_VIEW_IS_READ_ONLY')
    __setitem__=__delitem__=clear=pop=popitem=setdefault=update=__ior__=_readonly
    def __copy__(self):return self
    def __deepcopy__(self,memo):return self


class FrozenList(list):
    def _readonly(self,*a,**k):raise TypeError('INDEX_VIEW_IS_READ_ONLY')
    __setitem__=__delitem__=append=extend=insert=pop=remove=clear=reverse=sort=__iadd__=__imul__=_readonly
    def __copy__(self):return self
    def __deepcopy__(self,memo):return self


class FrozenCounts(FrozenDict):
    def __missing__(self,key):return 0


def freeze(value):
    if isinstance(value,(FrozenDict,FrozenList)):return value
    if isinstance(value,dict):return FrozenDict({k:freeze(v) for k,v in value.items()})
    if isinstance(value,list):return FrozenList(freeze(v) for v in value)
    if isinstance(value,tuple):return tuple(freeze(v) for v in value)
    return value


def plain(value):
    """API results never lend callers references into a published index."""
    if isinstance(value,dict):return {k:plain(v) for k,v in value.items()}
    if isinstance(value,list):return [plain(v) for v in value]
    if isinstance(value,tuple):return tuple(plain(v) for v in value)
    return value


@dataclass(frozen=True)
class IndexReadView:
    generation_id:str
    docs:dict
    blocks:dict
    chunks:list
    by_block:dict
    index:object
    gate:object
    structure:object
    dense:object
    _snapshot_epochs:dict
    estimated_bytes:int
    retrieval_ready:bool=True


class ViewLease:
    def __init__(self,manager,view):self.manager,self.view,self.closed=manager,view,False
    def close(self):
        if not self.closed:self.closed=True;self.manager.release(self.view)
    def __enter__(self):return self.view
    def __exit__(self,*args):self.close()


class IndexViews:
    def __init__(self,*,max_retired=2,max_bytes=256*1024*1024):
        if not 1<=max_retired<=8 or max_bytes<1024*1024:raise ValueError('BAD_INDEX_VIEW_LIMIT')
        self.lock=threading.RLock();self.boot=uuid4().hex;self.sequence=0
        self.current=None;self.active={};self.dense_readers={};self.retired_dense={}
        self.max_retired=max_retired;self.max_bytes=max_bytes;self.closed=False;self.cleanup_errors=0

    def _generation(self):self.sequence+=1;return self.boot+':'+str(self.sequence)

    def check_capacity(self,candidate_bytes=None):
        with self.lock:
            retained=[view for view,count in self.active.values() if count and (self.current is None or view is not self.current)]
            current_pinned=self.current is not None and self.current.generation_id in self.active
            if current_pinned and len(retained)>=self.max_retired:raise IndexViewBusy('INDEX_RETIRED_VIEW_LIMIT')
            if candidate_bytes is not None:
                # Every pinned generation must stay alive after the replacement.
                pinned=sum(view.estimated_bytes for view,count in self.active.values() if count)
                if self.current is not None and not current_pinned:pinned+=self.current.estimated_bytes
                if pinned+candidate_bytes>self.max_bytes:raise IndexViewBusy('INDEX_VIEW_MEMORY_BUDGET')

    def publish(self,**fields):
        with self.lock:
            if self.closed:raise RuntimeError('INDEX_VIEWS_CLOSED')
            self.check_capacity(fields['estimated_bytes'])
            view=IndexReadView(generation_id=self._generation(),**fields);old=self.current
            if view.dense is not None:
                if hasattr(view.dense,'by_id'):view.dense.by_id=freeze(view.dense.by_id)
                setattr(view.dense,'_published',True)
            self.current=view
            if old and old.dense is not None and old.dense is not view.dense:self._retire_dense(old.dense)
            return view

    def restore(self,view):
        """Restore a pinned checkpoint; rollback must not allocate a fresh index."""
        with self.lock:
            if self.closed or view.generation_id not in self.active:raise RuntimeError('INDEX_CHECKPOINT_NOT_PINNED')
            failed=self.current
            if view.dense is not None:self.retired_dense.pop(id(view.dense),None)
            self.current=view
            if failed and failed.dense is not None and failed.dense is not view.dense:self._retire_dense(failed.dense)

    def refresh_epochs(self,epochs):
        with self.lock:
            if self.current:self.current=replace(self.current,generation_id=self._generation(),_snapshot_epochs=freeze(dict(epochs)))

    def pin(self):
        with self.lock:
            if self.closed or self.current is None:raise RuntimeError('INDEX_VIEW_UNAVAILABLE')
            view=self.current;entry=self.active.setdefault(view.generation_id,[view,0]);entry[1]+=1
            if view.dense is not None:self.dense_readers[id(view.dense)]=self.dense_readers.get(id(view.dense),0)+1
            return ViewLease(self,view)

    def release(self,view):
        with self.lock:
            entry=self.active[view.generation_id];entry[1]-=1
            if not entry[1]:del self.active[view.generation_id]
            if view.dense is not None:
                key=id(view.dense);self.dense_readers[key]-=1
                if not self.dense_readers[key]:
                    del self.dense_readers[key]
                    if key in self.retired_dense:self._close_dense(self.retired_dense.pop(key))

    def _retire_dense(self,dense):
        if self.dense_readers.get(id(dense),0):self.retired_dense[id(dense)]=dense
        else:self._close_dense(dense)

    def _close_dense(self,dense):
        close=getattr(dense,'close',None)
        if close:
            try:close()
            except Exception:self.cleanup_errors+=1

    def close(self):
        with self.lock:
            if self.closed:return
            self.closed=True
            if self.current and self.current.dense is not None:self._retire_dense(self.current.dense)
            self.current=None

    def stats(self):
        with self.lock:return {'generation_id':self.current.generation_id if self.current else None,
            'active_readers':sum(c for _,c in self.active.values()),'active_generations':len(self.active),
            'retired_dense':len(self.retired_dense),'max_retired':self.max_retired,'memory_budget_bytes':self.max_bytes,
            'estimated_current_bytes':self.current.estimated_bytes if self.current else 0,'cleanup_errors':self.cleanup_errors}
