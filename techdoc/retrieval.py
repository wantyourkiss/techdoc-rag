"""Optional real-embedding + Qdrant-local retrieval adapter.

No mock embeddings or synthetic scores in production. Missing dependencies/model
weights raise an explicit error. For a single-process, local-only demonstration.
Qdrant :memory: collection is rebuilt from durable source snapshots on startup.
"""
from __future__ import annotations

from collections import defaultdict
import math
import os
import re
import json
import hashlib
import tempfile
from pathlib import Path
from time import perf_counter
from uuid import NAMESPACE_URL, uuid5,uuid4


def query_facets(question):
    parts=[p.strip() for p in re.split(r'[？?；;]',question) if p.strip()]
    # A single question without its final punctuation is not a new facet.
    if len(parts)<2:return [question]
    return list(dict.fromkeys([question]+[p for p in parts if len(p)>=8]))[:3]


def fuse_rankings(rankings, limit):
    scores=defaultdict(float);chunks={}
    for ranking in rankings:
        seen=set()
        for rank,hit in enumerate(ranking,1):
            cid=hit['chunk']['chunk_id']
            if cid not in seen:
                seen.add(cid);scores[cid]+=1/(60+rank);chunks[cid]=hit['chunk']
    return [{'score':scores[cid],'chunk':chunks[cid]} for cid in
            sorted(scores,key=lambda cid:(-scores[cid],cid))[:limit]]


def interleave_facets(rankings, per_snapshot=5):
    """Reserve ranking positions for each requested part, deduplicated by chunk."""
    selected=[];seen=set();counts=defaultdict(int)
    from itertools import zip_longest
    for row in zip_longest(*rankings):
        for hit in row:
            if hit is None:continue
            cid=hit['chunk']['chunk_id'];sid=hit['chunk']['snapshot_id']
            if cid not in seen and counts[sid]<per_snapshot:
                selected.append(hit);seen.add(cid);counts[sid]+=1
    return selected


def configure_cpu_model(model):
    """Optional dynamic INT8 linear layers; embedding tables remain FP32."""
    import torch
    from .inference import configure_startup_threads
    configure_startup_threads(torch)
    if os.getenv('TECHDOC_MODEL_INT8') == '1':
        from torch.ao.quantization import quantize_dynamic
        model = quantize_dynamic(model, {torch.nn.Linear}, dtype=torch.qint8, inplace=True)
    return model.eval()


class CrossEncoderReranker:
    """Local-only pair scorer. Failure raises; never silently skips reranking."""
    def __init__(self, model_dir: str):
        from .inference import policy
        if policy()=='isolated_v1':raise RuntimeError('RERANK_PROCESS_REQUIRED')
        from transformers import AutoTokenizer, AutoModelForSequenceClassification
        from .score_cache import ScoreCache,model_cache_identity
        capacity=int(os.getenv('TECHDOC_RERANK_CACHE_CAPACITY','512'))
        cache_path=os.getenv('TECHDOC_RERANK_CACHE') if capacity else None
        stable_identity=model_cache_identity(model_dir) if cache_path else None
        self.model_id = model_dir
        self.max_length = int(os.getenv('TECHDOC_RERANK_MAX_LENGTH', '1024'))
        self.batch_size=int(os.getenv('TECHDOC_RERANK_BATCH_SIZE','1'))
        if self.batch_size not in (1,2,4,8):raise ValueError('RERANK_BAD_BATCH_SIZE')
        self.tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True,
                                                       trust_remote_code=False)
        self.model = configure_cpu_model(AutoModelForSequenceClassification.from_pretrained(
            model_dir, local_files_only=True, trust_remote_code=False))
        if cache_path and model_cache_identity(model_dir)!=stable_identity:raise RuntimeError('RERANK_MODEL_CHANGED_DURING_LOAD')
        self._stable_cache_identity=stable_identity
        self._cache_objects=(id(self.model),id(self.tokenizer))
        self.score_cache=ScoreCache(capacity,path=cache_path,identity=stable_identity)

    def rerank(self,question,hits,*,per_snapshot=5,trace=None,capture_scores=False):
        from .inference import inference_slot,assert_fixed_threads
        with inference_slot(trace.check_cancelled if trace else None):
            if trace and hasattr(trace,'begin_pairs') and not trace.pairs_planned:
                trace.begin_pairs(len(hits));trace.pairs_planned=False
            import torch
            assert_fixed_threads(torch)
            return self._rerank(question,hits,per_snapshot=per_snapshot,trace=trace,capture_scores=capture_scores)

    def _rerank(self, question: str, hits: list[dict], *, per_snapshot: int = 5, trace=None, capture_scores=False):
        batch_size=getattr(self,'batch_size',1)
        if batch_size not in (1,2,4,8):raise ValueError('RERANK_BAD_BATCH_SIZE')
        if batch_size!=1:
            return self._rerank_batched(question,hits,per_snapshot=per_snapshot,trace=trace,capture_scores=capture_scores)
        import torch
        from .inference import enabled
        fixed=enabled()
        start = perf_counter()
        ranked = [];cache_hits=0;cache_misses=0
        progress_base=(getattr(trace,'pairs_completed',0),getattr(trace,'pairs_cached',0),getattr(trace,'pairs_scored',0))
        cache=getattr(self,'score_cache',None)
        previous_threads=torch.get_num_threads()
        threads=int(os.getenv('TECHDOC_RERANK_CPU_THREADS',str(previous_threads)))
        if not 1<=threads<=32:raise ValueError('RERANK_BAD_THREAD_COUNT')
        stable=getattr(self,'_stable_cache_identity',None)
        if stable is not None and getattr(self,'_cache_objects',None)!=(id(self.model),id(self.tokenizer)):
            cache.detach_persistence();self._stable_cache_identity=None;stable=None
        # A stable identity is used only for the exact model/tokenizer loaded and
        # verified above. Replacing either object invalidates persisted reuse.
        identity=[stable if stable is not None else [id(self.model),id(self.tokenizer),self.model_id],self.max_length,
                  os.getenv('TECHDOC_MODEL_INT8','0'),threads] if cache else None
        try:
            for hit in hits:
                if trace:trace.check_cancelled()
                chunk=hit['chunk'];key=cache.key(identity,question,chunk) if cache else None
                score=cache.get(key) if cache else None
                if score is not None:
                    cache_hits+=1
                else:
                    cache_misses+=1
                    if not fixed and threads!=torch.get_num_threads():torch.set_num_threads(threads)
                    inputs = self.tokenizer(question, chunk['index_text'],
                                            return_tensors='pt', truncation=False)
                    if inputs['input_ids'].shape[1] > self.max_length:
                        raise ValueError('RERANK_INPUT_OVER_MODEL_LIMIT')
                    with torch.inference_mode():
                        score = float(self.model(**inputs).logits.reshape(-1).item())
                    if not math.isfinite(score):raise RuntimeError('RERANK_NONFINITE_SCORE')
                    if cache:cache.put(key,score,chunk)
                if trace:trace.check_cancelled()
                ranked.append({**hit, 'retrieval_score': hit['score'], 'score': score})
                if trace and hasattr(trace,'report_pairs'):
                    trace.report_pairs(progress_base[0]+len(ranked),getattr(trace,'pairs_total',None) or len(hits),progress_base[1]+cache_hits,progress_base[2]+len(ranked)-cache_hits)
        finally:
            if not fixed and torch.get_num_threads()!=previous_threads:torch.set_num_threads(previous_threads)
            if cache:cache.flush()
            if trace:
                trace.count('rerank_cache_hits',cache_hits);trace.count('rerank_cache_misses',cache_misses)
        ranked.sort(key=lambda h: (-h['score'], h['chunk']['chunk_id']))
        counts = defaultdict(int)
        selected = []
        for hit in ranked:
            sid = hit['chunk']['snapshot_id']
            if counts[sid] < per_snapshot:
                selected.append(hit)
                counts[sid] += 1
        metadata = {'model': self.model_id, 'candidate_count': len(hits), 'cpu_threads':threads,
                          'cache_hits':cache_hits,'cache_misses':cache_misses,
                          'elapsed_ms': round((perf_counter()-start)*1000, 3),
                          'ranking': [{'chunk_id': h['chunk']['chunk_id'],
                                       'snapshot_id': h['chunk']['snapshot_id'],
                                       'score': h['score']} for h in selected]}
        if capture_scores:
            metadata['_all_ranking']=[{'chunk_id':h['chunk']['chunk_id'],
                                      'snapshot_id':h['chunk']['snapshot_id'],'score':h['score']}
                                     for h in ranked]
        return selected, metadata

    def _rerank_batched(self,question,hits,*,per_snapshot,trace,capture_scores):
        import torch
        from .inference import enabled
        fixed=enabled()
        start=perf_counter();cache=getattr(self,'score_cache',None);ranked=[];cache_hits=0;cache_misses=0
        progress_base=(getattr(trace,'pairs_completed',0),getattr(trace,'pairs_cached',0),getattr(trace,'pairs_scored',0))
        previous_threads=torch.get_num_threads();threads=int(os.getenv('TECHDOC_RERANK_CPU_THREADS',str(previous_threads)))
        if not 1<=threads<=32:raise ValueError('RERANK_BAD_THREAD_COUNT')
        stable=getattr(self,'_stable_cache_identity',None)
        if stable is not None and getattr(self,'_cache_objects',None)!=(id(self.model),id(self.tokenizer)):
            cache.detach_persistence();self._stable_cache_identity=None;stable=None
        identity=[stable if stable is not None else [id(self.model),id(self.tokenizer),self.model_id],self.max_length,
                  os.getenv('TECHDOC_MODEL_INT8','0'),threads,'pytorch_length_groups_v1',self.batch_size,str(torch.__version__)]
        try:
            encoded=[]
            for hit in hits:
                if trace:trace.check_cancelled()
                values=self.tokenizer(question,hit['chunk']['index_text'],truncation=False)
                if len(values['input_ids'])>self.max_length:raise ValueError('RERANK_INPUT_OVER_MODEL_LIMIT')
                encoded.append((len(values['input_ids']),hit['chunk']['chunk_id'],hit,values))
            encoded.sort(key=lambda item:(item[0],item[1]))
            for offset in range(0,len(encoded),self.batch_size):
                if trace:trace.check_cancelled()
                group=encoded[offset:offset+self.batch_size]
                # INT8 activation scales can depend on peers in a batch. Freeze
                # groups BEFORE cache lookup, key scores by peer content, and
                # recompute the whole group on a partial miss.
                peers=hashlib.sha256(json.dumps([(item[1],hashlib.sha256(item[2]['chunk']['index_text'].encode()).hexdigest()) for item in group]).encode()).hexdigest()
                keys=[cache.key(identity+[peers],question,item[2]['chunk']) if cache else None for item in group]
                scores=[cache.get(key) if cache else None for key in keys]
                if all(score is not None for score in scores):cache_hits+=len(group)
                else:
                    cache_misses+=len(group)
                    if not fixed and threads!=torch.get_num_threads():torch.set_num_threads(threads)
                    inputs=self.tokenizer.pad([item[3] for item in group],padding=True,return_tensors='pt')
                    with torch.inference_mode():scores=self.model(**inputs).logits.reshape(-1).tolist()
                    if len(scores)!=len(group) or any(not math.isfinite(float(score)) for score in scores):
                        raise RuntimeError('RERANK_NONFINITE_OR_INVALID_SCORES')
                    if trace:
                        trace.count('rerank_inference_pairs',len(group));trace.check_cancelled()
                    if cache:
                        for key,score,item in zip(keys,scores,group):cache.put(key,float(score),item[2]['chunk'])
                for score,item in zip(scores,group):
                    ranked.append({**item[2],'retrieval_score':item[2]['score'],'score':float(score)})
                if trace and hasattr(trace,'report_pairs'):
                    trace.report_pairs(progress_base[0]+len(ranked),getattr(trace,'pairs_total',None) or len(hits),progress_base[1]+cache_hits,progress_base[2]+len(ranked)-cache_hits)
                if trace:trace.check_cancelled()
        finally:
            if not fixed and torch.get_num_threads()!=previous_threads:torch.set_num_threads(previous_threads)
            if cache:cache.flush()
            if trace:trace.count('rerank_cache_hits',cache_hits);trace.count('rerank_cache_misses',cache_misses)
        ranked.sort(key=lambda hit:(-hit['score'],hit['chunk']['chunk_id']))
        selected=[];counts=defaultdict(int)
        for hit in ranked:
            sid=hit['chunk']['snapshot_id']
            if counts[sid]<per_snapshot:selected.append(hit);counts[sid]+=1
        metadata={'model':self.model_id,'candidate_count':len(hits),'cpu_threads':threads,'batch_size':self.batch_size,
                  'numeric_policy':'pytorch_length_groups_v1','cache_hits':cache_hits,'cache_misses':cache_misses,
                  'elapsed_ms':round((perf_counter()-start)*1000,3),
                  'ranking':[{'chunk_id':h['chunk']['chunk_id'],'snapshot_id':h['chunk']['snapshot_id'],'score':h['score']} for h in selected]}
        if capture_scores:metadata['_all_ranking']=[{'chunk_id':h['chunk']['chunk_id'],'snapshot_id':h['chunk']['snapshot_id'],'score':h['score']} for h in ranked]
        return selected,metadata


def fuse_rrf(lexical_hits: list[dict], dense_hits: list[dict], *, k: int = 60,
             limit: int = 10) -> list[dict]:
    """Rank-only reciprocal rank fusion, deduplicated by stable chunk_id."""
    if k < 1 or limit < 1:
        raise ValueError('BAD_RRF_CONFIG')
    scores = defaultdict(float)
    chunks = {}
    for ranking in (lexical_hits, dense_hits):
        seen = set()
        for rank, result in enumerate(ranking, 1):
            item = result['chunk']; cid = item['chunk_id']
            if cid in seen:
                continue
            seen.add(cid)
            scores[cid] += 1.0 / (k + rank)
            chunks[cid] = item
    ids = sorted(scores, key=lambda cid: (-scores[cid], cid))[:limit]
    return [{'score': round(scores[cid], 10), 'chunk': chunks[cid]} for cid in ids]


class DenseQdrantLocal:
    """Real SentenceTransformer vectors and Qdrant Python local mode.

    The local-memory index is deliberately rebuilt on startup/revisions to avoid
    serving obsolete vectors after a source snapshot is deleted or changed.
    Not intended for large corpora or concurrent workers.
    """
    def __init__(self, model_id: str | None = None):
        self.model_id = model_id or os.getenv('TECHDOC_EMBEDDING_MODEL', 'BAAI/bge-m3')
        try:
            from sentence_transformers import SentenceTransformer
            from qdrant_client import QdrantClient, models
        except ImportError as exc:
            raise RuntimeError('DENSE_DEPENDENCY_MISSING: install requirements-dense.txt') from exc
        try:
            # Explicit opt-in is necessary to fetch model weights from the internet.
            download = os.getenv('TECHDOC_ALLOW_MODEL_DOWNLOAD') == '1'
            self.encoder = SentenceTransformer(self.model_id,
                                               device='cpu',
                                               local_files_only=not download,
                                               trust_remote_code=False)
            self.encoder[0].auto_model = configure_cpu_model(self.encoder[0].auto_model)
        except Exception as exc:
            raise RuntimeError('DENSE_MODEL_UNAVAILABLE: supply local model directory or explicitly allow download') from exc
        self.tokenizer = getattr(self.encoder, 'tokenizer', None)
        self.max_seq_length = int(getattr(self.encoder, 'max_seq_length', 0) or 0)
        self.vector_dim = self.encoder.get_sentence_embedding_dimension()
        if not self.tokenizer or not self.max_seq_length or not self.vector_dim:
            raise RuntimeError('DENSE_MODEL_TOKENIZER_OR_DIMENSION_UNAVAILABLE')
        self.vector_dim = int(self.vector_dim)
        self.client = QdrantClient(':memory:')
        self.models = models
        self.collection = 'techdoc_local_dense'
        self.by_id = {}
        self.ready = False
        self.indexed_chunks = 0
        self.cache_path=Path(os.environ['TECHDOC_EMBEDDING_CACHE']) if os.getenv('TECHDOC_EMBEDDING_CACHE') else None
        self.cache={}
        self.cache_hits=0
        self.cache_misses=0
        self.cache_identity=None
        if self.cache_path:
            fingerprint=hashlib.sha256()
            for p in sorted(Path(self.model_id).rglob('*')):
                if p.is_file() and p.suffix in ('.json','.bin','.safetensors','.model'):
                    fingerprint.update(str(p.relative_to(self.model_id)).encode())
                    with p.open('rb') as f:
                        for part in iter(lambda:f.read(4*1024*1024),b''):fingerprint.update(part)
            import importlib.metadata
            fingerprint.update(json.dumps({n:importlib.metadata.version(n) for n in
                ['torch','transformers','sentence-transformers']},sort_keys=True).encode())
            fingerprint.update(('normalize-v1-int8='+os.getenv('TECHDOC_MODEL_INT8','0')).encode())
            fingerprint.update(('threads='+os.getenv('TECHDOC_CPU_THREADS','2')).encode())
            from .inference import policy
            if policy()=='shared_v1':fingerprint.update(b'inference=shared_v1')
            self.cache_identity=fingerprint.hexdigest()
            try:
                obj=json.loads(self.cache_path.read_text())
                if obj['identity']==self.cache_identity:
                    for key,vector in obj['vectors'].items():
                        if isinstance(vector,list) and len(vector)==self.vector_dim and all(isinstance(v,(int,float)) and math.isfinite(v) for v in vector):
                            self.cache[key]=vector
            except (OSError,ValueError,KeyError,TypeError,AttributeError):
                self.cache={}

    def _check_length(self, text: str):
        # Must not let model.encode silently truncate indexed evidence or queries.
        count = len(self.tokenizer.encode(text, add_special_tokens=True, truncation=False))
        if count > self.max_seq_length:
            raise ValueError(f'DENSE_INPUT_OVER_MODEL_LIMIT: {count}>{self.max_seq_length}')

    def _vectors(self,texts):
        from .inference import inference_slot,BUDGET,assert_fixed_threads,enabled
        with inference_slot():
            if enabled():
                import torch
                assert_fixed_threads(torch)
            BUDGET.check()
            vectors=self._encode_vectors(texts)
            BUDGET.check();return vectors

    def _encode_vectors(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        for text in texts:
            self._check_length(text)
        arr = self.encoder.encode(texts, batch_size=8, normalize_embeddings=True,
                                  convert_to_numpy=True, show_progress_bar=False)
        out = arr.tolist()
        if len(out) != len(texts) or any(len(v) != self.vector_dim or
                                           not all(math.isfinite(float(x)) for x in v) for v in out):
            raise RuntimeError('DENSE_INVALID_VECTOR_SHAPE_OR_VALUE')
        return out

    def rebuild(self, chunks: list[dict]):
        """Rebuild all vectors. Never report readiness until full upsert finishes."""
        if getattr(self,'_published',False):raise RuntimeError('PUBLISHED_DENSE_VIEW_IS_READ_ONLY')
        self.ready = False
        self.cache_hits=0
        self.cache_misses=0
        self.by_id = {}
        if self.client.collection_exists(self.collection):
            self.client.delete_collection(self.collection)
        self.client.create_collection(self.collection, vectors_config=self.models.VectorParams(
            size=self.vector_dim, distance=self.models.Distance.COSINE))
        from .inference import BUDGET
        for offset in range(0, len(chunks), 32):
            BUDGET.check()
            subset = chunks[offset:offset + 32]
            texts=[c['index_text'] for c in subset]
            keys=[hashlib.sha256(t.encode()).hexdigest() for t in texts]
            missing=list(dict.fromkeys(k for k in keys if k not in self.cache))
            self.cache_hits+=len(keys)-len(missing)
            self.cache_misses+=len(missing)
            text_by_key=dict(zip(keys,texts))
            if missing:
                computed=self._vectors([text_by_key[k] for k in missing])
                self.cache.update(zip(missing,computed))
            vectors=[self.cache[k] for k in keys]
            points = []
            for ch, vector in zip(subset, vectors):
                uid = str(uuid5(NAMESPACE_URL, ch['chunk_id']))
                points.append(self.models.PointStruct(id=uid, vector=vector,
                              payload={'chunk_id': ch['chunk_id'],
                                       'snapshot_id': ch['snapshot_id']}))
                self.by_id[ch['chunk_id']] = ch
            if points:
                self.client.upsert(collection_name=self.collection, points=points,
                                   wait=True)
        self.indexed_chunks = len(chunks)
        self.ready = True
        active={hashlib.sha256(c['index_text'].encode()).hexdigest() for c in chunks}
        self.cache={k:v for k,v in self.cache.items() if k in active}
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True,exist_ok=True)
            temp=None
            try:
                with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=self.cache_path.parent,
                        prefix='.tmp_embedding_',delete=False) as f:
                    temp=Path(f.name);os.chmod(temp,0o600)
                    json.dump({'identity':self.cache_identity,'vectors':self.cache},f)
                    f.flush();os.fsync(f.fileno())
                os.replace(temp,self.cache_path)
            finally:
                if temp is not None:temp.unlink(missing_ok=True)

    def fork_for_chunks(self,chunks):
        """New data/client, same encoder/tokenizer weights and verified cached vectors."""
        from qdrant_client import QdrantClient
        candidate=object.__new__(type(self));candidate.__dict__=self.__dict__.copy()
        candidate.client=QdrantClient(':memory:');candidate.collection='techdoc_dense_'+uuid4().hex
        candidate.by_id={};candidate.cache=dict(self.cache);candidate.ready=False;candidate._published=False;candidate._closed=False
        try:candidate.rebuild(chunks)
        except BaseException:candidate.close();raise
        return candidate

    def close(self):
        if not getattr(self,'_closed',False):
            self._closed=True;self.client.close()

    def query(self, question: str, snapshot_id: str, topk: int = 10) -> list[dict]:
        return self.query_many(question,[snapshot_id],topk=topk)

    def query_scope(self,question,snapshot_ids,*,required_snapshots=frozenset(),topk=32,trace=None):
        """Bounded global allowed-set search plus at most four coverage reserves.

        A single query embedding serves all searches; work does not grow as one
        independent search per allowed document. The legacy query_many is intact.
        """
        from .knowledge_scope import MAX_ALLOWED_SOURCES
        selected=tuple(snapshot_ids);required=frozenset(required_snapshots)
        if (not 1<=len(selected)<=MAX_ALLOWED_SOURCES or len(selected)!=len(set(selected))
                or not required<=set(selected) or len(required)>4 or not 1<=topk<=64):
            raise ValueError('DENSE_BAD_KNOWLEDGE_SCOPE')
        if not self.ready:raise RuntimeError('DENSE_INDEX_NOT_READY')
        if trace:
            vec=trace.call('query_embedding',self._vectors,[question])[0]
            trace.count('query_embedding_calls');trace.count('query_embedding_reuses',len(required))
        else:vec=self._vectors([question])[0]
        result=[];seen=set()
        for allowed,cap in [(set(selected),topk)]+[({sid},4) for sid in sorted(required)]:
            if trace:trace.check_cancelled()
            match=self.models.MatchAny(any=sorted(allowed))
            kwargs=dict(collection_name=self.collection,query=vec,with_payload=True,limit=cap,
                query_filter=self.models.Filter(must=[self.models.FieldCondition(key='snapshot_id',match=match)]))
            points=(trace.call('dense_search',self.client.query_points,**kwargs) if trace else self.client.query_points(**kwargs)).points
            for point in points:
                payload=point.payload or {};ch=self.by_id.get(payload.get('chunk_id'))
                if ch is None or ch['snapshot_id'] not in allowed or payload.get('snapshot_id')!=ch['snapshot_id']:
                    raise RuntimeError('DENSE_PAYLOAD_SCOPE_MISMATCH')
                if ch['chunk_id'] not in seen:
                    seen.add(ch['chunk_id']);result.append({'score':float(point.score),'chunk':ch})
        return result

    def query_many(self, question: str, snapshot_ids, topk: int = 10, *, trace=None) -> list[dict]:
        """One real query vector, independently filtered searches for each source.

        Reuse is confined to this call; no vector crosses requests/model state.
        """
        if not self.ready:
            raise RuntimeError('DENSE_INDEX_NOT_READY')
        selected=tuple(snapshot_ids)
        if not 1<=len(selected)<=4 or len(selected)!=len(set(selected)):
            raise ValueError('DENSE_BAD_QUERY_SCOPE')
        if trace:
            vec=trace.call('query_embedding',self._vectors,[question])[0]
            trace.count('query_embedding_calls');trace.count('query_embedding_reuses',len(selected)-1)
        else:vec=self._vectors([question])[0]
        hits = []
        for snapshot_id in selected:
            if trace:trace.check_cancelled()
            kwargs=dict(collection_name=self.collection,query=vec,
                query_filter=self.models.Filter(must=[self.models.FieldCondition(
                    key='snapshot_id',match=self.models.MatchValue(value=snapshot_id))]),
                with_payload=True,limit=topk)
            res=(trace.call('dense_search',self.client.query_points,**kwargs) if trace
                 else self.client.query_points(**kwargs)).points
            for p in res:
                payload=p.payload or {};ch=self.by_id.get(payload.get('chunk_id'))
                if ch is None or ch['snapshot_id']!=snapshot_id or payload.get('snapshot_id')!=snapshot_id:
                    raise RuntimeError('DENSE_PAYLOAD_SCOPE_MISMATCH')
                hits.append({'score':float(p.score),'chunk':ch})
        return hits
