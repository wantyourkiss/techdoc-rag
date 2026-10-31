"""Bounded raw-score cache; optional atomic persistence, never generated answers."""
from collections import OrderedDict
from hashlib import sha256
from threading import Lock
from pathlib import Path
import json,math,os,re,tempfile


def model_cache_identity(model_dir):
    """Hash the actual local weights/tokenizer/config and numeric runtime contract."""
    import importlib.metadata
    import platform
    import torch
    root=Path(model_dir)
    if not root.is_dir():raise ValueError('PERSISTENT_CACHE_REQUIRES_LOCAL_MODEL')
    fingerprint=sha256();count=0
    for path in sorted(root.rglob('*')):
        if not path.is_file() or any(p.startswith('.') or p=='__pycache__' for p in path.relative_to(root).parts):continue
        fingerprint.update(path.relative_to(root).as_posix().encode());fingerprint.update(b'\0')
        with path.open('rb') as source:
            for data in iter(lambda:source.read(4*1024*1024),b''):fingerprint.update(data)
        fingerprint.update(b'\0');count+=1
    if not count:raise ValueError('EMPTY_RERANK_MODEL_DIRECTORY')
    from .inference import enabled,configured_threads
    inference={'inference_contract':'shared_v1','initialization_cpu_threads':configured_threads()} if enabled() else {}
    return {**inference,'contract':'single_pair_logits_v1','files_sha256':fingerprint.hexdigest(),
            'runtime':{n:importlib.metadata.version(n) for n in ['torch','transformers','tokenizers']},
            'machine':platform.machine(),'quantized_engine':torch.backends.quantized.engine,
            'interop_threads':torch.get_num_interop_threads()}

class ScoreCache:
    def __init__(self,capacity=512,*,path=None,identity=None):
        if not 0<=capacity<=8192:raise ValueError('BAD_RERANK_CACHE_CAPACITY')
        self.capacity=capacity;self.data=OrderedDict();self.lock=Lock()
        self.path=Path(path) if path and capacity else None
        if self.path is not None and identity is None:raise ValueError('CACHE_IDENTITY_REQUIRED')
        self.identity=identity;self.dirty=False;self.load_state='not_configured';self.io_failed=False
        if self.path is not None:self._load()

    @staticmethod
    def _encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()

    def _load(self):
        self.load_state='missing'
        try:
            if not self.path.exists():return
            with self.path.open('rb') as source:raw=source.read(8_000_001)
            if len(raw)>8_000_000:raise ValueError('CACHE_TOO_LARGE')
            obj=json.loads(raw);payload=obj['payload']
            if sha256(self._encoded(payload)).hexdigest()!=obj['sha256']:raise ValueError('CACHE_CHECKSUM')
            if payload['version']!=1:raise ValueError('CACHE_VERSION')
            if payload['identity']!=self.identity:self.load_state='identity_changed';return
            entries=payload['entries']
            if not isinstance(entries,list) or len(entries)>8192:raise ValueError('CACHE_ENTRIES')
            values=OrderedDict()
            for item in entries:
                if not isinstance(item,dict) or set(item)!={'key','score','scope'}:raise ValueError('CACHE_ENTRY')
                key,score,scope=item['key'],item['score'],item['scope']
                if not isinstance(key,str) or not re.fullmatch('[0-9a-f]{64}',key) or key in values:raise ValueError('CACHE_KEY')
                if type(score) not in (int,float) or not math.isfinite(score):raise ValueError('CACHE_SCORE')
                if (not isinstance(scope,list) or len(scope)!=3 or any(not isinstance(s,str) or not 1<=len(s)<=256 for s in scope)
                    or not re.fullmatch('[0-9a-f]{64}',scope[2])):raise ValueError('CACHE_SCOPE')
                values[key]={'score':float(score),'scope':tuple(scope)}
            self.data=OrderedDict(list(values.items())[-self.capacity:]);self.load_state='loaded'
            self.dirty=len(values)>self.capacity
        except OSError:self.load_state='io_error';self.io_failed=True
        except (ValueError,KeyError,TypeError,OverflowError,RecursionError):self.load_state='invalid';self.data.clear()

    def flush(self):
        """Cache I/O failure falls back to real scoring, never to invented scores."""
        with self.lock:
            if self.path is None or self.io_failed or not self.dirty:return False
            payload={'version':1,'identity':self.identity,'entries':[{'key':k,**v} for k,v in self.data.items()]}
            temp=None
            try:
                self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                with tempfile.NamedTemporaryFile(dir=self.path.parent,prefix='.tmp_score_',delete=False) as file:
                    temp=Path(file.name);os.chmod(temp,0o600)
                    file.write(self._encoded({'payload':payload,'sha256':sha256(self._encoded(payload)).hexdigest()}))
                    file.flush();os.fsync(file.fileno())
                os.replace(temp,self.path);self.dirty=False;return True
            except OSError:self.io_failed=True;return False
            finally:
                if temp is not None:
                    try:temp.unlink(missing_ok=True)
                    except OSError:pass

    def detach_persistence(self):
        with self.lock:
            self.path=None;self.data.clear();self.dirty=False;self.load_state='runtime_objects_changed'

    @staticmethod
    def key(identity,question,chunk):
        return sha256(json.dumps([identity,question,chunk['snapshot_id'],chunk['chunk_id'],
            sha256(chunk['index_text'].encode()).hexdigest()],ensure_ascii=False,sort_keys=True).encode()).hexdigest()

    def get(self,key):
        with self.lock:
            item=self.data.get(key)
            if item is None:return None
            # Access order matters in memory, not for persisted score validity.
            self.data.move_to_end(key);return item['score']

    def put(self,key,score,chunk):
        if not self.capacity:return
        if not math.isfinite(score):raise ValueError('NONFINITE_CACHE_SCORE')
        with self.lock:
            self.data[key]={'score':score,'scope':(chunk['snapshot_id'],chunk['chunk_id'],sha256(chunk['index_text'].encode()).hexdigest())}
            self.data.move_to_end(key)
            self.dirty=True
            while len(self.data)>self.capacity:self.data.popitem(last=False)

    def prune(self,chunks):
        active={(c['snapshot_id'],c['chunk_id'],sha256(c['index_text'].encode()).hexdigest()) for c in chunks}
        with self.lock:
            for k in list(self.data):
                if self.data[k]['scope'] not in active:del self.data[k];self.dirty=True

    def stats(self):
        with self.lock:return {'enabled':bool(self.capacity),'capacity':self.capacity,'size':len(self.data),
            'persistence':'atomic_local_file' if self.path is not None and not self.io_failed else 'process_local',
            'load_state':self.load_state,'io_failed':self.io_failed,
            'identity_sha256':sha256(self._encoded(self.identity)).hexdigest() if self.identity is not None else None}
