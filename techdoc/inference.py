"""One process-wide FIFO CPU permit; fixed Torch budget is configured at load time."""
from collections import deque
from contextlib import contextmanager,nullcontext
import os
import threading


def policy():
    value=os.getenv('TECHDOC_INFERENCE_POLICY','legacy')
    if value not in ('legacy','shared_v1','isolated_v1'):raise ValueError('BAD_INFERENCE_POLICY')
    return value


def enabled():return policy()!='legacy'


def configured_threads():
    threads=int(os.getenv('TECHDOC_CPU_THREADS','2'))
    if not 1<=threads<=32:raise ValueError('BAD_INFERENCE_THREAD_BUDGET')
    rerank=int(os.getenv('TECHDOC_RERANK_CPU_THREADS',str(threads)))
    if not 1<=rerank<=32:raise ValueError('BAD_INFERENCE_THREAD_BUDGET')
    if policy()=='shared_v1' and rerank!=threads:
        raise ValueError('INFERENCE_THREAD_BUDGET_MISMATCH')
    return threads


_startup_lock=threading.Lock()
_startup_threads=None


def configure_startup_threads(torch):
    global _startup_threads
    threads=configured_threads()
    if not enabled():torch.set_num_threads(threads);return
    with _startup_lock:
        if _startup_threads is None:
            torch.set_num_threads(threads);_startup_threads=threads
        elif _startup_threads!=threads or torch.get_num_threads()!=threads:
            raise RuntimeError('INFERENCE_THREAD_BUDGET_CHANGED')


def assert_fixed_threads(torch):
    if enabled() and torch.get_num_threads()!=configured_threads():
        raise RuntimeError('INFERENCE_THREAD_BUDGET_CHANGED')


class InferenceBudget:
    def __init__(self):
        self.condition=threading.Condition();self.waiters=deque();self.owner=None;self.depth=0
        self.checks=[];self.peak_active=0

    def check(self):
        with self.condition:
            callbacks=tuple(self.checks) if self.owner==threading.get_ident() else ()
        for check in callbacks:
            if check:check()

    @contextmanager
    def slot(self,check=None):
        ident=threading.get_ident();ticket=object();acquired=False
        with self.condition:
            if self.owner==ident:
                if check:check()
                self.depth+=1;self.checks.append(check);acquired=True
            else:
                self.waiters.append(ticket)
                try:
                    while True:
                        if check:check()
                        if self.owner is None and self.waiters[0] is ticket:
                            self.waiters.popleft();self.owner=ident;self.depth=1;self.checks=[check]
                            self.peak_active=max(self.peak_active,1);acquired=True;break
                        self.condition.wait(.1)
                finally:
                    if not acquired:
                        self.waiters.remove(ticket);self.condition.notify_all()
        try:
            self.check();yield
        finally:
            with self.condition:
                self.checks.pop();self.depth-=1
                if not self.depth:self.owner=None;self.condition.notify_all()

    def stats(self):
        with self.condition:return {'active':int(self.owner is not None),'waiting':len(self.waiters),'peak_active':self.peak_active}


BUDGET=InferenceBudget()


def inference_slot(check=None):return BUDGET.slot(check) if enabled() else nullcontext()


@contextmanager
def checked_lock(lock,check):
    acquired=False
    try:
        while not acquired:
            check();acquired=lock.acquire(timeout=.1)
        check();yield
    finally:
        if acquired:lock.release()
