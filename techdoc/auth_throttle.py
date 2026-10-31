"""Bounded source-based authentication throttling; never trust arbitrary forwarding."""
from collections import OrderedDict,deque
from ipaddress import ip_address,ip_network
import math,secrets,threading,time


class AuthThrottle:
    def __init__(self,proxy_token='',*,limit=20,window=60,capacity=4096,clock=time.monotonic):
        if proxy_token and len(proxy_token)<32:raise RuntimeError('INVALID_PROXY_AUTH_TOKEN')
        self.proxy_token=proxy_token;self.limit=limit;self.window=window;self.capacity=capacity
        self.clock=clock;self.lock=threading.Lock();self.failures=OrderedDict()

    def source(self,request):
        raw=request.client.host if request.client else 'unknown'
        supplied=request.headers.get('x-techdoc-proxy-auth','')
        if self.proxy_token and secrets.compare_digest(supplied.encode(),self.proxy_token.encode()):
            raw=request.headers.get('x-techdoc-client-ip','unknown')
        try:
            address=ip_address(raw)
            if address.version==6 and address.ipv4_mapped:address=address.ipv4_mapped
            return str(ip_network(str(address)+'/64',strict=False)) if address.version==6 else str(address)
        except ValueError:return 'unknown'

    def _prune(self,now):
        while self.failures and next(iter(self.failures.values()))[-1]<=now-self.window:
            self.failures.popitem(last=False)

    def retry_after(self,key):
        with self.lock:
            now=self.clock();self._prune(now);values=self.failures.get(key)
            if values:
                while values and values[0]<=now-self.window:values.popleft()
                if len(values)>=self.limit:return max(1,math.ceil(values[0]+self.window-now))
            elif len(self.failures)>=self.capacity:
                return max(1,math.ceil(next(iter(self.failures.values()))[-1]+self.window-now))
            return 0

    def failure(self,key):
        with self.lock:
            now=self.clock();self._prune(now)
            if key not in self.failures and len(self.failures)>=self.capacity:return
            values=self.failures.setdefault(key,deque(maxlen=self.limit));values.append(now);self.failures.move_to_end(key)
